"""YFinanceDataSource — adapter wrapping the legacy ``bot.data`` module.

This is the default backend (preserves the bot's behaviour from before
the Phase 5 migration). All real logic — FIX #20 partial-bar trim, FIX
#27 retry + stale-cache fallback, the synthetic option/spread/IC bar
generation — lives in :mod:`bot.data` and is left untouched. This
adapter is just a thin shim that exposes the existing functions
through the new :class:`bot.data_sources.base.DataSource` protocol.

We intentionally do NOT rewrite the yfinance code into this class.
The legacy module has years of fix-points (FIX #20, #27, the dedup'd
warning logger, the 60s Redis cache, the synthetic Black-Scholes
option chain) that all work today. Refactoring them carries no
upside and a real risk of regressing the FIX list. The Phase 5
migration is about ADDING a Dhan path, not about reshuffling the
yfinance one.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .base import DataSource, Tick


class YFinanceDataSource:
    """Default backend — the existing yfinance scrape behind a Protocol."""

    name = "yfinance"

    def __init__(self) -> None:
        # Lazy import: avoid pulling yfinance into the import graph until
        # someone actually instantiates this backend.
        from .. import data as _legacy
        self._legacy = _legacy

    def is_available(self) -> bool:
        """yfinance has no auth — always 'available'.

        We could probe with a real fetch but that would slow every
        registry lookup. The :class:`FallbackDataSource` chain handles
        actual upstream failures by catching empty DataFrames.
        """
        return True

    def history(self, symbol: str, days: int = 5,
                interval: str = "1m") -> pd.DataFrame:
        return self._legacy.history(symbol, days=days, interval=interval)

    def daily_history(self, symbol: str, days: int = 90) -> pd.DataFrame:
        return self._legacy.daily_history(symbol, days=days)

    def previous_close(self, symbol: str) -> Optional[float]:
        return self._legacy.previous_close(symbol)

    def todays_open(self, symbol: str) -> Optional[float]:
        return self._legacy.todays_open(symbol)

    def latest_quote(self, symbol: str) -> Optional[Tick]:
        legacy_tick = self._legacy.latest_quote(symbol)
        if legacy_tick is None:
            return None
        # Legacy Tick and our Tick are field-compatible — re-wrap for
        # type-system cleanliness.
        return Tick(
            symbol=legacy_tick.symbol,
            ts=legacy_tick.ts,
            ltp=legacy_tick.ltp,
            volume=legacy_tick.volume,
        )

    def intraday_bars(self, symbol: str,
                      interval: str = "5m") -> pd.DataFrame:
        return self._legacy.intraday_bars(symbol, interval=interval)


# Note: DataSource is a runtime-checkable Protocol — registry uses
# ``isinstance(src, DataSource)`` at construction time, so a missing
# method is caught the first time the source is built (not at import,
# to avoid pulling in the yfinance + bot.data dependency graph for any
# module that touches data_sources/__init__).
