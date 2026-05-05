"""Black-Scholes-Merton pricer for paper-mode option premiums.

Phase 3 paper trading needs *some* way to assign a premium to an option
contract that the strategy chose, since we don't have a live option chain
yet. We use Black-Scholes with constant assumptions (15% IV, 7% risk-free
rate) — close enough to give realistic premiums on ATM and near-ATM
NIFTY/BANKNIFTY options. Phase 5 swaps this for live Kite Connect quotes
which have real per-strike IVs.

The model deliberately ignores:
  * Dividend yield (zero for index options anyway)
  * IV smile / skew (one σ per underlying)
  * Early exercise (Indian index options are European cash-settled, so BS
    is the right model)
  * Time-of-day (premium for the strike at 09:30 vs 14:30 differs only
    because of T; we update T per call)

If you find the synthesised premiums diverge materially from your broker's
quotes for the same strike + expiry, bump :data:`DEFAULT_IV` (or pass per
underlying) until they line up. Don't trust paper P&L within ₹100 of
breakeven — the IV assumption can be off by 5-10% on a volatile day.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Literal, Optional

OptionType = Literal["CE", "PE"]

# ─── Default model assumptions ──────────────────────────────────────────────
DEFAULT_IV = 0.15           # 15% — typical for NIFTY ATM weekly/monthly
DEFAULT_RISK_FREE = 0.07    # 7% — RBI repo benchmark


def _norm_cdf(x: float) -> float:
    """CDF of N(0,1) using ``math.erf`` (no scipy dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """PDF of N(0,1) — used by all Greek formulas (∂N/∂x)."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(S: float, K: float, T: float, sigma: float, r: float) -> tuple[float, float]:
    """Compute the Black-Scholes ``d1`` and ``d2`` for shared use across
    pricer + Greek formulas. Caller guarantees positive S/K/T/sigma.
    """
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return d1, d2


def _years_between(expiry: date, today: Optional[date] = None) -> float:
    """Calendar-day fraction of a year from ``today`` to ``expiry``.

    Uses 365-day year (Indian markets convention; US uses 252-trading-day).
    Floors at half a day so we don't divide by zero in BS — at expiry
    the option is just intrinsic value anyway and the BS formula
    degenerates correctly.
    """
    today = today or date.today()
    days = (expiry - today).days
    return max(days, 0.5) / 365.0


def years_to_expiry(expiry: date, today: Optional[date] = None) -> float:
    """Public wrapper around :func:`_years_between` for callers."""
    return _years_between(expiry, today)


def bs_call(S: float, K: float, T: float,
            sigma: float = DEFAULT_IV, r: float = DEFAULT_RISK_FREE) -> float:
    """European call price via Black-Scholes.

    ``S`` = spot, ``K`` = strike, ``T`` = years to expiry, ``sigma`` =
    volatility (annualised, decimal), ``r`` = risk-free rate
    (annualised, decimal).

    Edge cases:
      * ``T <= 0`` → returns intrinsic ``max(S - K, 0)``.
      * ``sigma <= 0`` → returns intrinsic.
      * Either ``S`` or ``K`` non-positive → returns 0.
    """
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def bs_put(S: float, K: float, T: float,
           sigma: float = DEFAULT_IV, r: float = DEFAULT_RISK_FREE) -> float:
    """European put price via Black-Scholes (mirror of :func:`bs_call`)."""
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs(S: float, K: float, T: float, opt_type: OptionType,
       sigma: float = DEFAULT_IV, r: float = DEFAULT_RISK_FREE) -> float:
    """Single-entry-point pricer dispatching on option type."""
    if opt_type == "CE":
        return bs_call(S, K, T, sigma, r)
    if opt_type == "PE":
        return bs_put(S, K, T, sigma, r)
    raise ValueError(f"opt_type must be 'CE' or 'PE', got {opt_type!r}")


def bs_from_expiry(S: float, K: float, expiry: date, opt_type: OptionType,
                   sigma: float = DEFAULT_IV, r: float = DEFAULT_RISK_FREE,
                   today: Optional[date] = None) -> float:
    """Convenience: price an option given a calendar expiry date."""
    T = _years_between(expiry, today)
    return bs(S, K, T, opt_type, sigma, r)


# ─── Synthetic option-bar generation ────────────────────────────────────────


# ─── Greeks ─────────────────────────────────────────────────────────────────
#
# All Greeks below assume European options on a non-dividend-paying asset
# (Indian index options are cash-settled European, so this is correct).
# Returned units:
#   delta   ∂Price/∂Spot          — fraction (0..1 for calls, -1..0 for puts)
#   gamma   ∂²Price/∂Spot²        — per ₹ of spot
#   theta   ∂Price/∂Time          — ₹ per CALENDAR day (negative for longs)
#   vega    ∂Price/∂σ             — ₹ per 1.00 (i.e. 100%) change in IV
#                                   — divide by 100 to get ₹ per 1% IV move
#
# All four are POSITION-SIDE-AGNOSTIC (the caller flips signs for shorts).


def delta(S: float, K: float, T: float, opt_type: OptionType,
          sigma: float = DEFAULT_IV, r: float = DEFAULT_RISK_FREE) -> float:
    """Delta — sensitivity to a 1-rupee change in spot."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        # At/after expiry the option is purely intrinsic — delta is
        # 1.0 (ITM call) / 0.0 (OTM call) / -1.0 (ITM put) / 0.0 (OTM put).
        if opt_type == "CE":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1, _ = _d1_d2(S, K, T, sigma, r)
    return _norm_cdf(d1) if opt_type == "CE" else _norm_cdf(d1) - 1.0


def gamma(S: float, K: float, T: float,
          sigma: float = DEFAULT_IV, r: float = DEFAULT_RISK_FREE) -> float:
    """Gamma — same magnitude for calls and puts (put-call symmetric)."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d1, _ = _d1_d2(S, K, T, sigma, r)
    return _norm_pdf(d1) / (S * sigma * math.sqrt(T))


def theta(S: float, K: float, T: float, opt_type: OptionType,
          sigma: float = DEFAULT_IV, r: float = DEFAULT_RISK_FREE) -> float:
    """Theta in ₹ per CALENDAR day. Always negative for long options."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d1, d2 = _d1_d2(S, K, T, sigma, r)
    sqrt_T = math.sqrt(T)
    common = -(S * _norm_pdf(d1) * sigma) / (2.0 * sqrt_T)
    if opt_type == "CE":
        annual = common - r * K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        annual = common + r * K * math.exp(-r * T) * _norm_cdf(-d2)
    return annual / 365.0     # → per calendar day


def vega(S: float, K: float, T: float,
         sigma: float = DEFAULT_IV, r: float = DEFAULT_RISK_FREE) -> float:
    """Vega — same magnitude for calls and puts. Returns ₹ per 1.00 (100%)
    move in volatility; divide the result by 100 to get ₹ per 1%.
    """
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d1, _ = _d1_d2(S, K, T, sigma, r)
    return S * _norm_pdf(d1) * math.sqrt(T)


def all_greeks(S: float, K: float, T: float, opt_type: OptionType,
               sigma: float = DEFAULT_IV, r: float = DEFAULT_RISK_FREE) -> dict:
    """Convenience: return all four primary Greeks in one call.

    Used by the dashboard to render per-option-position risk in a
    compact widget without four redundant BS recomputations.
    """
    return {
        "delta": delta(S, K, T, opt_type, sigma, r),
        "gamma": gamma(S, K, T, sigma, r),
        "theta": theta(S, K, T, opt_type, sigma, r),
        "vega":  vega(S, K, T, sigma, r),
    }


def synth_option_ohlc(spot_high: float, spot_low: float,
                      spot_open: float, spot_close: float,
                      K: float, T: float, opt_type: OptionType,
                      sigma: float = DEFAULT_IV,
                      r: float = DEFAULT_RISK_FREE) -> dict:
    """Synthesise an option's OHLC from one bar of the underlying's OHLC.

    For a call (CE), premium is monotonically increasing in spot, so:
      option_high  = price(spot_high)
      option_low   = price(spot_low)
    For a put (PE) it's flipped.

    This is good enough for paper-mode position management — the
    position manager checks "did the premium hit SL or TP this bar"
    and that's exactly what these synthetic high/low capture.

    NOT good enough for IV-driven strategies (an IV crush will tank
    the premium even if spot didn't move much). That's a Phase 4
    concern.
    """
    if opt_type == "CE":
        h = bs_call(spot_high, K, T, sigma, r)
        l = bs_call(spot_low,  K, T, sigma, r)
    else:
        h = bs_put(spot_low,  K, T, sigma, r)   # put high when spot low
        l = bs_put(spot_high, K, T, sigma, r)   # put low when spot high
    o = bs(spot_open,  K, T, opt_type, sigma, r)
    c = bs(spot_close, K, T, opt_type, sigma, r)
    return {"open": o, "high": h, "low": l, "close": c}
