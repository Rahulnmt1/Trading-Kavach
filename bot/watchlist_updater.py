"""Auto-watchlist updater.

Scans the NIFTY 100 universe and ranks each candidate by a composite score:
  - Liquidity      (avg daily volume)        — must clear `min_avg_volume` floor
  - Trend          (SMA20 slope)             — strongly weighted
  - Momentum       (recent N-day return)     — strongly weighted
  - Volatility     (ATR%)                    — slight weight; avoid both ends

The top-N symbols become tomorrow's watchlist. Result is cached in Redis under
`watchlist:auto` AND **always** written back to `config.yaml` so the YAML stays
the canonical source-of-truth for what the bot trades (this is mandatory by
design — see `WatchlistUpdaterCfg` in `bot/config.py`).

If the scoring pass yields zero qualifying symbols (e.g. data feed is broken),
the persist step is skipped to avoid clobbering the existing watchlist.

Runs daily pre-market via the scheduler. Manual run: `python -m cli update-watchlist`.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd
import yaml

from .cache import get_cache
from .config import PROJECT_ROOT, load_config
from .data import daily_history
from .indicators import atr, sma
from .logger import logger
from .universe import NIFTY_100


@dataclass
class Candidate:
    symbol: str
    last_close: float
    avg_volume: float
    sma20_slope_pct: float
    momentum_pct: float
    atr_pct: float
    score: float = 0.0
    bias: str = "neutral"      # "long" / "short" / "neutral"


def _score_candidate(c: Candidate, min_avg_volume: int) -> Candidate:
    if c.avg_volume < min_avg_volume:
        c.score = -1.0
        return c

    momentum_score = np.tanh(c.momentum_pct / 5.0)      # ±1 saturated at ±5%
    trend_score = np.tanh(c.sma20_slope_pct * 10)       # ±1 saturated at ±10%/period
    vol_penalty = -abs(c.atr_pct - 1.5) * 0.05          # prefer ~1.5% ATR
    liquidity = min(np.log10(c.avg_volume / 100_000), 2.0) / 2.0  # 0..1

    bullish = max(momentum_score, 0) + max(trend_score, 0) + liquidity + vol_penalty
    bearish = max(-momentum_score, 0) + max(-trend_score, 0) + liquidity + vol_penalty

    if bullish >= bearish:
        c.score = float(bullish)
        c.bias = "long" if c.score > 0.5 else "neutral"
    else:
        c.score = float(bearish)
        c.bias = "short" if c.score > 0.5 else "neutral"
    return c


def _candidate_for(symbol: str, lookback_days: int, momentum_days: int) -> Optional[Candidate]:
    # We need: lookback_days (warm-up of SMA) + lookback_days (slope window) + buffer
    # of trading bars. yfinance `days=` is *calendar* days, so we ask for ~1.6× to
    # absorb weekends and holidays.
    needed_trading_bars = lookback_days * 2 + 10
    df = daily_history(symbol, days=max(int(needed_trading_bars * 1.6), 90))
    if df.empty or len(df) < needed_trading_bars:
        return None
    s20 = sma(df["close"], lookback_days)
    s20_clean = s20.dropna()
    if len(s20_clean) < 2:
        return None
    last = df.iloc[-1]
    # Slope of SMA over the lookback window — operate on the NaN-cleaned series so
    # we never index into the warm-up region (was a silent off-by-one bug).
    s20_now = float(s20_clean.iloc[-1])
    slope_window = min(lookback_days, len(s20_clean) - 1)
    s20_prev = float(s20_clean.iloc[-slope_window - 1])
    sma_slope_pct = (s20_now / s20_prev - 1) * 100 if s20_prev else 0.0

    if len(df) > momentum_days:
        momentum = (df["close"].iloc[-1] / df["close"].iloc[-momentum_days - 1] - 1) * 100
    else:
        momentum = 0.0

    a = atr(df, 14)
    atr_pct = float(a.iloc[-1]) / float(last["close"]) * 100 if not a.isna().all() else 0.0
    avg_vol = float(df["volume"].tail(20).mean())

    return Candidate(
        symbol=symbol, last_close=float(last["close"]),
        avg_volume=avg_vol, sma20_slope_pct=float(sma_slope_pct),
        momentum_pct=float(momentum), atr_pct=float(atr_pct),
    )


def update_watchlist() -> List[Candidate]:
    cfg = load_config()
    upd = cfg.watchlist_updater
    cache = get_cache()

    candidates: List[Candidate] = []
    for sym in NIFTY_100:
        try:
            c = _candidate_for(sym, upd.trend_lookback_days, upd.momentum_lookback_days)
            if c is not None:
                candidates.append(_score_candidate(c, upd.min_avg_volume))
        except Exception as ex:
            logger.warning("[watchlist] {} skipped: {}", sym, ex)

    candidates.sort(key=lambda x: x.score, reverse=True)
    selected = [c for c in candidates if c.score > 0][: upd.top_n]

    payload = {
        "date": date.today().isoformat(),
        "symbols": [c.symbol for c in selected],
        "candidates": [asdict(c) for c in selected],
    }
    cache.set_json("watchlist:auto", payload, ttl=24 * 3600)
    logger.info("[watchlist] updated: {} symbols — top: {}",
                len(selected), [(c.symbol, c.bias, round(c.score, 2)) for c in selected[:5]])

    # Persisting to config.yaml is MANDATORY (see module docstring). The only
    # guard is a safety check: if scoring produced nothing usable, we leave the
    # existing watchlist untouched rather than overwriting it with [].
    if selected:
        _persist_to_yaml([c.symbol for c in selected])
    else:
        logger.warning("[watchlist] no symbols cleared scoring — config.yaml left unchanged.")

    return selected


def _persist_to_yaml(symbols: List[str]) -> None:
    """Write `symbols` to `watchlist.symbols` in config.yaml.

    Preserves comments and key ordering when `ruamel.yaml` is installed
    (recommended — see requirements.txt). Falls back to PyYAML's safe_dump,
    which loses comments but still produces a valid file.
    """
    cfg_path = PROJECT_ROOT / "config.yaml"
    if not cfg_path.exists():
        logger.warning("[watchlist] config.yaml not found at {} — skipping persist.", cfg_path)
        return

    try:
        from ruamel.yaml import YAML  # type: ignore
        ry = YAML()
        ry.preserve_quotes = True
        ry.indent(mapping=2, sequence=4, offset=2)
        with cfg_path.open() as fh:
            data = ry.load(fh) or {}
        data.setdefault("watchlist", {})["symbols"] = symbols
        with cfg_path.open("w") as fh:
            ry.dump(data, fh)
    except ImportError:
        logger.warning("[watchlist] ruamel.yaml not installed — falling back to "
                       "PyYAML (comments in config.yaml will be lost). "
                       "`pip install ruamel.yaml` to fix.")
        with cfg_path.open() as fh:
            raw = yaml.safe_load(fh) or {}
        raw.setdefault("watchlist", {})["symbols"] = symbols
        with cfg_path.open("w") as fh:
            yaml.safe_dump(raw, fh, sort_keys=False, default_flow_style=False)

    logger.info("[watchlist] wrote {} symbols back to config.yaml", len(symbols))


def auto_watchlist() -> List[str]:
    """Return the latest auto-watchlist, falling back to config.yaml watchlist."""
    cache = get_cache()
    snap = cache.get_json("watchlist:auto")
    if snap and snap.get("date") == date.today().isoformat():
        return list(snap["symbols"])
    return list(load_config().symbols)
