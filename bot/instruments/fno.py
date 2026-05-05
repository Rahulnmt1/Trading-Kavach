"""F&O instrument definitions and resolvers.

Phase 2 covers **index futures only** (NIFTY, BANKNIFTY, FINNIFTY).
Phase 3 will extend this with option-chain resolvers (strike + CE/PE).

This module is the single source of truth for:

* :func:`lot_size`       — contract lot size for an underlying.
* :func:`margin_pct`     — paper-mode SPAN+exposure margin as a fraction
                           of contract value, used by :class:`PaperBroker`
                           to debit margin on entry instead of full notional.
* :func:`current_expiry` — next monthly expiry (last Thursday of the month,
                           rolled to the following month if today >= that
                           Thursday). Phase 2 trades the monthly contract;
                           weekly options come in Phase 3.
* :func:`tradingsymbol`  — Zerodha-style ``<UNDERLYING><yymmm>FUT`` (e.g.
                           ``NIFTY26MAYFUT``).
* :func:`resolve_underlying` — given an underlying name from the F&O
                           watchlist, return the list of concrete
                           tradingsymbols the executor should subscribe
                           to + trade. For Phase 2 this is exactly one
                           futures contract per underlying.
* :func:`yfinance_proxy` — paper-mode fallback: map a futures
                           tradingsymbol to a yfinance index ticker so
                           we can use the underlying spot as a price
                           proxy until Kite Connect (Phase 5) is wired
                           in for real futures bars.

LOT SIZES — VERIFY BEFORE LIVE TRADING.
SEBI revises F&O lot sizes periodically (most recently Nov 2024 and
Apr 2025). The table here reflects the post-2025 sizes. Before going
live, cross-check against the latest NSE bhavcopy at
https://www.nseindia.com/products-services/equity-derivatives-list-underlyings.
The healthcheck's "Fee schedule" check is the analogue for fees; a
similar lot-size audit job is on the roadmap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Literal, Optional

from ..logger import logger

OptionType = Literal["CE", "PE"]

# ─── Lot-size table ─────────────────────────────────────────────────────────
#
# Sourced from NSE F&O contract specifications. ALWAYS VERIFY before going
# live — SEBI raised these in 2024-2025 and may again. The tickers below
# match Zerodha's underlying naming (no exchange suffix).
LOT_SIZES: Dict[str, int] = {
    # ── Index futures/options (Phase 2/3/4) ─────────────────────────
    "NIFTY":      75,    # Index futures — was 25 pre-Nov-2024
    "BANKNIFTY":  30,    # Index futures — was 15 pre-Nov-2024
    "FINNIFTY":   65,    # Index futures — was 40 pre-Nov-2024
    "MIDCPNIFTY": 120,
    "SENSEX":     20,    # BSE index futures

    # ── Stock futures/options (Phase 4.5) ───────────────────────────
    # Top-traded F&O stocks by liquidity. Sourced from NSE F&O master
    # (Apr 2026) — verify with the broker before live trading; SEBI
    # adjusts these every quarter.
    "RELIANCE":  500,
    "INFY":      400,
    "HDFCBANK":  550,
    "ICICIBANK": 700,
    "TCS":       175,
    "SBIN":     1500,
    "AXISBANK":  625,
    "KOTAKBANK": 400,
    "ITC":      1600,
    "LT":        300,
}


# ─── Margin model ───────────────────────────────────────────────────────────
#
# In paper mode we need a margin multiplier so the broker debits a small
# fraction of contract value (rather than the full notional which would
# exceed any reasonable starting capital). 5% is roughly Zerodha's intraday
# SPAN+exposure margin for index futures with intraday leverage. Without
# leverage (overnight / NRML) it's ~12-15%. The default below is the
# intraday value; the user can override per-segment via cfg if they want
# a more conservative simulation.
DEFAULT_MARGIN_PCT_INDEX_FUT = 0.05


def _lookup_underlying(symbol: str) -> str:
    """Map ``symbol`` (bare underlying OR tradingsymbol) to a known underlying.

    Accepts both ``"NIFTY"`` and ``"NIFTY26MAYFUT"``. Returns the matched
    underlying name. Raises ``KeyError`` if no known prefix matches —
    callers (lot_size / margin_pct) translate that into a user-friendly
    rejection in the risk manager.
    """
    s = symbol.upper()
    if s in LOT_SIZES:
        return s
    for u in LOT_SIZES:
        if s.startswith(u):
            return u
    raise KeyError(
        f"unknown F&O underlying {symbol!r} — expected one of "
        f"{sorted(LOT_SIZES.keys())} (or a tradingsymbol prefixed with one)"
    )


def lot_size(symbol: str) -> int:
    """Return the contract lot size for ``symbol`` (e.g. "NIFTY" -> 75).

    Accepts both bare underlyings ("NIFTY") and full tradingsymbols
    ("NIFTY26MAYFUT") so the risk manager can pass the same string the
    strategy emitted without round-tripping through a resolver.

    Raises ``KeyError`` if the symbol isn't in :data:`LOT_SIZES`. The
    risk manager catches this and rejects the signal with a clear
    message so a typo in the F&O watchlist doesn't silently size to 1
    share.
    """
    return LOT_SIZES[_lookup_underlying(symbol)]


def margin_pct(symbol: str) -> float:
    """Margin as fraction of contract value for paper-mode sizing.

    Accepts both bare underlyings and full tradingsymbols.

    All five underlyings in :data:`LOT_SIZES` are index futures, which
    share the same margin band — but stock futures (Phase 2.5) can have
    significantly higher margin requirements (15-25% with leverage), so
    we keep this as a per-underlying lookup with a sensible default.
    """
    _lookup_underlying(symbol)  # validate it's known
    # All current entries are index futures; stock futures will get their
    # own table when Phase 2.5 lands.
    return DEFAULT_MARGIN_PCT_INDEX_FUT


# ─── Expiry resolution ──────────────────────────────────────────────────────
#
# All Indian index F&O contracts expire on the LAST THURSDAY of the month
# (or the preceding business day if Thursday is a holiday — we don't
# adjust for holidays here in Phase 2; the executor's broker-side check
# will catch the rare edge case at order placement).

_MONTH_NAMES = [
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
]


def _last_thursday(year: int, month: int) -> date:
    """Return the date of the last Thursday in (year, month)."""
    # Walk back from the end of the month.
    if month == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, month + 1, 1)
    last_day = next_first - timedelta(days=1)
    # weekday(): Mon=0 ... Thu=3 ... Sun=6
    offset = (last_day.weekday() - 3) % 7
    return last_day - timedelta(days=offset)


# ─── Rollover buffer ────────────────────────────────────────────────────────
#
# Real futures traders roll OUT of the expiring contract a day or two
# BEFORE expiry, because:
#
#   1. Liquidity in the dying contract dries up — bid/ask widens, fills
#      get worse, partial-fills become common.
#   2. On expiry day the dying contract is volatile in the last hour as
#      the basis collapses to spot.
#
# Zerodha/Kite's own watchlists flip the "current month" pointer to the
# next contract typically 1-2 trading days before expiry. We mirror that
# with a configurable buffer.
#
# Set via :func:`set_rollover_buffer_days` from ``cli.py`` at bot start
# (driven by ``config.yaml::fno.rollover_buffer_days``). Default is 2,
# which matches the "standard professional" practice. Set to 0 to keep
# the legacy behaviour (roll only AFTER expiry day).

_DEFAULT_ROLLOVER_BUFFER_DAYS = 2


def set_rollover_buffer_days(n: int) -> None:
    """Set the module-wide rollover buffer (called once at startup).

    ``n`` must be ``>= 0``. The buffer is the number of calendar days
    BEFORE monthly expiry on which the resolver flips to the next
    contract:

      * ``n=0`` → roll exactly on expiry day (Thu → MAY contract)
      * ``n=1`` → roll on T-1 (Wed → MAY)
      * ``n=2`` → roll on T-2 (Tue → MAY)  ← recommended default

    Higher values exit the dying contract sooner — safer but you miss a
    little theta/basis on the expiring month.
    """
    global _DEFAULT_ROLLOVER_BUFFER_DAYS
    if n < 0:
        raise ValueError(f"rollover buffer must be >= 0, got {n}")
    _DEFAULT_ROLLOVER_BUFFER_DAYS = int(n)


def get_rollover_buffer_days() -> int:
    """Inspector for the current buffer (used by healthcheck/dashboard)."""
    return _DEFAULT_ROLLOVER_BUFFER_DAYS


def current_expiry(today: Optional[date] = None,
                   buffer_days: Optional[int] = None) -> date:
    """Return the **monthly** F&O expiry currently being traded.

    With the rollover buffer applied: if ``today`` is within
    ``buffer_days`` calendar days of this month's last-Thursday expiry
    (inclusive), return **next** month's expiry — mimicking how active
    futures traders roll positions to the next contract a day or two
    before the dying contract's liquidity collapses.

    Args:
      today: Override for date.today() (used by tests).
      buffer_days: Override the module-default buffer
        (set via :func:`set_rollover_buffer_days`). Use ``0`` to disable
        the buffer entirely (legacy behaviour).

    Examples (April 2026, expiry = Thu Apr 30):

      buffer=2 (default):
        * Mon Apr 27  → APR (T-3, no roll)
        * Tue Apr 28  → MAY (T-2, ROLL)
        * Thu Apr 30  → MAY (already rolled)
        * Fri May  1  → MAY

      buffer=0 (no advance roll):
        * Wed Apr 29  → APR
        * Thu Apr 30  → MAY (rolls exactly on expiry day)
        * Fri May  1  → MAY
    """
    today = today or date.today()
    buf = _DEFAULT_ROLLOVER_BUFFER_DAYS if buffer_days is None else int(buffer_days)
    this_month_exp = _last_thursday(today.year, today.month)
    # Roll when (expiry - today) <= buf, i.e. ON the day that is `buf`
    # calendar days before expiry — and every day thereafter.
    # Equivalently: don't roll while today + buf < expiry.
    if today + timedelta(days=buf) < this_month_exp:
        return this_month_exp
    if today.month == 12:
        return _last_thursday(today.year + 1, 1)
    return _last_thursday(today.year, today.month + 1)


# ─── Tradingsymbol formatting ───────────────────────────────────────────────


def tradingsymbol(underlying: str, expiry: Optional[date] = None) -> str:
    """Format the Zerodha-style monthly futures tradingsymbol.

    Example: ``tradingsymbol("NIFTY", date(2026, 5, 28))`` → ``"NIFTY26MAYFUT"``.

    NSE / Zerodha use the **2-digit year + 3-letter month** convention for
    monthly contracts (the weekly format adds a ``Wn`` segment which we
    don't need here in Phase 2).
    """
    expiry = expiry or current_expiry()
    yy = f"{expiry.year % 100:02d}"
    mmm = _MONTH_NAMES[expiry.month - 1]
    return f"{underlying.upper()}{yy}{mmm}FUT"


@dataclass(frozen=True)
class FuturesInstrument:
    """Resolved view of an F&O futures contract.

    Frozen so it can live in a set / be a dict key. Construction goes
    through :func:`resolve_futures` so all fields are populated
    consistently.
    """
    underlying: str       # "NIFTY"
    tradingsymbol: str    # "NIFTY26MAYFUT"
    expiry: date          # 2026-05-28
    lot_size: int         # 75
    margin_pct: float     # 0.05

    def contract_value(self, price: float) -> float:
        """Total contract notional at ``price`` = lot_size × price (one lot)."""
        return self.lot_size * price

    def margin_per_lot(self, price: float) -> float:
        """Margin required for one lot at ``price``.

        For paper mode this is what we debit cash by on entry. Real
        SPAN+exposure margin from Zerodha can be higher or lower
        depending on volatility; the broker-side calc is authoritative
        in live mode.
        """
        return self.contract_value(price) * self.margin_pct


def resolve_futures(underlying: str,
                    today: Optional[date] = None) -> FuturesInstrument:
    """Resolve an F&O underlying into the current monthly futures contract."""
    u = underlying.upper()
    if u not in LOT_SIZES:
        raise KeyError(f"unknown F&O underlying {u!r} — expected one of "
                       f"{sorted(LOT_SIZES.keys())}")
    exp = current_expiry(today)
    return FuturesInstrument(
        underlying=u,
        tradingsymbol=tradingsymbol(u, exp),
        expiry=exp,
        lot_size=LOT_SIZES[u],
        margin_pct=margin_pct(u),
    )


def resolve_underlying(underlying: str,
                       today: Optional[date] = None) -> List[FuturesInstrument]:
    """Expand an F&O watchlist entry into concrete tradeable instruments.

    Returns a list because Phase 3+ (options) will return many strikes
    per underlying. In Phase 2 we return exactly one futures contract.

    Unknown underlyings emit a warning and return an empty list — the
    F&O bot will then skip that watchlist entry rather than crashing.
    """
    try:
        return [resolve_futures(underlying, today)]
    except KeyError as e:
        logger.warning("[fno] {}", e)
        return []


# ─── yfinance proxy mapping ─────────────────────────────────────────────────
#
# Paper-mode fallback. Until Kite Connect is wired in for real futures
# bars (Phase 5), we approximate the futures price using the underlying
# index spot from yfinance. This has a small basis — futures lead/lag
# spot by 0.1-0.5% typically — but is good enough for paper-validating
# strategy logic and the SL/TP/trailing flows.

_YFINANCE_INDEX: Dict[str, str] = {
    # Underlying → yfinance ticker. The bot calls this by underlying NAME,
    # not by full tradingsymbol, so a single mapping per index works for
    # all months' contracts.
    "NIFTY":      "^NSEI",
    "BANKNIFTY":  "^NSEBANK",
    "FINNIFTY":   "^CNXFIN",     # may be unreliable on yfinance — see note below
    "MIDCPNIFTY": "^CNXMIDCAP",  # may be unreliable
    "SENSEX":     "^BSESN",
}


def yfinance_proxy(tradingsymbol_or_underlying: str) -> Optional[str]:
    """Map a futures tradingsymbol or bare underlying to its yfinance ticker.

    Accepts BOTH formats so the data layer can be agnostic about which it
    holds:

    * ``"NIFTY"``         → ``"^NSEI"``
    * ``"NIFTY26MAYFUT"`` → ``"^NSEI"``  (strips suffix)

    Returns ``None`` for unknown underlyings; the caller should fall back
    to whatever default behaviour is appropriate (typically: skip the
    symbol with a warning).

    CRITICAL — does NOT match synthetic option / spread / iron-condor
    tradingsymbols. Those are SYNTHETIC instruments whose price is
    derived from the underlying via Black-Scholes — they are NOT the
    same as the underlying spot. Returning ``^NSEI`` for a NIFTY put
    spread would cause the data layer to mark the spread at the NIFTY
    spot price (~24,000), producing astronomical fake P&L (the
    2026-04-30 ``-₹8.3M`` paper-bot incident). The 2026-04-30 fix
    explicitly rejects these symbols so the data layer's synthetic
    pricing path (BS via :func:`bot.data.intraday_bars`) is used
    instead.

    NOTE on FINNIFTY / MIDCPNIFTY: yfinance's coverage of these is
    spotty. If you find empty bars, switch the F&O watchlist to NIFTY
    and BANKNIFTY only until live broker bars are wired in.
    """
    s = tradingsymbol_or_underlying.upper()

    # Reject synthetic instruments first — these MUST go through the
    # Black-Scholes synthesis path in `bot.data.intraday_bars`, never
    # through yfinance.  We use the strict parsers below (rather than
    # naive suffix matching) so we don't accidentally reject any real
    # NSE equity that happens to end in "CE" / "PE" / "IC".
    if (parse_option_tradingsymbol(s) is not None
            or parse_spread_tradingsymbol(s) is not None
            or parse_iron_condor_tradingsymbol(s) is not None):
        return None

    # Try direct match first.
    if s in _YFINANCE_INDEX:
        return _YFINANCE_INDEX[s]
    # Strip a "<yy><mmm>FUT" suffix if present.
    for u in _YFINANCE_INDEX:
        if s.startswith(u):
            return _YFINANCE_INDEX[u]
    return None


def underlying_from_tradingsymbol(s: str) -> str:
    """Inverse of :func:`tradingsymbol` — given ``"NIFTY26MAYFUT"`` return ``"NIFTY"``.

    Also handles option tradingsymbols (``"NIFTY26MAY24600CE"`` → ``"NIFTY"``).

    Falls back to the input if no known underlying prefix matches. Used
    by the executor when it has a tradingsymbol from the broker side and
    needs to look up the lot size / margin from this module.
    """
    s = s.upper()
    for u in LOT_SIZES:
        if s.startswith(u):
            return u
    return s


# ─── Phase 3: Options ───────────────────────────────────────────────────────
#
# Strike-step rounding rules (NSE):
#   NIFTY      strikes every 50 points (24500, 24550, ...)
#   BANKNIFTY  strikes every 100 points (53000, 53100, ...)
#   FINNIFTY   strikes every 50 points
#   MIDCPNIFTY strikes every 25 points
#   SENSEX     strikes every 100 points
#
# Lot sizes for OPTIONS are the same as for FUTURES on the same
# underlying (see :data:`LOT_SIZES`). NSE / SEBI raised both
# simultaneously in the Nov-2024 / Apr-2025 revisions.

STRIKE_STEPS: Dict[str, int] = {
    # ── Index ───────────────────────────────────────────────────────
    "NIFTY":      50,
    "BANKNIFTY":  100,
    "FINNIFTY":   50,
    "MIDCPNIFTY": 25,
    "SENSEX":     100,

    # ── Stock options ───────────────────────────────────────────────
    # NSE quotes stock-option strikes in ₹2.50, ₹5, ₹10, or ₹20 grids
    # depending on the underlying's price tier. The values below are
    # current as of Apr 2026; live mode should fall back to the actual
    # strikes returned by ``kite.instruments()`` rather than these.
    "RELIANCE":  20,    # ~₹2,800 — 20-rupee strikes
    "INFY":      10,    # ~₹1,500
    "HDFCBANK":  10,    # ~₹1,650
    "ICICIBANK": 10,    # ~₹1,200
    "TCS":       20,    # ~₹3,800
    "SBIN":      10,    # ~₹820
    "AXISBANK":  10,    # ~₹1,150
    "KOTAKBANK": 10,    # ~₹1,750
    "ITC":        5,    # ~₹450 — 5-rupee strikes
    "LT":        20,    # ~₹3,500
}


def strike_step(underlying: str) -> int:
    """Strike-spacing for ``underlying``. Defaults to 50 if unknown."""
    return STRIKE_STEPS.get(underlying.upper(), 50)


def atm_strike(underlying: str, spot: float) -> int:
    """Round ``spot`` to the nearest tradeable strike for ``underlying``.

    >>> atm_strike("NIFTY", 24617.40)
    24600
    >>> atm_strike("BANKNIFTY", 53082.0)
    53100
    """
    step = strike_step(underlying)
    return int(round(spot / step) * step)


def option_tradingsymbol(underlying: str, expiry: date,
                         strike: int, opt_type: OptionType) -> str:
    """Format an NSE-style monthly option tradingsymbol.

    >>> option_tradingsymbol("NIFTY", date(2026, 5, 28), 24600, "CE")
    'NIFTY26MAY24600CE'

    Note: NSE's *weekly* option format inserts a numeric week marker,
    which Phase 3 deliberately does not handle — we only trade the
    monthly contract here.
    """
    o = opt_type.upper()
    if o not in ("CE", "PE"):
        raise ValueError(f"opt_type must be 'CE' or 'PE', got {opt_type!r}")
    yy = f"{expiry.year % 100:02d}"
    mmm = _MONTH_NAMES[expiry.month - 1]
    return f"{underlying.upper()}{yy}{mmm}{int(strike)}{o}"


# Regex to parse a monthly option tradingsymbol back into its components.
# Anchored on opt-type so a stray "NIFTY26MAYFUT" can never match this
# pattern (FUT lacks the digits + CE/PE suffix).
_OPTION_RE = re.compile(
    r"^(?P<underlying>[A-Z]+?)"        # NIFTY / BANKNIFTY / ...
    r"(?P<yy>\d{2})"                   # 26
    r"(?P<mmm>[A-Z]{3})"               # MAY
    r"(?P<strike>\d+)"                 # 24600
    r"(?P<opt>CE|PE)$"                 # CE / PE
)


def parse_option_tradingsymbol(ts: str) -> Optional[dict]:
    """Inverse of :func:`option_tradingsymbol`.

    Returns ``None`` if ``ts`` doesn't match the monthly option format.
    Returns a dict with keys ``underlying``, ``expiry`` (date),
    ``strike`` (int), ``opt_type`` ("CE" or "PE") on success.

    >>> r = parse_option_tradingsymbol("NIFTY26MAY24600CE")
    >>> r["underlying"], r["strike"], r["opt_type"]
    ('NIFTY', 24600, 'CE')
    """
    m = _OPTION_RE.match(ts.upper())
    if not m:
        return None
    underlying = m.group("underlying")
    if underlying not in LOT_SIZES:
        return None
    yy = int(m.group("yy"))
    mmm = m.group("mmm")
    if mmm not in _MONTH_NAMES:
        return None
    month = _MONTH_NAMES.index(mmm) + 1
    # Two-digit year heuristic: 00-89 → 21st century, 90-99 → 20th.
    # Indian F&O didn't exist before 2001 so the 90-99 branch is purely
    # defensive.
    year = 2000 + yy if yy < 90 else 1900 + yy
    expiry = _last_thursday(year, month)
    return {
        "underlying": underlying,
        "expiry": expiry,
        "strike": int(m.group("strike")),
        "opt_type": m.group("opt"),
    }


def is_option_tradingsymbol(ts: str) -> bool:
    """Cheap predicate: does ``ts`` look like a monthly option tradingsymbol?"""
    return _OPTION_RE.match(ts.upper()) is not None


@dataclass(frozen=True)
class OptionInstrument:
    """Resolved view of an F&O option contract.

    Long options have:
      * Limited risk = full premium paid (capped at ``premium × lot_size``
        per lot — far less than equivalent futures margin).
      * Unlimited reward (call) or capped at strike (put).
      * Time decay (theta) — premium drifts toward intrinsic as expiry
        approaches.

    Short options (Phase 4) have unlimited risk and require margin —
    that's why we split CE/PE BUYING (Phase 3) from CE/PE SELLING.
    """
    underlying: str           # "NIFTY"
    tradingsymbol: str        # "NIFTY26MAY24600CE"
    expiry: date              # 2026-05-28
    strike: int               # 24600
    opt_type: str             # "CE" or "PE"
    lot_size: int             # 75 for NIFTY (same as futures)


def resolve_atm_option(underlying: str, opt_type: OptionType,
                       spot: float,
                       today: Optional[date] = None) -> OptionInstrument:
    """Build the OptionInstrument for the ATM strike at ``spot``.

    Used by the option-buying strategy at signal time: the strategy has
    a directional view (CE if bullish, PE if bearish) and wants to buy
    the ATM strike of the current monthly expiry.
    """
    u = underlying.upper()
    if u not in LOT_SIZES:
        raise KeyError(f"unknown F&O underlying {u!r}")
    o = opt_type.upper()
    if o not in ("CE", "PE"):
        raise ValueError(f"opt_type must be 'CE' or 'PE', got {opt_type!r}")
    expiry = current_expiry(today)
    strike = atm_strike(u, spot)
    return OptionInstrument(
        underlying=u,
        tradingsymbol=option_tradingsymbol(u, expiry, strike, o),
        expiry=expiry,
        strike=strike,
        opt_type=o,
        lot_size=LOT_SIZES[u],
    )


# ─── Phase 4: Vertical credit spreads (synthetic single-leg model) ──────────
#
# A credit spread is two real legs (sell one option, buy a further-OTM
# option of the same type), but for paper-mode we model it as ONE
# synthetic position whose price is the net spread (short premium minus
# long premium). This keeps the existing single-leg position manager,
# risk manager, and broker close-out logic working unchanged. Phase 5
# (going-live) will translate the synthetic spread_id into two real Kite
# orders via a multi-leg executor.
#
# Spread tradingsymbol format (deliberately NOT a real NSE symbol so we
# can never confuse it with a single option):
#
#   <UNDERLYING><yymmm><SHORT>-<LONG><CE|PE>SPRD
#
# Examples:
#   NIFTY26MAY24500-24400PESPRD     # Bull put spread (sell 24500 PE, buy 24400 PE)
#   NIFTY26MAY24800-24900CESPRD     # Bear call spread (sell 24800 CE, buy 24900 CE)
#
# The "SPRD" suffix and the "<short>-<long>" hyphen guarantee these
# symbols can't match the option regex, futures regex, or any equity.

SpreadType = Literal["bull_put", "bear_call"]


_SPREAD_RE = re.compile(
    r"^(?P<underlying>[A-Z]+?)"        # NIFTY / BANKNIFTY / ...
    r"(?P<yy>\d{2})"                   # 26
    r"(?P<mmm>[A-Z]{3})"               # MAY
    r"(?P<short>\d+)"                  # 24500
    r"-"
    r"(?P<long>\d+)"                   # 24400
    r"(?P<opt>CE|PE)"                  # CE / PE
    r"SPRD$"                           # marker suffix
)


def spread_tradingsymbol(underlying: str, expiry: date,
                         short_strike: int, long_strike: int,
                         opt_type: OptionType) -> str:
    """Format a synthetic credit-spread tradingsymbol (paper-mode only).

    >>> spread_tradingsymbol("NIFTY", date(2026, 5, 28), 24500, 24400, "PE")
    'NIFTY26MAY24500-24400PESPRD'
    """
    o = opt_type.upper()
    if o not in ("CE", "PE"):
        raise ValueError(f"opt_type must be 'CE' or 'PE', got {opt_type!r}")
    yy = f"{expiry.year % 100:02d}"
    mmm = _MONTH_NAMES[expiry.month - 1]
    return f"{underlying.upper()}{yy}{mmm}{int(short_strike)}-{int(long_strike)}{o}SPRD"


def parse_spread_tradingsymbol(ts: str) -> Optional[dict]:
    """Inverse of :func:`spread_tradingsymbol`.

    Returns ``None`` if ``ts`` doesn't match the spread format. Returns
    a dict with keys ``underlying``, ``expiry``, ``short_strike``,
    ``long_strike``, ``opt_type``, ``spread_type`` on success.
    """
    m = _SPREAD_RE.match(ts.upper())
    if not m:
        return None
    underlying = m.group("underlying")
    if underlying not in LOT_SIZES:
        return None
    mmm = m.group("mmm")
    if mmm not in _MONTH_NAMES:
        return None
    yy = int(m.group("yy"))
    year = 2000 + yy if yy < 90 else 1900 + yy
    month = _MONTH_NAMES.index(mmm) + 1
    short_k = int(m.group("short"))
    long_k = int(m.group("long"))
    opt_type = m.group("opt")
    # Direction inference: PE spread with short_strike > long_strike =
    # bull_put_spread (bullish bias). CE spread with short_strike <
    # long_strike = bear_call_spread (bearish bias).
    if opt_type == "PE" and short_k > long_k:
        spread_type = "bull_put"
    elif opt_type == "CE" and short_k < long_k:
        spread_type = "bear_call"
    else:
        # Configurations like a bear-put-spread (debit) or invalid
        # orderings — rejected at this layer.
        return None
    return {
        "underlying": underlying,
        "expiry": _last_thursday(year, month),
        "short_strike": short_k,
        "long_strike": long_k,
        "opt_type": opt_type,
        "spread_type": spread_type,
    }


def is_spread_tradingsymbol(ts: str) -> bool:
    """Cheap predicate."""
    return _SPREAD_RE.match(ts.upper()) is not None


@dataclass(frozen=True)
class SpreadInstrument:
    """Resolved view of a vertical credit spread.

    DEFINED-RISK structure:

      max_loss_per_share = abs(short_strike − long_strike) − net_credit
      max_gain_per_share = net_credit
      breakeven (bull_put)  = short_strike − net_credit
      breakeven (bear_call) = short_strike + net_credit

    The single-leg synthetic position uses ``avg_price = net_credit``
    (per share, positive) and ``side = SELL`` (we sold the spread for
    a credit). Profit when current spread net price drops, loss when
    it rises.
    """
    underlying: str
    spread_tradingsymbol: str
    spread_type: str           # "bull_put" or "bear_call"
    expiry: date
    short_strike: int
    long_strike: int
    opt_type: str              # "CE" for bear_call, "PE" for bull_put
    lot_size: int

    def width(self) -> int:
        """Strike width in points."""
        return abs(self.short_strike - self.long_strike)

    def max_loss_per_share(self, net_credit: float) -> float:
        """Defined-risk max loss after collecting ``net_credit`` per share."""
        return self.width() - net_credit

    def max_loss_per_lot(self, net_credit: float) -> float:
        return self.max_loss_per_share(net_credit) * self.lot_size


# ─── Phase 4.5: Iron condor (synthetic single-leg model) ────────────────────
#
# An iron condor is FOUR legs (sell ATM-x PE + buy ATM-x-w PE + sell ATM+y CE
# + buy ATM+y+w CE), but for paper-mode we model it as ONE synthetic position
# whose price is the COMBINED net premium of all four legs. The structure is
# delta-neutral by design and profits from theta decay when spot stays
# inside the wings (between the short put and short call strikes).
#
# Iron-condor tradingsymbol format (deliberately distinct from spreads/options):
#
#   <UNDERLYING><yymmm><PUT_LONG>-<PUT_SHORT>-<CALL_SHORT>-<CALL_LONG>IC
#
# Example:
#   NIFTY26MAY24300-24400-24700-24800IC
#       ^         ^     ^     ^     ^
#       |         |     |     |     +--- long call (further OTM, insurance)
#       |         |     |     +--------- short call (closer to spot)
#       |         |     +--------------- short put  (closer to spot)
#       |         +--------------------- long put   (further OTM, insurance)
#       +------------------------------- underlying

_IC_RE = re.compile(
    r"^(?P<underlying>[A-Z]+?)"
    r"(?P<yy>\d{2})"
    r"(?P<mmm>[A-Z]{3})"
    r"(?P<put_long>\d+)-"
    r"(?P<put_short>\d+)-"
    r"(?P<call_short>\d+)-"
    r"(?P<call_long>\d+)"
    r"IC$"
)


def iron_condor_tradingsymbol(underlying: str, expiry: date,
                              put_long: int, put_short: int,
                              call_short: int, call_long: int) -> str:
    """Format the synthetic iron-condor tradingsymbol.

    >>> iron_condor_tradingsymbol("NIFTY", date(2026, 5, 28),
    ...                            24300, 24400, 24700, 24800)
    'NIFTY26MAY24300-24400-24700-24800IC'
    """
    yy = f"{expiry.year % 100:02d}"
    mmm = _MONTH_NAMES[expiry.month - 1]
    return (f"{underlying.upper()}{yy}{mmm}"
            f"{put_long}-{put_short}-{call_short}-{call_long}IC")


def parse_iron_condor_tradingsymbol(ts: str) -> Optional[dict]:
    """Inverse of :func:`iron_condor_tradingsymbol`.

    Returns ``None`` if ``ts`` doesn't match. On success returns dict
    with: underlying, expiry, put_long, put_short, call_short, call_long,
    put_width, call_width.

    Strict ordering check: requires
    put_long < put_short < call_short < call_long. Anything else is
    rejected as a malformed structure.
    """
    m = _IC_RE.match(ts.upper())
    if not m:
        return None
    underlying = m.group("underlying")
    if underlying not in LOT_SIZES:
        return None
    mmm = m.group("mmm")
    if mmm not in _MONTH_NAMES:
        return None
    yy = int(m.group("yy"))
    year = 2000 + yy if yy < 90 else 1900 + yy
    month = _MONTH_NAMES.index(mmm) + 1
    pl = int(m.group("put_long"))
    ps = int(m.group("put_short"))
    cs = int(m.group("call_short"))
    cl = int(m.group("call_long"))
    if not (pl < ps < cs < cl):
        return None
    return {
        "underlying": underlying,
        "expiry": _last_thursday(year, month),
        "put_long": pl,
        "put_short": ps,
        "call_short": cs,
        "call_long": cl,
        "put_width": ps - pl,
        "call_width": cl - cs,
    }


def is_iron_condor_tradingsymbol(ts: str) -> bool:
    return _IC_RE.match(ts.upper()) is not None


@dataclass(frozen=True)
class IronCondorInstrument:
    """Resolved view of an iron condor.

    Defined-risk neutral structure:

      max_gain = net_credit (per share)            — when spot expires
                                                     inside the wings
      max_loss = max(put_width, call_width) − net_credit
                                                   — when spot punches
                                                     through ONE wing
      breakeven_lower = put_short  − net_credit
      breakeven_upper = call_short + net_credit

    Margin = max_loss × qty (since spot can hit AT MOST ONE wing at
    expiry). This is a real ~50% reduction from the sum of the two
    individual spread margins; iron condors are MORE capital-efficient
    than running two separate verticals.
    """
    underlying: str
    ic_tradingsymbol: str
    expiry: date
    put_long: int
    put_short: int
    call_short: int
    call_long: int
    lot_size: int

    def put_width(self) -> int:
        return self.put_short - self.put_long

    def call_width(self) -> int:
        return self.call_long - self.call_short

    def max_loss_per_share(self, net_credit: float) -> float:
        """Spot can punch through AT MOST ONE wing — max loss is the
        wider wing's structural loss, NOT the sum of both."""
        return max(self.put_width(), self.call_width()) - net_credit

    def max_gain_per_share(self, net_credit: float) -> float:
        return net_credit


def resolve_iron_condor(
    underlying: str,
    spot: float,
    put_width: int,
    call_width: int,
    wings_distance: int,
    today: Optional[date] = None,
) -> IronCondorInstrument:
    """Build an iron-condor instrument centred at ``spot``.

    The four strikes are spaced by ``wings_distance`` from spot (for
    short legs) and by ``put_width`` / ``call_width`` further OTM
    (for the protective long legs):

        put_long   = ATM − wings_distance − put_width
        put_short  = ATM − wings_distance
        call_short = ATM + wings_distance
        call_long  = ATM + wings_distance + call_width

    For NIFTY at 24500 with wings_distance=100 and put/call widths=100:
        ic strikes: 24300 - 24400 - 24600 - 24700
        put-side max loss / share = 100 − put_credit
        call-side max loss / share = 100 − call_credit
        IC max loss / share = max(both) − net_credit
    """
    u = underlying.upper()
    if u not in LOT_SIZES:
        raise KeyError(f"unknown F&O underlying {u!r}")
    if put_width <= 0 or call_width <= 0 or wings_distance <= 0:
        raise ValueError(
            f"all of put_width / call_width / wings_distance must be > 0; "
            f"got {put_width}/{call_width}/{wings_distance}"
        )

    expiry = current_expiry(today)
    atm = atm_strike(u, spot)
    step = strike_step(u)
    # Round wings_distance and widths to the nearest valid strike step
    # so we never produce an off-grid strike that the broker can't hit.
    wd = max(step, (wings_distance // step) * step)
    pw = max(step, (put_width // step) * step)
    cw = max(step, (call_width // step) * step)

    put_short  = atm - wd
    put_long   = put_short - pw
    call_short = atm + wd
    call_long  = call_short + cw

    return IronCondorInstrument(
        underlying=u,
        ic_tradingsymbol=iron_condor_tradingsymbol(
            u, expiry, put_long, put_short, call_short, call_long,
        ),
        expiry=expiry,
        put_long=put_long,
        put_short=put_short,
        call_short=call_short,
        call_long=call_long,
        lot_size=LOT_SIZES[u],
    )


def resolve_credit_spread(
    underlying: str,
    spread_type: SpreadType,
    spot: float,
    width: int,
    today: Optional[date] = None,
) -> SpreadInstrument:
    """Resolve a credit spread for ``underlying`` at the current ATM
    strike with the given ``width`` (in strike points).

    For ``bull_put`` (bullish bias):
        short = ATM PE, long = (ATM − width) PE
        — collect premium betting spot stays above short strike.

    For ``bear_call`` (bearish bias):
        short = ATM CE, long = (ATM + width) CE
        — collect premium betting spot stays below short strike.
    """
    u = underlying.upper()
    if u not in LOT_SIZES:
        raise KeyError(f"unknown F&O underlying {u!r}")
    if spread_type not in ("bull_put", "bear_call"):
        raise ValueError(f"spread_type must be 'bull_put' or 'bear_call', got {spread_type!r}")

    expiry = current_expiry(today)
    short_k = atm_strike(u, spot)
    if spread_type == "bull_put":
        long_k = short_k - width
        opt_type = "PE"
    else:
        long_k = short_k + width
        opt_type = "CE"

    return SpreadInstrument(
        underlying=u,
        spread_tradingsymbol=spread_tradingsymbol(u, expiry, short_k, long_k, opt_type),
        spread_type=spread_type,
        expiry=expiry,
        short_strike=short_k,
        long_strike=long_k,
        opt_type=opt_type,
        lot_size=LOT_SIZES[u],
    )
