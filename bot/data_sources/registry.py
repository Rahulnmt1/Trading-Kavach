"""Registry — selects the active :class:`DataSource` and wires fallback.

The bot's strategies and the legacy :mod:`bot.data` module call
:func:`get_data_source` to obtain whichever backend is currently
configured. The selection is driven by:

1. Env var ``DATA_SOURCE`` (one of ``yfinance``, ``dhan``, ``auto``)
2. ``config.yaml::market_data.source`` (same values; lower priority)
3. Default: ``yfinance`` (preserves the bot's pre-Phase-5 behaviour
   so anyone running the code without setting env vars sees no
   change of behaviour).

When ``DATA_SOURCE=auto``, the registry tries each backend in
configurable order and returns the first one whose
:meth:`is_available` reports True. The fallback chain wraps the
result so a transient upstream failure on the primary doesn't
poison a single strategy tick — the next backend in the chain
serves the request.

Why singleton?
--------------
DataSource instances hold network state — Dhan keeps an HTTP session
+ cached health verdict + cached symbol lookups. Re-instantiating
on every call would burn those, so we cache one instance per source
name. The cache is invalidated only on explicit
:func:`reset_registry` (used by tests).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Iterable, List, Optional

import pandas as pd

from .base import DataSource, Tick

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Source name → factory mapping
# ---------------------------------------------------------------------------

#: Order matters for ``DATA_SOURCE=auto`` — first-available wins.
#: Edit this list to add a new backend (e.g. ``angel_source``,
#: ``upstox_source``, ``kite_source``).
_AUTO_PROBE_ORDER = ("dhan", "yfinance")


def _build_yfinance() -> DataSource:
    from .yfinance_source import YFinanceDataSource
    return YFinanceDataSource()


def _build_dhan() -> DataSource:
    from .dhan_source import DhanDataSource
    return DhanDataSource()


_FACTORIES = {
    "yfinance": _build_yfinance,
    "dhan":     _build_dhan,
    # Future: "angel": _build_angel, "upstox": _build_upstox, "kite": _build_kite,
}

# ---------------------------------------------------------------------------
# Singleton cache
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_singletons: dict[str, DataSource] = {}
_active_source: Optional[DataSource] = None


def _get_or_build(name: str) -> Optional[DataSource]:
    name = name.lower().strip()
    if name not in _FACTORIES:
        logger.warning("[data_sources] unknown source name: %s — known: %s",
                       name, sorted(_FACTORIES.keys()))
        return None
    with _lock:
        cached = _singletons.get(name)
        if cached is not None:
            return cached
        try:
            inst = _FACTORIES[name]()
        except Exception as exc:  # noqa: BLE001
            logger.error("[data_sources] failed to build %s: %s", name, exc)
            return None
        _singletons[name] = inst
        return inst


# ---------------------------------------------------------------------------
# FallbackDataSource — chains multiple backends with degrade-on-empty
# ---------------------------------------------------------------------------

class FallbackDataSource:
    """Tries each wrapped source in order; returns the first non-empty answer.

    Used both for explicit ``DATA_SOURCE=auto`` selection and for
    user-configured fallback (``market_data.fallback``). The chain
    is short — typically 2 sources — so we don't bother memoising
    "which source last served this symbol" the way a smart router
    would. Predictable behaviour wins over micro-optimisation here.
    """

    def __init__(self, sources: Iterable[DataSource]) -> None:
        self._sources: List[DataSource] = [s for s in sources if s is not None]
        if not self._sources:
            raise ValueError("FallbackDataSource needs at least one source")
        self.name = "+".join(s.name for s in self._sources)

    def is_available(self) -> bool:
        return any(s.is_available() for s in self._sources)

    def history(self, symbol: str, days: int = 5,
                interval: str = "1m") -> pd.DataFrame:
        for src in self._sources:
            if not src.is_available():
                continue
            df = src.history(symbol, days=days, interval=interval)
            if df is not None and not df.empty:
                return df
        return pd.DataFrame()

    def daily_history(self, symbol: str, days: int = 90) -> pd.DataFrame:
        for src in self._sources:
            if not src.is_available():
                continue
            df = src.daily_history(symbol, days=days)
            if df is not None and not df.empty:
                return df
        return pd.DataFrame()

    def previous_close(self, symbol: str) -> Optional[float]:
        for src in self._sources:
            if not src.is_available():
                continue
            v = src.previous_close(symbol)
            if v is not None:
                return v
        return None

    def todays_open(self, symbol: str) -> Optional[float]:
        for src in self._sources:
            if not src.is_available():
                continue
            v = src.todays_open(symbol)
            if v is not None:
                return v
        return None

    def latest_quote(self, symbol: str) -> Optional[Tick]:
        for src in self._sources:
            if not src.is_available():
                continue
            t = src.latest_quote(symbol)
            if t is not None:
                return t
        return None

    def intraday_bars(self, symbol: str,
                      interval: str = "5m") -> pd.DataFrame:
        for src in self._sources:
            if not src.is_available():
                continue
            df = src.intraday_bars(symbol, interval=interval)
            if df is not None and not df.empty:
                return df
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_data_source() -> DataSource:
    """Return the active data source (cached after first call).

    Resolution order:
      1. ``DATA_SOURCE`` env var (highest priority)
      2. ``config.yaml::market_data.source``
      3. ``"yfinance"`` (default)
    """
    global _active_source
    with _lock:
        if _active_source is not None:
            return _active_source

    # Env var wins.
    chosen = os.environ.get("DATA_SOURCE", "").strip().lower()

    # Then config.yaml.
    if not chosen:
        try:
            from ..config import load_config
            cfg = load_config()
            md = getattr(cfg, "market_data", None)
            if md is not None and getattr(md, "source", None):
                chosen = str(md.source).strip().lower()
        except Exception:  # noqa: BLE001
            chosen = ""

    if not chosen:
        chosen = "yfinance"

    if chosen == "auto":
        primary = _resolve_auto()
    else:
        primary_inst = _get_or_build(chosen)
        if primary_inst is None or not primary_inst.is_available():
            logger.warning("[data_sources] requested source '%s' unavailable — "
                           "falling back to yfinance", chosen)
            primary_inst = _get_or_build("yfinance")
        primary = primary_inst

    if primary is None:
        # Catastrophic — no backend works at all. Build the yfinance
        # adapter directly so we at least have something callable.
        from .yfinance_source import YFinanceDataSource
        primary = YFinanceDataSource()

    # Wrap with fallback (yfinance) when the primary isn't already yfinance.
    # This is the resilience layer FIX #36 promises: a Dhan outage
    # silently degrades to yfinance for that one tick.
    if primary.name != "yfinance":
        yf = _get_or_build("yfinance")
        if yf is not None:
            wrapped: DataSource = FallbackDataSource([primary, yf])
        else:
            wrapped = primary
    else:
        wrapped = primary

    with _lock:
        _active_source = wrapped
    logger.info("[data_sources] active backend: %s", wrapped.name)
    return wrapped


def _resolve_auto() -> Optional[DataSource]:
    """``DATA_SOURCE=auto`` — first source whose creds are configured wins."""
    for name in _AUTO_PROBE_ORDER:
        inst = _get_or_build(name)
        if inst is not None and inst.is_available():
            return inst
    # Nothing reachable — return whichever source we managed to build,
    # so callers at least get a real DataSource instance.
    return _get_or_build("yfinance")


def reset_registry() -> None:
    """Test-only — drop all cached instances. Forces re-resolution on
    the next :func:`get_data_source` call.
    """
    global _active_source
    with _lock:
        _singletons.clear()
        _active_source = None
