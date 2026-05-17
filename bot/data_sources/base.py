"""DataSource — abstract market-data provider interface (FIX #36).

This module defines the contract every market-data backend must satisfy
so the rest of the bot can be backend-agnostic. Today's backends:

  * :class:`bot.data_sources.yfinance_source.YFinanceDataSource`  — the
    legacy free Yahoo Finance scrape (current default).
  * :class:`bot.data_sources.dhan_source.DhanDataSource` — Dhan REST
    + WebSocket via the ``dhanhq`` SDK (Phase 5 primary target — free
    with a Dhan trading account, no daily login).

Future backends slot in by implementing this protocol:

  * Angel SmartAPI (`smartapi-python`, free with daily TOTP login)
  * Zerodha Kite Connect (`kiteconnect`, ₹2K/mo with daily login)
  * Upstox API (`upstox-python-sdk`, free with daily OAuth)

Why a protocol and not a base class?
------------------------------------
We want each backend to be self-contained — no inheritance gymnastics,
no leaky-abstraction risk where a base class assumes yfinance-shaped
data and breaks Dhan integration silently. The Protocol keeps the
contract minimal and testable; concrete implementations are loosely
coupled (anything that quacks like a DataSource will work).

Method semantics
----------------
Every method is expected to be SAFE to call from any thread, return
SHAPE-IDENTICAL pandas DataFrames (columns: open/high/low/close/volume,
tz-aware IST DatetimeIndex), and FAIL OPEN — return an empty DataFrame
or ``None`` rather than raising — when the upstream is down. The
fallback wrapper in :mod:`bot.data_sources.registry` handles
provider-degradation by chaining sources, so a single method raising
WILL stop the chain.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

import pandas as pd


@dataclass
class Tick:
    """Single most-recent quote.

    Mirrors :class:`bot.data.Tick` for backward compatibility — the
    legacy import path still works while callers migrate to
    :mod:`bot.data_sources`.
    """
    symbol: str
    ts: datetime
    ltp: float
    volume: float


@runtime_checkable
class DataSource(Protocol):
    """Contract every market-data backend implements.

    Concrete implementations live in sibling modules
    (``yfinance_source.py``, ``dhan_source.py``, etc.). The registry
    factory in ``registry.py`` selects which one is active based on
    ``DATA_SOURCE`` env var / ``config.yaml::data_source``.
    """

    #: Stable identifier — must match the ``DATA_SOURCE`` env-var value
    #: that selects this backend (e.g. ``"yfinance"``, ``"dhan"``).
    name: str

    def is_available(self) -> bool:
        """Cheap health probe — credentials present + reachable.

        MUST NOT raise. Used by the registry's auto-fallback to decide
        whether to route through this backend or skip to the next one.
        Implementations should cache the verdict for ~30 seconds to
        avoid hammering the upstream on every call.
        """
        ...

    def history(self, symbol: str, days: int = 5,
                interval: str = "1m") -> pd.DataFrame:
        """OHLCV bars for the last ``days`` days at ``interval`` resolution.

        Returns columns ``open / high / low / close / volume`` indexed
        by tz-aware IST timestamps (bar-start convention). Drops the
        partial in-progress bar (FIX #20 invariant).
        """
        ...

    def daily_history(self, symbol: str, days: int = 90) -> pd.DataFrame:
        """Daily OHLCV — used by the backtester and gap analyses."""
        ...

    def previous_close(self, symbol: str) -> Optional[float]:
        """Yesterday's close price, or ``None`` if unavailable."""
        ...

    def todays_open(self, symbol: str) -> Optional[float]:
        """Today's session-open price, or ``None`` if pre-market."""
        ...

    def latest_quote(self, symbol: str) -> Optional[Tick]:
        """Most recent quote — typically the last 1-min close.

        Implementations that have a tick stream (Dhan WebSocket,
        Kite KiteTicker) MAY return sub-second freshness; backends
        that only do REST (yfinance) return the last closed minute.
        """
        ...

    def intraday_bars(self, symbol: str,
                      interval: str = "5m") -> pd.DataFrame:
        """The bar feed strategies actually consume in tick().

        Same OHLCV shape as :meth:`history`. Implementations are
        responsible for:

          * tz-aware IST index
          * dropping the partial in-progress bar (FIX #20)
          * the equity-vs-FNO date-filtering rules described in
            :func:`bot.data.intraday_bars` (only today's bars for
            equity; up to 2 days for F&O so EMA50 is warm by 09:15)
          * synthetic option-bar generation for option tradingsymbols
            (Phase 3 BS-based — yfinance has no real option chain;
            Dhan does, but the strategy contract is the same).
        """
        ...
