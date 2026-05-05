"""NSE/BSE trading-holiday calendar.

Source of truth: the NSE "holiday-master" JSON endpoint at
``https://www.nseindia.com/api/holiday-master?type=trading``. NSE publishes
*per-segment* holiday lists — the keys we care about are:

* ``CM``  — Capital Market (Equity)
* ``FO``  — Futures & Options

These match SEBI's segment definitions exactly; BSE follows the same
calendar (the two exchanges co-ordinate so all-India intraday traders
never see a one-exchange-open / one-exchange-closed day). We therefore
treat NSE's calendar as authoritative for both bots.

The endpoint is gated by NSE's anti-bot WAF, so we hit it like a browser:
GET the homepage first to seed cookies, then the JSON URL with those
cookies + a desktop User-Agent + a Referer header.

Caching:
    Holidays change a few times a year. We cache the parsed-and-merged
    result in Redis under ``nse:holidays:vN`` with a 24-hour TTL plus a
    ``last_refresh`` timestamp so the dashboard can show staleness. If
    NSE is unreachable (rate-limited, WAF block, network) we serve the
    last cached value indefinitely. If even Redis is empty we fall back
    to a hardcoded 2026 calendar so the dashboard never lies about
    "open" on Republic Day.

Public surface (callers should only use these):

* :func:`refresh_holidays`   — force a fresh fetch (CLI / scheduler)
* :func:`get_holidays`       — cached read (dashboard, healthcheck)
* :func:`is_trading_day`     — bool: equity OR F&O on this date?
* :func:`market_status`      — rich dict for one date + one segment
* :func:`next_trading_day`   — first date >= ``after`` that is open
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime, time, timedelta
from typing import Optional

import pytz

from .cache import get_cache
from .logger import logger
from .segment import Segment

IST = pytz.timezone("Asia/Kolkata")

# Cache key. Bumped to v2 on schema changes; v1 was an early prototype.
_CACHE_KEY = "nse:holidays:v2"
# Cache TTL — 24h is plenty: NSE updates this list ~quarterly.
_CACHE_TTL_SEC = 24 * 60 * 60

# NSE endpoints + browser-shaped headers.
_NSE_HOME = "https://www.nseindia.com/"
_NSE_HOLIDAY_API = "https://www.nseindia.com/api/holiday-master?type=trading"
_NSE_REFERER = "https://www.nseindia.com/resources/exchange-communication-holidays"
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36"
)
_REQUEST_TIMEOUT_SEC = 15

# NSE segment-key → our :class:`Segment`.
_NSE_TO_SEG = {"CM": Segment.EQUITY, "FO": Segment.FNO}

# Bootstrap calendar — used ONLY when both NSE and Redis are unavailable.
# Pulled from the NSE 2026 trading-holidays circular (Jan-2026 publication).
# The dashboard shows a clear "stale — bootstrap" warning when this is the
# active source so the user knows to refresh.
#
# Format: { "DD-MMM-YYYY": "Description" }. Both segments share the list
# (NSE 2026 has identical CM/FO holidays).
_BOOTSTRAP_HOLIDAYS_2026 = {
    "26-Jan-2026": "Republic Day",
    "19-Feb-2026": "Chatrapati Shivaji Maharaj Jayanti",
    "03-Mar-2026": "Holi",
    "19-Mar-2026": "Gudi Padwa",
    "26-Mar-2026": "Shri Ram Navami",
    "31-Mar-2026": "Shri Mahavir Jayanti",
    "03-Apr-2026": "Good Friday",
    "14-Apr-2026": "Dr. Baba Saheb Ambedkar Jayanti",
    "01-May-2026": "Maharashtra Day",
    "28-May-2026": "Bakri Id",
    "15-Aug-2026": "Independence Day",
    "07-Sep-2026": "Ganesh Chaturthi",
    "21-Sep-2026": "Eid-E-Milad",
    "02-Oct-2026": "Mahatma Gandhi Jayanti",
    "20-Oct-2026": "Diwali Laxmi Pujan",
    "21-Oct-2026": "Diwali-Balipratipada",
    "25-Nov-2026": "Guru Nanak Jayanti",
    "25-Dec-2026": "Christmas",
}


# ───────────────────────── data shapes ─────────────────────────────────────

@dataclass
class Holiday:
    """One trading-holiday entry."""

    date_iso: str          # YYYY-MM-DD (sortable, JSON-friendly)
    weekday: str           # "Monday", "Tuesday", ...
    description: str       # NSE's human label, e.g. "Republic Day"

    @property
    def d(self) -> date:
        """Parsed :class:`datetime.date`."""
        return datetime.strptime(self.date_iso, "%Y-%m-%d").date()


@dataclass
class HolidayCalendar:
    """A snapshot of all known holidays for both segments."""

    by_segment: dict[str, list[Holiday]]   # {"equity": [...], "fno": [...]}
    last_refresh: str                      # ISO datetime, IST
    source: str                            # "nse" | "bootstrap"

    def to_jsonable(self) -> dict:
        return {
            "by_segment": {
                seg: [asdict(h) for h in lst]
                for seg, lst in self.by_segment.items()
            },
            "last_refresh": self.last_refresh,
            "source": self.source,
        }

    @classmethod
    def from_jsonable(cls, raw: dict) -> "HolidayCalendar":
        by_seg = {
            seg: [Holiday(**h) for h in lst]
            for seg, lst in raw.get("by_segment", {}).items()
        }
        return cls(
            by_segment=by_seg,
            last_refresh=raw.get("last_refresh", ""),
            source=raw.get("source", "unknown"),
        )


# ───────────────────────── NSE fetcher ─────────────────────────────────────

def _fetch_from_nse() -> Optional[dict]:
    """Hit NSE for the live holiday calendar. ``None`` on any failure.

    We intentionally swallow every exception here — the dashboard MUST
    keep rendering even if NSE is down. The caller is responsible for
    falling back to cache or bootstrap.
    """
    try:
        import requests  # local import so the module imports cleanly even
                         # if requests is missing (offline tests, tooling)
    except ImportError:
        logger.warning("[holidays] requests not installed — cannot fetch NSE")
        return None

    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": _NSE_REFERER,
        "Connection": "keep-alive",
    }
    try:
        with requests.Session() as s:
            # Step 1: prime cookies via the homepage. NSE's WAF rejects
            # API requests that come in without a valid `nsit` cookie.
            s.get(_NSE_HOME, headers=headers, timeout=_REQUEST_TIMEOUT_SEC)
            # Step 2: hit the holiday API.
            r = s.get(_NSE_HOLIDAY_API, headers=headers, timeout=_REQUEST_TIMEOUT_SEC)
            r.raise_for_status()
            return r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("[holidays] NSE fetch failed: {}", e)
        return None


def _parse_nse_payload(payload: dict) -> dict[str, list[Holiday]]:
    """Reduce NSE's segment-mega-dict to ``{segment_value: [Holiday, ...]}``.

    Only ``CM`` and ``FO`` segments are extracted. Dates are normalised
    from NSE's "DD-MMM-YYYY" to ISO "YYYY-MM-DD" so date-key lookups in
    the rest of the codebase are O(1) and timezone-independent.
    """
    out: dict[str, list[Holiday]] = {Segment.EQUITY.value: [], Segment.FNO.value: []}
    for nse_key, segment in _NSE_TO_SEG.items():
        rows = payload.get(nse_key) or []
        seen: set[str] = set()
        for row in rows:
            raw_date = row.get("tradingDate")
            if not raw_date:
                continue
            try:
                d = datetime.strptime(raw_date, "%d-%b-%Y").date()
            except ValueError:
                logger.warning("[holidays] unparseable NSE date: {}", raw_date)
                continue
            iso = d.isoformat()
            if iso in seen:           # NSE occasionally double-rows
                continue
            seen.add(iso)
            out[segment.value].append(Holiday(
                date_iso=iso,
                weekday=row.get("weekDay", d.strftime("%A")),
                description=row.get("description", "Trading holiday"),
            ))
        out[segment.value].sort(key=lambda h: h.date_iso)
    return out


def _bootstrap_calendar() -> HolidayCalendar:
    """Hardcoded fallback calendar for when both NSE and Redis are dry."""
    holidays: list[Holiday] = []
    for raw_date, desc in _BOOTSTRAP_HOLIDAYS_2026.items():
        d = datetime.strptime(raw_date, "%d-%b-%Y").date()
        holidays.append(Holiday(
            date_iso=d.isoformat(),
            weekday=d.strftime("%A"),
            description=desc,
        ))
    holidays.sort(key=lambda h: h.date_iso)
    return HolidayCalendar(
        by_segment={
            Segment.EQUITY.value: list(holidays),
            Segment.FNO.value: list(holidays),
        },
        last_refresh=datetime.now(IST).isoformat(),
        source="bootstrap",
    )


# ───────────────────────── public API ──────────────────────────────────────

def refresh_holidays() -> HolidayCalendar:
    """Fetch NSE → cache → return. Falls back gracefully on every failure.

    Order:

    1. Try NSE live. On success, cache and return.
    2. On NSE failure, return last Redis snapshot (any age).
    3. If Redis is empty too, return the hardcoded bootstrap.
    """
    cache = get_cache()
    payload = _fetch_from_nse()
    if payload is not None:
        try:
            by_seg = _parse_nse_payload(payload)
            cal = HolidayCalendar(
                by_segment=by_seg,
                last_refresh=datetime.now(IST).isoformat(),
                source="nse",
            )
            cache.set_json(_CACHE_KEY, cal.to_jsonable(), ttl=_CACHE_TTL_SEC)
            n_eq = len(by_seg.get(Segment.EQUITY.value, []))
            n_fo = len(by_seg.get(Segment.FNO.value, []))
            logger.info("[holidays] refreshed from NSE — equity={} fno={}", n_eq, n_fo)
            return cal
        except Exception as e:  # noqa: BLE001
            logger.warning("[holidays] NSE payload parse failed: {}", e)

    cached = cache.get_json(_CACHE_KEY)
    if cached:
        logger.info("[holidays] using cached snapshot (NSE unreachable)")
        return HolidayCalendar.from_jsonable(cached)

    logger.warning("[holidays] no cache — using BOOTSTRAP calendar")
    return _bootstrap_calendar()


def get_holidays(*, allow_refresh: bool = True) -> HolidayCalendar:
    """Cached read — re-fetches only if the snapshot is missing or expired.

    Set ``allow_refresh=False`` if you're calling this from a hot path
    where a network round-trip would be unacceptable (the executor's
    per-minute tick, for instance). The dashboard and CLI happily
    refresh on cold cache.
    """
    cache = get_cache()
    cached = cache.get_json(_CACHE_KEY)
    if cached:
        return HolidayCalendar.from_jsonable(cached)
    if allow_refresh:
        return refresh_holidays()
    return _bootstrap_calendar()


def is_trading_day(d: date, segment: Segment, *, calendar: Optional[HolidayCalendar] = None) -> bool:
    """``True`` if the segment is open on ``d`` (Mon-Fri AND not a holiday)."""
    if d.weekday() >= 5:                # Sat/Sun
        return False
    cal = calendar or get_holidays(allow_refresh=False)
    iso = d.isoformat()
    return not any(h.date_iso == iso for h in cal.by_segment.get(segment.value, []))


def _holiday_for(d: date, segment: Segment, calendar: HolidayCalendar) -> Optional[Holiday]:
    iso = d.isoformat()
    for h in calendar.by_segment.get(segment.value, []):
        if h.date_iso == iso:
            return h
    return None


def market_status(d: date, segment: Segment, *, calendar: Optional[HolidayCalendar] = None) -> dict:
    """Rich status object for one date × one segment.

    Returns::

        {
            "date":      "2026-04-30",
            "segment":   "equity",
            "is_open":   True,
            "status":    "OPEN" | "WEEKEND" | "HOLIDAY",
            "reason":    "" | "Saturday" | "Republic Day",
            "weekday":   "Thursday",
            "open":      "09:15",      # only when status == OPEN
            "close":     "15:30",      # only when status == OPEN
            "source":    "nse" | "bootstrap" | "stale",
            "last_refresh": "2026-04-30T08:00:00+05:30",
        }
    """
    cal = calendar or get_holidays(allow_refresh=False)
    weekday_name = d.strftime("%A")
    base = {
        "date": d.isoformat(),
        "segment": segment.value,
        "weekday": weekday_name,
        "source": cal.source,
        "last_refresh": cal.last_refresh,
    }
    if d.weekday() >= 5:
        return {**base, "is_open": False, "status": "WEEKEND", "reason": weekday_name}
    h = _holiday_for(d, segment, cal)
    if h is not None:
        return {**base, "is_open": False, "status": "HOLIDAY", "reason": h.description}
    return {
        **base,
        "is_open": True,
        "status": "OPEN",
        "reason": "",
        "open": "09:15",
        "close": "15:30",
    }


def next_trading_day(after: date, segment: Segment, *, max_lookahead_days: int = 30) -> Optional[date]:
    """First trading day strictly AFTER ``after``. Returns ``None`` if none found
    in the lookahead window (defensive — a 30-day stretch with no trading
    means our calendar is broken)."""
    cal = get_holidays(allow_refresh=False)
    d = after + timedelta(days=1)
    for _ in range(max_lookahead_days):
        if is_trading_day(d, segment, calendar=cal):
            return d
        d += timedelta(days=1)
    return None


def is_market_open_now(segment: Segment) -> bool:
    """Is the market for ``segment`` open RIGHT NOW (IST)?

    Combines weekday + holiday + intraday-window. Used by the dashboard
    so the "Market" pill correctly shows CLOSED on Republic Day even at
    11am IST (which the legacy :func:`bot.data.is_market_open` got wrong).
    """
    now = datetime.now(IST)
    today = now.date()
    if not is_trading_day(today, segment):
        return False
    return time(9, 15) <= now.time() <= time(15, 30)
