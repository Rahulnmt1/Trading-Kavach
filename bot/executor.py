"""Executor — the orchestrator.

Each tick of the executor:
  1. Skip if outside trading window (or kill-switch active)
  2. **Manage existing positions** — close on SL/TP hit, trail stops on winners
  3. Check the daily profit lock-in — if today's P&L target is hit, square
     off everything and stop trading for the day
  4. Pull intraday bars for each symbol on the day's research watchlist
  5. Run the strategy ensemble
  6. Send valid signals through RiskManager
  7. Place approved orders via the broker
  8. Update state in Redis
At square-off time, force-close everything regardless of state.

Why position management runs *first*: a freshly-arrived bar may have
crossed an existing SL or TP — we must act on that *before* using
the same tick budget to enter new trades, otherwise the rest of the
loop is allocating risk on top of trades that should already be closed.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import pytz

from .broker import make_broker
from .broker.base import Order, OrderSide, OrderStatus, OrderType, Position
from .cache import get_cache
from .config import env, load_config
from .data import intraday_bars, latest_quote
from .indicators import atr as _atr
from .journal import get_journal
from .logger import logger
from .notify import get_notifier
from .research import todays_picks
from .risk import RiskManager
from .segment import (
    Segment,
    cache_key,
    cfg_strategies_enabled,
    cfg_watchlist_symbols,
    signal_key,
    trail_key,
)
from .strategies import build_default_ensemble
from .strategies.base import Signal, SignalType
from .watchlist_updater import auto_watchlist

IST = pytz.timezone("Asia/Kolkata")


@dataclass
class _TrailState:
    """Per-position bookkeeping for SL/TP/trailing-stop logic.

    Lives in the Executor (process-local). Lost on restart — the static
    SL/TP on the position itself still protect the trade; only the
    trailing-stop tightening is forfeited. Acceptable for paper. For
    live trading the broker-side bracket order is the durable layer.
    """
    entry_price: float
    side: OrderSide
    initial_stop: float          # the SL set at entry (used to compute R)
    risk_per_share: float        # |entry − initial_stop|
    peak_mark: float             # max-favorable-excursion price seen so far
    trailing_active: bool = False
    last_log_at: Optional[datetime] = field(default=None)


class Executor:
    def __init__(self, segment: Segment = Segment.EQUITY) -> None:
        self.segment = segment
        self.cfg = load_config()
        broker_name = env().BROKER if env().LIVE_TRADING else "paper"
        if broker_name != "paper" and not env().LIVE_TRADING:
            logger.warning("LIVE_TRADING=false — forcing paper broker regardless of BROKER={}", broker_name)
            broker_name = "paper"
        self.broker = make_broker(broker_name, segment=segment)
        self.ensemble = build_default_ensemble(segment=segment)
        self.risk = RiskManager(self.broker, segment=segment)
        self.cache = get_cache()
        self.notifier = get_notifier()
        self.journal = get_journal(segment=segment)
        self.feed = self._maybe_start_feed(broker_name)
        # Pre-compute the segment-namespaced cache keys we hit every tick.
        self._heartbeat_key = cache_key("heartbeat:tick", segment)
        self._profit_lock_key = cache_key("profit_lockin", segment)
        self._portfolio_key = cache_key("portfolio", segment)
        # Per-symbol trailing-stop state. Reconciled with broker positions
        # every tick (entries added, vanished symbols dropped).
        self._trails: Dict[str, _TrailState] = {}
        # Once profit lock-in fires we set this and stay locked for the day.
        self._profit_locked: bool = False
        # F&O Phase 1 has no strategies registered yet; we log a clear
        # idle marker on the FIRST tick so the operator sees it but
        # don't spam the log every minute thereafter.
        self._idle_logged_for_day: Optional[datetime] = None
        logger.info("Executor [{}] ready: broker={}, strategies={}, live={}, ws={}, notify={}",
                    segment.value, broker_name, [m.name for m in self.ensemble.members],
                    env().LIVE_TRADING, self.feed is not None, self.notifier.enabled)

    def _maybe_start_feed(self, broker_name: str):
        """Start KiteTicker WebSocket if available; fall back to polled bars."""
        if broker_name != "zerodha" or not self.cfg.feed.use_websocket:
            return None
        try:
            from .feeds.kite_ws import KiteFeed
            feed = KiteFeed(self._base_universe(), mode="ltp")
            feed.start()
            return feed
        except Exception as e:
            logger.warning("[feed] KiteFeed unavailable: {}. Falling back to polled bars.", e)
            return None

    def _base_universe(self) -> List[str]:
        # Equity merges the static config watchlist with the auto-watchlist.
        # F&O resolves each configured underlying into its current monthly
        # futures tradingsymbol (NIFTY → NIFTY26MAYFUT) so the WebSocket
        # feed subscribes to the actual instrument, not the bare index.
        if self.segment == Segment.FNO:
            return self._fno_tradingsymbols()
        return list({*self.cfg.symbols, *auto_watchlist()})

    def _watchlist(self) -> List[str]:
        # Equity uses the pre-market research picks → auto-watchlist →
        # static config fallback chain. F&O has no research agent yet
        # (Phase 3+) so we just return the resolved tradingsymbol(s).
        if self.segment == Segment.FNO:
            return self._fno_tradingsymbols()
        picks = todays_picks()
        if picks:
            symbols = [p.symbol for p in picks if p.bias in ("long", "short")]
            if symbols:
                return symbols
        return auto_watchlist() or self.cfg.symbols

    def _fno_tradingsymbols(self) -> List[str]:
        """Expand the configured F&O watchlist (underlyings) → tradingsymbols.

        For Phase 2 each underlying maps to exactly one futures contract
        (current monthly expiry). Phase 3 will add option strikes here:
        each underlying could expand into multiple option tradingsymbols
        for ATM ± N strikes.
        """
        from .instruments.fno import resolve_underlying
        out: List[str] = []
        for u in cfg_watchlist_symbols(self.cfg, self.segment):
            for inst in resolve_underlying(u):
                out.append(inst.tradingsymbol)
        return out

    def _publish(self, key: str, value) -> None:
        self.cache.set_json(key, value, ttl=86400)

    def tick(self) -> None:
        now = datetime.now(IST)

        # ── Tick heartbeat ───────────────────────────────────────────────
        # Stamp every tick into the cache. The healthcheck and dashboard read
        # this to detect silent stalls — exactly the failure mode that left
        # NESTLEIND unmanaged for 1h 33m on 2026-04-29 (no [strat] / [risk]
        # logs between 11:46 and 13:19, while price ran past the TP). A tick
        # age of >3 min during the trading window is now a FAIL on the
        # healthcheck dashboard regardless of whether yfinance is up.
        self.cache.set_json(self._heartbeat_key, {
            "ts": now.isoformat(),
            "weekday": now.weekday(),
            "in_window": self.risk.in_trading_window(now.time()),
            "segment": self.segment.value,
        }, ttl=3600)

        if now.weekday() >= 5:
            return

        # Roll over the profit-lock flag at the start of each new trading day.
        # (Compares against the risk manager's tracked day so both modules
        # stay in sync.)
        if self._profit_locked and self.risk._day != now.date():
            self._profit_locked = False
            self.cache.delete(self._profit_lock_key)

        if self.risk.should_square_off(now.time()):
            self._end_of_day()
            return

        if not self.risk.in_trading_window(now.time()):
            return

        # ── F&O Phase 1 idle short-circuit ───────────────────────────────
        # If there are no strategies registered AND no open positions, the
        # tick has literally nothing to do — skip the full pipeline. We
        # log a single "idling" notice the first time this happens each
        # day so the operator knows the bot is alive and the no-op is
        # intentional, not a hung tick. Phase 2 will register futures
        # strategies and this branch becomes a no-op.
        if (not self.ensemble.members
                and not [p for p in self.broker.positions() if p.qty != 0]):
            today = now.date()
            if self._idle_logged_for_day != today:
                logger.info("[{}] no strategies registered — idling. "
                            "Phase 2 will plug in futures-trend strategies.",
                            self.segment.value)
                self._idle_logged_for_day = today
            self._publish_state()
            return

        # ── 1. Manage existing positions FIRST ────────────────────────────
        # SL/TP/trailing checks against the latest 1-minute bar. Any open
        # position whose price has crossed its stop or target is closed
        # before we look for new entries.
        self._manage_open_positions(now)

        # ── 2. Daily profit lock-in ───────────────────────────────────────
        if not self._profit_locked and self.risk.profit_target_hit():
            self._lock_in_for_day()
            self._publish_state()
            return  # no new entries this tick

        if self._profit_locked:
            return  # day is done — wait for square-off / next session

        # ── 3. Generate new signals on the watchlist ─────────────────────
        marks: dict[str, float] = {}
        signals: List[Signal] = []
        for sym in self._watchlist():
            df = intraday_bars(sym, "5m")
            if df.empty:
                continue
            marks[sym] = float(df["close"].iloc[-1])
            sig = self.ensemble.generate(sym, df)
            signals.append(sig)
            # Phase 3: a strategy may emit a signal whose ``sig.symbol``
            # differs from the watchlist sym (e.g. option_buy_directional
            # generates signals for ``NIFTY26MAY24600CE`` while iterating
            # the underlying watchlist entry ``NIFTY``). Use sig.symbol so
            # the cache key matches what we'll actually trade and what the
            # dashboard renders.
            self._publish(signal_key(sig.symbol, self.segment), {
                "type": sig.type.value, "price": sig.price,
                "stop_loss": sig.stop_loss, "take_profit": sig.take_profit,
                "confidence": sig.confidence, "reason": sig.reason,
                "ts": now.isoformat(),
            })

        self.broker.update_marks(marks)

        # ── 4. Risk + place new entries ──────────────────────────────────
        for sig in signals:
            if sig.type == SignalType.HOLD:
                continue
            decision = self.risk.evaluate(sig)
            logger.info("[risk] {} {} -> {}: {}", sig.symbol, sig.type.value,
                        "APPROVED" if decision.allow else "REJECTED", decision.reason)
            if not decision.allow:
                self.notifier.rejection(sig, decision.reason)
                continue
            self._place(sig, decision.qty)
            self.risk.record_trade()

        self._publish_state()

    # ============================================================
    #  Position management (SL / TP / trailing stop)
    # ============================================================

    def _manage_open_positions(self, now: datetime) -> None:
        """Inspect each open position against the latest 1-min bar; close
        on SL or TP, ratchet SL on winners.

        Uses 1-minute bars so a 5-minute candle that swept through SL or
        TP intra-bar isn't missed. Worst-case exit price is assumed
        (SL/TP price, not the bar's close) so paper P&L matches what a
        live broker-side stop order would actually fill at — slippage
        is then layered on top by the paper broker's `_slippage()`.
        """
        positions = [p for p in self.broker.positions() if p.qty != 0]
        if not positions:
            self._trails.clear()
            return

        # Drop trail state for symbols no longer held.
        held = {p.symbol for p in positions}
        for sym in list(self._trails.keys()):
            if sym not in held:
                self._trails.pop(sym, None)

        marks: Dict[str, float] = {}
        for pos in positions:
            df = intraday_bars(pos.symbol, "1m")
            interval_used = "1m"
            if df.empty:
                # Fall back to 5-minute bars rather than silently skipping a
                # held position. The 5m fetcher uses a different yfinance call
                # + a different cache key, and is empirically more reliable —
                # the 2026-04-29 incident was a NESTLEIND ride-to-EOD because
                # this loop silently continue'd on every empty 1m fetch.
                df = intraday_bars(pos.symbol, "5m")
                interval_used = "5m"
            if df.empty:
                # Both intervals empty — we genuinely have no data. Surface
                # this loudly so the operator notices instead of letting the
                # position drift unmanaged. Use the LAST KNOWN broker mark as
                # an absolute-last-resort price for SL/TP evaluation.
                fallback_mark = self.broker._marks.get(pos.symbol) if hasattr(self.broker, "_marks") else None
                if fallback_mark is None:
                    logger.warning(
                        "[manage] {} has NO bars and NO mark — position is "
                        "currently unmanaged. yfinance may be rate-limited or "
                        "down. SL/TP/trail will retry next tick.", pos.symbol,
                    )
                    continue
                logger.warning(
                    "[manage] {} no fresh bars (yfinance empty); using last "
                    "broker mark ₹{:.2f} for SL/TP check.", pos.symbol, fallback_mark,
                )
                high = low = close = float(fallback_mark)
                marks[pos.symbol] = close
            else:
                last_bar = df.iloc[-1]
                high = float(last_bar["high"])
                low = float(last_bar["low"])
                close = float(last_bar["close"])
                marks[pos.symbol] = close
                if interval_used != "1m":
                    logger.info(
                        "[manage] {} 1m bars empty — falling back to 5m bar "
                        "(high=₹{:.2f}, low=₹{:.2f}). Position remains managed.",
                        pos.symbol, high, low,
                    )

            self._init_trail_if_missing(pos)

            # 3a. Hard stop-loss check (dominates TP if both touched same bar).
            if self._stop_loss_hit(pos, high, low):
                self._close_position(
                    pos,
                    exit_price=pos.stop_loss or close,
                    reason="stop_loss",
                )
                continue

            # 3b. Take-profit check.
            if self._take_profit_hit(pos, high, low):
                self._close_position(
                    pos,
                    exit_price=pos.take_profit or close,
                    reason="take_profit",
                )
                continue

            # 3c. Trailing-stop tightening (no exit yet).
            if self.cfg.risk.trailing_stop:
                self._maybe_trail_stop(pos, high, low, close, df, now)

        # Refresh marks so subsequent dashboard publish reflects truth.
        if marks:
            self.broker.update_marks(marks)

    def _init_trail_if_missing(self, pos: Position) -> None:
        """Build a _TrailState for a position we haven't seen before
        (typical after process restart or a fresh entry from this tick).
        """
        if pos.symbol in self._trails:
            return
        if pos.stop_loss is None:
            return
        risk = abs(pos.avg_price - pos.stop_loss)
        if risk <= 0:
            return
        self._trails[pos.symbol] = _TrailState(
            entry_price=pos.avg_price,
            side=pos.side,
            initial_stop=pos.stop_loss,
            risk_per_share=risk,
            peak_mark=pos.avg_price,
        )

    @staticmethod
    def _stop_loss_hit(pos: Position, high: float, low: float) -> bool:
        if pos.stop_loss is None:
            return False
        if pos.side == OrderSide.BUY:
            return low <= pos.stop_loss
        return high >= pos.stop_loss

    @staticmethod
    def _take_profit_hit(pos: Position, high: float, low: float) -> bool:
        if pos.take_profit is None:
            return False
        if pos.side == OrderSide.BUY:
            return high >= pos.take_profit
        return low <= pos.take_profit

    def _maybe_trail_stop(self, pos: Position, high: float, low: float,
                          close: float, df, now: datetime) -> None:
        st = self._trails.get(pos.symbol)
        if st is None or pos.stop_loss is None:
            return

        # Update the running peak (max-favorable price for the trade).
        if pos.side == OrderSide.BUY:
            st.peak_mark = max(st.peak_mark, high)
            unrealized_R = (st.peak_mark - st.entry_price) / st.risk_per_share
        else:
            st.peak_mark = min(st.peak_mark, low) if st.peak_mark else low
            unrealized_R = (st.entry_price - st.peak_mark) / st.risk_per_share

        # Activate trailing once the configured profit threshold is reached.
        if not st.trailing_active:
            if unrealized_R < self.cfg.risk.trail_activation_r:
                return
            st.trailing_active = True
            logger.info(
                "[trail] {} activated at +{:.2f}R (peak ₹{:.2f})",
                pos.symbol, unrealized_R, st.peak_mark,
            )

        # Compute the new stop using the most recent ATR(14) on 1-min bars.
        a_series = _atr(df, 14)
        atr_val = float(a_series.iloc[-1]) if len(a_series) else 0.0
        if atr_val <= 0:
            atr_val = st.risk_per_share  # fallback: use entry-time R

        trail_dist = self.cfg.risk.trail_atr_mult * atr_val
        # Floor: ensure we lock in at least `trail_lock_r` × R no matter what.
        lock_dist = (1.0 - self.cfg.risk.trail_lock_r) * st.risk_per_share

        if pos.side == OrderSide.BUY:
            atr_stop = st.peak_mark - trail_dist
            lock_stop = st.entry_price + (st.risk_per_share - lock_dist)
            new_stop = max(pos.stop_loss, atr_stop, lock_stop)
            if new_stop > pos.stop_loss + 1e-6:
                self._update_position_stop(pos, new_stop, now, unrealized_R)
        else:
            atr_stop = st.peak_mark + trail_dist
            lock_stop = st.entry_price - (st.risk_per_share - lock_dist)
            new_stop = min(pos.stop_loss, atr_stop, lock_stop)
            if new_stop < pos.stop_loss - 1e-6:
                self._update_position_stop(pos, new_stop, now, unrealized_R)

    def _update_position_stop(self, pos: Position, new_stop: float,
                              now: datetime, unrealized_R: float) -> None:
        """Mutate the position's stop-loss in place and journal the trail."""
        old_stop = pos.stop_loss
        pos.stop_loss = round(new_stop, 2)
        # Persist via the paper broker's normal state hook (it serialises
        # positions on every fill; we manually trigger a save here so a
        # process kill doesn't lose the trailed level).
        try:
            self.broker._persist()  # type: ignore[attr-defined]
        except AttributeError:
            pass

        st = self._trails[pos.symbol]
        # Throttle log spam: at most once per 60 s per symbol.
        if st.last_log_at is None or (now - st.last_log_at).total_seconds() > 60:
            logger.info(
                "[trail] {} SL ₹{:.2f} → ₹{:.2f} (+{:.2f}R, peak ₹{:.2f})",
                pos.symbol, old_stop or 0.0, pos.stop_loss,
                unrealized_R, st.peak_mark,
            )
            st.last_log_at = now

        self.cache.set_json(trail_key(pos.symbol, self.segment), {
            "ts": now.isoformat(),
            "entry": st.entry_price,
            "initial_stop": st.initial_stop,
            "current_stop": pos.stop_loss,
            "peak": st.peak_mark,
            "unrealized_R": round(unrealized_R, 2),
        }, ttl=86400)

    def _close_position(self, pos: Position, exit_price: float, reason: str) -> None:
        """Send an opposite-side market order to close `pos` at `exit_price`,
        journal it with the given exit reason.
        """
        opp = OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY
        order = Order(
            id=str(uuid.uuid4()), symbol=pos.symbol, side=opp,
            qty=abs(pos.qty), type=OrderType.MARKET, price=exit_price,
            product=self.cfg.execution.product,
        )
        before = next((p for p in self.broker.positions() if p.symbol == pos.symbol), None)
        result = self.broker.place_order(order)
        if result.status != OrderStatus.FILLED:
            logger.warning("[exit] {} close rejected: {}", pos.symbol, result.status.value)
            return
        after = next((p for p in self.broker.positions() if p.symbol == pos.symbol), None)
        self._trails.pop(pos.symbol, None)
        self.cache.delete(trail_key(pos.symbol, self.segment))
        self.notifier.fill(result)
        self.journal.record_fill(result, before, after, {
            "exit_reason": reason,
        })
        logger.info("[exit] {} closed via {} @ ₹{:.2f}", pos.symbol, reason, exit_price)

    def _lock_in_for_day(self) -> None:
        """Daily profit target hit — force-close everything and stop trading."""
        positions = [p for p in self.broker.positions() if p.qty != 0]
        pnl_pct = self.risk.daily_pnl_pct()
        logger.warning(
            "[profit-lock] daily target reached at +{:.2f}% — squaring off "
            "{} open position(s) and halting new entries for the day",
            pnl_pct, len(positions),
        )
        before_map = {p.symbol: p for p in positions}
        closed = self.broker.square_off_all()
        for o in closed:
            if o.status == OrderStatus.FILLED:
                self.notifier.fill(o)
                self.journal.record_fill(o, before_map.get(o.symbol), None, {
                    "exit_reason": "profit_lockin",
                })
        self._trails.clear()
        self._profit_locked = True
        self.cache.set_json(self._profit_lock_key, {
            "ts": datetime.now(IST).isoformat(),
            "pnl_pct": round(pnl_pct, 3),
            "target_pct": self.cfg.risk.daily_profit_target_pct,
        }, ttl=86400)

    def _place(self, sig: Signal, qty: int) -> None:
        side = OrderSide.BUY if sig.type == SignalType.BUY else OrderSide.SELL
        order_kwargs = dict(
            id=str(uuid.uuid4()), symbol=sig.symbol, side=side, qty=qty,
            type=OrderType.MARKET, price=sig.price,
            stop_loss=sig.stop_loss, take_profit=sig.take_profit,
            product=self.cfg.execution.product,
        )
        # F&O orders carry the instrument kind + lot size so the broker
        # uses the right fee schedule and cash/margin model:
        #  * spreads → margin = max_loss × qty + premium credit upfront
        #  * options → full premium debit + options fees
        #  * futures → margin debit + futures fees
        if self.segment == Segment.FNO:
            from .broker.base import InstrumentKind
            from .instruments.fno import (
                lot_size as _lot_size,
                parse_iron_condor_tradingsymbol,
                parse_option_tradingsymbol,
                parse_spread_tradingsymbol,
            )
            try:
                order_kwargs["lot_size"] = _lot_size(sig.symbol)
            except KeyError as e:
                logger.error("[fno] cannot place order for {}: {}", sig.symbol, e)
                return
            if parse_iron_condor_tradingsymbol(sig.symbol) is not None:
                order_kwargs["instrument_kind"] = InstrumentKind.IRON_CONDOR
            elif parse_spread_tradingsymbol(sig.symbol) is not None:
                order_kwargs["instrument_kind"] = InstrumentKind.SPREAD
            elif parse_option_tradingsymbol(sig.symbol) is not None:
                order_kwargs["instrument_kind"] = InstrumentKind.OPTION
            else:
                order_kwargs["instrument_kind"] = InstrumentKind.FUTURES
        order = Order(**order_kwargs)
        before = next((p for p in self.broker.positions() if p.symbol == sig.symbol), None)
        result = self.broker.place_order(order)
        after = next((p for p in self.broker.positions() if p.symbol == sig.symbol), None)
        self.cache.hset_json("orders", result.id, {
            "symbol": result.symbol, "side": result.side.value, "qty": result.qty,
            "status": result.status.value, "fill_price": result.fill_price,
            "fees": result.fees, "reason": sig.reason, "strategy": sig.strategy,
        })
        if result.status == OrderStatus.FILLED:
            self.notifier.fill(result)
            self.journal.record_fill(result, before, after, {
                "strategy": sig.strategy, "reason": sig.reason,
            })

    def _end_of_day(self) -> None:
        # ── Idempotency guard ────────────────────────────────────────────
        #
        # The 2026-05-04 ADANIENT incident (-₹24,887 phantom short) was
        # caused by `_end_of_day` running TWICE at 15:15:00 — once from
        # the dedicated `end_of_day` cron and once from the regular
        # `executor_tick` cron whose `should_square_off` branch also
        # routes here. APScheduler ran them in concurrent worker threads;
        # the second call's `square_off_all` saw `existing = None` for
        # the just-closed position and incorrectly opened a phantom SHORT,
        # which the broker then auto-covered, leaking ~₹25k.
        #
        # We persist the "EOD already ran" marker in Redis (not just an
        # instance attribute) so even a process restart between concurrent
        # calls would still be guarded. TTL of 2 days makes the key
        # self-clean over weekends without requiring an explicit reset.
        today = datetime.now(IST).date().isoformat()
        eod_key = f"eod_done:{self.segment.value}"
        prev = self.cache.get_json(eod_key)
        if isinstance(prev, dict) and prev.get("date") == today:
            logger.warning(
                "[{}] _end_of_day already ran today at {} — skipping "
                "duplicate call (this is the 2026-05-04 race-condition "
                "guard).", self.segment.value, prev.get("ts", "?"),
            )
            return

        positions = self.broker.positions()
        if not positions:
            # Mark as done even if nothing to close — otherwise a later
            # spurious call (e.g. from `tick` after we've cleared) could
            # still create phantom shorts via the broker's no-existing
            # path. The mark is the defence; the over-sell guard in the
            # broker is the second-line defence.
            self.cache.set_json(eod_key, {
                "date": today, "ts": datetime.now(IST).isoformat(),
                "closed": 0,
            }, ttl=86400 * 2)
            return
        logger.warning("[{}] Square-off time — closing {} open position(s)",
                       self.segment.value, len(positions))

        # Refresh marks one last time for accurate fill simulation.
        #
        # CRITICAL — use the SAME pricing path as `_manage_open_positions`
        # (intraday_bars), NOT `latest_quote`. For synthetic F&O symbols
        # (option, spread, iron condor) `intraday_bars` synthesises the
        # net premium via Black-Scholes; `latest_quote` previously fell
        # through to yfinance_proxy which mapped synthetic symbols to
        # the underlying SPOT (e.g. NIFTY26MAY24050-23950PESPRD →
        # ^NSEI ≈ ₹24,000) — that's how the 2026-04-30 paper bot
        # produced a fake -₹8.3M loss on two ₹100k credit spreads. If
        # `intraday_bars` is empty (rare — between sessions), we KEEP
        # the broker's last per-minute mark instead of overwriting with
        # a wrong one.
        marks = {}
        for p in positions:
            df = intraday_bars(p.symbol, "1m")
            if df.empty:
                df = intraday_bars(p.symbol, "5m")
            if not df.empty:
                marks[p.symbol] = float(df["close"].iloc[-1])
        if marks:
            self.broker.update_marks(marks)
        before_map = {p.symbol: p for p in positions}
        closed = self.broker.square_off_all()
        for o in closed:
            if o.status == OrderStatus.FILLED:
                self.notifier.fill(o)
                self.journal.record_fill(o, before_map.get(o.symbol), None, {
                    "exit_reason": "eod_squareoff",
                })
        # Mark EOD as done AFTER the closes so a partial-failure (e.g.
        # broker exception mid-loop) leaves the marker absent and the
        # next tick can retry. The duplicate-guard at the top of this
        # method is what protects against the cron-race; this mark is
        # the "successfully completed" stamp.
        self.cache.set_json(eod_key, {
            "date": today, "ts": datetime.now(IST).isoformat(),
            "closed": len([o for o in closed if o.status == OrderStatus.FILLED]),
        }, ttl=86400 * 2)
        self._publish_state()

    def _publish_state(self) -> None:
        positions = self.broker.positions()
        # Use the SEGMENT's capital, not the top-level (which is always equity).
        from .segment import cfg_capital as _cfg_capital
        snapshot = {
            "ts": datetime.now(IST).isoformat(),
            "segment": self.segment.value,
            "starting_capital": float(_cfg_capital(self.cfg, self.segment).total),
            "cash": self.broker.cash(),
            "profit_locked": self._profit_locked,
            "daily_pnl_pct": round(self.risk.daily_pnl_pct(), 3),
            "positions": [
                {
                    "symbol": p.symbol, "qty": p.qty, "side": p.side.value,
                    "avg_price": p.avg_price, "stop_loss": p.stop_loss,
                    "take_profit": p.take_profit,
                    "unrealized_pnl": p.unrealized_pnl,
                    "realized_pnl": p.realized_pnl,
                }
                for p in positions
            ],
        }
        self._publish(self._portfolio_key, snapshot)
