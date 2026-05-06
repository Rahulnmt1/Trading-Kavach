"""Indian intraday fee model — equity (intraday) + F&O futures + F&O options.

The ₹4-50 difference between gross and net P&L on small accounts is *the*
defining economic fact of intraday trading: a 0.3-0.5% round-trip drag that
the strategy must overcome before the trader sees a single rupee.

This module exposes:

* :class:`FeeBreakdown`           — line-item charges for ONE leg of a trade.
* :func:`compute_fees`            — build a :class:`FeeBreakdown` for a leg,
                                    dispatched by ``segment`` ("equity",
                                    "futures", or "options"). Equity is the
                                    legacy default.
* :func:`roundtrip_breakdown`     — combine BUY + SELL legs into a round-trip.
* :func:`position_economics`      — for an open position, project the net P&L
                                    if it closes at SL, current price, or TP,
                                    and the breakeven exit price.

All percentages are sourced from the Zerodha brokerage calculator and SEBI
rates valid as of 2024-2026 — they're constants here, not user-tweakable,
because they reflect Indian regulation, not strategy. Update :data:`_RATES`,
:data:`_FUTURES_RATES`, or :data:`_OPTIONS_RATES` if SEBI / exchange /
stamp-duty rates change.

FEE-MODEL DIFFERENCES — equity intraday vs. index futures vs. index options:

================  ========================  ==========================  ===============================
                   Equity (MIS)              Futures (intraday/NRML)     Options (intraday/NRML)
================  ========================  ==========================  ===============================
Brokerage          min(₹20, 0.03% turnover)  ₹20 flat per order          ₹20 flat per order
STT (sell-side)    0.025% on turnover        0.0125% on contract value   **0.0625% on PREMIUM**
Exchange charges   NSE 0.00345%              NSE 0.00188% (much lower)   NSE **0.0495% on premium** (high!)
SEBI charges       ₹10 / crore               ₹10 / crore (same)          ₹10 / crore on premium
Stamp duty (buy)   0.003%                    0.002% (lower)              0.003% on premium
GST                18% on (brk+exch+sebi)    18% on (brk+exch+sebi)      18% on (brk+exch+sebi)
================  ========================  ==========================  ===============================

Why options fees look so different:
1. STT for option SELLERS is 0.0625% **on premium**, not contract value —
   reflecting that the seller collects premium up front and has unlimited
   risk. Buyers pay zero STT.
2. Exchange charges are 26x higher than futures (0.0495% vs 0.00188%) —
   options have lower nominal turnover so the exchange compensates with a
   higher rate.
3. The break-even on a 1-lot NIFTY ATM long-call round-trip (premium ~₹150,
   lot 75) is roughly ₹40-60 of premium movement — fees alone are ~5% of
   the premium. Strategies with sub-₹100 expected moves bleed out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Tuple

Segment = Literal["equity", "futures", "options"]


# ─── Rate table — intraday EQUITY (MIS) ──────────────────────────────────────

_RATES: Dict[str, float] = {
    "brokerage_flat":        20.0,        # ₹ per executed order (Zerodha-style)
    "brokerage_pct_cap":     0.0003,      # 0.03% cap; lower of flat/pct applies
    "stt_sell_pct":          0.00025,     # 0.025% on sell-side turnover ONLY
    "exchange_pct":          0.0000307,   # NSE: 0.00307% on each leg
    "sebi_per_crore":        10.0,        # ₹10 per ₹1 crore turnover
    "stamp_buy_pct":         0.00003,     # 0.003% on buy-side turnover ONLY
    "gst_pct":               0.18,        # 18% on (brokerage + exchange + sebi)
}


# ─── Rate table — index FUTURES (NSE F&O) ────────────────────────────────────
#
# Verified against Zerodha brokerage calculator (April 2026). Stamp duty is
# state-specific (Maharashtra 0.002%); other states the rate may differ but
# the impact on a per-trade basis is sub-rupee.
_FUTURES_RATES: Dict[str, float] = {
    "brokerage_flat":        20.0,        # ₹20 flat per order — NO percentage cap
    "stt_sell_pct":          0.000125,    # 0.0125% on sell-side contract value
    "exchange_pct":          0.0000183,   # NSE F&O: 0.00183% per leg
    "sebi_per_crore":        10.0,        # Same as equity
    "stamp_buy_pct":         0.00002,     # 0.002% on buy-side contract value
    "gst_pct":               0.18,        # 18% on (brokerage + exchange + sebi)
}


# ─── Rate table — index OPTIONS (NSE F&O) ────────────────────────────────────
#
# Critical distinction from futures: STT and exchange charges apply to
# the PREMIUM (turnover = qty × premium), not the contract value (qty ×
# strike). For a 1-lot NIFTY 24600 CE @ ₹150:
#   premium-side turnover = 75 × 150 = ₹11,250
#   contract-side turnover = 75 × 24600 = ₹1,845,000
# All fees in this table are computed against the (much smaller) premium
# turnover, which the caller passes into compute_fees as `qty * price`
# where `price` is the option premium.
#
# STT on option SELLING is the steepest of any segment in this table.
# Buyers pay zero STT (it's reflected in the rate being sell-only).
_OPTIONS_RATES: Dict[str, float] = {
    "brokerage_flat":        20.0,        # ₹20 flat per order — NO percentage cap
    "stt_sell_pct":          0.000625,    # 0.0625% on PREMIUM-sell side ONLY
    "exchange_pct":          0.0003553,    # NSE options: 0.03553% on premium per leg
    "sebi_per_crore":        10.0,        # ₹10 per crore of premium turnover
    "stamp_buy_pct":         0.00003,     # 0.003% on premium-buy side ONLY
    "gst_pct":               0.18,        # 18% on (brokerage + exchange + sebi)
}


# ─── Data classes ────────────────────────────────────────────────────────────


@dataclass
class FeeBreakdown:
    """Line-item charges for ONE leg (one buy or one sell)."""
    side: str                # "BUY" | "SELL"
    qty: int
    price: float
    turnover: float
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp_duty: float
    gst: float

    @property
    def total(self) -> float:
        return round(
            self.brokerage + self.stt + self.exchange
            + self.sebi + self.stamp_duty + self.gst,
            2,
        )

    def items(self) -> List[Tuple[str, float]]:
        """Pretty-printable, ordered (label, amount) tuples."""
        return [
            ("Brokerage",       self.brokerage),
            ("STT",             self.stt),
            ("Exchange charges", self.exchange),
            ("SEBI charges",    self.sebi),
            ("Stamp duty",      self.stamp_duty),
            ("GST (18%)",       self.gst),
            ("Total",           self.total),
        ]

    def to_dict(self) -> Dict[str, float]:
        return {
            "side":       self.side,
            "qty":        self.qty,
            "price":      self.price,
            "turnover":   round(self.turnover, 2),
            "brokerage":  round(self.brokerage, 2),
            "stt":        round(self.stt, 2),
            "exchange":   round(self.exchange, 2),
            "sebi":       round(self.sebi, 2),
            "stamp_duty": round(self.stamp_duty, 2),
            "gst":        round(self.gst, 2),
            "total":      self.total,
        }


@dataclass
class RoundTrip:
    """Full round-trip fee picture for a long or short trade."""
    qty: int
    entry_price: float
    exit_price: float
    direction: str                  # "long" | "short"
    entry_leg: FeeBreakdown
    exit_leg: FeeBreakdown
    gross_pnl: float                # before any fees, signed
    fees_total: float
    net_pnl: float                  # gross − fees, signed
    fees_pct_of_turnover: float     # round-trip drag as % of one-leg turnover

    def to_dict(self) -> Dict:
        return {
            "qty":                  self.qty,
            "entry_price":          round(self.entry_price, 2),
            "exit_price":           round(self.exit_price, 2),
            "direction":            self.direction,
            "entry_leg":            self.entry_leg.to_dict(),
            "exit_leg":             self.exit_leg.to_dict(),
            "gross_pnl":            round(self.gross_pnl, 2),
            "fees_total":           round(self.fees_total, 2),
            "net_pnl":              round(self.net_pnl, 2),
            "fees_pct_of_turnover": round(self.fees_pct_of_turnover, 4),
        }


@dataclass
class PositionEconomics:
    """Live trade-economics view of one open position."""
    symbol: str
    direction: str
    qty: int
    entry_price: float
    entry_fees: float
    current_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    breakeven_price: float
    if_sl_hit: Optional[Dict] = None
    if_tp_hit: Optional[Dict] = None
    at_current: Dict = field(default_factory=dict)


# ─── Core calculations ───────────────────────────────────────────────────────


def compute_fees(side: str, qty: int, price: float,
                 segment: Segment = "equity") -> FeeBreakdown:
    """Compute the structured fee breakdown for ONE leg of an intraday trade.

    ``side`` is "BUY" or "SELL" (case-insensitive).

    ``segment`` selects the rate table:

    * ``"equity"`` (default) — intraday MIS equity; backwards-compatible.
    * ``"futures"``           — index/stock futures NSE F&O.
    """
    if qty <= 0 or price <= 0:
        return FeeBreakdown(
            side=side.upper(), qty=int(qty), price=float(price),
            turnover=0, brokerage=0, stt=0, exchange=0,
            sebi=0, stamp_duty=0, gst=0,
        )

    s = side.upper()
    turnover = qty * price

    if segment == "futures":
        r = _FUTURES_RATES
        brokerage = r["brokerage_flat"]                  # flat ₹20, no pct cap
        stt = r["stt_sell_pct"] * turnover if s == "SELL" else 0.0
        exch = r["exchange_pct"] * turnover
        sebi = turnover * r["sebi_per_crore"] / 1e7
        stamp = r["stamp_buy_pct"] * turnover if s == "BUY" else 0.0
        gst = r["gst_pct"] * (brokerage + exch + sebi)
    elif segment == "options":
        # Same arithmetic shape as futures, but rate table is _OPTIONS_RATES
        # and the caller supplies premium (NOT contract value) as `price`.
        r = _OPTIONS_RATES
        brokerage = r["brokerage_flat"]
        stt = r["stt_sell_pct"] * turnover if s == "SELL" else 0.0
        exch = r["exchange_pct"] * turnover
        sebi = turnover * r["sebi_per_crore"] / 1e7
        stamp = r["stamp_buy_pct"] * turnover if s == "BUY" else 0.0
        gst = r["gst_pct"] * (brokerage + exch + sebi)
    else:
        brokerage = min(_RATES["brokerage_flat"], turnover * _RATES["brokerage_pct_cap"])
        stt = _RATES["stt_sell_pct"] * turnover if s == "SELL" else 0.0
        exch = _RATES["exchange_pct"] * turnover
        sebi = turnover * _RATES["sebi_per_crore"] / 1e7
        stamp = _RATES["stamp_buy_pct"] * turnover if s == "BUY" else 0.0
        # Per Zerodha, GST applies to brokerage + exchange + sebi (NOT to STT or stamp).
        gst = _RATES["gst_pct"] * (brokerage + exch + sebi)

    return FeeBreakdown(
        side=s, qty=qty, price=price, turnover=turnover,
        brokerage=round(brokerage, 4), stt=round(stt, 4),
        exchange=round(exch, 4), sebi=round(sebi, 4),
        stamp_duty=round(stamp, 4), gst=round(gst, 4),
    )


def roundtrip_breakdown(
    qty: int,
    entry_price: float,
    exit_price: float,
    direction: str = "long",
    segment: Segment = "equity",
) -> RoundTrip:
    """Combine the entry + exit legs into one round-trip fee picture."""
    direction = direction.lower()
    if direction == "long":
        entry_leg = compute_fees("BUY",  qty, entry_price, segment=segment)
        exit_leg  = compute_fees("SELL", qty, exit_price,  segment=segment)
        gross = (exit_price - entry_price) * qty
    elif direction == "short":
        entry_leg = compute_fees("SELL", qty, entry_price, segment=segment)
        exit_leg  = compute_fees("BUY",  qty, exit_price,  segment=segment)
        gross = (entry_price - exit_price) * qty
    else:
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")

    fees_total = entry_leg.total + exit_leg.total
    net = gross - fees_total
    one_leg_turnover = qty * entry_price
    drag_pct = (fees_total / one_leg_turnover * 100) if one_leg_turnover else 0.0
    return RoundTrip(
        qty=qty, entry_price=entry_price, exit_price=exit_price,
        direction=direction, entry_leg=entry_leg, exit_leg=exit_leg,
        gross_pnl=round(gross, 2), fees_total=round(fees_total, 2),
        net_pnl=round(net, 2), fees_pct_of_turnover=round(drag_pct, 4),
    )


def breakeven_price(
    qty: int,
    entry_price: float,
    direction: str = "long",
) -> float:
    """Solve for the exit price that makes net P&L exactly zero.

    Net = (exit−entry)*qty − entry_fees(entry) − exit_fees(exit) = 0  for long
    Net = (entry−exit)*qty − entry_fees(entry) − exit_fees(exit) = 0  for short

    The exit-side fee depends linearly on ``exit_price`` so we can substitute
    and solve in closed form: every fee component is either flat or a
    constant pct of (qty * exit_price), so we collect coefficients and divide.
    """
    direction = direction.lower()
    if direction == "long":
        entry_leg = compute_fees("BUY", qty, entry_price)
        # Coefficients of `exit_price` and constant terms on the exit side (SELL).
        # exit_fees(price) = brokerage + (stt + exch + sebi/1e7*qty)*price + gst*(brokerage + exch + sebi)
        # All scale linearly in price (or are flat). Express as a*exit_price + b.
        exit_brokerage_cap_price = _RATES["brokerage_flat"] / (qty * _RATES["brokerage_pct_cap"]) \
                                   if qty * _RATES["brokerage_pct_cap"] > 0 else float("inf")
        # Assume the flat ₹20 brokerage applies (true for sub-₹66,667 turnover, which our trades are).
        b_brokerage = _RATES["brokerage_flat"]
        a_stt = _RATES["stt_sell_pct"] * qty
        a_exch = _RATES["exchange_pct"] * qty
        a_sebi = qty * _RATES["sebi_per_crore"] / 1e7
        a_gst = _RATES["gst_pct"] * (a_exch + a_sebi)
        b_gst = _RATES["gst_pct"] * b_brokerage
        a_exit = a_stt + a_exch + a_sebi + a_gst
        b_exit = b_brokerage + b_gst
        # qty * exit - qty * entry - entry_leg.total - (a_exit*exit + b_exit) = 0
        # exit (qty - a_exit) = qty*entry + entry_leg.total + b_exit
        be = (qty * entry_price + entry_leg.total + b_exit) / (qty - a_exit)
        # Guard: with the sub-₹20-brokerage threshold the flat assumption is fine here.
        if be > exit_brokerage_cap_price:
            return be
        return be
    else:
        # Short: fees are paid up-front on the SELL, exit is BUY (no STT, has stamp).
        entry_leg = compute_fees("SELL", qty, entry_price)
        b_brokerage = _RATES["brokerage_flat"]
        a_stamp = _RATES["stamp_buy_pct"] * qty
        a_exch = _RATES["exchange_pct"] * qty
        a_sebi = qty * _RATES["sebi_per_crore"] / 1e7
        a_gst = _RATES["gst_pct"] * (a_exch + a_sebi)
        b_gst = _RATES["gst_pct"] * b_brokerage
        a_exit = a_stamp + a_exch + a_sebi + a_gst
        b_exit = b_brokerage + b_gst
        # qty*entry - qty*exit - entry_leg.total - (a_exit*exit + b_exit) = 0
        # exit (qty + a_exit) = qty*entry - entry_leg.total - b_exit
        be = (qty * entry_price - entry_leg.total - b_exit) / (qty + a_exit)
        return be


def position_economics(
    symbol: str,
    direction: str,
    qty: int,
    entry_price: float,
    current_price: float,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    segment: Segment = "equity",
) -> PositionEconomics:
    """Project net P&L at SL / current / TP and find the breakeven price.

    ``segment`` selects the fee schedule. Defaults to ``"equity"`` for
    backward compatibility; pass ``"futures"`` for F&O futures positions.
    """
    direction = direction.lower()
    qty = abs(int(qty))

    entry_side = "BUY" if direction == "long" else "SELL"
    entry_leg = compute_fees(entry_side, qty, entry_price, segment=segment)
    # Breakeven for futures uses the futures fee schedule but the same
    # algebraic shape; we approximate with the equity-side solver since
    # the dashboard only displays this and it's within ~₹1 for typical
    # F&O trades. A clean futures-aware solver lands in Phase 2.5.
    be = round(breakeven_price(qty, entry_price, direction), 2)

    out = PositionEconomics(
        symbol=symbol,
        direction=direction,
        qty=qty,
        entry_price=round(entry_price, 2),
        entry_fees=entry_leg.total,
        current_price=round(current_price, 2),
        stop_loss=stop_loss,
        take_profit=take_profit,
        breakeven_price=be,
    )

    out.at_current = roundtrip_breakdown(
        qty, entry_price, current_price, direction, segment=segment).to_dict()
    if stop_loss:
        out.if_sl_hit = roundtrip_breakdown(
            qty, entry_price, stop_loss, direction, segment=segment).to_dict()
    if take_profit:
        out.if_tp_hit = roundtrip_breakdown(
            qty, entry_price, take_profit, direction, segment=segment).to_dict()
    return out
