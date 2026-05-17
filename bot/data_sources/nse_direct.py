"""NSE direct REST client (FIX #35 — multi-source price validation).

NSE exposes a free, no-auth-required REST endpoint at
``www.nseindia.com/api/...`` that returns the same prices the
exchange ticks publish. We use it as a *secondary* cross-check
against yfinance, NOT as the primary feed.

Why a cross-check matters
-------------------------
yfinance scrapes Yahoo Finance, which scrapes intermediate
aggregators. A small but non-zero fraction of intraday bars come
through with stale, zero, or wildly off-by-N% values — the 2026-04-27
"227 No history warnings" burst (see FIX #27) was the worst case in
11 trading days but bursts happen weekly. None of this session's
losses traced back to bad yfinance data, but the user has flagged
data confidence as a recurring concern, so this module gives us a
trust-but-verify pre-trade gate.

Behaviour
---------
* :func:`spot_price` returns the most recent NSE-published last
  price for an index (NIFTY 50, NIFTY BANK) or equity. Cached
  in-memory for ``CACHE_TTL_SECONDS`` (default 30s) per symbol.
* On any failure (HTTP non-200, JSON parse error, network timeout)
  the function returns ``None`` and emits a single-rate-limited
  warning. Callers are expected to fail-open in that case — i.e.,
  proceed with yfinance only.
* :func:`validate_against_yfinance` compares yfinance close vs NSE
  last price and returns a ``(ok, divergence_pct, nse_price)``
  tuple. Returns ``ok=True, divergence_pct=None, nse_price=None``
  when NSE is unreachable so trade entry is not blocked.

Usage
-----
The expected call site is ``Strategy.generate(...)`` right before
returning a BUY/SELL signal::

    ok, div, nse = validate_against_yfinance(
        symbol, yf_close, max_divergence_pct=1.0
    )
    if not ok:
        return self.hold(symbol, yf_close,
                         f"yfinance/NSE divergence {div:.2f}% — refusing trade",
                         self.name)

Limitations
-----------
* NSE rate-limits aggressive callers. We cache for 30s by default
  and the validator is invoked only at signal-emission moments
  (typically <10 calls/session/segment), so we stay well below
  any plausible throttling threshold.
* The endpoint returns *delayed* prices for non-members (most
  retail users); the lag is typically <2 seconds, fine for our
  5-min bar trading frequency. This is also true of yfinance.
* Pre-market and post-market the endpoint returns previous-day
  closes; the validator will then agree with yfinance trivially.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

CACHE_TTL_SECONDS: float = 30.0
HTTP_TIMEOUT_SECONDS: float = 5.0
WARN_DEDUP_SECONDS: float = 300.0  # one warning per failure-mode per 5 min

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 "
        "Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# Map our internal symbol → NSE index API parameter
_INDEX_PARAMS = {
    "NIFTY":      "NIFTY 50",
    "BANKNIFTY":  "NIFTY BANK",
    "FINNIFTY":   "NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NIFTY MIDCAP 50",
}

# Process-wide HTTP session (reuses TCP connection for back-to-back calls).
# ``threading.local`` keeps it isolated per worker thread; the bot is
# single-threaded today but this future-proofs the module.
_session_local = threading.local()
_cache_lock = threading.Lock()
_cache: dict[str, Tuple[float, float]] = {}  # symbol -> (price, fetched_at_unix)
_warn_log: dict[str, float] = {}              # dedup key -> last warned at


def _session() -> requests.Session:
    s = getattr(_session_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(_HEADERS)
        _session_local.session = s
    return s


def _warn_once(key: str, msg: str) -> None:
    """Emit ``msg`` at WARN level no more than once per ``WARN_DEDUP_SECONDS``."""
    now = time.time()
    last = _warn_log.get(key, 0.0)
    if now - last >= WARN_DEDUP_SECONDS:
        logger.warning(msg)
        _warn_log[key] = now


def _fetch_index(symbol: str) -> Optional[float]:
    nse_idx = _INDEX_PARAMS[symbol]
    url = (
        "https://www.nseindia.com/api/equity-stockIndices"
        f"?index={requests.utils.quote(nse_idx)}"
    )
    r = _session().get(url, timeout=HTTP_TIMEOUT_SECONDS)
    if r.status_code != 200:
        _warn_once(
            f"nse-direct-http-{symbol}",
            f"[nse_direct] {symbol}: HTTP {r.status_code} from index API",
        )
        return None
    payload = r.json()
    rows = payload.get("data") or []
    if not rows:
        return None
    return float(rows[0].get("lastPrice", 0.0)) or None


def _fetch_equity(symbol: str) -> Optional[float]:
    url = (
        "https://www.nseindia.com/api/quote-equity"
        f"?symbol={requests.utils.quote(symbol)}"
    )
    r = _session().get(url, timeout=HTTP_TIMEOUT_SECONDS)
    if r.status_code != 200:
        _warn_once(
            f"nse-direct-http-{symbol}",
            f"[nse_direct] {symbol}: HTTP {r.status_code} from equity API",
        )
        return None
    payload = r.json()
    price_info = payload.get("priceInfo") or {}
    last = price_info.get("lastPrice")
    return float(last) if last else None


def spot_price(symbol: str) -> Optional[float]:
    """Return latest NSE-published last price for ``symbol``.

    ``symbol`` is our internal (yfinance-stripped) name — ``NIFTY``,
    ``BANKNIFTY``, ``INFY``, etc. Returns ``None`` on any failure
    (network, parse, rate-limit, unknown symbol). Cached for
    ``CACHE_TTL_SECONDS``.
    """
    now = time.time()
    with _cache_lock:
        cached = _cache.get(symbol)
        if cached and (now - cached[1]) < CACHE_TTL_SECONDS:
            return cached[0]

    try:
        if symbol in _INDEX_PARAMS:
            price = _fetch_index(symbol)
        else:
            price = _fetch_equity(symbol)
    except Exception as exc:
        _warn_once(
            f"nse-direct-exc-{symbol}",
            f"[nse_direct] {symbol}: {type(exc).__name__}: {exc}",
        )
        return None

    if price is None or price <= 0:
        return None

    with _cache_lock:
        _cache[symbol] = (price, now)
    return price


def validate_against_yfinance(
    symbol: str,
    yfinance_close: float,
    *,
    max_divergence_pct: float = 1.0,
) -> Tuple[bool, Optional[float], Optional[float]]:
    """Cross-check ``yfinance_close`` against NSE's last price.

    Returns ``(ok, divergence_pct, nse_price)``:

    * ``ok`` is ``True`` when the two sources agree within
      ``max_divergence_pct`` (or when NSE is unreachable — we fail
      open so a third-party outage doesn't halt the bot).
    * ``divergence_pct`` is the absolute percentage difference, or
      ``None`` if NSE was unreachable.
    * ``nse_price`` is the NSE last price, or ``None`` if unreachable.
    """
    if yfinance_close is None or yfinance_close <= 0:
        return False, None, None

    nse = spot_price(symbol)
    if nse is None:
        # Fail-open. Caller proceeds with yfinance alone.
        return True, None, None

    div = abs(yfinance_close - nse) / nse * 100.0
    return (div <= max_divergence_pct), div, nse


def clear_cache() -> None:
    """Test-only helper — purges the in-memory price cache."""
    with _cache_lock:
        _cache.clear()
    _warn_log.clear()
