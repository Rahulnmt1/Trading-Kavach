"""Options pricing, Greeks & margin (paper-mode synthesis Phase 3-4)."""
from .pricing import (
    bs, bs_call, bs_put, bs_from_expiry,
    synth_option_ohlc, years_to_expiry,
    delta, gamma, theta, vega, all_greeks,
    DEFAULT_IV, DEFAULT_RISK_FREE,
)
from .margin import (
    vertical_spread_max_loss, vertical_spread_margin,
    naked_short_margin,
)

__all__ = [
    "bs", "bs_call", "bs_put", "bs_from_expiry",
    "synth_option_ohlc", "years_to_expiry",
    "delta", "gamma", "theta", "vega", "all_greeks",
    "vertical_spread_max_loss", "vertical_spread_margin",
    "naked_short_margin",
    "DEFAULT_IV", "DEFAULT_RISK_FREE",
]
