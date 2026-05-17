"""Dhan symbol resolver — yfinance-style name → (security_id, segment).

Dhan's REST and WebSocket APIs reference instruments by a numeric
``security_id`` plus an ``exchange_segment`` (NSE_EQ, NSE_INDEX, etc).
The bot internally uses yfinance-style symbols (``^NSEI``, ``^NSEBANK``,
``INFY``, ``NIFTY26MAYFUT``, ``NIFTY26MAY24600CE``); this module is the
single point of translation between the two.

Source of truth: Dhan's daily-published instrument-master CSV at
``https://images.dhan.co/api-data/api-scrip-master.csv`` (no auth
required, ~31MB, ~85K rows). We download once, parse the rows we
care about, and cache the resulting mapping in Redis with a 24h TTL.
The CSV refreshes nightly so a 24h cache is the right horizon — we
re-fetch the next morning to pick up new option strikes / new listings.

Schema (relevant columns)::

    SEM_EXM_EXCH_ID         exchange code: NSE / BSE / MCX / etc.
    SEM_SEGMENT             C (cash) / D (derivatives) / I (index)
    SEM_SMST_SECURITY_ID    numeric instrument ID (what Dhan calls "security_id")
    SEM_INSTRUMENT_NAME     EQUITY / INDEX / FUTIDX / OPTIDX / FUTSTK / OPTSTK / etc.
    SEM_TRADING_SYMBOL      Dhan's canonical name (e.g. "INFY", "NIFTY 50")
    SEM_LOT_UNITS           lot size for F&O
    SEM_EXPIRY_DATE         ISO date for F&O contracts
    SEM_STRIKE_PRICE        strike for options
    SEM_OPTION_TYPE         CE / PE for options

Symbol-translation rules (yfinance internal → Dhan):

    ^NSEI                   →  NSE  / I / "Nifty 50"            / INDEX
    ^NSEBANK                →  NSE  / I / "Nifty Bank"          / INDEX
    NIFTY                   →  NSE  / I / "Nifty 50"            / INDEX (spot proxy)
    BANKNIFTY               →  NSE  / I / "Nifty Bank"          / INDEX (spot proxy)
    INFY                    →  NSE  / E / "INFY"                / EQUITY
    HDFCBANK                →  NSE  / E / "HDFCBANK"            / EQUITY
    NIFTY26MAYFUT           →  NSE  / D / "NIFTY-26-May-2026"   / FUTIDX
    NIFTY26MAY24600CE       →  NSE  / D / "NIFTY-26-May-..-CE"  / OPTIDX
"""
from __future__ import annotations

import csv
import io
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Tuple

import pytz
import requests

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DHAN_INSTRUMENTS_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
DHAN_INSTRUMENTS_CACHE_KEY = "dhan:instruments:v1"
DHAN_INSTRUMENTS_TTL_SECONDS = 24 * 3600
DHAN_INSTRUMENTS_TIMEOUT_SECONDS = 30  # the file is ~31MB

# Hand-curated proxy table for the ~6 symbols our F&O strategies trade.
# This avoids loading the full 85K-row CSV when callers only need the
# common-case lookups. The full CSV is still pulled on first use for
# equity-option resolution and any new symbols.
#
# Note: Dhan's CSV puts the human-readable name in SEM_CUSTOM_SYMBOL
# ("Nifty 50") and the API-callable name in SEM_TRADING_SYMBOL
# ("NIFTY"). We index by SEM_TRADING_SYMBOL because that's the field
# the dhanhq SDK accepts in WebSocket subscriptions and quote_data.
_INDEX_PROXY: Dict[str, Tuple[str, str]] = {
    # yfinance ticker → (Dhan tradingsymbol, expected segment)
    "^NSEI":      ("NIFTY",       "I"),
    "^NSEBANK":   ("BANKNIFTY",   "I"),
    "NIFTY":      ("NIFTY",       "I"),
    "BANKNIFTY":  ("BANKNIFTY",   "I"),
    "FINNIFTY":   ("FINNIFTY",    "I"),
    "MIDCPNIFTY": ("MIDCPNIFTY",  "I"),
}

# Map Dhan segment code → exchange_segment string used by the dhanhq SDK
# for both REST historical-data calls and WebSocket subscriptions.
_SEGMENT_TO_EXCHANGE: Dict[str, str] = {
    "I": "IDX_I",     # INDEX (NIFTY 50, BANK NIFTY, etc.)
    "E": "NSE_EQ",    # EQUITY cash
    "C": "NSE_EQ",    # CASH segment (synonymous with E in NSE)
    "D": "NSE_FNO",   # F&O DERIVATIVES (futures + options)
}


@dataclass(frozen=True)
class DhanInstrument:
    """One row of the Dhan instrument master, normalised."""
    security_id: int
    tradingsymbol: str
    exchange: str
    segment: str
    instrument_name: str
    lot_size: int
    expiry: Optional[str]
    strike: Optional[float]
    option_type: Optional[str]

    @property
    def exchange_segment(self) -> str:
        """The string the dhanhq SDK accepts (NSE_EQ / NSE_FNO / IDX_I)."""
        return _SEGMENT_TO_EXCHANGE.get(self.segment, "NSE_EQ")


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_instruments_by_symbol: Dict[str, DhanInstrument] = {}
_instruments_loaded_at: Optional[datetime] = None


def _load_csv_from_dhan() -> Dict[str, DhanInstrument]:
    """Download the 31MB CSV and parse the rows we care about.

    We don't keep the entire 85K-row instrument master in memory —
    only the NSE rows that match an EQUITY, INDEX, FUTIDX or OPTIDX
    instrument. That trims ~85K → ~5K rows and stays under 1MB
    in-process.
    """
    logger.info("[dhan_resolver] downloading instrument master from %s", DHAN_INSTRUMENTS_URL)
    resp = requests.get(DHAN_INSTRUMENTS_URL, timeout=DHAN_INSTRUMENTS_TIMEOUT_SECONDS)
    resp.raise_for_status()
    text = resp.content.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    out: Dict[str, DhanInstrument] = {}
    kept = skipped = 0
    interesting_instruments = {
        "EQUITY", "INDEX",
        "FUTIDX", "OPTIDX",   # NIFTY/BANKNIFTY futures + options
        "FUTSTK", "OPTSTK",   # single-stock F&O
    }

    for row in reader:
        if row.get("SEM_EXM_EXCH_ID") != "NSE":
            skipped += 1
            continue
        instrument_name = (row.get("SEM_INSTRUMENT_NAME") or "").strip()
        if instrument_name not in interesting_instruments:
            skipped += 1
            continue
        try:
            sec_id = int(row["SEM_SMST_SECURITY_ID"])
        except (ValueError, KeyError):
            skipped += 1
            continue

        ts = (row.get("SEM_TRADING_SYMBOL") or "").strip()
        if not ts:
            skipped += 1
            continue

        try:
            lot = int(float(row.get("SEM_LOT_UNITS") or "1"))
        except (ValueError, TypeError):
            lot = 1
        try:
            strike_val = float(row.get("SEM_STRIKE_PRICE") or "0") or None
        except (ValueError, TypeError):
            strike_val = None

        inst = DhanInstrument(
            security_id=sec_id,
            tradingsymbol=ts,
            exchange="NSE",
            segment=(row.get("SEM_SEGMENT") or "").strip(),
            instrument_name=instrument_name,
            lot_size=lot,
            expiry=(row.get("SEM_EXPIRY_DATE") or "").strip() or None,
            strike=strike_val,
            option_type=(row.get("SEM_OPTION_TYPE") or "").strip() or None,
        )
        # Index by tradingsymbol (case-insensitive). Dhan ships duplicates
        # for some indices (one per exchange variant) — keep the first.
        key = ts.upper()
        if key not in out:
            out[key] = inst
            kept += 1

    logger.info("[dhan_resolver] loaded %d NSE instruments (skipped %d non-NSE / non-equity-index rows)",
                kept, skipped)
    return out


def _ensure_loaded(force: bool = False) -> None:
    """Populate the in-memory instrument index, with Redis 24h cache."""
    global _instruments_loaded_at, _instruments_by_symbol

    with _lock:
        if not force and _instruments_by_symbol:
            return

        # Try Redis cache first.
        try:
            from ..cache import get_cache
            cache = get_cache()
            cached = cache.get_json(DHAN_INSTRUMENTS_CACHE_KEY)
        except Exception:  # noqa: BLE001
            cached = None

        if cached and not force:
            try:
                _instruments_by_symbol = {
                    k.upper(): DhanInstrument(**v) for k, v in cached.items()
                }
                _instruments_loaded_at = datetime.now(IST)
                logger.debug("[dhan_resolver] loaded %d instruments from Redis cache",
                             len(_instruments_by_symbol))
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("[dhan_resolver] cache deserialise failed: %s — re-fetching", exc)

        # Cache miss / forced refresh — pull the CSV.
        try:
            fresh = _load_csv_from_dhan()
        except Exception as exc:  # noqa: BLE001
            logger.error("[dhan_resolver] CSV fetch failed: %s — symbol resolution unavailable", exc)
            return  # keep _instruments_by_symbol empty; resolve_symbol returns None

        _instruments_by_symbol = fresh
        _instruments_loaded_at = datetime.now(IST)

        # Persist to Redis for the next session.
        try:
            from ..cache import get_cache
            cache = get_cache()
            cache.set_json(
                DHAN_INSTRUMENTS_CACHE_KEY,
                {k: v.__dict__ for k, v in fresh.items()},
                ttl=DHAN_INSTRUMENTS_TTL_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dhan_resolver] failed to write cache: %s", exc)


def resolve_symbol(yfinance_symbol: str) -> Optional[DhanInstrument]:
    """Translate a yfinance-style symbol to its Dhan instrument record.

    Returns ``None`` if the symbol is unknown; callers should fall
    back to the next data source in that case.
    """
    if not yfinance_symbol:
        return None

    # Indices first — fast path via the hard-coded proxy table.
    if yfinance_symbol in _INDEX_PROXY:
        target_name, target_segment = _INDEX_PROXY[yfinance_symbol]
        _ensure_loaded()
        # Try exact tradingsymbol match (case-insensitive).
        inst = _instruments_by_symbol.get(target_name.upper())
        if inst and inst.segment == target_segment:
            return inst
        # Slow scan: some Dhan rows have variant names ("NIFTY", "Nifty 50",
        # "NIFTY50"); we accept any I-segment row whose tradingsymbol
        # collapses to a known nickname.
        wanted = re.sub(r"\W+", "", target_name).upper()
        for cand in _instruments_by_symbol.values():
            if cand.segment != target_segment:
                continue
            collapsed = re.sub(r"\W+", "", cand.tradingsymbol).upper()
            if collapsed == wanted:
                return cand
        return None

    # F&O futures: NIFTY26MAYFUT etc. Pattern: <SYM><YY><MMM>FUT
    fut_match = re.match(r"^([A-Z]+)(\d{2})([A-Z]{3})FUT$", yfinance_symbol.upper())
    if fut_match:
        # Dhan tradingsymbol shape is "NIFTY-DD-Mmm-YYYY-FUT" (varies).
        # We do a fuzzy contains-match on the underlying + month token.
        underlying, yy, mmm = fut_match.groups()
        _ensure_loaded()
        for cand in _instruments_by_symbol.values():
            if cand.instrument_name not in {"FUTIDX", "FUTSTK"}:
                continue
            ts_upper = cand.tradingsymbol.upper()
            if (underlying in ts_upper and mmm in ts_upper and yy in ts_upper
                    and "FUT" in ts_upper):
                return cand
        return None

    # F&O options: NIFTY26MAY24600CE etc.
    opt_match = re.match(
        r"^([A-Z]+)(\d{2})([A-Z]{3})(\d+)(CE|PE)$", yfinance_symbol.upper(),
    )
    if opt_match:
        underlying, yy, mmm, strike_str, opt_type = opt_match.groups()
        wanted_strike = float(strike_str)
        _ensure_loaded()
        for cand in _instruments_by_symbol.values():
            if cand.instrument_name not in {"OPTIDX", "OPTSTK"}:
                continue
            if cand.option_type != opt_type:
                continue
            if cand.strike is None or abs(cand.strike - wanted_strike) > 0.01:
                continue
            ts_upper = cand.tradingsymbol.upper()
            if underlying in ts_upper and mmm in ts_upper and yy in ts_upper:
                return cand
        return None

    # Otherwise: assume it's a plain NSE equity tradingsymbol.
    _ensure_loaded()
    inst = _instruments_by_symbol.get(yfinance_symbol.upper())
    if inst and inst.instrument_name == "EQUITY":
        return inst
    return None


def warm_cache() -> int:
    """Force-load the instrument master. Called from preflight.

    Returns the number of instruments loaded.
    """
    _ensure_loaded(force=True)
    return len(_instruments_by_symbol)


def clear_cache() -> None:
    """Test-only — purges in-memory + Redis cache."""
    global _instruments_by_symbol, _instruments_loaded_at
    with _lock:
        _instruments_by_symbol = {}
        _instruments_loaded_at = None
    try:
        from ..cache import get_cache
        get_cache().delete(DHAN_INSTRUMENTS_CACHE_KEY)
    except Exception:  # noqa: BLE001
        pass
