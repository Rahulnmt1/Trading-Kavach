"""Trading strategies."""
from .base import Signal, SignalType, Strategy
from .ensemble import Ensemble
from .ema_supertrend import EMASupertrendStrategy
from .multitimeframe import MultiTimeframeStrategy
from .orb import ORBStrategy
from .vwap_revert import VWAPRevertStrategy
from .fno import (
    CreditSpreadStrategy,
    FuturesTrendStrategy,
    IronCondorStrategy,
    OptionBuyDirectionalStrategy,
)

__all__ = [
    "Signal",
    "SignalType",
    "Strategy",
    "Ensemble",
    "EMASupertrendStrategy",
    "MultiTimeframeStrategy",
    "ORBStrategy",
    "VWAPRevertStrategy",
    "FuturesTrendStrategy",
    "OptionBuyDirectionalStrategy",
    "CreditSpreadStrategy",
    "IronCondorStrategy",
]


def build_default_ensemble(segment=None):
    """Construct the default ensemble for ``segment`` from config.

    For ``Segment.EQUITY`` (the default) this picks up the top-level
    ``strategies:`` block and assembles ORB / VWAP-Revert /
    EMA-Supertrend, optionally wrapped in MTF confirmation.

    For ``Segment.FNO`` it reads ``fno.strategies:`` instead. Phase 1
    ships with ``fno.strategies.enabled = []`` so the ensemble is
    empty — every ``generate()`` call returns HOLD and no orders are
    placed. Phase 2 will register the F&O-specific strategies (futures
    trend-follower, then option-buying / option-selling).
    """
    from ..config import load_config
    from ..segment import Segment

    if segment is None:
        segment = Segment.EQUITY

    cfg = load_config()

    # Pick the strategies block to read from.
    if segment == Segment.FNO:
        if cfg.fno is None or cfg.fno.strategies is None:
            return Ensemble([], min_agree=1)
        strat_cfg = cfg.fno.strategies
    else:
        strat_cfg = cfg.strategies

    members: list[Strategy] = []
    if segment == Segment.FNO:
        # F&O segment registers F&O-only strategies. Equity strategies
        # (ORB on a 09:15 opening range, VWAP mean-reversion calibrated
        # on stock liquidity, etc.) don't make sense on index futures
        # and are deliberately not registered here even if the YAML lists
        # them.
        if "futures_trend" in strat_cfg.enabled:
            members.append(FuturesTrendStrategy(strat_cfg.futures_trend))
        if "option_buy_directional" in strat_cfg.enabled:
            members.append(
                OptionBuyDirectionalStrategy(strat_cfg.option_buy_directional)
            )
        if "credit_spread" in strat_cfg.enabled:
            members.append(CreditSpreadStrategy(strat_cfg.credit_spread))
        if "iron_condor" in strat_cfg.enabled:
            members.append(IronCondorStrategy(strat_cfg.iron_condor))
        # Phase 5+: naked_short_option (margin model exists in
        # ``bot/options/margin.py`` but no strategy registered — naked
        # NIFTY shorts need ~₹2.5L margin per lot, out of reach for
        # the default ₹50K F&O budget).
    else:
        if "orb" in strat_cfg.enabled:
            members.append(ORBStrategy(strat_cfg.orb))
        if "vwap_revert" in strat_cfg.enabled:
            members.append(VWAPRevertStrategy(strat_cfg.vwap_revert))
        if "ema_supertrend" in strat_cfg.enabled:
            members.append(EMASupertrendStrategy(strat_cfg.ema_supertrend))

    if strat_cfg.multitimeframe.enabled and members:
        mtf_cfg = strat_cfg.multitimeframe
        members = [MultiTimeframeStrategy(m, mtf_cfg) for m in members]

    return Ensemble(members, min_agree=strat_cfg.ensemble.min_agree)
