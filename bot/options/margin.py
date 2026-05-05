"""Margin model for paper-mode option selling (Phase 4).

Three margin regimes are surfaced here:

1. **Naked short option** — :func:`naked_short_margin` returns a flat
   15% of contract value (SPAN ~12% + exposure ~3% per Zerodha calc, ATM
   index option, intraday MIS). The premium received is NOT netted off
   here — the caller decides whether the working margin is gross or
   premium-adjusted.

   Real Indian SPAN is volatility-band based (NSE publishes SPAN files
   daily) and the net required margin for a NIFTY ATM short is ~₹2.5 L
   on contract value of ₹18.5 L (~13.5%). Our 15% flat is intentionally
   conservative for paper sims.

2. **Vertical spread** — :func:`vertical_spread_max_loss` returns the
   worst-case loss per share, which equals the strike-width minus the
   net credit received. Margin = max_loss × qty (defined-risk: cash is
   blocked at the worst possible outcome, not a SPAN-based estimate).

3. **Iron condor / butterfly** — Phase 4.5. Same defined-risk principle
   but the worst case is the wider of the two wings.

WHY DEFINED-RISK MARGIN MATTERS
================================

Naked NIFTY short (Phase 4.5+): margin ≈ ₹2.5 L per lot — needs ₹3 L+
capital to be feasible. Out of reach for ₹50K paper budget.

Vertical credit spread: margin ≈ ₹2-5 K per lot — fits ₹50K budget
comfortably. THIS is the strategy Phase 4 ships with.

The arithmetic for a NIFTY 24500/24400 bull-put-spread at +70 net credit
per share:
    width            = 24500 − 24400 = 100 ₹/share
    max_loss/share   = 100 − 70      = 30 ₹/share
    margin/lot       = 30 × 75       = ₹2,250
"""
from __future__ import annotations

from typing import Literal

OptionType = Literal["CE", "PE"]

# SPAN+exposure margin pct for naked index option SHORTS — paper-mode
# heuristic. Used by Phase 4.5 (naked theta plays); Phase 4 vertical
# credit spreads compute margin from defined max-loss instead.
NAKED_SHORT_MARGIN_PCT = 0.15


def naked_short_margin(spot: float, qty: int) -> float:
    """Approximate SPAN+exposure margin for a naked short option.

    Args:
      spot: current underlying spot price (used as a proxy for contract
        value when scaling margin — option strike could be used too,
        but spot-based is what Zerodha's intraday calculator does for
        rough ATM strikes).
      qty: number of shares (i.e. ``lot_size × num_lots``) being sold short.
    """
    return spot * qty * NAKED_SHORT_MARGIN_PCT


def vertical_spread_max_loss(short_strike: int, long_strike: int,
                             net_credit_per_share: float) -> float:
    """Worst-case loss PER SHARE on a vertical credit spread.

    For a credit spread:
        max_loss = abs(K_long − K_short) − net_credit_received

    For a debit spread (Phase 5+) it would be the net debit paid; this
    function deliberately rejects the debit case to surface the bug
    (a paper trader configuring a debit spread with this helper has
    misunderstood the structure).
    """
    if net_credit_per_share <= 0:
        raise ValueError(
            f"vertical_spread_max_loss expects a CREDIT spread "
            f"(net credit > 0), got net_credit={net_credit_per_share}"
        )
    width = abs(long_strike - short_strike)
    max_loss = width - net_credit_per_share
    if max_loss <= 0:
        # Net credit > width is theoretically free money; in practice
        # it indicates a bad fill or stale quote. Reject loudly.
        raise ValueError(
            f"vertical_spread_max_loss: net_credit={net_credit_per_share} "
            f">= width={width} — impossible (free money). Refusing to size."
        )
    return max_loss


def vertical_spread_margin(short_strike: int, long_strike: int,
                           net_credit_per_share: float, qty: int) -> float:
    """Total ₹ margin to block for a vertical credit spread of size ``qty`` shares.

    For defined-risk strategies, margin == max possible loss. There's
    no SPAN-based reduction because the long-leg insurance caps the
    payout at ``width × qty``. This is what Zerodha actually charges
    for a vertical spread.
    """
    return vertical_spread_max_loss(
        short_strike, long_strike, net_credit_per_share,
    ) * qty
