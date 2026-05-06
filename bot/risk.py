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


class RiskManager:
    def __init__(self, broker: Broker, segment: Segment = Segment.EQUITY) -> None:
        self.broker = broker
        self.segment = segment
        self.cfg = load_config()
        # Segment-aware capital + risk caps. Equity reads top-level
        # ``capital`` / ``risk``; F&O reads ``fno.capital`` / ``fno.risk``.
        self._capital_cfg = cfg_capital(self.cfg, segment)
        self._risk_cfg = cfg_risk(self.cfg, segment)
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
            logger.info(
                "[risk:{}] restored starting_equity=₹{:,.2f} for {}",
                segment.value, self._starting_equity, self._day,
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
            self._starting_equity = self._equity()
            self._persist_starting_equity()
            return 0.0
        eq = self._equity()
        return (eq - self._starting_equity) / max(self._starting_equity, 1.0) * 100

    def _kill_switch_active(self) -> bool:
        return KILL_SWITCH.exists()

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

        return RiskDecision(True, qty, f"approved qty={qty} (risk=₹{max_loss:.0f}, sl={risk_per_share:.2f}/sh)")

    def record_trade(self) -> None:
        self._trades_today += 1
        if self._starting_equity is None:
            self._starting_equity = self._equity()
            self._persist_starting_equity()

    def should_square_off(self, now_time) -> bool:
        return now_time >= self.cfg.session.t("square_off")

    def in_trading_window(self, now_time) -> bool:
        return self.cfg.session.t("trade_start") <= now_time <= self.cfg.session.t("trade_cutoff")

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
            self._starting_equity = self._equity()
            self._persist_starting_equity()
        return self._daily_pnl_pct()
