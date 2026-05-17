"""DhanDataSource — Dhan REST + WebSocket adapter (Phase 5 primary target).

Dhan offers a free Indian-market data feed for any retail trader who
holds a Dhan trading account. Compared to yfinance:

  * **Real-time** — sub-second WebSocket ticks vs yfinance's 1-3 min
    polling lag.
  * **Authoritative** — direct exchange feed (NSE) vs yfinance's
    multi-hop scrape pipeline.
  * **Persistent token** — no daily login (Kite Connect requires
    daily TOTP). You set ``DHAN_ACCESS_TOKEN`` once and it lasts
    until you regenerate it from the Dhan dashboard.
  * **Real option chains** — Dhan exposes the full NSE option
    chain (LTP / OI / IV) for indices and large-cap equities,
    where yfinance has nothing.

What this adapter implements TODAY (Phase 5A — data only)
---------------------------------------------------------

Strategy callers use exactly two methods on the hot path:
:meth:`history` and :meth:`intraday_bars`. This adapter implements
both via Dhan's ``intraday_minute_data`` REST endpoint. The other
methods (``daily_history``, ``previous_close``, ``todays_open``,
``latest_quote``) are also implemented but with thinner test
coverage — they're called rarely and are easy to fix forward.

What lands in Phase 5B (data + ticks, deferred):
  * Subscribe ``bot.feeds.dhan_ws.DhanFeed`` for the watchlist
    symbols at startup, write ticks to ``tick:<sym>`` Redis keys.
  * :meth:`latest_quote` reads from Redis instead of polling.

What lands in Phase 5C (live trading, much later):
  * :class:`bot.broker.dhan.DhanBroker` learns to place real
    orders and reconcile fills; ``LIVE_TRADING=true`` activates
    the path. Spread/IC multi-leg wiring per the Zerodha skeleton
    in ``bot/broker/zerodha.py``.

Configuration
-------------
Set in ``.env``::

    DHAN_CLIENT_ID=1100xxxxxx          # numeric client ID
    DHAN_ACCESS_TOKEN=eyJ0eXAi...      # JWT from web.dhan.co/profile
    DATA_SOURCE=dhan                   # routes the bot's market-data calls here

Or in ``config.yaml`` under ``market_data:``::

    market_data:
      source: dhan         # yfinance | dhan
      fallback: yfinance   # if Dhan fails, fall back here

Failure modes
-------------
The adapter is FAIL-OPEN:

  * Missing credentials → :meth:`is_available` returns False; the
    registry routes to the fallback (yfinance).
  * Dhan REST 401/403 (token expired) → log once, return empty
    DataFrame; registry's :class:`FallbackDataSource` retries via
    yfinance for that one call.
  * Network blip → return empty DataFrame; same fallback path.

The bot is NEVER halted by a Dhan outage. yfinance is the safety
net (FIX #27 retry + stale-cache, all preserved).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import pandas as pd
import pytz

from .base import DataSource, Tick
from .dhan_resolver import resolve_symbol

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: How long :meth:`is_available` caches its verdict between calls.
#: Avoids hammering the upstream on every strategy tick.
HEALTH_CHECK_TTL_SECONDS = 30.0

#: Per-call REST timeout. Dhan responds in <500ms typically; 5s gives
#: huge safety margin for transient network slowness without blocking
#: the tick loop for too long.
HTTP_TIMEOUT_SECONDS = 5.0


class DhanDataSource:
    """Pluggable :class:`bot.data_sources.base.DataSource` backed by Dhan."""

    name = "dhan"

    def __init__(self,
                 client_id: Optional[str] = None,
                 access_token: Optional[str] = None) -> None:
        # Lazy import — keep the import graph free of dhanhq for users
        # who never enable this backend.
        from ..config import env
        e_ = env()
        self._client_id = client_id or e_.DHAN_CLIENT_ID
        self._access_token = access_token or e_.DHAN_ACCESS_TOKEN
        self._client: Optional[Any] = None
        self._available_until: float = 0.0
        self._last_availability: bool = False
        self._creds_present = bool(self._client_id and self._access_token)

        if self._creds_present:
            try:
                from dhanhq import dhanhq
                self._client = dhanhq(self._client_id, self._access_token)
            except ImportError:
                logger.warning("[dhan_source] dhanhq not installed — DhanDataSource disabled. "
                               "Run: pip install dhanhq")
                self._creds_present = False
            except Exception as exc:  # noqa: BLE001
                logger.error("[dhan_source] dhanhq init failed: %s", exc)
                self._creds_present = False
        else:
            logger.info("[dhan_source] DHAN_CLIENT_ID/DHAN_ACCESS_TOKEN not set — "
                        "DhanDataSource will report unavailable; registry will "
                        "route to fallback. Set both in .env to enable.")

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Cached health probe.

        Returns ``True`` if credentials are present AND a recent
        probe call succeeded. Cached for ``HEALTH_CHECK_TTL_SECONDS``
        so we hit Dhan at most once every 30s on average.
        """
        if not self._creds_present:
            return False
        now = time.time()
        if now < self._available_until:
            return self._last_availability
        try:
            # ``get_fund_limits`` is one of the cheapest authenticated
            # calls Dhan exposes — auth-validates without subscribing
            # to anything or fetching big payloads.
            resp = self._client.get_fund_limits()  # type: ignore[union-attr]
            ok = isinstance(resp, dict) and resp.get("status") == "success"
            self._last_availability = ok
            self._available_until = now + HEALTH_CHECK_TTL_SECONDS
            if not ok:
                logger.warning("[dhan_source] health probe failed: %s", resp)
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dhan_source] health probe raised: %s", exc)
            self._last_availability = False
            self._available_until = now + HEALTH_CHECK_TTL_SECONDS
            return False

    # ------------------------------------------------------------------
    # Hot-path: intraday bars (used by every strategy tick)
    # ------------------------------------------------------------------

    @staticmethod
    def _interval_to_minutes(interval: str) -> Optional[int]:
        """``"5m"`` → 5, ``"1h"`` → 60, ``"1m"`` → 1, else None."""
        if not interval:
            return None
        unit = interval[-1].lower()
        try:
            n = int(interval[:-1])
        except ValueError:
            return None
        if unit == "m":
            return n
        if unit == "h":
            return n * 60
        return None

    def _intraday_minute_data(self, inst, from_dt: datetime,
                              to_dt: datetime, interval_minutes: int
                              ) -> pd.DataFrame:
        """Wrap Dhan's REST endpoint and normalise the response shape."""
        if self._client is None:
            return pd.DataFrame()
        try:
            resp = self._client.intraday_minute_data(  # type: ignore[union-attr]
                security_id=str(inst.security_id),
                exchange_segment=inst.exchange_segment,
                instrument_type=inst.instrument_name,
                from_date=from_dt.strftime("%Y-%m-%d"),
                to_date=to_dt.strftime("%Y-%m-%d"),
                interval=str(interval_minutes),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dhan_source] intraday_minute_data raised for %s: %s",
                           inst.tradingsymbol, exc)
            return pd.DataFrame()

        if not isinstance(resp, dict) or resp.get("status") != "success":
            logger.warning("[dhan_source] intraday_minute_data failed for %s: %s",
                           inst.tradingsymbol, resp)
            return pd.DataFrame()

        data = resp.get("data") or {}
        # Dhan returns parallel arrays: {open: [...], high: [...], low: [...],
        # close: [...], volume: [...], timestamp: [...]} where timestamps
        # are unix epoch seconds.
        timestamps = data.get("timestamp") or data.get("start_Time") or []
        if not timestamps:
            return pd.DataFrame()

        df = pd.DataFrame({
            "open":   data.get("open", []),
            "high":   data.get("high", []),
            "low":    data.get("low", []),
            "close":  data.get("close", []),
            "volume": data.get("volume", []),
        })
        # Convert epoch → tz-aware IST DatetimeIndex.
        df.index = pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(IST)
        df = df.dropna()
        return df

    def history(self, symbol: str, days: int = 5,
                interval: str = "1m") -> pd.DataFrame:
        if not self.is_available():
            return pd.DataFrame()
        inst = resolve_symbol(symbol)
        if inst is None:
            logger.debug("[dhan_source] no Dhan instrument for %s", symbol)
            return pd.DataFrame()
        minutes = self._interval_to_minutes(interval)
        if minutes is None:
            logger.debug("[dhan_source] non-minute interval %s — falling back", interval)
            return pd.DataFrame()
        now = datetime.now(IST)
        from_dt = now - timedelta(days=max(days, 1))
        df = self._intraday_minute_data(inst, from_dt, now, minutes)
        if df.empty:
            return df
        # Drop the still-forming last bar (FIX #20 invariant — keep
        # behaviour identical between yfinance and Dhan so strategies
        # don't see in-progress candle whipsaws when the source flips).
        bar_delta = timedelta(minutes=minutes)
        if len(df) > 0 and df.index[-1] + bar_delta > now:
            df = df.iloc[:-1]
        return df

    def intraday_bars(self, symbol: str,
                      interval: str = "5m") -> pd.DataFrame:
        # Phase 5A: same shape as :meth:`history`. Phase 5B will swap
        # this to read from the WebSocket tick cache in Redis.
        # We delegate to history() so all the FIX #20 / interval logic
        # lives in one place. The legacy ``bot.data.intraday_bars``
        # also handles the synthetic option-bar generation for option
        # tradingsymbols (e.g. ``NIFTY26MAY24600CE``); Dhan exposes
        # real option chains via a different endpoint, but until we
        # wire that we delegate option symbols back to the legacy
        # path so the BS synthesis still works.
        from ..instruments.fno import (
            parse_option_tradingsymbol, parse_spread_tradingsymbol,
            parse_iron_condor_tradingsymbol,
        )
        is_synthetic_option = (
            parse_option_tradingsymbol(symbol) is not None
            or parse_spread_tradingsymbol(symbol) is not None
            or parse_iron_condor_tradingsymbol(symbol) is not None
        )
        if is_synthetic_option:
            # Hand off to legacy BS synthesis — the underlying spot bars
            # used by the synthesis will themselves come from this
            # adapter (registry routes them back through us) so the
            # net effect is "Dhan spot, BS-synthesised option."
            from .. import data as _legacy
            return _legacy.intraday_bars(symbol, interval=interval)

        # Equity / index / futures — direct Dhan fetch.
        # Equity strategies want today-only; F&O wants up to 2 days
        # for EMA50 warmup. Match the legacy contract.
        from ..instruments.fno import yfinance_proxy
        is_fno = yfinance_proxy(symbol) is not None
        days = 2 if is_fno else 1
        df = self.history(symbol, days=days, interval=interval)
        if df.empty:
            return df
        if not is_fno:
            # Equity: keep only today's bars.
            today = datetime.now(IST).date()
            df = df[df.index.date == today]
        return df

    # ------------------------------------------------------------------
    # Cold-path: daily / quotes (rarely called)
    # ------------------------------------------------------------------

    def daily_history(self, symbol: str, days: int = 90) -> pd.DataFrame:
        # Dhan's historical_daily_data endpoint is the real impl;
        # for Phase 5A we delegate to yfinance (zero strategies use
        # this on the hot path). Wire when needed.
        from .yfinance_source import YFinanceDataSource
        return YFinanceDataSource().daily_history(symbol, days=days)

    def previous_close(self, symbol: str) -> Optional[float]:
        from .yfinance_source import YFinanceDataSource
        return YFinanceDataSource().previous_close(symbol)

    def todays_open(self, symbol: str) -> Optional[float]:
        from .yfinance_source import YFinanceDataSource
        return YFinanceDataSource().todays_open(symbol)

    def latest_quote(self, symbol: str) -> Optional[Tick]:
        if not self.is_available():
            return None
        inst = resolve_symbol(symbol)
        if inst is None:
            return None
        try:
            assert self._client is not None
            resp = self._client.quote_data({inst.exchange_segment: [int(inst.security_id)]})
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dhan_source] quote_data raised for %s: %s",
                           inst.tradingsymbol, exc)
            return None
        if not isinstance(resp, dict) or resp.get("status") != "success":
            return None
        # Dhan's response shape: {data: {NSE_EQ: {408065: {LTP: 1116.0, volume: ...}}}}
        data = resp.get("data") or {}
        seg_payload = data.get(inst.exchange_segment) or {}
        quote = seg_payload.get(str(inst.security_id)) or seg_payload.get(inst.security_id)
        if not quote:
            return None
        ltp = float(quote.get("LTP") or quote.get("last_price") or 0.0)
        vol = float(quote.get("volume") or quote.get("volume_traded") or 0.0)
        if ltp <= 0:
            return None
        return Tick(
            symbol=symbol,
            ts=datetime.now(IST),
            ltp=ltp,
            volume=vol,
        )
