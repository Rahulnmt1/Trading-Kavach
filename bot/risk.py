"""Risk manager — the most important module in the bot.

Responsibilities:
  - Position sizing so per-trade loss <= max_loss_per_trade_pct of capital
  - Block new entries when daily loss >= max_daily_loss_pct
  - Cap concurrent open positions
  - Cap trades per day
  - Honour kill-switch file (`KILL_SWITCH` in project root)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from .broker.base import Broker, InstrumentKind
from .cache import get_cache
from .config import PROJECT_ROOT, load_config
from .fees import roundtrip_breakdown as _fee_roundtrip
from .logger import logger
from .segment import Segment, cache_key, cfg_capital, cfg_risk
from .strategies.base import Signal, SignalType

KILL_SWITCH = PROJECT_ROOT / "KILL_SWITCH"

# TTL for the persisted starting-equity snapshot. 36 hours covers an
# overnight restart on a normal weekday and a Friday-evening → Monday-
# morning gap (the snapshot's stored date prevents stale data from
# leaking into a new trading day even if Redis somehow keeps the key).
_STARTING_EQUITY_TTL_S = 36 * 3600


@dataclass
class RiskDecision:
    allow: bool
    qty: int = 0
    reason: str = ""


class RiskConfigError(ValueError):
    """Raised at RiskManager construction when the segment's risk caps
    are structurally unsafe — i.e. the bot is configured to allow more
    cumulative loss across simultaneously-open trades than the daily
    kill-switch budget. See :meth:`RiskManager._verify_risk_invariants`
    for the exact check.

    This is a startup error, not a runtime warning, because the sizing
    code in :meth:`RiskManager.evaluate` reads these caps on every
    signal — once the bot is running on an unsafe ratio it WILL size a
    single trade past the daily cap (the 2026-05-07 incident: per_trade
    5% × open 5 = 25% potential simultaneous SL on a 2% daily-loss
    budget; the iron-condor sizer used the per-trade cap to allocate
    6 lots/IC and exposed ₹8,864 (8.8% of capital) on two ICs).
    """


class RiskManager:
    def __init__(self, broker: Broker, segment: Segment = Segment.EQUITY) -> None:
        self.broker = broker
        self.segment = segment
        self.cfg = load_config()
        # Segment-aware capital + risk caps. Equity reads top-level
        # ``capital`` / ``risk``; F&O reads ``fno.capital`` / ``fno.risk``.
        self._capital_cfg = cfg_capital(self.cfg, segment)
        self._risk_cfg = cfg_risk(self.cfg, segment)
        # FIX #22 (2026-05-07): refuse to start if the loaded risk caps
        # would let the sizer allocate a position whose stop-loss alone
        # exceeds the daily kill-switch. See the docstring on
        # ``_verify_risk_invariants`` for the rationale.
        self._verify_risk_invariants()
        self._cache = get_cache()
        self._equity_key = cache_key("risk:starting_equity", segment)
        self._trades_today = 0
        self._day = date.today()
        # Restore the kill-switch baseline persisted by an earlier process
        # today (if any). Without this, a mid-session restart would resnap
        # ``_starting_equity`` from current equity — silently zeroing the
        # daily-loss meter so a -1.5% drawdown would no longer count
        # against the 2% kill switch.
        self._starting_equity: Optional[float] = self._load_starting_equity()
        if self._starting_equity is not None:
            configured = float(self._capital_cfg.total)
            divergence_pct = (abs(self._starting_equity - configured)
                              / max(configured, 1.0) * 100)
            if divergence_pct > 5.0:
                logger.warning(
                    "[risk:{}] RESTORED suspicious starting_equity=₹{:,.2f} "
                    "for {} (configured capital ₹{:,.2f}, divergence {:.1f}%). "
                    "If this is wrong, the kill-switch + profit-lock will be "
                    "off all day. To force a fresh capture: "
                    "`redis-cli DEL {}` and restart the bot.",
                    segment.value, self._starting_equity, self._day,
                    configured, divergence_pct, self._equity_key,
                )
            else:
                logger.info(
                    "[risk:{}] restored starting_equity=₹{:,.2f} for {}",
                    segment.value, self._starting_equity, self._day,
                )

    def _verify_risk_invariants(self) -> None:
        """Refuse to start if the configured risk caps are structurally
        unsafe.

        The invariant we enforce:

            max_loss_per_trade_pct × max_open_positions ≤ max_daily_loss_pct

        Reasoning. The position sizer in :meth:`evaluate` allocates qty
        such that ``risk_per_share × qty ≈ capital × max_loss_per_trade_pct``
        — i.e. each trade's SL hit costs at most ``max_loss_per_trade_pct``
        of capital. With ``max_open_positions`` simultaneous trades, a
        synchronised stop-out across all of them costs the *product* of
        those caps. If that product exceeds ``max_daily_loss_pct``, the
        kill-switch threshold can be punched through by a single market
        move that knocks out every open position at once, with no
        opportunity for the daily-loss cap to halt new entries (no new
        entries are needed — the existing book alone breaches it).

        2026-05-07 incident — the realised version of this:

          F&O config had per_trade=5.0% × open=5 = 25% potential
          simultaneous SL on a 2% daily-loss budget. The IC sizer used
          the per-trade cap to allocate 6 lots/IC; two ICs open at
          11:10 IST exposed ₹8,864 (8.8% of capital) at SL — 4.4× the
          daily kill-switch. We squared off manually for a realised
          -₹528 vs that ₹8,864 tail.

        We raise :class:`RiskConfigError` rather than just logging a
        warning, because the failure mode is silent at runtime — the
        sizer happily allocates the unsafe size and the operator only
        finds out when the dashboard's Net @ SL column shows a tail
        bigger than the daily cap.
        """
        per_trade = float(self._risk_cfg.max_loss_per_trade_pct)
        daily_cap = float(self._risk_cfg.max_daily_loss_pct)
        open_cap = int(self._risk_cfg.max_open_positions)
        product = per_trade * open_cap
        # Allow a tiny float-comparison fudge so 1.0 × 2 == 2.0 doesn't
        # fail to a 2.0000000004 representation.
        if product > daily_cap + 1e-9:
            msg = (
                f"[risk:{self.segment.value}] STRUCTURAL RISK CONFIG ERROR — "
                f"max_loss_per_trade_pct ({per_trade}%) × max_open_positions "
                f"({open_cap}) = {product:.2f}% exceeds max_daily_loss_pct "
                f"({daily_cap}%). At this ratio a synchronised SL on every "
                f"open trade would breach the daily kill-switch with no "
                f"opportunity for the daily-loss cap to halt new entries. "
                f"Edit config.yaml so per_trade × open ≤ daily_cap "
                f"(e.g. per_trade=1.0, open=2 → 2.0% ≤ 2.0%) and restart. "
                f"See FIX #22 in bot/risk.py::_verify_risk_invariants."
            )
            logger.error(msg)
            raise RiskConfigError(msg)
        logger.info(
            "[risk:{}] risk-invariant OK: per_trade {}% × open {} = "
            "{:.2f}% ≤ daily_cap {}%",
            self.segment.value, per_trade, open_cap, product, daily_cap,
        )

    def _load_starting_equity(self) -> Optional[float]:
        snap = self._cache.get_json(self._equity_key)
        if isinstance(snap, dict) and snap.get("date") == self._day.isoformat():
            try:
                return float(snap["value"])
            except (KeyError, TypeError, ValueError):
                return None
        return None

    def _persist_starting_equity(self) -> None:
        if self._starting_equity is None:
            return
        self._cache.set_json(self._equity_key, {
            "date": self._day.isoformat(),
            "value": self._starting_equity,
        }, ttl=_STARTING_EQUITY_TTL_S)

    def _reset_if_new_day(self) -> None:
        today = date.today()
        if today != self._day:
            self._day = today
            self._trades_today = 0
            self._starting_equity = None
            # Drop yesterday's persisted snapshot so the new day's first
            # ``_daily_pnl_pct()`` call writes a fresh baseline.
            self._cache.delete(self._equity_key)
            logger.info("[risk:{}] new day {}, counters reset", self.segment.value, today)

    def _equity_breakdown(self) -> dict:
        """Return ``_equity()`` decomposed into named components.

        Used by the diagnostic logger that fires every time
        ``_starting_equity`` is captured — without the breakdown we can't
        reconstruct *why* a wrong baseline got persisted (the 2026-05-06
        F&O "+100%" symptom: ``_starting_equity`` was set to ~₹50K when
        configured F&O capital is ₹100K, and the persisted Redis key
        cemented the bad value for the rest of the day). Logging cash,
        long-cost-basis, unrealized, margin-offset, and the per-position
        contributions lets us see at a glance which term was abnormal at
        capture time.
        """
        cash = self.broker.cash()
        positions = self.broker.positions()
        long_cost_basis = sum(p.avg_price * p.qty for p in positions if p.qty > 0)
        unrealized = sum(p.unrealized_pnl for p in positions)
        margin_offset = 0.0
        per_position = []
        for p in positions:
            kind = getattr(p, "instrument_kind", InstrumentKind.EQUITY)
            margin = float(getattr(p, "margin_blocked", 0.0) or 0.0)
            contrib = 0.0
            if kind == InstrumentKind.FUTURES:
                contrib = margin
                margin_offset += margin
            elif kind in (InstrumentKind.SPREAD, InstrumentKind.IRON_CONDOR):
                if p.qty < 0:
                    credit_received = p.avg_price * abs(p.qty)
                    contrib = margin - credit_received
                    margin_offset += contrib
            per_position.append({
                "symbol": p.symbol, "qty": p.qty, "avg": round(p.avg_price, 2),
                "kind": kind.value if hasattr(kind, "value") else str(kind),
                "margin_blocked": round(margin, 2),
                "unrealized_pnl": round(p.unrealized_pnl, 2),
                "margin_offset_contrib": round(contrib, 2),
            })
        return {
            "cash": round(cash, 2),
            "long_cost_basis": round(long_cost_basis, 2),
            "unrealized": round(unrealized, 2),
            "margin_offset": round(margin_offset, 2),
            "n_positions": len(positions),
            "per_position": per_position,
            "equity": round(cash + long_cost_basis + unrealized + margin_offset, 2),
        }

    def _capture_starting_equity(self, *, source: str) -> float:
        """Snap ``_starting_equity`` and persist it, with diagnostic logging.

        This is the single chokepoint for the "first read of the day"
        capture. Every site that previously did
        ``self._starting_equity = self._equity()`` now goes through
        here so we always log the breakdown and the source path
        ("first_pnl_call" / "record_trade" / "public_pnl_call").
        """
        breakdown = self._equity_breakdown()
        self._starting_equity = breakdown["equity"]
        self._persist_starting_equity()
        configured = float(self._capital_cfg.total)
        # Loud warning if the captured baseline diverges from configured
        # capital by more than 5% — the F&O "+100%" symptom would have
        # caught this (₹50K captured vs ₹100K configured = 50% divergence).
        # 5% tolerance accommodates the legitimate margin-offset settling
        # that happens when positions are already open at process start.
        divergence_pct = abs(breakdown["equity"] - configured) / max(configured, 1.0) * 100
        if divergence_pct > 5.0:
            logger.warning(
                "[risk:{}] BASELINE DIVERGENCE — captured _starting_equity="
                "₹{:,.2f} differs from configured capital ₹{:,.2f} by {:.1f}%. "
                "source={}  cash=₹{:,.2f}  long_cost_basis=₹{:,.2f}  "
                "unrealized=₹{:,.2f}  margin_offset=₹{:,.2f}  n_positions={}. "
                "Per-position: {}. This baseline drives the kill-switch + "
                "profit-lock all day; if it's wrong, every later P&L pct is "
                "off by the same constant.",
                self.segment.value, breakdown["equity"], configured, divergence_pct,
                source, breakdown["cash"], breakdown["long_cost_basis"],
                breakdown["unrealized"], breakdown["margin_offset"],
                breakdown["n_positions"], breakdown["per_position"],
            )
        else:
            logger.info(
                "[risk:{}] captured _starting_equity=₹{:,.2f} (source={}, "
                "cash=₹{:,.2f}, unrealized=₹{:,.2f}, margin_offset=₹{:,.2f}, "
                "n_positions={})",
                self.segment.value, breakdown["equity"], source,
                breakdown["cash"], breakdown["unrealized"],
                breakdown["margin_offset"], breakdown["n_positions"],
            )
        return self._starting_equity

    def _equity(self) -> float:
        """Total account equity = cash + economic value of held positions.

        The paper broker uses different cash-flow models depending on
        ``InstrumentKind``. The equity formula must account for each so a
        position never produces a phantom P&L the moment it opens:

        * **Long equity / long option BUY** — cash debited by full notional
          (``qty * avg_price + fees``). Position contributes
          ``avg_price * qty`` (cost basis, recovers at close) plus the
          mark-to-market drift in ``unrealized_pnl``.
        * **Equity short SELL** — paper broker simplification: cash is
          debited by entry fees only (no proceeds credited). Position
          contributes only ``unrealized_pnl``.
        * **Futures (long or short)** — cash debited by ``margin_blocked``
          + fees; position holds the margin and refunds it at close.
          Position contributes ``margin_blocked + unrealized_pnl``.
        * **Credit spread / iron-condor (short)** — cash flow at entry
          is ``+credit_received - margin_blocked - fees``: the broker
          banks the credit and locks the margin. Position contributes
          ``margin_blocked - credit_received + unrealized_pnl``: at
          close, margin refunds, the credit is "spent" buying back the
          spread, and unrealized captures the mark drift.

        ── Why this matters (the 2026-05-05 PM kill-switch trip) ─────────
        The original formula was ``cash + long_cost_basis + unrealized``,
        written for a Phase-1 equity-only book. On 2026-05-05 the F&O
        bot opened two NIFTY/BANKNIFTY put credit-spreads at 13:26 and
        the dashboard immediately reported ``daily_pnl_pct = -2.252%`` —
        purely the margin block (₹10,868) net of premium collected
        (₹8,628), which is *not* economic loss. The kill-switch
        threshold is ``-2.0%``, so the bot was 0.252 percentage-points
        from halting all further trading on a non-event. This formula
        now adds the credit-spread / IC offset (margin − credit) so a
        flat-mark spread reports ~0% P&L, with kill-switch reserved
        for actual mark-to-market loss.
        """
        cash = self.broker.cash()
        positions = self.broker.positions()
        long_cost_basis = sum(p.avg_price * p.qty for p in positions if p.qty > 0)
        unrealized = sum(p.unrealized_pnl for p in positions)

        margin_offset = 0.0
        for p in positions:
            kind = getattr(p, "instrument_kind", InstrumentKind.EQUITY)
            margin = float(getattr(p, "margin_blocked", 0.0) or 0.0)
            if kind == InstrumentKind.FUTURES:
                margin_offset += margin
            elif kind in (InstrumentKind.SPREAD, InstrumentKind.IRON_CONDOR):
                if p.qty < 0:
                    credit_received = p.avg_price * abs(p.qty)
                    margin_offset += margin - credit_received

        return cash + long_cost_basis + unrealized + margin_offset

    def _daily_pnl_pct(self) -> float:
        if self._starting_equity is None:
            self._capture_starting_equity(source="first_pnl_call")
            return 0.0
        eq = self._equity()
        return (eq - self._starting_equity) / max(self._starting_equity, 1.0) * 100

    def _kill_switch_active(self) -> bool:
        return KILL_SWITCH.exists()

    # ── FIX #37 (2026-05-16) — Fee-aware entry gate ───────────────────────
    def _check_fee_edge(
        self,
        signal: Signal,
        qty: int,
        fee_segment: str,
    ) -> Optional[str]:
        """Reject a sized trade if expected round-trip fees consume too
        much of the expected gross profit at take-profit.

        Returns the rejection reason (string) if the gate fires, or
        ``None`` if the trade clears the gate.

        Inputs
        ------
        ``signal`` is a sized BUY/SELL signal with ``price``, ``stop_loss``,
        ``take_profit`` set. ``qty`` is the quantity the sizer just
        approved. ``fee_segment`` is the value passed to
        :func:`bot.fees.roundtrip_breakdown` — one of ``"equity"`` /
        ``"futures"`` / ``"options"``.

        Math
        ----
        ``gross = (TP − entry) × qty`` for a long trade,
        ``gross = (entry − TP) × qty`` for a short.
        ``fees = roundtrip_breakdown(qty, entry, TP, direction, segment).fees_total``
        — i.e. realistic post-FIX-#21r STT + flat ₹20 brokerage + GST +
        exchange + SEBI + stamp.

        The gate fires when ``fees / gross × 100 > max_fee_pct_of_gross``.

        Why TP-based gross
        ------------------
        Using TP gives the BEST-CASE gross — i.e. the gate is permissive,
        rejecting only trades whose fee drag is unmanageable even in the
        winning scenario. If TP isn't set (rare; some squareoff signals
        skip TP), the gate is silently bypassed.

        Disabled when
        -------------
        * ``max_fee_pct_of_gross`` is ≤ 0 or ≥ 100 (full disable);
        * ``signal.take_profit`` is None or non-positive;
        * gross profit is non-positive (degenerate / invalid TP).
        """
        threshold = float(getattr(self._risk_cfg, "max_fee_pct_of_gross", 0.0) or 0.0)
        if threshold <= 0.0 or threshold >= 100.0:
            return None
        tp = signal.take_profit
        if tp is None or tp <= 0.0 or signal.price <= 0.0 or qty <= 0:
            return None

        # BUY signal → long; SELL → short. Same convention as Signal.type.
        direction = "long" if signal.type == SignalType.BUY else "short"
        if direction == "long":
            gross_per_share = tp - signal.price
        else:
            gross_per_share = signal.price - tp
        if gross_per_share <= 0.0:
            # Degenerate — TP is on the wrong side of entry. Defensive: let
            # the rest of the pipeline reject (this should already have
            # been caught upstream).
            return None

        try:
            rt = _fee_roundtrip(
                qty=int(qty),
                entry_price=float(signal.price),
                exit_price=float(tp),
                direction=direction,
                segment=fee_segment,  # type: ignore[arg-type]
            )
        except Exception as e:  # noqa: BLE001
            # Fee module raises ValueError on direction typos etc. Defensive
            # — shouldn't happen given the call sites, but fail-open so the
            # gate never blocks a trade due to its own bug.
            logger.warning(
                "[risk:{}] _check_fee_edge: roundtrip_breakdown failed "
                "({}); fee gate skipped for {}", self.segment.value, e,
                signal.symbol,
            )
            return None

        gross = rt.gross_pnl
        if gross <= 0:
            return None
        fee_pct = rt.fees_total / gross * 100.0
        if fee_pct > threshold:
            return (
                f"FIX #37 fee-edge: expected round-trip fees "
                f"₹{rt.fees_total:,.0f} = {fee_pct:.1f}% of "
                f"₹{gross:,.0f} TP-gross > {threshold:.0f}% threshold "
                f"(qty={qty}, entry=₹{signal.price:.2f}, "
                f"TP=₹{tp:.2f}, segment={fee_segment})"
            )
        return None

    def evaluate(self, signal: Signal) -> RiskDecision:
        self._reset_if_new_day()

        if self._kill_switch_active():
            return RiskDecision(False, 0, "KILL_SWITCH file present — bot halted")

        if signal.type == SignalType.HOLD:
            return RiskDecision(False, 0, "HOLD signal")

        # Daily loss cap.
        pnl_pct = self._daily_pnl_pct()
        if pnl_pct <= -self._risk_cfg.max_daily_loss_pct:
            return RiskDecision(False, 0, f"daily loss {pnl_pct:.2f}% ≤ -{self._risk_cfg.max_daily_loss_pct}%")

        # Daily profit lock-in: stop opening new trades once we've made
        # the day's target. Existing positions remain open (the executor
        # squares them off separately in `_check_profit_lockin`).
        target = self._risk_cfg.daily_profit_target_pct
        if target > 0 and pnl_pct >= target:
            return RiskDecision(
                False, 0,
                f"daily profit target {pnl_pct:+.2f}% ≥ +{target}% — locked in for the day",
            )

        # Trade count.
        if self._trades_today >= self._risk_cfg.max_trades_per_day:
            return RiskDecision(False, 0, f"max trades/day reached ({self._trades_today})")

        # Concurrent positions.
        open_positions = [p for p in self.broker.positions() if p.qty != 0]
        if len(open_positions) >= self._risk_cfg.max_open_positions:
            return RiskDecision(False, 0, f"max open positions ({len(open_positions)})")

        # Skip if we already hold this symbol.
        if any(p.symbol == signal.symbol and p.qty != 0 for p in open_positions):
            return RiskDecision(False, 0, f"already in position for {signal.symbol}")

        # Position sizing — risk-based.
        if signal.stop_loss is None or signal.price <= 0:
            return RiskDecision(False, 0, "missing stop-loss")

        risk_per_share = abs(signal.price - signal.stop_loss)
        if risk_per_share <= 0:
            return RiskDecision(False, 0, "stop-loss equals entry price")

        capital = self._capital_cfg.total
        max_loss = capital * self._risk_cfg.max_loss_per_trade_pct / 100.0
        risk_qty = int(max_loss // risk_per_share)

        # Position-size cap + cash cap. Computed differently for F&O than
        # equity, and differently again for options / futures / spreads.
        cash = self.broker.cash()
        if self.segment == Segment.FNO:
            from .instruments.fno import (
                lot_size as _lot_size,
                margin_pct as _margin_pct,
                parse_iron_condor_tradingsymbol,
                parse_option_tradingsymbol,
                parse_spread_tradingsymbol,
            )
            try:
                lot = _lot_size(signal.symbol)
            except KeyError:
                # Strategy emitted a signal for an underlying we don't recognise
                # — typo in fno.watchlist.symbols. Reject loudly so the user
                # notices instead of silently sizing as 1 share.
                return RiskDecision(
                    False, 0,
                    f"unknown F&O underlying {signal.symbol!r} — add it to "
                    "bot/instruments/fno.py LOT_SIZES",
                )

            is_option = parse_option_tradingsymbol(signal.symbol) is not None
            spread_meta = parse_spread_tradingsymbol(signal.symbol)
            is_spread = spread_meta is not None
            ic_meta = parse_iron_condor_tradingsymbol(signal.symbol)
            is_iron_condor = ic_meta is not None

            if is_iron_condor:
                # ── Iron-condor sizing (Phase 4.5) ───────────────────
                # Defined-risk: margin = max(put_width, call_width) − net_credit
                # × qty. (Not the SUM of both spread maxes — spot can hit
                # at most one wing at expiry.) Cash debit at entry is
                # margin − net_credit.
                worst_wing = max(ic_meta["put_width"], ic_meta["call_width"])
                if signal.price <= 0 or signal.price >= worst_wing:
                    return RiskDecision(
                        False, 0,
                        f"iron-condor sizing rejected: net credit "
                        f"₹{signal.price:.2f} must be in (0, {worst_wing}) "
                        "(else degenerate / free-money)",
                    )
                max_loss_per_share = worst_wing - signal.price
                max_pos_margin = capital * self._risk_cfg.max_position_pct / 100.0
                size_cap = int(max_pos_margin // max(max_loss_per_share, 1e-9))
                net_cash_per_share = max_loss_per_share - signal.price
                if net_cash_per_share <= 0:
                    cash_cap = size_cap
                else:
                    cash_cap = int(cash // net_cash_per_share)
                qty = max(0, min(risk_qty, size_cap, cash_cap))
                qty = (qty // lot) * lot
                if qty < lot:
                    margin_per_lot = max_loss_per_share * lot
                    credit_per_lot = signal.price * lot
                    return RiskDecision(
                        False, 0,
                        f"qty=0 (need ≥1 lot of {lot}, have risk_qty={risk_qty}, "
                        f"size_cap={size_cap}, cash_cap={cash_cap}). 1 lot of "
                        f"{signal.symbol} blocks ₹{margin_per_lot:,.0f} margin "
                        f"(worst wing={worst_wing}, max_loss/share="
                        f"₹{max_loss_per_share:.2f}, credit/lot="
                        f"₹{credit_per_lot:,.0f}); available cash ₹{cash:,.0f}.",
                    )
                lots = qty // lot
                return RiskDecision(
                    True, qty,
                    f"approved qty={qty} ({lots} lot{'s' if lots != 1 else ''} of {lot}) "
                    f"credit/lot=₹{signal.price * lot:,.0f} "
                    f"margin/lot=₹{max_loss_per_share * lot:,.0f}",
                )

            if is_spread:
                # ── Vertical credit-spread sizing (Phase 4) ──────────
                # Defined-risk: margin = max_loss × qty. Cash debit is
                # the NET margin (margin − credit collected).
                from .options.margin import vertical_spread_max_loss
                try:
                    max_loss_per_share = vertical_spread_max_loss(
                        spread_meta["short_strike"],
                        spread_meta["long_strike"],
                        signal.price,
                    )
                except ValueError as e:
                    return RiskDecision(False, 0, f"spread sizing rejected: {e}")
                # max_position_pct caps the MARGIN deployed (capital at risk).
                max_pos_margin = capital * self._risk_cfg.max_position_pct / 100.0
                size_cap = int(max_pos_margin // max(max_loss_per_share, 1e-9))
                # Net cash impact at entry = margin − credit. Use that for the
                # cash-cap (not max_loss alone) so we account for the credit
                # offset in the cash gate.
                net_cash_per_share = max_loss_per_share - signal.price
                if net_cash_per_share <= 0:
                    # Rare degenerate case: credit ≥ max_loss = "free money".
                    # Allow but cap at size_cap (cash isn't binding).
                    cash_cap = size_cap
                else:
                    cash_cap = int(cash // net_cash_per_share)
                qty = max(0, min(risk_qty, size_cap, cash_cap))
                qty = (qty // lot) * lot
                if qty < lot:
                    margin_per_lot = max_loss_per_share * lot
                    credit_per_lot = signal.price * lot
                    return RiskDecision(
                        False, 0,
                        f"qty=0 (need ≥1 lot of {lot}, have risk_qty={risk_qty}, "
                        f"size_cap={size_cap}, cash_cap={cash_cap}). 1 lot of "
                        f"{signal.symbol} blocks ₹{margin_per_lot:,.0f} margin "
                        f"(max_loss/share=₹{max_loss_per_share:.2f}, credit/lot="
                        f"₹{credit_per_lot:,.0f}); available cash ₹{cash:,.0f}.",
                    )
                lots = qty // lot
                return RiskDecision(
                    True, qty,
                    f"approved qty={qty} ({lots} lot{'s' if lots != 1 else ''} of {lot}) "
                    f"credit/lot=₹{signal.price * lot:,.0f} "
                    f"margin/lot=₹{max_loss_per_share * lot:,.0f}",
                )

            if is_option:
                # ── Long-option sizing (Phase 3) ─────────────────────
                # Cash debit on entry = full premium × qty, no margin.
                # Worst-case loss caps at the entire premium paid (the
                # SL premium might be lower, in which case risk_qty is
                # the binding constraint anyway).
                premium_per_share = signal.price
                if premium_per_share <= 0:
                    return RiskDecision(
                        False, 0,
                        f"option premium {premium_per_share:.2f} non-positive — "
                        "BS pricer returned zero (deep OTM or expired)",
                    )
                # max_position_pct caps the PREMIUM DEPLOYED, since that's
                # the capital actually tied up by the position.
                max_pos_premium = capital * self._risk_cfg.max_position_pct / 100.0
                size_cap = int(max_pos_premium // max(premium_per_share, 1e-9))
                cash_cap = int(cash // max(premium_per_share, 1e-9))
                qty = max(0, min(risk_qty, size_cap, cash_cap))
                qty = (qty // lot) * lot
                if qty < lot:
                    premium_per_lot = premium_per_share * lot
                    return RiskDecision(
                        False, 0,
                        f"qty=0 (need ≥1 lot of {lot}, have risk_qty={risk_qty}, "
                        f"size_cap={size_cap}, cash_cap={cash_cap}). "
                        f"1 lot of {signal.symbol} costs ~₹{premium_per_lot:,.0f} "
                        f"(premium ₹{premium_per_share:.2f}); available cash "
                        f"₹{cash:,.0f}. Either raise fno.capital.total OR widen "
                        f"max_loss_per_trade_pct OR pick a deeper-OTM strike.",
                    )
                # FIX #37 (2026-05-16) — fee-aware gate (options).
                fee_reject = self._check_fee_edge(signal, qty, "options")
                if fee_reject:
                    return RiskDecision(False, 0, fee_reject)
                lots = qty // lot
                return RiskDecision(
                    True, qty,
                    f"approved qty={qty} ({lots} lot{'s' if lots != 1 else ''} of {lot}) "
                    f"premium=₹{premium_per_share:.2f}/share "
                    f"capital_at_risk=₹{premium_per_share*qty:,.0f} "
                    f"(SL caps loss at ₹{risk_per_share*qty:,.0f})",
                )

            # ── Futures sizing (Phase 2) ────────────────────────────
            m_pct = _margin_pct(signal.symbol)
            margin_per_share = signal.price * m_pct
            max_pos_margin = capital * self._risk_cfg.max_position_pct / 100.0
            size_cap = int(max_pos_margin // max(margin_per_share, 1e-9))
            cash_cap = int(cash // max(margin_per_share, 1e-9))
            qty = max(0, min(risk_qty, size_cap, cash_cap))
            qty = (qty // lot) * lot
            if qty < lot:
                margin_per_lot = margin_per_share * lot
                return RiskDecision(
                    False, 0,
                    f"qty=0 (need ≥1 lot of {lot}, have risk_qty={risk_qty}, "
                    f"size_cap={size_cap}, cash_cap={cash_cap}). "
                    f"1 lot needs ~₹{margin_per_lot:,.0f} margin "
                    f"({m_pct*100:.0f}% of ₹{signal.price * lot:,.0f} contract value); "
                    f"available cash ₹{cash:,.0f}.",
                )
            # FIX #37 (2026-05-16) — fee-aware gate (futures).
            fee_reject = self._check_fee_edge(signal, qty, "futures")
            if fee_reject:
                return RiskDecision(False, 0, fee_reject)
            lots = qty // lot
            return RiskDecision(
                True, qty,
                f"approved qty={qty} ({lots} lot{'s' if lots != 1 else ''} of {lot}) "
                f"risk=₹{max_loss:.0f} margin=₹{margin_per_share*qty:,.0f}",
            )

        # ── Equity (legacy path, unchanged) ─────────────────────────────
        max_pos_value = capital * self._risk_cfg.max_position_pct / 100.0
        size_cap = int(max_pos_value // signal.price)
        cash_cap = int(cash // signal.price)

        qty = max(0, min(risk_qty, size_cap, cash_cap))
        if qty == 0:
            return RiskDecision(
                False, 0,
                f"qty=0 (risk_qty={risk_qty}, size_cap={size_cap}, cash_cap={cash_cap})",
            )

        # FIX #37 (2026-05-16) — fee-aware gate (equity).
        fee_reject = self._check_fee_edge(signal, qty, "equity")
        if fee_reject:
            return RiskDecision(False, 0, fee_reject)

        return RiskDecision(True, qty, f"approved qty={qty} (risk=₹{max_loss:.0f}, sl={risk_per_share:.2f}/sh)")

    def record_trade(self) -> None:
        self._trades_today += 1
        if self._starting_equity is None:
            self._capture_starting_equity(source="record_trade")

    def should_square_off(self, now_time) -> bool:
        return now_time >= self.cfg.session.t("square_off")

    def in_trading_window(self, now_time) -> bool:
        """Window during which NEW entries are allowed.

        Note: this is NOT the position-management window — see
        :meth:`in_management_window` for that. Confusing the two caused
        FIX #29 (trade_cutoff returning early in ``Executor.tick`` left
        open positions unmanaged for 1h 45m on 2026-05-15, which rode
        BANKNIFTY26MAY54200CE from a recoverable -₹6.7K SL fill all the
        way to a -₹22.7K EOD square-off).
        """
        return self.cfg.session.t("trade_start") <= now_time <= self.cfg.session.t("trade_cutoff")

    def in_management_window(self, now_time) -> bool:
        """Window during which open positions MUST be managed (SL/TP/trail).

        Wider than :meth:`in_trading_window`: starts at ``trade_start``
        (no point managing before market is open) and runs until
        ``square_off`` (after which ``_end_of_day`` does the forced
        close). The gap between ``trade_cutoff`` and ``square_off`` —
        currently 13:30→15:15 IST = 1h 45m — is precisely the window
        where today's catastrophic BANKNIFTY trade rode unmanaged.

        FIX #29: ``Executor.tick`` now uses this method (not
        ``in_trading_window``) to decide whether to call
        ``_manage_open_positions``.
        """
        return self.cfg.session.t("trade_start") <= now_time < self.cfg.session.t("square_off")

    def daily_loss_kill_breached(self) -> bool:
        """FIX #30 — Open-position kill switch.

        Returns True when today's combined realized + unrealized P&L has
        breached the configured ``max_daily_loss_pct`` threshold. The
        existing :meth:`evaluate` gate uses the same threshold to BLOCK
        new entries; this method exists so the executor can force-close
        ALREADY-OPEN positions when one of them blows past its per-trade
        SL faster than the manager can react (theta + adverse spot move
        on a long option premium is the canonical case — today's
        BANKNIFTY26MAY54200CE breached -2% combined while the bot was
        still holding it open).

        Combined with FIX #29 (positions are now managed all the way to
        square_off) and FIX #31 (option-buying SL tightened to 35%
        max premium drop), the worst-case single-trade loss should now
        be capped well below the daily kill threshold.
        """
        self._reset_if_new_day()
        if self._kill_switch_active():
            return True
        return self._daily_pnl_pct() <= -float(self._risk_cfg.max_daily_loss_pct)

    def profit_target_hit(self) -> bool:
        """Return True if today's P&L (incl. unrealized) has reached the
        configured `daily_profit_target_pct`. The executor uses this to
        force-square-off everything mid-session and stop trading for the day.
        """
        target = self._risk_cfg.daily_profit_target_pct
        if target <= 0:
            return False
        self._reset_if_new_day()
        return self._daily_pnl_pct() >= target

    def daily_pnl_pct(self) -> float:
        """Public accessor for today's P&L percentage — used by the dashboard
        and logging. Initialises ``_starting_equity`` if not already set.
        """
        self._reset_if_new_day()
        if self._starting_equity is None:
            self._capture_starting_equity(source="public_pnl_call")
        return self._daily_pnl_pct()
