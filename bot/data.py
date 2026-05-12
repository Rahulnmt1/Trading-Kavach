"""Market data layer.

Historical: yfinance (NSE symbols suffixed with `.NS`).
Live: pluggable. Paper mode polls the latest 1-min bar from yfinance; live mode
should plug in a broker WebSocket feed (Zerodha KiteTicker, Dhan Marketfeed, etc.).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import pytz
import yfinance as yf

from .cache import get_cache
from .config import load_config
from .logger import logger

IST = pytz.timezone("Asia/Kolkata")

# FIX #27 — yfinance empty-bar retry + stale-cache fallback.
#
# yfinance intermittently returns empty payloads for both index tickers
# (^NSEI / ^NSEBANK) and ordinary NSE equities — most aggressively on
# the first 1-2 trading days after a weekend or NSE holiday, but also
# in 1-2 min bursts mid-session at random. Empirical "No history"
# warning counts per day from logs/bot_*.log:
#
#   2026-04-27 Mon  : 227  ← post-weekend Mon (worst case observed)
#   2026-04-28 Tue  :   4
#   2026-04-29 Wed  :   0
#   2026-04-30 Thu  :   3
#   2026-05-04 Mon  :   0  (counter-example — bursts are stochastic)
#   2026-05-05 Tue  :  30
#   2026-05-06 Wed  :  12
#   2026-05-07 Thu  :   5
#   2026-05-08 Fri  :   6
#   2026-05-11 Mon  :  63  ← post-weekend Mon, drove F&O HOLD-all-day
#   2026-05-12 Tue  :   0  (the burst can also skip the next day)
#
# Each burst was previously fatal for that minute's tick: F&O's
# `option_buy_directional` needs ^NSEI 5m bars to compute EMA20/EMA50,
# and on an empty fetch returned HOLD. If every poll happened to land
# inside a burst (as on 2026-05-11), F&O produced zero signals all day.
#
# Mitigations layered here:
#   1. ``history()`` now retries up to 3 times with backoff (0.5s, 1.5s,
#      3.5s) on empty/exception. Most bursts last <2s — one retry is
#      usually enough to get a populated response.
#   2. ``intraday_bars()`` writes a longer-TTL ":stale" cache copy on
#      every successful fetch, and falls back to it when the fresh
#      fetch is empty. The fallback only fires if the last bar in the
#      stale copy is within ONE BAR + 2 min of now — beyond that we
#      return empty (no signal) so we never trade on materially stale
#      data.
#   3. A per-(symbol, interval, hour) dedup keeps the warning log from
#      ballooning into 60+ identical lines per burst (yesterday it
#      reached 63 in one day; with dedup the same day would emit ~6).
_RETRY_DELAYS = (0.5, 1.5, 3.5)
_STALE_CACHE_TTL = 900   # 15 min — long enough to bridge multi-min bursts
_warning_dedup: dict[tuple[str, str, str], None] = {}


def _warn_once(symbol: str, interval: str, message: str) -> None:
    """Log a yfinance/empty-bar warning at most once per (symbol, interval, hour).

    Loud enough to surface real outages, quiet enough that a 5-min
    yfinance hiccup does not produce 60+ identical lines (the 2026-05-11
    log signature). The dedup window resets every wall-clock hour, so a
    recurring fault gets one fresh diagnostic each hour.
    """
    now = datetime.now(IST)
    bucket = now.strftime("%Y-%m-%d %H")
    key = (symbol, interval, bucket)
    if key in _warning_dedup:
        return
    _warning_dedup[key] = None
    if len(_warning_dedup) > 2048:
        _warning_dedup.clear()
        _warning_dedup[key] = None
    logger.warning(message)


@dataclass
class Tick:
    symbol: str
    ts: datetime
    ltp: float
    volume: float


def to_yf(symbol: str) -> str:
    """Map an internal symbol to its yfinance ticker.

    Handles three cases:

    1. **Equity** (default): ``RELIANCE`` → ``RELIANCE.NS``.
    2. **Already-mapped**: anything ending in ``.NS`` or starting with ``^``
       is returned as-is (so callers can pass yfinance tickers directly).
    3. **F&O tradingsymbols** (Phase 2 paper-mode proxy): ``NIFTY26MAYFUT``
       or bare ``NIFTY`` → ``^NSEI``; ``BANKNIFTY26MAYFUT`` → ``^NSEBANK``.
       This is a *spot proxy* — the yfinance index lags futures by 0.1-0.5%
       on volatile sessions. When live broker bars are wired in (Phase 5),
       this path is bypassed entirely.
    """
    if symbol.endswith(".NS") or symbol.startswith("^"):
        return symbol
    # Try the F&O proxy table first (handles "NIFTY", "NIFTY26MAYFUT", etc.).
    from .instruments.fno import yfinance_proxy
    proxy = yfinance_proxy(symbol)
    if proxy is not None:
        return proxy
    # Fallback: treat as NSE equity.
    return f"{symbol}.NS"


def _interval_to_timedelta(interval: str) -> Optional[timedelta]:
    """Parse a yfinance interval string into a ``timedelta``.

    Supports the intraday forms we actually use (``"1m"``, ``"5m"``,
    ``"15m"``, ``"30m"``, ``"60m"``, ``"1h"``). Daily / weekly / unknown
    intervals return ``None`` — the caller skips the partial-bar trim
    for those (backtests work off fully-closed daily bars).
    """
    if not interval:
        return None
    unit = interval[-1].lower()
    try:
        n = int(interval[:-1])
    except ValueError:
        return None
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    return None


def history(symbol: str, days: int = 5, interval: str = "1m") -> pd.DataFrame:
    """Fetch OHLCV. yfinance limits intraday lookback (~7 days for 1m).

    FIX #27: retries up to 3 times with exponential backoff (0.5s,
    1.5s, 3.5s — total worst-case 5.5s extra per failed fetch) when
    yfinance returns an empty payload or raises. yfinance is known to
    return spurious empty responses in 1-2 min bursts, especially on
    Mondays after weekends — see the module-level FIX #27 note. A fresh
    ``yf.Ticker`` is created per attempt to bypass any internal
    "no-data" caching inside yfinance.
    """
    ticker_str = to_yf(symbol)
    period = f"{days}d"
    df = pd.DataFrame()
    last_exc: Optional[BaseException] = None
    for attempt, delay in enumerate((0.0, *_RETRY_DELAYS), start=1):
        if delay > 0:
            time.sleep(delay)
        try:
            ticker = yf.Ticker(ticker_str)
            df = ticker.history(
                period=period, interval=interval,
                prepost=False, auto_adjust=False,
            )
        except Exception as exc:                                # noqa: BLE001
            last_exc = exc
            df = pd.DataFrame()
            logger.debug(
                "history({} {}): attempt {} raised {} — retrying",
                symbol, interval, attempt, exc,
            )
            continue
        if not df.empty:
            break
    if df.empty:
        suffix = f" (last error: {last_exc})" if last_exc is not None else ""
        _warn_once(
            symbol, interval,
            f"No history for {symbol} {interval} after "
            f"{len(_RETRY_DELAYS) + 1} attempts (yfinance empty){suffix}",
        )
        return df
    df = df.rename(columns=str.lower)
    df.index = df.index.tz_convert(IST) if df.index.tz else df.index.tz_localize("UTC").tz_convert(IST)
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    # Drop the still-forming last bar. yfinance returns the in-progress
    # candle (timestamp = bar START), so at e.g. 10:14:30 IST the 5m frame
    # ends with the half-formed 10:10–10:15 bar. Strategies reading
    # ``df.iloc[-1]`` would see flickering OHLC inside a 5-min window
    # and could whipsaw between BUY/HOLD multiple times within the same
    # bar; the backtester sees the closed bar and over-reports edge.
    # Trimming here aligns live with backtest and removes the lookahead.
    delta = _interval_to_timedelta(interval)
    if delta is not None and len(df) > 0:
        last_bar_end = df.index[-1] + delta
        if last_bar_end > datetime.now(IST):
            df = df.iloc[:-1]
    return df


def daily_history(symbol: str, days: int = 90) -> pd.DataFrame:
    """Daily OHLCV — used by backtester and gap analysis."""
    end = datetime.now(IST)
    start = end - timedelta(days=days)
    df = yf.download(
        to_yf(symbol),
        start=start.strftime("%Y-%m-%d"),
        end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
        interval="1d",
        progress=False,
        auto_adjust=False,
    )
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    df.index = pd.to_datetime(df.index)
    return df[["open", "high", "low", "close", "volume"]].dropna()


def previous_close(symbol: str) -> Optional[float]:
    df = daily_history(symbol, days=10)
    if df.empty or len(df) < 2:
        return None
    return float(df["close"].iloc[-2])


def todays_open(symbol: str) -> Optional[float]:
    df = daily_history(symbol, days=5)
    if df.empty:
        return None
    return float(df["open"].iloc[-1])


def latest_quote(symbol: str) -> Optional[Tick]:
    """Latest 1-min bar close as a tick. Cached for 30s to avoid rate limits."""
    cache = get_cache()
    ck = f"quote:{symbol}"
    cached = cache.get_json(ck)
    if cached:
        return Tick(symbol=symbol, ts=datetime.fromisoformat(cached["ts"]),
                    ltp=cached["ltp"], volume=cached["volume"])
    df = history(symbol, days=1, interval="1m")
    if df.empty:
        return None
    last = df.iloc[-1]
    tick = Tick(symbol=symbol, ts=last.name.to_pydatetime(),
                ltp=float(last["close"]), volume=float(last["volume"]))
    cache.set_json(ck, {"ts": tick.ts.isoformat(), "ltp": tick.ltp, "volume": tick.volume}, ttl=30)
    return tick


def intraday_bars(symbol: str, interval: str = "5m") -> pd.DataFrame:
    """Intraday bars at the requested interval. Cached for 60s.

    Always returns a tz-aware index in IST, regardless of whether the bars came
    from a fresh yfinance call (where ``history()`` already converts) or from
    the JSON cache (where ``pd.read_json`` re-localises the ISO offsets to UTC).

    DATE-FILTERING SEMANTICS (subtle but critical for F&O to ever trade):

      * **Equity** symbols (``RELIANCE``, ``INFY``, ...): ONLY today's
        bars are returned. ORB and VWAP_Revert key off a fresh
        per-session boundary — yesterday's open / high / low / VWAP
        would corrupt today's signal.

      * **F&O** symbols (``NIFTY``, ``BANKNIFTY``, ``NIFTY26MAYFUT``,
        ...): up to TWO days of bars are returned. F&O strategies use
        generic momentum indicators (EMA20/EMA50, ATR) which need
        ``ema_slow + cross_lookback_bars + 1 = 56`` 5-min bars before
        they emit anything. Without prior-day bars EMA50 is undefined
        for the first ~4h of the session and the F&O strategies can
        never trade in the morning. Including yesterday's bars lets
        the EMA fully warm up before 09:15 IST today.

    Symbol classification uses ``yfinance_proxy()``: it returns a
    non-None mapping for the index F&O underlyings (NIFTY/BANKNIFTY/etc.)
    and any of their futures tradingsymbols, and ``None`` for equity.

    Special-case for F&O option tradingsymbols (e.g. ``NIFTY26MAY24600CE``):
    yfinance has no option chain, so we **synthesise** the option's OHLC
    bars from the underlying spot's bars via Black-Scholes (Phase 3
    paper-mode shortcut). The position manager can then evaluate SL/TP
    against these synthetic bars exactly as it does for equity/futures.
    Phase 5 swaps this for real Kite Connect option-side bars.

    Phase 4: vertical credit spreads (e.g.
    ``NIFTY26MAY24500-24400PESPRD``) are also synthesised, with each
    bar's OHLC representing the **net spread price** (short_leg
    premium − long_leg premium) computed from BS at every spot bar.

    Phase 4.5: 4-leg iron condors (e.g.
    ``NIFTY26MAY24300-24400-24700-24800IC``) are synthesised as the
    NET premium across all four legs (short put + long put + short
    call + long call), with the OHLC range computed at the spot
    extremes of each bar.
    """
    from .instruments.fno import (
        parse_iron_condor_tradingsymbol,
        parse_option_tradingsymbol, parse_spread_tradingsymbol,
        yfinance_proxy,
    )
    parsed_ic = parse_iron_condor_tradingsymbol(symbol)
    if parsed_ic is not None:
        return _synth_iron_condor_bars(parsed_ic, interval)
    parsed_spread = parse_spread_tradingsymbol(symbol)
    if parsed_spread is not None:
        return _synth_spread_bars(parsed_spread, interval)
    parsed = parse_option_tradingsymbol(symbol)
    if parsed is not None:
        return _synth_option_bars(parsed, interval)

    is_fno = yfinance_proxy(symbol) is not None

    from io import StringIO
    cache = get_cache()
    # Cache key bumped from "bars:" → "bars2:" so any pre-Option-B
    # today-only-filtered F&O bars cached by the prior code don't get
    # served as the new multi-day series. Equity entries also re-fetch
    # once at the next call (60s TTL) — harmless.
    ck = f"bars2:{symbol}:{interval}"
    cached = cache.get_json(ck)
    if cached:
        # pandas 2.3+ deprecated passing a literal JSON string to read_json;
        # wrap in StringIO to silence the FutureWarning and stay forward-compatible.
        df = pd.read_json(StringIO(cached), orient="split")
        df.index = pd.to_datetime(df.index)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(IST)
        return df
    # Calendar-day window for the yfinance fetch.
    #
    # For EQUITY: we discard everything except today below, so a small
    # window is fine. ``days=2`` covers all weekday cases.
    #
    # For F&O: the strategies (credit_spread, futures_trend,
    # option_buy_directional, iron_condor) all use EMA50 on 5-min bars,
    # which needs ~50 bars of warmup before producing valid values.
    # 75 bars/day means EMA50 is only fully warm ~4 hours into the
    # session — by which time the day's early crosses (typically at
    # the open) are stale. We MUST pre-warm EMA50 from prior sessions.
    #
    # The previous ``days=2`` was a silent failure on Mondays:
    # ``2 days ago`` = Saturday (no data) → yfinance returns only
    # today's bars. This was the root cause of the 2026-05-04 zero-F&O-
    # trades incident — NIFTY's 09:20 IST bullish cross and 12:20 IST
    # bearish cross both occurred BEFORE EMA50 had any data to warm
    # against. ``days=7`` guarantees ≥4 prior trading days regardless
    # of weekday (covers Mon-after-long-weekend, Tue-after-Mon-holiday,
    # etc.), so EMA50 is fully warm at every market open.
    fetch_days = 7 if is_fno else 2
    df = history(symbol, days=fetch_days, interval=interval)
    if not is_fno:
        # Equity: discard yesterday's bars so ORB and VWAP_Revert see
        # a clean per-day session.
        today = datetime.now(IST).date()
        df = df[df.index.date == today] if not df.empty else df
    # F&O: keep prior 6 trading days + today so EMA50 has enough
    # warmup to emit signals from the open of today.
    if not df.empty:
        payload = df.to_json(orient="split", date_format="iso")
        cache.set_json(ck, payload, ttl=60)
        # FIX #27: parallel ":stale" copy with a much longer TTL so the
        # NEXT call inside a yfinance empty-bar burst can still serve
        # near-fresh bars instead of forcing the strategy into HOLD.
        cache.set_json(f"{ck}:stale", payload, ttl=_STALE_CACHE_TTL)
        return df
    # FIX #27: fresh fetch returned EMPTY (yfinance burst). Fall back
    # to the stale-cache copy if it exists AND the last bar is fresh
    # enough that we're not trading on a materially old picture. The
    # cutoff is ``interval + 2 min`` — i.e. at most one missed bar.
    stale_raw = cache.get_json(f"{ck}:stale")
    if not stale_raw:
        return df  # empty — caller will skip this symbol for the tick
    df = pd.read_json(StringIO(stale_raw), orient="split")
    df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(IST)
    if df.empty:
        return df
    interval_td = _interval_to_timedelta(interval) or timedelta(minutes=5)
    cutoff = interval_td + timedelta(minutes=2)
    last_bar_age = datetime.now(IST) - df.index[-1].to_pydatetime()
    if last_bar_age > cutoff:
        _warn_once(
            symbol, interval,
            f"[data] {symbol} {interval} empty AND stale cache too old "
            f"({last_bar_age.total_seconds():.0f}s > "
            f"{int(cutoff.total_seconds())}s cutoff) — returning empty",
        )
        return pd.DataFrame()
    _warn_once(
        symbol, interval,
        f"[data] {symbol} {interval} bars empty from yfinance — serving "
        f"stale cache (last bar {last_bar_age.total_seconds():.0f}s old, "
        f"within {int(cutoff.total_seconds())}s cutoff)",
    )
    return df


def _synth_option_bars(parsed: dict, interval: str) -> pd.DataFrame:
    """Build a DataFrame of synthetic option bars from underlying spot bars.

    ``parsed`` is the dict returned by
    :func:`bot.instruments.fno.parse_option_tradingsymbol`, containing
    underlying / expiry / strike / opt_type. For each spot bar we compute
    the option's OHLC via Black-Scholes (see :mod:`bot.options.pricing`).

    Returns an empty DataFrame if the underlying has no bars (no
    yfinance data, off-market, etc.) — same fail-mode as ``intraday_bars``
    for unknown symbols, so the position-manager fallback chain works
    unchanged.
    """
    from .options.pricing import bs, synth_option_ohlc, years_to_expiry
    underlying = parsed["underlying"]
    strike = parsed["strike"]
    opt_type = parsed["opt_type"]
    expiry = parsed["expiry"]

    # Get the underlying spot bars (recursive call on the bare underlying
    # name — ``to_yf`` maps "NIFTY" → "^NSEI" via the proxy table).
    spot_df = intraday_bars(underlying, interval)
    if spot_df.empty:
        return spot_df

    T = years_to_expiry(expiry)
    out_open, out_high, out_low, out_close = [], [], [], []
    for _, bar in spot_df.iterrows():
        ohlc = synth_option_ohlc(
            spot_high=float(bar["high"]),
            spot_low=float(bar["low"]),
            spot_open=float(bar["open"]),
            spot_close=float(bar["close"]),
            K=float(strike),
            T=T,
            opt_type=opt_type,
        )
        out_open.append(ohlc["open"])
        out_high.append(ohlc["high"])
        out_low.append(ohlc["low"])
        out_close.append(ohlc["close"])
    return pd.DataFrame({
        "open":   out_open,
        "high":   out_high,
        "low":    out_low,
        "close":  out_close,
        # Volume isn't used by SL/TP logic; carry the underlying's for
        # the rare strategy that consumes it.
        "volume": spot_df["volume"].values,
    }, index=spot_df.index)


def _synth_spread_bars(parsed: dict, interval: str) -> pd.DataFrame:
    """Build a DataFrame of synthetic *net spread price* bars from
    underlying spot bars.

    For a credit spread the net price = short_leg_premium −
    long_leg_premium. We compute both legs at every spot bar and
    return a single OHLC dataframe of the net.

    Direction-aware OHLC mapping:

    * ``bull_put`` (sell higher PE, buy lower PE): the SHORT leg's
      premium INCREASES as spot DROPS, so the net spread price
      INCREASES on a spot drop → spread net high coincides with spot
      LOW (inverse mapping).
    * ``bear_call`` (sell lower CE, buy higher CE): the SHORT leg's
      premium INCREASES as spot RISES, so net high coincides with
      spot HIGH.

    The position manager evaluates the spread SL/TP against these
    synthetic bars; combined with the broker's SELL-side P&L math
    (profit when net DROPS), this yields the correct exit behaviour
    for credit-spread management.
    """
    from .options.pricing import bs, years_to_expiry
    underlying = parsed["underlying"]
    short_k = parsed["short_strike"]
    long_k = parsed["long_strike"]
    opt_type = parsed["opt_type"]
    expiry = parsed["expiry"]
    spread_type = parsed["spread_type"]

    spot_df = intraday_bars(underlying, interval)
    if spot_df.empty:
        return spot_df

    T = years_to_expiry(expiry)

    def _net(spot: float) -> float:
        s_prem = bs(spot, short_k, T, opt_type)
        l_prem = bs(spot, long_k,  T, opt_type)
        return s_prem - l_prem

    out_open, out_high, out_low, out_close = [], [], [], []
    for _, bar in spot_df.iterrows():
        n_open  = _net(float(bar["open"]))
        n_close = _net(float(bar["close"]))
        # For monotonic mapping (premium-vs-spot moves in one direction
        # for each leg), the net's high/low correspond to specific spot
        # extremes determined by spread_type.
        if spread_type == "bear_call":
            n_high = _net(float(bar["high"]))
            n_low  = _net(float(bar["low"]))
        else:   # bull_put — net is inverse-monotonic in spot
            n_high = _net(float(bar["low"]))
            n_low  = _net(float(bar["high"]))
        out_open.append(n_open)
        out_high.append(max(n_high, n_low))    # safety: ensure high>=low
        out_low.append(min(n_high, n_low))
        out_close.append(n_close)

    return pd.DataFrame({
        "open":   out_open,
        "high":   out_high,
        "low":    out_low,
        "close":  out_close,
        "volume": spot_df["volume"].values,
    }, index=spot_df.index)


def _synth_iron_condor_bars(parsed: dict, interval: str) -> pd.DataFrame:
    """Build a DataFrame of synthetic *net iron-condor price* bars.

    For an iron condor, the net price (per share, paid to close) is:

        net = (short_put_prem  − long_put_prem)           # put spread
            + (short_call_prem − long_call_prem)          # call spread

    We computed both legs at every spot bar via Black-Scholes and
    return a single OHLC dataframe of the net.

    Iron-condor net-price-vs-spot is U-shaped (higher net at spot
    extremes, lower in the middle), so HIGH / LOW don't map cleanly
    to spot HIGH / LOW the way they do for verticals. We sample the
    net at three points per bar (open / high / low / close spots)
    and pick min/max — accurate enough for SL/TP evaluation.
    """
    from .options.pricing import bs, years_to_expiry
    underlying = parsed["underlying"]
    pl, ps = parsed["put_long"], parsed["put_short"]
    cs, cl = parsed["call_short"], parsed["call_long"]
    expiry = parsed["expiry"]

    spot_df = intraday_bars(underlying, interval)
    if spot_df.empty:
        return spot_df

    T = years_to_expiry(expiry)

    def _net(spot: float) -> float:
        # Net cost to BUY-back the iron condor (close it). The short
        # legs dominate when spot is near them; the long-OTM legs cap
        # losses at the wing widths.
        sp_prem = bs(spot, ps, T, "PE")
        lp_prem = bs(spot, pl, T, "PE")
        sc_prem = bs(spot, cs, T, "CE")
        lc_prem = bs(spot, cl, T, "CE")
        return (sp_prem - lp_prem) + (sc_prem - lc_prem)

    out_open, out_high, out_low, out_close = [], [], [], []
    for _, bar in spot_df.iterrows():
        n_o = _net(float(bar["open"]))
        n_c = _net(float(bar["close"]))
        # Sample at the bar's high & low spot too — the U-shape means
        # the bar's net-high might be at either spot extreme.
        n_h_spot = _net(float(bar["high"]))
        n_l_spot = _net(float(bar["low"]))
        bar_high = max(n_o, n_c, n_h_spot, n_l_spot)
        bar_low  = min(n_o, n_c, n_h_spot, n_l_spot)
        out_open.append(n_o)
        out_high.append(bar_high)
        out_low.append(bar_low)
        out_close.append(n_c)
    return pd.DataFrame({
        "open":   out_open,
        "high":   out_high,
        "low":    out_low,
        "close":  out_close,
        "volume": spot_df["volume"].values,
    }, index=spot_df.index)


def is_market_open() -> bool:
    """09:15-15:30 IST, Mon-Fri (does not check holidays)."""
    cfg = load_config()
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_t = cfg.session.t("market_open")
    close_t = cfg.session.t("square_off")
    return open_t <= now.time() <= close_t
