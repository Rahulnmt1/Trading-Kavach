"""Paper broker — full simulator with slippage and Indian fee model.

The fee schedule (brokerage, STT, exchange charges, SEBI, stamp duty, GST)
lives in :mod:`bot.fees` so the dashboard / journal / risk manager all see
the *same* numbers the broker debits — no drift, no duplication.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

import pytz

from ..cache import get_cache
from ..config import load_config
from ..fees import compute_fees
from ..logger import logger
from ..segment import Segment, cache_key, cfg_capital
from .base import Broker, InstrumentKind, Order, OrderSide, OrderStatus, OrderType, Position

IST = pytz.timezone("Asia/Kolkata")

# A restored position whose avg_price diverges from the current market mark
# by more than this fraction is treated as corrupt and discarded. Set high
# enough to tolerate normal intraday moves (a 30% gap-down is rare but
# possible) but low enough to catch the kind of corruption we saw on
# 2026-04-29 — an avg_price of ₹2400 on a stock currently trading at ₹1454
# (an 87% divergence). Stock splits are the main legitimate cause; we
# accept losing position state across a split as a fair trade for safety.
_AVG_PRICE_TOLERANCE = 0.30


def _fee_segment_for(kind: InstrumentKind) -> str:
    """Map InstrumentKind → ``compute_fees`` ``segment`` argument.

    SPREAD/IRON_CONDOR map to "options" too — every leg of a multi-leg
    options structure incurs the options fee schedule. We compute fees
    ONCE on the net premium turnover (per share × qty) and then add a
    flat per-extra-leg brokerage surcharge in :func:`_fees`. STT
    applies only to the sell-side, and the long-leg buy fees are
    dominated by the surcharge anyway — see :func:`_fees` for the full
    rationale.
    """
    if kind == InstrumentKind.FUTURES:
        return "futures"
    if kind in (InstrumentKind.OPTION, InstrumentKind.SPREAD,
                InstrumentKind.IRON_CONDOR):
        return "options"
    return "equity"


def _fees(side: OrderSide, qty: int, price: float,
          kind: InstrumentKind = InstrumentKind.EQUITY) -> float:
    """Thin shim around :func:`bot.fees.compute_fees` returning the rounded total.

    Dispatches the rate table by instrument kind:
      * ``EQUITY``       → equity intraday MIS rates (legacy default).
      * ``FUTURES``      → ₹20 flat + 0.0125% sell-STT + 0.00188% exchange.
      * ``OPTION``       → ₹20 flat + 0.0625% premium-sell-STT + 0.0495% exch.
      * ``SPREAD``       → options rates on |net_price| × qty + ₹20
                            surcharge for the second leg's brokerage.
      * ``IRON_CONDOR``  → options rates on |net_price| × qty + ₹60
                            surcharge for the three additional legs
                            (4 legs total, ₹20 brokerage per leg).

    For options/spreads/ICs ``price`` is the **net premium** (per share);
    callers pass the right number.
    """
    base = compute_fees(side.value, qty, abs(price), segment=_fee_segment_for(kind)).total
    if kind == InstrumentKind.SPREAD:
        base = round(base + 20.0, 2)         # 1 extra leg
    elif kind == InstrumentKind.IRON_CONDOR:
        base = round(base + 60.0, 2)         # 3 extra legs
    return base


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, segment: Segment = Segment.EQUITY) -> None:
        cfg = load_config()
        self.segment = segment
        # Segment-aware capital lookup: equity reads top-level
        # ``capital``; F&O reads ``fno.capital``. Defaults to top-level
        # for unknown segments (defensive).
        self._starting_cash = float(cfg_capital(cfg, segment).total)
        self._cash = self._starting_cash
        self._slip_bps = cfg.execution.slippage_bps
        self._state_key = cache_key("paper:state", segment)
        self._positions: dict[str, Position] = {}
        self._orders: list[Order] = []
        self._marks: dict[str, float] = {}
        self._restore_state()

    def _restore_state(self) -> None:
        """Restore from cache, with multiple staleness gates.

        Today's NESTLEIND incident (post-mortem in bot/lock.py) was caused by a
        stale paper:state snapshot whose ``avg_price`` had no relation to the
        current market price. The new gates protect against that:

        1. **Capital mismatch** — user changed ``capital.total`` in config.yaml.
        2. **Cross-session staleness** — snapshot is from a previous trading
           day. Paper trades are intraday — any open positions left over from
           a prior day are an artefact of a crash during square-off, not a
           real position.
        3. **Price divergence** — restored ``avg_price`` differs from the
           current market mark by more than ``_AVG_PRICE_TOLERANCE``. This is
           the smoking-gun guard that would have rejected the 13:19 phantom
           NESTLEIND-at-₹2400 restore.
        """
        cache = get_cache()
        snap = cache.get_json(self._state_key)
        if not snap:
            return

        # Gate 1: capital mismatch.
        saved_capital = snap.get("starting_capital")
        if saved_capital is not None and abs(saved_capital - self._starting_cash) > 0.5:
            logger.warning(
                "Paper broker [{}]: cached starting_capital ₹{:.2f} != configured "
                "capital.total ₹{:.2f}. Discarding stale snapshot and starting fresh.",
                self.segment.value, saved_capital, self._starting_cash,
            )
            cache.delete(self._state_key)
            return

        # Gate 2: cross-session staleness. Paper trades are intraday — any
        # cached position from a previous trading day is necessarily a crash
        # artefact (we *should* have squared-off at 15:15, but didn't).
        today = datetime.now(IST).date()
        saved_at_iso = snap.get("saved_at")
        if saved_at_iso:
            try:
                saved_date = datetime.fromisoformat(saved_at_iso).astimezone(IST).date()
            except (TypeError, ValueError):
                saved_date = today  # tolerate malformed timestamp
            if saved_date != today:
                logger.warning(
                    "Paper broker [{}]: snapshot is from {} (today is {}). "
                    "Discarding stale state — any positions are crash artefacts, "
                    "not live trades.", self.segment.value, saved_date, today,
                )
                cache.delete(self._state_key)
                return

        self._cash = snap.get("cash", self._cash)

        for sym, p in snap.get("positions", {}).items():
            avg = float(p["avg_price"])
            kind_str = p.get("instrument_kind", InstrumentKind.EQUITY.value)
            try:
                kind = InstrumentKind(kind_str)
            except ValueError:
                kind = InstrumentKind.EQUITY

            # Gate 3: price-divergence sanity check. EQUITY only — for
            # futures the divergence check would constantly false-positive
            # because the spot-vs-futures basis can be 0.5%+ even on a
            # single name, and the yfinance proxy returns spot rather than
            # the futures price. The cross-session check (Gate 2) is the
            # real safety net for futures anyway: any restore at all means
            # something got crash-stuck overnight.
            if kind == InstrumentKind.EQUITY:
                mark = self._safe_latest_mark(sym)
                if mark is not None and avg > 0:
                    ratio = abs(mark - avg) / avg
                    if ratio > _AVG_PRICE_TOLERANCE:
                        logger.error(
                            "[paper] REFUSING to restore {}: cached avg_price ₹{:.2f} "
                            "diverges {:.0%} from current mark ₹{:.2f}. This is the "
                            "kind of corruption that caused the 2026-04-29 phantom "
                            "trail-close. Deleting position from cache.",
                            sym, avg, ratio, mark,
                        )
                        continue

            self._positions[sym] = Position(
                symbol=p["symbol"], qty=p["qty"], avg_price=avg,
                side=OrderSide(p["side"]), stop_loss=p.get("stop_loss"),
                take_profit=p.get("take_profit"),
                # Restore the immutable original SL/TP if persisted; fall back
                # to the (possibly trailed) current value for legacy snapshots.
                initial_stop_loss=p.get("initial_stop_loss", p.get("stop_loss")),
                initial_take_profit=p.get("initial_take_profit", p.get("take_profit")),
                realized_pnl=p.get("realized_pnl", 0.0),
                instrument_kind=kind,
                lot_size=int(p.get("lot_size", 1)),
                margin_blocked=float(p.get("margin_blocked", 0.0)),
            )
        logger.info("Paper broker [{}] restored: cash=₹{:.2f}, {} open positions",
                    self.segment.value, self._cash, len(self._positions))

        # If we discarded any positions, immediately re-persist so the cache
        # reflects the cleaned state — otherwise the next restart re-reads
        # the corrupt snapshot.
        cached_count = len(snap.get("positions", {}))
        if cached_count != len(self._positions):
            self._persist()

    @staticmethod
    def _safe_latest_mark(symbol: str) -> Optional[float]:
        """Best-effort fetch of the current market price for sanity-checks.

        Imported lazily to avoid a circular import at module load
        (``bot.data`` pulls in the cache which pulls in this module).
        Any failure returns ``None`` — the divergence check is then skipped.
        """
        try:
            from ..data import latest_quote
            tick = latest_quote(symbol)
            return float(tick.ltp) if tick and tick.ltp else None
        except Exception as e:
            logger.debug("[paper] safe_latest_mark({}) failed: {}", symbol, e)
            return None

    def _persist(self) -> None:
        cache = get_cache()
        snap = {
            "starting_capital": self._starting_cash,
            "cash": self._cash,
            "saved_at": datetime.now(IST).isoformat(),
            "positions": {
                s: {**p.__dict__, "side": p.side.value, "opened_at": p.opened_at.isoformat()}
                for s, p in self._positions.items()
            },
        }
        cache.set_json(self._state_key, snap)

    def _slippage(self, side: OrderSide, price: float) -> float:
        bps = self._slip_bps / 10000.0
        return price * (1 + bps) if side == OrderSide.BUY else price * (1 - bps)

    def place_order(self, order: Order) -> Order:
        if order.qty <= 0:
            order.status = OrderStatus.REJECTED
            return order
        ref = order.price or self._marks.get(order.symbol)
        if not ref:
            logger.warning("[paper] no reference price for {}, rejecting", order.symbol)
            order.status = OrderStatus.REJECTED
            return order

        # If we already have a position on this symbol, F&O orders inherit
        # its instrument kind / lot size — the strategy emits a plain "BUY"
        # signal at SL/TP without re-tagging the order, and we must close the
        # position with the same fee schedule it was opened on.
        existing = self._positions.get(order.symbol)
        if existing is not None and existing.instrument_kind != InstrumentKind.EQUITY:
            order.instrument_kind = existing.instrument_kind
            order.lot_size = existing.lot_size

        # ── Equity over-sell guard (FIX #13b, refined 2026-05-05) ───────
        #
        # The 2026-05-04 ADANIENT phantom-short incident: a duplicate
        # `square_off_all` call sent a SECOND SELL of the same qty while
        # `existing` was already None (just popped by the first close).
        # The else-branch (line ~470) treated the orphan SELL as a NEW
        # SHORT entry and only debited fees, creating a phantom −10
        # position. The auto-cover BUY then leaked ~₹25k via the
        # equity-short-close cash formula.
        #
        # The original guard (2026-05-04) blocked ALL equity SELL on a
        # flat book — which on 2026-05-05 was found to also reject every
        # *legitimate* fresh strategy short (intraday MIS shorting is
        # explicitly supported by every Indian broker, and the equity
        # ensemble emits SELL entries via MTF(ORB) etc.). That blocked
        # the bot from trading for the entire morning of 2026-05-05.
        #
        # The refined guard fires in exactly two cases:
        #   1.  ``is_squareoff`` orphan: the order originates from
        #       ``square_off_all`` AND there is no held position. This is
        #       the precise 2026-05-04 race-condition signature — a
        #       second square-off looking at a stale snapshot. A fresh
        #       short here would be a bug, not a strategy decision.
        #   2.  Over-sell of held long: existing is a long position and
        #       order.qty exceeds existing.qty. Closing more than we
        #       hold would leak cash via the residual phantom short.
        #
        # Strategy-driven entries (``is_squareoff=False`` on a flat book)
        # are now allowed through, restoring intraday short capability.
        # The 2026-05-04 leak path remains closed because the duplicate
        # ``square_off_all`` is what carried ``is_squareoff=True``.
        is_equity_kind = (order.instrument_kind == InstrumentKind.EQUITY)
        if is_equity_kind and order.side == OrderSide.SELL:
            held_long_qty = existing.qty if (existing is not None
                                              and existing.side == OrderSide.BUY
                                              and existing.qty > 0) else 0
            if order.is_squareoff and existing is None:
                order.status = OrderStatus.REJECTED
                logger.warning(
                    "[paper] over-sell guard: REJECTED square-off SELL {} "
                    "qty={} on flat book (held long=0). This is the exact "
                    "2026-05-04 phantom-short signature — a duplicate "
                    "square_off_all working from a stale positions snapshot.",
                    order.symbol, order.qty,
                )
                return order
            if held_long_qty > 0 and order.qty > held_long_qty:
                order.status = OrderStatus.REJECTED
                logger.warning(
                    "[paper] over-sell guard: REJECTED SELL {} qty={} > "
                    "held long qty={}. Closing more than held would leak "
                    "cash via a residual phantom short.",
                    order.symbol, order.qty, held_long_qty,
                )
                return order

        fill = self._slippage(order.side, ref)
        fees = _fees(order.side, order.qty, fill, kind=order.instrument_kind)
        signed_qty = order.qty if order.side == OrderSide.BUY else -order.qty
        is_futures = order.instrument_kind == InstrumentKind.FUTURES
        is_option = order.instrument_kind == InstrumentKind.OPTION
        is_spread = order.instrument_kind == InstrumentKind.SPREAD
        is_iron_condor = order.instrument_kind == InstrumentKind.IRON_CONDOR

        if existing is None:
            # ── ENTRY ────────────────────────────────────────────────────
            # Equity:  full notional debit on BUY, zero on SELL (short).
            # Futures: margin = qty × fill × margin_pct on BOTH sides.
            # Option BUY (Phase 3): full premium debit (limited risk; max
            #                       loss is the entire premium paid).
            # Option SELL (Phase 4): margin required (unlimited risk) —
            #                       NOT supported in paper broker yet (4.5).
            # Spread SELL (Phase 4): credit collected upfront + margin =
            #                       max_loss (defined-risk).
            # Spread BUY: same shape as a debit spread or a close — net
            #             debit cash flow with margin = 0.
            if is_iron_condor:
                # Iron condor is always sold for a net credit (4 legs:
                # short put + long-OTM put + short call + long-OTM call).
                if order.side != OrderSide.SELL:
                    order.status = OrderStatus.REJECTED
                    logger.warning(
                        "[paper:ic] rejected: opening an IRON_CONDOR requires "
                        "side=SELL (credit collection); got side={}",
                        order.side.value,
                    )
                    return order
                from ..instruments.fno import parse_iron_condor_tradingsymbol
                meta = parse_iron_condor_tradingsymbol(order.symbol)
                if meta is None:
                    order.status = OrderStatus.REJECTED
                    logger.warning(
                        "[paper:ic] rejected: cannot parse iron-condor "
                        "tradingsymbol {}", order.symbol,
                    )
                    return order
                # IC max-loss/share = max(put_width, call_width) − net_credit.
                # Spot can hit AT MOST one wing → margin is the *worse* wing,
                # not the sum of both spread maxes (this is what makes the
                # iron condor ~50% more capital-efficient than two separate
                # vertical credit spreads on the same underlying).
                worst_wing = max(meta["put_width"], meta["call_width"])
                if fill <= 0:
                    order.status = OrderStatus.REJECTED
                    logger.warning(
                        "[paper:ic] rejected: net credit must be > 0, got ₹{:.2f}",
                        fill,
                    )
                    return order
                if fill >= worst_wing:
                    order.status = OrderStatus.REJECTED
                    logger.warning(
                        "[paper:ic] rejected: net credit ₹{:.2f} ≥ worst "
                        "wing width {} pts — impossible (free money). "
                        "Refusing to size {}.", fill, worst_wing, order.symbol,
                    )
                    return order
                max_loss_per_share = worst_wing - fill
                margin_blocked = max_loss_per_share * order.qty
                credit_received = fill * order.qty
                cash_delta = credit_received - margin_blocked - fees
                if -cash_delta > self._cash:
                    order.status = OrderStatus.REJECTED
                    logger.warning(
                        "[paper:ic] rejected: insufficient cash for {} — "
                        "need ₹{:.2f} margin (worst wing={} pts, "
                        "max_loss/share=₹{:.2f}) − ₹{:.2f} credit + "
                        "₹{:.2f} fees, have ₹{:.2f}",
                        order.symbol, margin_blocked, worst_wing,
                        max_loss_per_share, credit_received, fees, self._cash,
                    )
                    return order
                self._cash += cash_delta
                self._positions[order.symbol] = Position(
                    symbol=order.symbol, qty=signed_qty,
                    avg_price=fill,
                    side=order.side,
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    initial_stop_loss=order.stop_loss,
                    initial_take_profit=order.take_profit,
                    instrument_kind=InstrumentKind.IRON_CONDOR,
                    lot_size=order.lot_size,
                    margin_blocked=margin_blocked,
                )
            elif is_spread:
                # Credit spread is opened with side=SELL (we receive net
                # credit). Debit-spread opens are not supported here —
                # the resolver only emits credit spreads.
                if order.side != OrderSide.SELL:
                    order.status = OrderStatus.REJECTED
                    logger.warning(
                        "[paper:spread] rejected: opening a SPREAD requires "
                        "side=SELL (credit collection); got side={}",
                        order.side.value,
                    )
                    return order
                from ..instruments.fno import parse_spread_tradingsymbol
                from ..options.margin import vertical_spread_max_loss
                meta = parse_spread_tradingsymbol(order.symbol)
                if meta is None:
                    order.status = OrderStatus.REJECTED
                    logger.warning(
                        "[paper:spread] rejected: cannot parse spread tradingsymbol {}",
                        order.symbol,
                    )
                    return order
                # margin_blocked = max_loss × qty. fill is the net credit
                # per share (positive); width − net_credit = max_loss/share.
                try:
                    max_loss_per_share = vertical_spread_max_loss(
                        meta["short_strike"], meta["long_strike"], fill,
                    )
                except ValueError as e:
                    order.status = OrderStatus.REJECTED
                    logger.warning("[paper:spread] rejected: {}", e)
                    return order
                margin_blocked = max_loss_per_share * order.qty
                # Cash flow at entry:
                #   + credit_received (premium × qty)
                #   − margin_blocked
                #   − fees
                credit_received = fill * order.qty
                cash_delta = credit_received - margin_blocked - fees
                if -cash_delta > self._cash:
                    # Margin requirement exceeds available cash even after
                    # netting the credit. Reject loudly.
                    order.status = OrderStatus.REJECTED
                    logger.warning(
                        "[paper:spread] rejected: insufficient cash for {} — "
                        "need ₹{:.2f} margin (max_loss/share=₹{:.2f}) − "
                        "₹{:.2f} credit + ₹{:.2f} fees, have ₹{:.2f}",
                        order.symbol, margin_blocked, max_loss_per_share,
                        credit_received, fees, self._cash,
                    )
                    return order
                self._cash += cash_delta
                self._positions[order.symbol] = Position(
                    symbol=order.symbol, qty=signed_qty,        # qty < 0 (short)
                    avg_price=fill,                              # net credit/share
                    side=order.side,                             # SELL
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    initial_stop_loss=order.stop_loss,
                    initial_take_profit=order.take_profit,
                    instrument_kind=InstrumentKind.SPREAD,
                    lot_size=order.lot_size,
                    margin_blocked=margin_blocked,
                )
            elif is_option:
                if order.side == OrderSide.SELL:
                    # We can't OPEN a short option without a margin model,
                    # which is Phase 4. Allow SELL only if there's an
                    # existing LONG to close (handled by the `existing is
                    # not None` branch below — we'd never reach this).
                    order.status = OrderStatus.REJECTED
                    logger.warning(
                        "[paper:options] rejected: opening a SHORT option "
                        "({} {}) needs margin support, which is Phase 4. "
                        "Use option BUYING (long CE / long PE) for now.",
                        order.symbol, order.side.value,
                    )
                    return order
                # Long option BUY: cash debit = premium × qty + fees.
                premium_cost = fill * order.qty
                cash_debit = premium_cost + fees
                if cash_debit > self._cash:
                    order.status = OrderStatus.REJECTED
                    logger.warning(
                        "[paper:options] rejected: insufficient cash for {} "
                        "— need ₹{:.2f} premium + ₹{:.2f} fees, have ₹{:.2f}",
                        order.symbol, premium_cost, fees, self._cash,
                    )
                    return order
                self._cash -= cash_debit
                self._positions[order.symbol] = Position(
                    symbol=order.symbol, qty=signed_qty, avg_price=fill,
                    side=order.side,
                    stop_loss=order.stop_loss, take_profit=order.take_profit,
                    initial_stop_loss=order.stop_loss,
                    initial_take_profit=order.take_profit,
                    instrument_kind=InstrumentKind.OPTION,
                    lot_size=order.lot_size,
                    margin_blocked=0.0,           # long options: no margin
                )
            elif is_futures:
                from ..instruments.fno import margin_pct, underlying_from_tradingsymbol
                m_pct = margin_pct(underlying_from_tradingsymbol(order.symbol))
                margin_blocked = fill * order.qty * m_pct
                cash_debit = margin_blocked + fees
                if cash_debit > self._cash:
                    order.status = OrderStatus.REJECTED
                    logger.warning(
                        "[paper:fno] rejected: insufficient cash for {} — "
                        "need ₹{:.2f} margin (₹{:.2f}/lot @ {:.0f}%) + ₹{:.2f} fees, "
                        "have ₹{:.2f}",
                        order.symbol, margin_blocked,
                        margin_blocked / max(order.qty // max(order.lot_size, 1), 1),
                        m_pct * 100, fees, self._cash,
                    )
                    return order
                self._cash -= cash_debit
                self._positions[order.symbol] = Position(
                    symbol=order.symbol, qty=signed_qty, avg_price=fill,
                    side=order.side,
                    stop_loss=order.stop_loss, take_profit=order.take_profit,
                    initial_stop_loss=order.stop_loss,
                    initial_take_profit=order.take_profit,
                    instrument_kind=InstrumentKind.FUTURES,
                    lot_size=order.lot_size,
                    margin_blocked=margin_blocked,
                )
            else:
                cost = fill * order.qty
                if order.side == OrderSide.BUY and cost + fees > self._cash:
                    order.status = OrderStatus.REJECTED
                    logger.warning("[paper] rejected: insufficient cash for {}", order.symbol)
                    return order
                self._cash -= (cost if order.side == OrderSide.BUY else 0) + fees
                self._positions[order.symbol] = Position(
                    symbol=order.symbol, qty=signed_qty, avg_price=fill,
                    side=order.side,
                    stop_loss=order.stop_loss, take_profit=order.take_profit,
                    initial_stop_loss=order.stop_loss, initial_take_profit=order.take_profit,
                )
        else:
            new_qty = existing.qty + signed_qty
            if new_qty == 0 or (existing.qty * new_qty < 0):
                # ── CLOSE / FLIP ─────────────────────────────────────────
                close_qty = min(abs(existing.qty), abs(signed_qty))
                if existing.side == OrderSide.BUY:
                    pnl = (fill - existing.avg_price) * close_qty
                else:
                    pnl = (existing.avg_price - fill) * close_qty
                pnl -= fees
                existing.realized_pnl += pnl

                if is_futures:
                    # Refund margin proportional to qty closed; carry the rest
                    # forward to the (possibly nonzero) remainder.
                    refund_ratio = close_qty / abs(existing.qty)
                    margin_refund = existing.margin_blocked * refund_ratio
                    self._cash += margin_refund + pnl
                    existing.margin_blocked -= margin_refund
                elif is_spread or is_iron_condor:
                    # Spread / iron-condor close (BUY to close a SELL):
                    #   + margin_refund   (margin freed proportional to qty)
                    #   − close_premium × close_qty   (buy back at current net price)
                    #   − fees             (close-leg fees, with the
                    #                        appropriate per-leg surcharge)
                    refund_ratio = close_qty / abs(existing.qty)
                    margin_refund = existing.margin_blocked * refund_ratio
                    self._cash += margin_refund - fill * close_qty - fees
                    existing.margin_blocked -= margin_refund
                elif is_option:
                    # Long option close: refund the entry premium that was
                    # debited at open + add the realized P&L (including
                    # close fees that were subtracted into ``pnl`` above).
                    # Phase 3 doesn't open short options, so existing.side
                    # is always BUY here.
                    self._cash += existing.avg_price * close_qty + pnl
                else:
                    # Equity legacy branch — preserved verbatim from the
                    # pre-Phase-2 code so behaviour is unchanged.
                    self._cash += existing.avg_price * close_qty + pnl if existing.side == OrderSide.BUY else \
                                  -existing.avg_price * close_qty + close_qty * (existing.avg_price - fill) - fees

                if new_qty == 0:
                    self._positions.pop(order.symbol)
                    seg_tag = (
                        "fno" if is_futures
                        else "ic" if is_iron_condor
                        else "spread" if is_spread
                        else "options" if is_option
                        else "paper"
                    )
                    logger.info("[{}] CLOSED {} qty={} P&L=₹{:.2f}",
                                seg_tag, order.symbol, close_qty, pnl)
                else:
                    # Flipped sides (extremely rare for futures Phase 2,
                    # but the equity flow supports it).
                    self._positions[order.symbol] = Position(
                        symbol=order.symbol, qty=new_qty, avg_price=fill,
                        side=order.side, stop_loss=order.stop_loss,
                        take_profit=order.take_profit,
                        initial_stop_loss=order.stop_loss,
                        initial_take_profit=order.take_profit,
                        realized_pnl=existing.realized_pnl,
                        instrument_kind=existing.instrument_kind,
                        lot_size=existing.lot_size,
                        margin_blocked=0.0,  # remainder is fresh, no carryover
                    )
            else:
                # ── ADD-TO-POSITION ──────────────────────────────────────
                tot_qty = existing.qty + signed_qty
                avg = (existing.avg_price * abs(existing.qty) + fill * order.qty) / abs(tot_qty)
                existing.qty = tot_qty
                existing.avg_price = avg
                if is_futures:
                    from ..instruments.fno import margin_pct, underlying_from_tradingsymbol
                    m_pct = margin_pct(underlying_from_tradingsymbol(order.symbol))
                    extra_margin = fill * order.qty * m_pct
                    self._cash -= extra_margin + fees
                    existing.margin_blocked += extra_margin
                elif is_option:
                    # Adding to an existing long option: full premium debit
                    # for the new lot + fees. No margin involved.
                    self._cash -= fill * order.qty + fees
                else:
                    self._cash -= (fill * order.qty if order.side == OrderSide.BUY else 0) + fees

        order.status = OrderStatus.FILLED
        order.fill_price = fill
        order.fees = fees
        self._orders.append(order)
        self._persist()
        seg_tag = (
            "fno" if is_futures
            else "ic" if is_iron_condor
            else "spread" if is_spread
            else "options" if is_option
            else "paper"
        )
        logger.info("[{}] FILLED {} {} {}@₹{:.2f} fees=₹{:.2f}",
                    seg_tag, order.side.value, order.qty, order.symbol, fill, fees)
        return order

    def cancel_order(self, order_id: str) -> bool:
        return False  # Paper broker fills immediately, nothing to cancel.

    def positions(self) -> List[Position]:
        for sym, p in self._positions.items():
            mark = self._marks.get(sym, p.avg_price)
            if p.side == OrderSide.BUY:
                p.unrealized_pnl = (mark - p.avg_price) * abs(p.qty)
            else:
                p.unrealized_pnl = (p.avg_price - mark) * abs(p.qty)
        return list(self._positions.values())

    def cash(self) -> float:
        return self._cash

    def update_marks(self, marks: dict[str, float]) -> None:
        self._marks.update(marks)

    def square_off_all(self) -> List[Order]:
        out = []
        for sym, pos in list(self._positions.items()):
            mark = self._marks.get(sym, pos.avg_price)
            opp = OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY
            o = Order(
                id=str(uuid.uuid4()), symbol=sym, side=opp,
                qty=abs(pos.qty), type=OrderType.MARKET, price=mark,
                instrument_kind=pos.instrument_kind,
                lot_size=pos.lot_size,
                is_squareoff=True,
            )
            out.append(self.place_order(o))
        return out

    def total_pnl(self) -> float:
        realized = sum(p.realized_pnl for p in self._positions.values())
        unrealized = sum(p.unrealized_pnl for p in self.positions())
        return realized + unrealized
