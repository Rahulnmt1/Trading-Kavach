"""Pluggable market-data sources (FIX #35 cross-check + FIX #36 Phase 5A migration).

This subpackage replaces the legacy "yfinance everywhere" assumption
with a Protocol-based architecture where any backend that implements
:class:`DataSource` can serve the bot's market-data calls. The current
backends:

  * :class:`YFinanceDataSource`  — legacy default, free Yahoo scrape.
  * :class:`DhanDataSource`      — free with a Dhan trading account,
    no daily login required (Phase 5A primary target).

Adding a new backend (Angel SmartAPI, Upstox, Kite Connect):

  1. Drop a new file alongside ``dhan_source.py`` implementing the
     :class:`DataSource` protocol.
  2. Register the factory in :mod:`bot.data_sources.registry`'s
     ``_FACTORIES`` dict and (optionally) ``_AUTO_PROBE_ORDER``.
  3. Add the corresponding ``.env`` knobs and a config.yaml entry.

The cross-check surface (FIX #35 — :mod:`bot.data_sources.nse_direct`)
is preserved as a separate, narrow tool: validates one yfinance close
against the NSE direct REST endpoint at signal-emission time. This
is independent of the backend selection — it's a pre-trade gate that
runs regardless of which backend is active.
"""

from .base import DataSource, Tick                             # noqa: F401
from .registry import (                                          # noqa: F401
    FallbackDataSource,
    get_data_source,
    reset_registry,
)

