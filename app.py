"""Streamlit dashboard — live view of the bot's state.

REDESIGNED 2026-04-30 — tab-based navigation, hero KPI strip, status-pill
header, bordered cards for grouping, consistent green/red/gray palette.
A pristine copy of the prior layout lives at
``dashboard_code_bkp_20260430.py`` for one-step rollback:

    cp dashboard_code_bkp_20260430.py app.py

The dashboard is **segment-aware**. A sidebar selector chooses between
EQUITY (default) and F&O. Switching segments re-reads everything from
that segment's namespaced Redis keys (``paper:state:fno`` etc.) and
that segment's journal directory (``logs/trades/fno/...``), so each
segment has its own self-contained view.

Page structure (top to bottom):

    [ Status pill strip — Mode | Broker | Market | Heartbeat | Date ]
    [ Heartbeat-stale banner (only when bot is silent) ]
    [ Hero KPI panel — Today's Net P&L + progress bar + 4 sub-metrics ]
    [ Tabs:
        📊 Overview     — open positions, market schedule, picks, signals
        📈 Performance  — edge metrics, journal, EOD report, by-strategy
        🕯️  Charts      — candles + EMA + VWAP + RSI
        Ω  Greeks       — F&O only: Δ Γ Θ Vega per leg
        🩺 System       — health checks, fee audit, heartbeat trail
    ]
    [ Footer — last-refreshed timestamp ]

Sidebar:

    [ Segment radio ]
    [ Combined P&L card (both segments) ]
    [ Today's risk envelope card (capital, caps, profit target) ]
    [ Quick actions — refresh NSE holidays, run healthcheck ]
"""
from __future__ import annotations

from datetime import datetime, date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from bot.cache import get_cache
from bot.config import env, load_config
from bot.data import intraday_bars, is_market_open, latest_quote
from bot.fees import position_economics, roundtrip_breakdown
from bot.holidays import get_holidays, market_status, refresh_holidays
from bot.indicators import ema, rsi, vwap
# Hoisted from inline imports inside Greeks / open-position chart helpers —
# Streamlit re-runs the entire script on every interaction, so even cheap
# per-render imports add up. These are the bot's own modules, not optional
# heavy deps; importing them at module top is free.
from bot.instruments.fno import (
    parse_iron_condor_tradingsymbol,
    parse_option_tradingsymbol,
    parse_spread_tradingsymbol,
    underlying_from_tradingsymbol,
)
from bot.journal import (
    daily_summary,
    eod_report,
    trades_csv,
    trades_jsonl,
)
from bot.options.pricing import all_greeks, years_to_expiry
from bot.segment import (
    Segment,
    cache_key,
    cfg_capital,
    cfg_risk,
    cfg_watchlist_symbols,
    fno_enabled,
    signal_pattern,
)


# ────────────────────────── cached wrappers ──────────────────────────────────
# Streamlit re-runs the whole script on every interaction. Without these
# decorators, EVERY rerun re-fetches yfinance bars, re-walks the JSONL
# trade journal, and re-parses ``config.yaml`` — burning Redis ops, disk
# I/O, and yfinance rate-limit budget that the bot also needs.
#
# TTLs are picked to align with the bot's data cadence:
#   * yfinance intraday bars ⇒ 60s (matches ``bot/data.py`` Redis TTL)
#   * yfinance latest_quote  ⇒ 30s (matches ``bot/data.py`` Redis TTL)
#   * journal daily_summary  ⇒ 30s (post-fill ledger writes are atomic;
#                                    sub-30s lag is fine for the dashboard)
#   * NSE holiday calendar   ⇒ 1h  (refreshes once a day in the bot, but
#                                   we never want a render-path network
#                                   call to NSE — the explicit "Refresh"
#                                   button still bypasses this cache)
#   * config.yaml            ⇒ 10min via cache_resource (read-only here;
#                              ``cache_data`` would deep-copy the Pydantic
#                              model on every retrieval)

@st.cache_resource(ttl=600)
def _cached_load_config():
    return load_config()


@st.cache_data(ttl=3600)
def _cached_get_holidays(_unused_bucket: int = 0):
    """Cached holiday calendar.

    The ``_unused_bucket`` arg lets the explicit Refresh button bust the
    cache by incrementing ``st.session_state['holidays_bucket']``.
    """
    return get_holidays(allow_refresh=False)


@st.cache_data(ttl=60)
def _cached_intraday_bars(symbol: str, interval: str):
    return intraday_bars(symbol, interval)


@st.cache_data(ttl=30)
def _cached_latest_quote(symbol: str):
    return latest_quote(symbol)


@st.cache_data(ttl=30)
def _cached_daily_summary(day, segment: Segment):
    return daily_summary(day, segment=segment)


# ── Theming ──────────────────────────────────────────────────────────────────
# Streamlit lets us inject CSS once per page render. Keep this minimal —
# the goal is consistent typography, color-coded P&L, and chip-style
# status pills, NOT a full custom theme (Streamlit's dark/light auto-
# detection handles the chrome). Colors picked to read on both themes.
_CSS = """
<style>
/* Status-pill chips — small rounded badges in the header strip. */
.pill {
    display: inline-block;
    padding: 0.18rem 0.65rem;
    margin-right: 0.4rem;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 600;
    line-height: 1.4;
    border: 1px solid var(--pill-border, rgba(127,127,127,0.35));
    background: var(--pill-bg, rgba(127,127,127,0.10));
    color: inherit;
}
.pill-green   { --pill-border: #16a34a; --pill-bg: rgba(34,197,94,0.12); color: #16a34a; }
.pill-red     { --pill-border: #dc2626; --pill-bg: rgba(239,68,68,0.12);  color: #dc2626; }
.pill-amber   { --pill-border: #ca8a04; --pill-bg: rgba(234,179,8,0.12);  color: #b45309; }
.pill-gray    { --pill-border: #6b7280; --pill-bg: rgba(107,114,128,0.12); color: #4b5563; }
.pill-blue    { --pill-border: #2563eb; --pill-bg: rgba(59,130,246,0.12); color: #2563eb; }

/* Hero P&L number — the single most important figure on the page. */
.hero-pnl       { font-size: 2.6rem; font-weight: 800; line-height: 1.05; letter-spacing: -0.02em; }
.hero-pnl-pos   { color: #16a34a; }
.hero-pnl-neg   { color: #dc2626; }
.hero-pnl-flat  { color: #6b7280; }
.hero-label     { font-size: 0.78rem; color: #6b7280; text-transform: uppercase;
                  letter-spacing: 0.06em; margin-bottom: 0.15rem; }
.hero-sub       { font-size: 0.85rem; color: #6b7280; margin-top: 0.25rem; }

/* Section heading inside tabs. */
.section-h     { font-size: 1.05rem; font-weight: 700; margin: 0.2rem 0 0.4rem 0; }

/* Compact metric labels on small KPI cards. */
.kpi-mini-l    { font-size: 0.73rem; color: #6b7280; text-transform: uppercase;
                 letter-spacing: 0.05em; margin-bottom: 0.05rem; }
.kpi-mini-v    { font-size: 1.15rem; font-weight: 700; }
.kpi-mini-pos  { color: #16a34a; }
.kpi-mini-neg  { color: #dc2626; }

/* Status card (used inside Overview / market schedule). */
.status-card   { padding: 0.6rem 0.8rem; border-radius: 0.6rem;
                 background: rgba(127,127,127,0.06);
                 border: 1px solid rgba(127,127,127,0.18); }

/* Streamlit metric polish — slightly tighter so the hero strip has room. */
[data-testid="stMetricLabel"] p { font-size: 0.78rem !important; opacity: 0.75; }
[data-testid="stMetricValue"]  { font-size: 1.4rem !important; }
</style>
"""

# ────────────────────────── small utilities ──────────────────────────────────

def _pnl_class(value: float) -> str:
    """Return ``hero-pnl-pos|neg|flat`` based on the sign of ``value``."""
    if value > 0:
        return "hero-pnl-pos"
    if value < 0:
        return "hero-pnl-neg"
    return "hero-pnl-flat"


def _kpi_class(value: float) -> str:
    if value > 0:
        return "kpi-mini-pos"
    if value < 0:
        return "kpi-mini-neg"
    return ""


def _pill(label: str, kind: str = "gray") -> str:
    """Return a `<span class="pill pill-...">label</span>` HTML chip."""
    return f"<span class='pill pill-{kind}'>{label}</span>"


def _format_inr(amount: float, signed: bool = True) -> str:
    """``₹+1,234.56`` / ``₹1,234.56``."""
    if signed:
        return f"₹{amount:+,.2f}"
    return f"₹{amount:,.2f}"


# ────────────────────────── Header (status pills) ────────────────────────────

def render_header_pills(*, mode: str, segment: Segment, broker: str,
                        market_status_dict: dict, hb: dict) -> None:
    """Compact chip strip — replaces the old 5-metric-card row.

    Each chip is a small colored pill. Loud red on critical states
    (bot stalled, market closed for holiday) so the operator can spot
    issues at a glance without reading numbers.
    """
    chips: list[str] = []

    # Mode (PAPER / LIVE).
    if "LIVE" in mode:
        chips.append(_pill("🔴 LIVE TRADING", "red"))
    else:
        chips.append(_pill("🟢 PAPER MODE", "green"))

    # Segment.
    seg_color = "blue" if segment == Segment.FNO else "gray"
    chips.append(_pill(f"📂 {segment.label}", seg_color))

    # Broker.
    chips.append(_pill(f"🏦 {broker.upper()}", "gray"))

    # Market.
    st_status = market_status_dict["status"]
    if st_status == "OPEN" and is_market_open():
        chips.append(_pill("🟢 MARKET OPEN", "green"))
    elif st_status == "HOLIDAY":
        chips.append(_pill(
            f"🔴 HOLIDAY — {market_status_dict.get('reason', '')}".rstrip(" —"),
            "red",
        ))
    elif st_status == "WEEKEND":
        chips.append(_pill(f"⚪ WEEKEND ({market_status_dict.get('weekday', '')})", "gray"))
    else:
        chips.append(_pill("⚪ MARKET CLOSED", "gray"))

    # Heartbeat — only meaningful while market should be open.
    if hb and is_market_open():
        try:
            ts = datetime.fromisoformat(hb["ts"])
            age = (datetime.now(ts.tzinfo) - ts).total_seconds()
        except Exception:
            age = None
        if age is None:
            chips.append(_pill("♥ unknown", "gray"))
        elif age > 180:
            chips.append(_pill(f"🛑 STALL {int(age//60)}m", "red"))
        elif age > 90:
            chips.append(_pill(f"♥ slow {int(age)}s", "amber"))
        else:
            chips.append(_pill(f"♥ {int(age)}s", "green"))
    else:
        chips.append(_pill("♥ idle", "gray"))

    # Date.
    chips.append(_pill(f"📅 {date.today().strftime('%a, %d %b %Y')}", "gray"))

    st.markdown("".join(chips), unsafe_allow_html=True)


# ────────────────────────── Hero KPI strip ───────────────────────────────────

def render_hero_kpis(*, cash: float, realized: float, unrealized: float,
                     n_open: int, today_pnl_pct: float, target_pct: float,
                     loss_cutoff_pct: float, capital_total: float,
                     summary: dict, lockin: dict | None) -> None:
    """Big, color-coded P&L panel + 4 sub-metrics + progress bar.

    The Day P&L number is the single most important figure on the page,
    so it gets a large bold display (vs. the prior dashboard where it
    was buried in a `st.caption()`). The progress bar visualises
    progress toward the daily profit target so you see at a glance
    "we're 30% of the way to today's goal".
    """
    today_pnl_inr = capital_total * today_pnl_pct / 100.0

    with st.container(border=True):
        c_main, c_kpi = st.columns([1.4, 2.6])

        # ── LEFT: hero P&L ────────────────────────────────────────────
        with c_main:
            st.markdown(
                f"<div class='hero-label'>Today's net P&amp;L · {capital_total:,.0f} ₹ deployable</div>"
                f"<div class='hero-pnl {_pnl_class(today_pnl_inr)}'>"
                f"{_format_inr(today_pnl_inr)}"
                f"</div>"
                f"<div class='hero-sub'>{today_pnl_pct:+.2f}% of capital  ·  "
                f"target {target_pct:.1f}% (₹{capital_total*target_pct/100:,.0f})  ·  "
                f"loss cutoff −{loss_cutoff_pct:.1f}%</div>",
                unsafe_allow_html=True,
            )
            if target_pct > 0:
                bar_pct = max(0.0, min(1.0, today_pnl_pct / target_pct))
                if today_pnl_pct >= 0:
                    st.progress(bar_pct, text=f"{bar_pct*100:.0f}% of profit target")
                else:
                    # Show the loss-cap progress instead — how close we are
                    # to the daily-loss kill-switch.
                    loss_bar = max(0.0, min(1.0, abs(today_pnl_pct) / loss_cutoff_pct))
                    st.progress(loss_bar, text=f"{loss_bar*100:.0f}% of loss cutoff")
            if lockin:
                st.success(
                    f"🎯 Daily profit target locked in at "
                    f"{lockin.get('ts','')[11:19]} IST  ·  "
                    f"+{lockin.get('pnl_pct',0):.2f}% — no new entries until tomorrow."
                )

        # ── RIGHT: 4 sub-KPIs in a 2×2 grid ───────────────────────────
        with c_kpi:
            r1c1, r1c2, r1c3, r1c4 = st.columns(4)

            def _mini(col, label: str, value: str, klass: str = "") -> None:
                col.markdown(
                    f"<div class='kpi-mini-l'>{label}</div>"
                    f"<div class='kpi-mini-v {klass}'>{value}</div>",
                    unsafe_allow_html=True,
                )

            _mini(r1c1, "Realized P&L", _format_inr(realized), _kpi_class(realized))
            _mini(r1c2, "Unrealized P&L", _format_inr(unrealized), _kpi_class(unrealized))
            _mini(r1c3, "Cash", _format_inr(cash, signed=False))
            _mini(r1c4, "Open positions", str(n_open))

            r2c1, r2c2, r2c3, r2c4 = st.columns(4)
            n_trades = int(summary.get("trades", 0))
            if n_trades > 0:
                wr = float(summary["win_rate"]) * 100
                exp = float(summary["expectancy"])
                _mini(r2c1, "Trades closed", str(n_trades))
                _mini(r2c2, "Win rate",
                      f"{wr:.0f}%  ({summary['wins']}W/{summary['losses']}L)")
                _mini(r2c3, "Expectancy", _format_inr(exp), _kpi_class(exp))
                _mini(r2c4, "Profit factor", str(summary.get("profit_factor", "—")))
            else:
                _mini(r2c1, "Trades closed", "0")
                _mini(r2c2, "Win rate", "—")
                _mini(r2c3, "Expectancy", "—")
                _mini(r2c4, "Profit factor", "—")


# ────────────────────────── Market schedule (compact) ────────────────────────

def render_market_schedule_compact() -> None:
    """Compact 4-card strip — today + tomorrow × Equity + F&O.

    Smaller variant of the original full-width panel. Lives inside the
    Overview tab next to picks/signals so it doesn't dominate the page.
    """
    # Use the cached wrapper (1h TTL) on the render path. The "Refresh"
    # button below still calls ``refresh_holidays`` directly which makes
    # the live NSE fetch and re-populates the cache for subsequent renders.
    cal = _cached_get_holidays()
    today = date.today()
    tomorrow = today + timedelta(days=1)

    with st.container(border=True):
        h_l, h_r = st.columns([3, 1])
        h_l.markdown("<div class='section-h'>📅 Market schedule</div>",
                     unsafe_allow_html=True)
        if h_r.button("Refresh", help="Force a live fetch from NSE",
                      key="refresh_holidays_compact"):
            cal = refresh_holidays()
            _cached_get_holidays.clear()
            st.success(f"Refreshed: {cal.last_refresh[:19].replace('T', ' ')} IST")

        src = {"nse": "🟢 NSE (live)", "bootstrap": "🟡 BOOTSTRAP", "stale": "🟡 STALE"}.get(
            cal.source, f"⚪ {cal.source}")
        try:
            last = datetime.fromisoformat(cal.last_refresh).strftime("%d %b %H:%M")
        except Exception:
            last = cal.last_refresh[:16]
        st.caption(f"Source: {src} · refreshed {last}")

        cols = st.columns(4)
        for col, (label, d, seg_) in zip(cols, [
            ("TODAY · Equity",     today,    Segment.EQUITY),
            ("TODAY · F&O",        today,    Segment.FNO),
            ("TOMORROW · Equity",  tomorrow, Segment.EQUITY),
            ("TOMORROW · F&O",     tomorrow, Segment.FNO),
        ]):
            s = market_status(d, seg_, calendar=cal)
            klass = {"OPEN": "pill-green", "HOLIDAY": "pill-red",
                     "WEEKEND": "pill-gray"}[s["status"]]
            sub = s.get("reason") or "09:15–15:30 IST"
            col.markdown(
                f"<div class='kpi-mini-l'>{label} · {s['weekday']}</div>"
                f"<div><span class='pill {klass}'>{s['status']}</span></div>"
                f"<div style='font-size:0.78rem;color:#6b7280;margin-top:0.3rem;'>{sub}</div>",
                unsafe_allow_html=True,
            )

        # Tomorrow-closed banner — single source of truth.
        eq_t = market_status(tomorrow, Segment.EQUITY, calendar=cal)
        fo_t = market_status(tomorrow, Segment.FNO,    calendar=cal)
        if not eq_t["is_open"] and not fo_t["is_open"]:
            st.error(
                f"🛑 Markets CLOSED tomorrow ({tomorrow.strftime('%a, %d %b')}) — "
                f"{eq_t['reason']}. MIS positions square off today @ 15:15 IST."
            )
        elif not eq_t["is_open"] or not fo_t["is_open"]:
            closed = "Equity" if not eq_t["is_open"] else "F&O"
            reason = eq_t["reason"] if not eq_t["is_open"] else fo_t["reason"]
            st.warning(f"⚠ Tomorrow: {closed} CLOSED ({reason}). Other segment trades as normal.")

        with st.expander("Next 14 days — both segments", expanded=False):
            rows = []
            for offset in range(14):
                d = today + timedelta(days=offset)
                eq = market_status(d, Segment.EQUITY, calendar=cal)
                fo = market_status(d, Segment.FNO,    calendar=cal)
                emoji = {"OPEN": "🟢", "HOLIDAY": "🔴", "WEEKEND": "⚪"}
                rows.append({
                    "Date":   d.isoformat(),
                    "Day":    d.strftime("%a"),
                    "Equity": f"{emoji[eq['status']]} {eq['status']}",
                    "F&O":    f"{emoji[fo['status']]} {fo['status']}",
                    "Reason": eq.get("reason") or fo.get("reason") or "",
                })
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


# ────────────────────────── Open-positions panel ─────────────────────────────

def _render_open_trade_charts(positions: list[dict], pos_econ: list) -> None:
    """One candle chart per open position with entry / SL / TP overlays.

    Sits between the Open-positions table and the fee-breakdown
    expander in the Overview tab so the operator can answer "is this
    trade going the way the strategy expected?" without leaving the
    page.

    Charting symbol resolution:
    * Equity positions plot the symbol directly (NESTLEIND, INFY, …).
      Entry/SL/TP are in the SAME price space as the chart and are
      drawn as horizontal reference lines.
    * F&O positions (futures, options, spreads, iron condors) plot the
      UNDERLYING (NIFTY, BANKNIFTY, …) because the broker's historical
      bars cache only stores underlyings, not synthetic spread or
      premium marks. Entry/SL/TP for F&O are in the spread/premium
      space and would be misleading if drawn on the underlying chart;
      we surface them in the caption beneath the chart instead.

    Falls back to a single-line caption if bars aren't available
    (yfinance hiccup, market closed, fresh underlying not yet polled).
    """
    if not positions:
        return
    # ``underlying_from_tradingsymbol`` is now hoisted to module top —
    # handles bare equity symbols, FUT, options, spreads, and iron
    # condors uniformly.

    st.markdown("<div class='section-h'>📉 Live trade charts — track each open position</div>",
                unsafe_allow_html=True)

    for p, econ in zip(positions, pos_econ):
        sym  = p.get("symbol", "")
        side = p.get("side", "BUY")
        qty  = abs(int(p.get("qty", 0)))
        # Resolve the chart symbol. For equity this is just `sym`; for
        # F&O it's the underlying (NIFTY, BANKNIFTY, RELIANCE, …).
        try:
            chart_sym = underlying_from_tradingsymbol(sym) or sym
        except Exception:
            chart_sym = sym
        is_fno = (chart_sym != sym)

        df = _cached_intraday_bars(chart_sym, "5m")
        if df.empty:
            df = _cached_intraday_bars(chart_sym, "1m")
        if df.empty:
            st.caption(
                f"  ⚠ {sym} — no bars yet for chart symbol `{chart_sym}` "
                f"(yfinance may be rate-limiting or markets are closed)."
            )
            continue

        df = df.copy()
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert("Asia/Kolkata")
        elif str(df.index.tz) != "Asia/Kolkata":
            df.index = df.index.tz_convert("Asia/Kolkata")

        # Light-touch overlays — EMA 9/21 are familiar to anyone who's
        # read the Charts tab, no VWAP/RSI here to keep each chart
        # compact when several positions stack.
        df["ema9"]  = ema(df["close"], 9)
        df["ema21"] = ema(df["close"], 21)

        fig = go.Figure()
        fig.add_trace(go.Candlestick(
            x=df.index, open=df["open"], high=df["high"],
            low=df["low"], close=df["close"], name=chart_sym,
            showlegend=False,
        ))
        fig.add_trace(go.Scatter(x=df.index, y=df["ema9"],  name="EMA 9",
                                 line=dict(width=1, color="#42a5f5")))
        fig.add_trace(go.Scatter(x=df.index, y=df["ema21"], name="EMA 21",
                                 line=dict(width=1, color="#ef5350")))

        entry_px = float(p.get("avg_price", 0))
        sl_px    = p.get("stop_loss")
        tp_px    = p.get("take_profit")
        unreal   = float(econ.at_current["net_pnl"])

        if not is_fno:
            # Equity — draw entry / SL / TP horizontal lines on the chart.
            fig.add_hline(
                y=entry_px,
                line=dict(color="#9e9e9e", width=1, dash="dot"),
                annotation_text=f"Entry ₹{entry_px:.2f}",
                annotation_position="top right",
            )
            if sl_px is not None:
                fig.add_hline(
                    y=float(sl_px),
                    line=dict(color="#ef5350", width=1, dash="dash"),
                    annotation_text=f"SL ₹{float(sl_px):.2f}",
                    annotation_position="bottom right",
                )
            if tp_px is not None:
                fig.add_hline(
                    y=float(tp_px),
                    line=dict(color="#26a69a", width=1, dash="dash"),
                    annotation_text=f"TP ₹{float(tp_px):.2f}",
                    annotation_position="top right",
                )
            caption = (
                f"**{sym}** · {side} · qty {qty} · "
                f"entry ₹{entry_px:.2f} → now ₹{econ.current_price:.2f} · "
                f"SL ₹{float(sl_px):.2f} / TP ₹{float(tp_px):.2f} · "
                f"unrealised **₹{unreal:+,.2f}**"
            )
        else:
            # F&O — entry/SL/TP are in spread/premium space, NOT the
            # underlying. Drawing them on the underlying chart would
            # be wrong (e.g. SL=83 on a spread is not the underlying
            # at price 83). Surface them in the caption instead.
            sl_txt = f"₹{float(sl_px):.2f}" if sl_px is not None else "—"
            tp_txt = f"₹{float(tp_px):.2f}" if tp_px is not None else "—"
            caption = (
                f"**{sym}** · {side} · qty {qty}  ·  underlying: `{chart_sym}`  \n"
                f"premium-space levels: entry ₹{entry_px:.2f} → now "
                f"₹{econ.current_price:.2f} · SL {sl_txt} / TP {tp_txt} · "
                f"unrealised **₹{unreal:+,.2f}**"
            )

        # Layout: a Streamlit-side header above the chart carries the
        # position symbol so the chart itself doesn't need a Plotly
        # title — that title was overlapping the EMA legend on
        # 2026-05-05 (legend at y=1.02 + Plotly title at y≈1.04 fight
        # for the same horizontal strip above the plot). The legend
        # now sits inside the plot in the top-right corner instead.
        fig.update_layout(
            height=300, xaxis_rangeslider_visible=False,
            margin=dict(l=10, r=10, t=10, b=10),
            legend=dict(
                orientation="h",
                yanchor="top",     y=0.98,
                xanchor="right",   x=0.99,
                bgcolor="rgba(255,255,255,0.85)",
                bordercolor="#e0e0e0",
                borderwidth=1,
                font=dict(size=10),
            ),
        )
        st.markdown(f"##### `{sym}`")
        st.plotly_chart(fig, width="stretch",
                        key=f"open_trade_chart_{sym}")
        st.markdown(caption)


def render_open_positions(positions: list[dict]) -> list:
    """Compact open-positions table + per-row fee-breakdown expander.

    Returns the list of :class:`PositionEconomics` objects so the F&O
    Greeks tab can re-use them without re-running the math.
    """
    if not positions:
        st.info("📭 No open positions. The bot will enter on the next valid signal.")
        return []

    econ_rows = []
    pos_econ = []
    for p in positions:
        side_str = p.get("side", "BUY")
        direction = "long" if side_str == "BUY" else "short"
        qty = abs(int(p.get("qty", 0)))
        entry = float(p.get("avg_price", 0))
        sl = p.get("stop_loss")
        tp = p.get("take_profit")
        unreal = float(p.get("unrealized_pnl", 0))
        if direction == "long":
            curr = entry + (unreal / qty if qty else 0)
        else:
            curr = entry - (unreal / qty if qty else 0)
        econ = position_economics(
            symbol=p.get("symbol", ""),
            direction=direction, qty=qty, entry_price=entry,
            current_price=curr, stop_loss=sl, take_profit=tp,
        )
        pos_econ.append(econ)

        sl_net = econ.if_sl_hit["net_pnl"] if econ.if_sl_hit else None
        tp_net = econ.if_tp_hit["net_pnl"] if econ.if_tp_hit else None

        econ_rows.append({
            "Symbol":    econ.symbol,
            "Side":      direction.upper(),
            "Qty":       qty,
            "Entry":     entry,
            "Now":       round(curr, 2),
            "SL":        sl,
            "TP":        tp,
            "Breakeven": econ.breakeven_price,
            "Net @ SL":  sl_net,
            "Net @ Now": econ.at_current["net_pnl"],
            "Net @ TP":  tp_net,
        })

    edf = pd.DataFrame(econ_rows)
    # NOTE: the inline ``_render_open_trade_charts(positions, pos_econ)``
    # call below this dataframe was added 2026-05-05 so the operator can
    # SEE each open trade's underlying price action without leaving the
    # Overview tab. It sits between the position table and the fee
    # breakdown expander as a visual debugger for "is this trade going
    # the way the strategy expected?".
    st.dataframe(
        edf, width="stretch", hide_index=True,
        column_config={
            "Entry":      st.column_config.NumberColumn(format="₹%.2f"),
            "Now":        st.column_config.NumberColumn(format="₹%.2f"),
            "Breakeven":  st.column_config.NumberColumn(
                format="₹%.2f", help="Exit price needed to net zero after fees"),
            "SL":         st.column_config.NumberColumn(format="₹%.2f"),
            "TP":         st.column_config.NumberColumn(format="₹%.2f"),
            "Net @ SL":   st.column_config.NumberColumn(
                format="₹%+.2f", help="Take-home P&L if stop-loss hits"),
            "Net @ Now":  st.column_config.NumberColumn(
                format="₹%+.2f", help="Take-home P&L if you closed at the current mark"),
            "Net @ TP":   st.column_config.NumberColumn(
                format="₹%+.2f", help="Take-home P&L if take-profit hits"),
        },
    )

    # ── Live trade charts (added 2026-05-05) ────────────────────────────
    # Renders one candle chart per open position so the operator can
    # visually correlate the trade's entry / SL / TP against the
    # underlying's price action right inside the Overview tab —
    # otherwise you have to flip to the Charts tab and manually pick
    # the symbol, and for F&O spreads the underlying isn't the same
    # token as the position symbol so it's easy to look at the wrong
    # chart by mistake.
    _render_open_trade_charts(positions, pos_econ)

    with st.expander("💸 Per-position fee breakdowns (every charge Zerodha would debit)"):
        for econ in pos_econ:
            st.markdown(
                f"**{econ.symbol}** — entry fees ₹{econ.entry_fees:.2f}  ·  "
                f"breakeven ₹{econ.breakeven_price:.2f}"
            )
            scenarios = {}
            if econ.if_sl_hit:
                scenarios[f"If SL hits (₹{econ.stop_loss:.2f})"] = econ.if_sl_hit
            scenarios[f"At current (₹{econ.current_price:.2f})"] = econ.at_current
            if econ.if_tp_hit:
                scenarios[f"If TP hits (₹{econ.take_profit:.2f})"] = econ.if_tp_hit
            for label, scn in scenarios.items():
                st.markdown(f"_{label}_")
                lines = [
                    ("",            "Entry leg",                                     "Exit leg"),
                    ("Side",        scn["entry_leg"]["side"],                        scn["exit_leg"]["side"]),
                    ("Turnover",    f"₹{scn['entry_leg']['turnover']:,.2f}",         f"₹{scn['exit_leg']['turnover']:,.2f}"),
                    ("Brokerage",   f"₹{scn['entry_leg']['brokerage']:.2f}",         f"₹{scn['exit_leg']['brokerage']:.2f}"),
                    ("STT",         f"₹{scn['entry_leg']['stt']:.2f}",               f"₹{scn['exit_leg']['stt']:.2f}"),
                    ("Exchange",    f"₹{scn['entry_leg']['exchange']:.2f}",          f"₹{scn['exit_leg']['exchange']:.2f}"),
                    ("SEBI",        f"₹{scn['entry_leg']['sebi']:.2f}",              f"₹{scn['exit_leg']['sebi']:.2f}"),
                    ("Stamp duty",  f"₹{scn['entry_leg']['stamp_duty']:.2f}",        f"₹{scn['exit_leg']['stamp_duty']:.2f}"),
                    ("GST",         f"₹{scn['entry_leg']['gst']:.2f}",               f"₹{scn['exit_leg']['gst']:.2f}"),
                    ("Leg total",   f"₹{scn['entry_leg']['total']:.2f}",             f"₹{scn['exit_leg']['total']:.2f}"),
                ]
                bdf = pd.DataFrame(lines[1:], columns=lines[0])
                st.dataframe(bdf, width="stretch", hide_index=True)
                st.caption(
                    f"Gross P&L: ₹{scn['gross_pnl']:+.2f}   |   "
                    f"Total round-trip fees: ₹{scn['fees_total']:.2f} "
                    f"({scn['fees_pct_of_turnover']:.3f}% of turnover)   |   "
                    f"**Net P&L: ₹{scn['net_pnl']:+.2f}**"
                )
            st.markdown("---")
    return pos_econ


# ────────────────────────── Picks / underlyings panel ────────────────────────

def render_picks_or_underlyings(*, cache, segment: Segment, watchlist: list[str]) -> None:
    """Equity → research picks; F&O → configured underlyings."""
    if segment == Segment.EQUITY:
        st.markdown("<div class='section-h'>🎯 Today's research picks</div>",
                    unsafe_allow_html=True)
        picks = cache.get_json(f"research:{date.today().isoformat()}") or []
        if picks:
            st.dataframe(pd.DataFrame(picks), width="stretch", hide_index=True)
        else:
            st.info("No research picks yet. The 08:30 IST scheduler populates this. "
                    "Run on demand: `python -m cli research`.")
    else:
        st.markdown("<div class='section-h'>📈 Today's underlyings</div>",
                    unsafe_allow_html=True)
        if watchlist:
            st.write(", ".join(watchlist))
        else:
            st.info("No F&O underlyings configured. Edit `fno.watchlist.symbols` in `config.yaml`.")
        st.caption(
            "F&O Phase 1 ships without a research agent — the bot trades the "
            "configured underlyings. A pre-market F&O research pass (option-chain "
            "scoring, IV-rank ranking) is on the Phase 6 roadmap."
        )


# ────────────────────────── Latest signals panel ─────────────────────────────

def render_latest_signals(*, cache, segment: Segment) -> None:
    st.markdown("<div class='section-h'>📡 Latest signals</div>",
                unsafe_allow_html=True)
    sig_keys = cache.keys(signal_pattern(segment))
    if not sig_keys:
        st.info(f"No {segment.label} signals cached yet. Start the bot with "
                f"`python -m cli run --segment {segment.value}`.")
        return
    rows = []
    for k in sig_keys:
        s = cache.get_json(k)
        if s:
            rows.append({"symbol": k.split(":", 2)[2], **s})
    if not rows:
        st.info("Signal keys found but all empty.")
        return
    sig_df = pd.DataFrame(rows).sort_values("ts", ascending=False)
    st.dataframe(sig_df, width="stretch", hide_index=True)


# ────────────────────────── Performance tab ──────────────────────────────────

def render_performance_tab(*, segment: Segment, summary: dict) -> None:
    st.markdown("<div class='section-h'>📊 Today's performance breakdown</div>",
                unsafe_allow_html=True)
    if summary.get("trades", 0) == 0:
        st.info("No trades closed yet today. The performance breakdown will populate as fills land.")
    else:
        with st.container(border=True):
            j1, j2, j3, j4, j5 = st.columns(5)
            j1.metric("Trades", summary["trades"])
            j2.metric("Win rate", f"{summary['win_rate']*100:.1f}%",
                      delta=f"{summary['wins']}W / {summary['losses']}L",
                      delta_color="off")
            j3.metric("Gross P&L", f"₹{summary['gross_pnl']:+,.2f}")
            j4.metric("Fees", f"₹{-summary['fees']:+,.2f}")
            pnl_color = "normal" if summary["net_pnl"] >= 0 else "inverse"
            j5.metric("Net P&L", f"₹{summary['net_pnl']:+,.2f}",
                      delta=f"{summary['return_pct']:+.3f}%",
                      delta_color=pnl_color)
            j6, j7, j8 = st.columns(3)
            j6.metric("Avg win",       f"₹{summary['avg_win']:+,.2f}")
            j7.metric("Avg loss",      f"₹{summary['avg_loss']:+,.2f}")
            j8.metric("Profit factor", str(summary["profit_factor"]))

        st.markdown("<div class='section-h'>📜 Trade journal — today</div>",
                    unsafe_allow_html=True)
        trades_df = pd.DataFrame(summary["trades_list"])
        show_cols = ["ts", "symbol", "side", "qty", "entry_price", "exit_price",
                     "duration_min", "gross_pnl", "fees", "net_pnl",
                     "exit_reason", "strategy"]
        show_cols = [c for c in show_cols if c in trades_df.columns]
        st.dataframe(trades_df[show_cols], width="stretch", hide_index=True)

        bs  = pd.DataFrame.from_dict(summary["by_strategy"], orient="index")
        bsm = pd.DataFrame.from_dict(summary["by_symbol"],   orient="index")
        cs1, cs2 = st.columns(2)
        if not bs.empty:
            cs1.markdown("**By strategy**")
            cs1.dataframe(bs,  width="stretch")
        if not bsm.empty:
            cs2.markdown("**By symbol**")
            cs2.dataframe(bsm, width="stretch")

    today = date.today()
    with st.expander("🪵 Live trade log (raw events)"):
        log_path = trades_jsonl(today, segment=segment)
        if log_path.exists():
            with log_path.open() as fh:
                lines = fh.readlines()
            st.caption(f"{log_path}  —  {len(lines)} event(s)")
            st.code("".join(lines[-200:]) or "(empty)", language="json")
        else:
            st.caption(f"No log yet at {log_path}")

    with st.expander("📄 End-of-day report"):
        eod_path = eod_report(today, segment=segment)
        if eod_path.exists():
            st.text(eod_path.read_text())
        else:
            st.caption("Report not generated yet (auto-runs after 15:15 IST square-off).")
        st.caption(f"CSV export: `{trades_csv(today, segment=segment)}`")


# ────────────────────────── Charts tab ───────────────────────────────────────

def render_charts_tab(*, segment: Segment, watchlist: list[str], cfg) -> None:
    st.markdown("<div class='section-h'>🕯️ Candlesticks + EMA + VWAP</div>",
                unsafe_allow_html=True)
    sym_options = watchlist or cfg.symbols
    if not sym_options:
        st.info("No symbols available for this segment.")
        return
    cc1, cc2 = st.columns([3, 1])
    sym = cc1.selectbox("Symbol", sym_options, key="chart_symbol")
    interval = cc2.selectbox("Interval", ["1m", "5m", "15m"], index=1, key="chart_interval")
    if not sym:
        return
    df = _cached_intraday_bars(sym, interval)
    if df.empty:
        st.warning("No bars yet for the selected symbol — markets may be closed or "
                   "yfinance may be rate-limiting. The bot will retry on the next tick.")
        return

    df = df.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert("Asia/Kolkata")
    elif str(df.index.tz) != "Asia/Kolkata":
        df.index = df.index.tz_convert("Asia/Kolkata")
    df["ema9"]  = ema(df["close"], 9)
    df["ema21"] = ema(df["close"], 21)
    df["vwap"]  = vwap(df)
    df["rsi"]   = rsi(df["close"], 14)

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["open"], high=df["high"],
        low=df["low"], close=df["close"], name=sym,
    ))
    fig.add_trace(go.Scatter(x=df.index, y=df["ema9"],  name="EMA 9",  line=dict(width=1)))
    fig.add_trace(go.Scatter(x=df.index, y=df["ema21"], name="EMA 21", line=dict(width=1)))
    fig.add_trace(go.Scatter(x=df.index, y=df["vwap"],  name="VWAP",
                             line=dict(width=1, dash="dash")))
    fig.update_layout(height=520, xaxis_rangeslider_visible=False,
                      margin=dict(l=10, r=10, t=30, b=10),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0))
    st.plotly_chart(fig, width="stretch")

    rfig = go.Figure()
    rfig.add_trace(go.Scatter(x=df.index, y=df["rsi"], name="RSI 14"))
    rfig.add_hline(y=70, line_dash="dot", line_color="red")
    rfig.add_hline(y=30, line_dash="dot", line_color="green")
    rfig.update_layout(height=200, title="RSI (14)",
                       margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(rfig, width="stretch")


# ────────────────────────── F&O Greeks tab ───────────────────────────────────

def render_greeks_tab(positions: list[dict]) -> None:
    """Per-leg Δ Γ Θ Vega for every open option / spread / iron condor."""
    if not positions:
        st.info("📭 No F&O positions open. Greeks will populate when an option, "
                "spread, or iron-condor position is filled.")
        return
    # Parsers / pricing / quote are now hoisted to module top — see the
    # imports block. ``latest_quote`` is wrapped in a 30s cache below so
    # multiple legs sharing an underlying only fetch the spot once.

    DEFAULT_IV = 0.15
    DEFAULT_R  = 0.07

    greek_rows = []
    net_delta = net_theta = net_vega = 0.0
    for p in positions:
        sym = p.get("symbol", "")
        qty = int(p.get("qty", 0))
        ic_meta  = parse_iron_condor_tradingsymbol(sym)
        sp_meta  = parse_spread_tradingsymbol(sym)
        opt_meta = parse_option_tradingsymbol(sym)
        if not (ic_meta or sp_meta or opt_meta):
            continue
        underlying = (ic_meta or sp_meta or opt_meta)["underlying"]
        spot_tick = _cached_latest_quote(underlying)
        spot = float(spot_tick.ltp) if spot_tick and spot_tick.ltp else None
        if spot is None or spot <= 0:
            continue
        if opt_meta:
            T = years_to_expiry(opt_meta["expiry"])
            g = all_greeks(spot, opt_meta["strike"], T, opt_meta["opt_type"],
                           sigma=DEFAULT_IV, r=DEFAULT_R)
            sign = 1 if qty > 0 else -1
            d = g["delta"] * abs(qty) * sign
            th = g["theta"] * abs(qty) * sign
            ve = g["vega"]  * abs(qty) * sign
            greek_rows.append({
                "Symbol":  sym, "Kind": "OPTION", "Spot": round(spot, 2),
                "Δ (per share)": round(g["delta"], 3), "Δ × qty": round(d, 1),
                "Γ":       round(g["gamma"], 5),
                "Θ /day":  round(th, 1), "Vega": round(ve, 1),
            })
        elif sp_meta:
            T = years_to_expiry(sp_meta["expiry"])
            short_g = all_greeks(spot, sp_meta["short_strike"], T,
                                 sp_meta["opt_type"], sigma=DEFAULT_IV, r=DEFAULT_R)
            long_g  = all_greeks(spot, sp_meta["long_strike"], T,
                                 sp_meta["opt_type"], sigma=DEFAULT_IV, r=DEFAULT_R)
            d_sh = (-short_g["delta"] + long_g["delta"]) * abs(qty)
            t_sh = (-short_g["theta"] + long_g["theta"]) * abs(qty)
            v_sh = (-short_g["vega"]  + long_g["vega"])  * abs(qty)
            greek_rows.append({
                "Symbol":  sym, "Kind": "SPREAD", "Spot": round(spot, 2),
                "Δ (per share)": round((-short_g["delta"] + long_g["delta"]), 3),
                "Δ × qty":       round(d_sh, 1),
                "Γ":       round(-short_g["gamma"] + long_g["gamma"], 5),
                "Θ /day":  round(t_sh, 1), "Vega": round(v_sh, 1),
            })
        elif ic_meta:
            T = years_to_expiry(ic_meta["expiry"])
            sp_g = all_greeks(spot, ic_meta["put_short"],  T, "PE", sigma=DEFAULT_IV, r=DEFAULT_R)
            lp_g = all_greeks(spot, ic_meta["put_long"],   T, "PE", sigma=DEFAULT_IV, r=DEFAULT_R)
            sc_g = all_greeks(spot, ic_meta["call_short"], T, "CE", sigma=DEFAULT_IV, r=DEFAULT_R)
            lc_g = all_greeks(spot, ic_meta["call_long"],  T, "CE", sigma=DEFAULT_IV, r=DEFAULT_R)
            d_per = -sp_g["delta"] + lp_g["delta"] - sc_g["delta"] + lc_g["delta"]
            g_per = -sp_g["gamma"] + lp_g["gamma"] - sc_g["gamma"] + lc_g["gamma"]
            t_per = -sp_g["theta"] + lp_g["theta"] - sc_g["theta"] + lc_g["theta"]
            v_per = -sp_g["vega"]  + lp_g["vega"]  - sc_g["vega"]  + lc_g["vega"]
            greek_rows.append({
                "Symbol":  sym, "Kind": "IRON_CONDOR", "Spot": round(spot, 2),
                "Δ (per share)": round(d_per, 3),
                "Δ × qty":       round(d_per * abs(qty), 1),
                "Γ":       round(g_per, 5),
                "Θ /day":  round(t_per * abs(qty), 1),
                "Vega":    round(v_per * abs(qty), 1),
            })
        else:
            continue
        net_delta += greek_rows[-1]["Δ × qty"]
        net_theta += greek_rows[-1]["Θ /day"]
        net_vega  += greek_rows[-1]["Vega"]

    if not greek_rows:
        st.info("F&O positions are open but no option/spread/IC legs detected. "
                "Greeks panel skipped (futures don't have Greeks).")
        return

    with st.container(border=True):
        cnet1, cnet2, cnet3 = st.columns(3)
        cnet1.metric("Net Δ", f"{net_delta:+.1f}",
                     help="Position-wide directional exposure in shares-of-spot. "
                          "Positive = bullish, negative = bearish, ≈0 = neutral.")
        cnet2.metric("Net Θ /day", f"₹{net_theta:+.1f}",
                     help="Theta = ₹ collected per CALENDAR day from time decay. "
                          "Positive = decay works for you (short premium).")
        cnet3.metric("Net Vega", f"₹{net_vega:+.1f}",
                     help="Vega = ₹ change for a 1.0 absolute IV change "
                          "(divide by 100 for 1%-IV shocks).")

    st.dataframe(pd.DataFrame(greek_rows), width="stretch", hide_index=True)
    st.caption(
        "Greeks computed via Black-Scholes at iv=15%, r=7% on the current spot. "
        "For credit spreads / iron condors, Δ and Θ are signed for the SHORT structure. "
        "Phase 5 swaps BS for live broker IV per leg."
    )


# ────────────────────────── System health tab ────────────────────────────────

_HEALTH_OVERALL_COLOR = {"OK": "#16a34a", "DEGRADED": "#ca8a04", "FAILED": "#dc2626"}
_HEALTH_STATUS_EMOJI  = {"OK": "🟢", "WARN": "🟡", "FAIL": "🔴"}
_HEALTH_CRON_HOURS    = (9, 11, 13, 15)


def _next_healthcheck_label() -> str:
    import pytz
    now = datetime.now(pytz.timezone("Asia/Kolkata"))
    if now.weekday() >= 5:
        return "Mon 09:00"
    for h in _HEALTH_CRON_HOURS:
        if now.hour < h:
            return f"{h:02d}:00 IST"
        if now.hour == h and now.minute == 0:
            return "now"
    return "Tomorrow 09:00"


def render_system_tab(cache, segment: Segment) -> None:
    """Health checks + fee audit + heartbeat trail — all the boring infra
    that you hope you never need to look at, organised so when you DO need it
    everything's one click away.
    """
    _hc         = cache.get_json(cache_key("healthcheck:latest", segment)) or {}
    _hc_history = cache.get_json(cache_key("healthcheck:history", segment)) or []
    _audit      = cache.get_json("fee_audit:latest") or {}

    st.markdown(f"<div class='section-h'>🩺 System health — {segment.label}</div>",
                unsafe_allow_html=True)
    if not _hc:
        st.info(
            f"No {segment.label} health-check report yet today. The scheduler "
            "runs the check at 09:00, 11:00, 13:00, 15:00 IST. Run it now: "
            f"`python -m cli healthcheck --segment {segment.value}`"
        )
        return

    overall = _hc.get("overall", "?")
    color = _HEALTH_OVERALL_COLOR.get(overall, "#6b7280")
    ts_iso = _hc.get("timestamp", "")
    try:
        ts_label = datetime.fromisoformat(ts_iso).strftime("%H:%M:%S IST")
    except Exception:
        ts_label = ts_iso[:19]
    n_ok = sum(1 for c in _hc.get("checks", []) if c.get("status") == "OK")
    n_total = len(_hc.get("checks", []))

    with st.container(border=True):
        h1, h2, h3, h4 = st.columns([1.4, 1, 1, 1])
        h1.markdown(
            f"<div class='kpi-mini-l'>OVERALL</div>"
            f"<div style='font-size:1.7rem;font-weight:800;color:{color};line-height:1;'>{overall}</div>",
            unsafe_allow_html=True,
        )
        h2.metric("Checks passed", f"{n_ok} / {n_total}")
        h3.metric("Last run", ts_label)
        h4.metric("Next run", _next_healthcheck_label())

    checks_df = pd.DataFrame([
        {
            "": _HEALTH_STATUS_EMOJI.get(c.get("status", ""), "⚪"),
            "Check":  c.get("name", ""),
            "Status": c.get("status", ""),
            "Detail": c.get("detail", ""),
        }
        for c in _hc.get("checks", [])
    ])
    if not checks_df.empty:
        st.dataframe(
            checks_df, width="stretch", hide_index=True,
            column_config={
                "":       st.column_config.TextColumn(width="small"),
                "Check":  st.column_config.TextColumn(width="medium"),
                "Status": st.column_config.TextColumn(width="small"),
                "Detail": st.column_config.TextColumn(width="large"),
            },
        )

    if _audit:
        sources_reachable = _audit.get("sources_reachable", []) or []
        sources_checked   = _audit.get("sources_checked", []) or []
        sources_pretty    = (
            f"{len(sources_reachable)}/{len(sources_checked)} sources reachable: "
            f"{', '.join(sources_reachable) or 'none'}"
            + (f"  (unreachable: {', '.join(set(sources_checked) - set(sources_reachable))})"
               if set(sources_checked) - set(sources_reachable) else "")
        )
        with st.expander(
            f"💰 Fee schedule audit — {_audit.get('status', '?')} "
            f"(verified {(_audit.get('timestamp', '') or '')[11:19] or '—'} IST · "
            f"{sources_pretty})"
        ):
            st.caption(_audit.get("summary", ""))

            def _verdict_emoji(v: str) -> str:
                return {
                    "OK":              "🟢",
                    "DRIFT_CONFIRMED": "🔴",
                    "DRIFT_SINGLE":    "🟠",
                    "AMBIGUOUS":       "🟣",
                    "UNVERIFIED":      "🟡",
                }.get(v, "⚪")

            def _src_summary(sources: list) -> str:
                """Compact per-source value display (e.g., 'z=0.05 u=0.05 d=—')."""
                bits = []
                for s in (sources or []):
                    label = (s.get("source") or "?")[:1]
                    val = s.get("value")
                    bits.append(f"{label}={val if val is not None else '—'}")
                return "  ".join(bits) or "(no sources)"

            audit_df = pd.DataFrame([
                {
                    "":           _verdict_emoji(c.get("verdict", "OK")),
                    "Segment":    (c.get("segment") or "equity").upper(),
                    "Rate":       (c.get("label", c.get("key", "")).split("] ", 1)[-1]
                                   if "] " in (c.get("label") or "")
                                   else c.get("label", c.get("key", ""))),
                    "Configured": c.get("configured"),
                    "Consensus":  c.get("observed"),
                    "Sources (z=Zerodha · u=Upstox · d=Dhan)": _src_summary(c.get("sources")),
                    "Verdict":    c.get("verdict", ""),
                }
                for c in _audit.get("checks", [])
            ])
            # Drifted rows on top; then equity → futures → options.
            if not audit_df.empty:
                verdict_rank = {"DRIFT_CONFIRMED": 0, "AMBIGUOUS": 1,
                                "DRIFT_SINGLE": 2, "UNVERIFIED": 3, "OK": 4}
                audit_df["_sort_v"] = audit_df["Verdict"].map(verdict_rank).fillna(9)
                seg_order = {"EQUITY": 0, "FUTURES": 1, "OPTIONS": 2}
                audit_df["_sort_seg"] = audit_df["Segment"].map(seg_order).fillna(99)
                audit_df = (audit_df
                            .sort_values(["_sort_v", "_sort_seg"], ascending=[True, True])
                            .drop(columns=["_sort_v", "_sort_seg"]))
            st.dataframe(audit_df, width="stretch", hide_index=True,
                         column_config={"": st.column_config.TextColumn(width="small")})

    summary = _hc.get("summary", {}) or {}
    if summary:
        with st.expander("📐 Summary metrics"):
            sdf = pd.DataFrame([(k, v) for k, v in summary.items()],
                               columns=["Metric", "Value"])
            st.dataframe(sdf, width="stretch", hide_index=True)

    if _hc_history:
        with st.expander(f"🕒 Today's health-check trail ({len(_hc_history)} runs)"):
            trail = pd.DataFrame([
                {
                    "Time": (datetime.fromisoformat(r["timestamp"]).strftime("%H:%M:%S")
                             if r.get("timestamp") else ""),
                    "Overall": r.get("overall", ""),
                    "OK":   sum(1 for c in r.get("checks", []) if c.get("status") == "OK"),
                    "WARN": sum(1 for c in r.get("checks", []) if c.get("status") == "WARN"),
                    "FAIL": sum(1 for c in r.get("checks", []) if c.get("status") == "FAIL"),
                }
                for r in _hc_history
            ])
            st.dataframe(trail, width="stretch", hide_index=True)


# ════════════════════════════════════════════════════════════════════════════
#  PAGE FLOW — top-to-bottom render of the whole dashboard.
# ════════════════════════════════════════════════════════════════════════════

st.set_page_config(page_title="Stock Market Bot", layout="wide",
                   page_icon="📈")
st.markdown(_CSS, unsafe_allow_html=True)

cfg   = _cached_load_config()
cache = get_cache()

# ─────────────────────────── Auto-refresh (meta-refresh) ─────────────────────
# The bot ticks once per minute. Anything more frequent is wasted Redis
# ops + journal disk reads + potential yfinance contention. We use a
# pure HTML meta-refresh tag (no extra deps) — full page reload is fine
# at this cadence on a local laptop. The pause toggle in the sidebar
# (rendered below) sets ``st.session_state['autorefresh_paused']``; when
# True we omit the tag and the page holds steady for reading.
st.session_state.setdefault("autorefresh_paused", False)
if not st.session_state["autorefresh_paused"]:
    _refresh_secs = 60 if is_market_open() else 300
    st.markdown(
        f"<meta http-equiv='refresh' content='{_refresh_secs}'>",
        unsafe_allow_html=True,
    )

# ─────────────────────────── Sidebar ─────────────────────────────────────────
st.sidebar.markdown("## 🎛️ Controls")

_segment_options = ["Equity"]
if fno_enabled(cfg):
    _segment_options.append("F&O")
_segment_choice = st.sidebar.radio(
    "Segment", _segment_options, index=0,
    help="Equity and F&O are isolated — they share zero state.",
)
seg = Segment.FNO if _segment_choice == "F&O" else Segment.EQUITY

seg_capital   = cfg_capital(cfg, seg)
seg_risk      = cfg_risk(cfg, seg)
seg_watchlist = cfg_watchlist_symbols(cfg, seg)

# Combined-P&L card — both segments at a glance.
#
# 2026-05-05: previously this widget only surfaced REALIZED P&L (i.e.
# closed round-trips from ``daily_summary``). On a day where every
# trade is still open — e.g. F&O bought 2 credit spreads at 13:26 and
# both were running into the EOD square-off — the widget showed
# "₹+0.00 / 0 trades" while the bot legitimately had ~₹+96 unrealized
# on 2 winning positions. The operator's reasonable conclusion ("no
# trades happened") was wrong, and the dashboard was silently lying.
# We now surface BOTH realized (from the journal) and unrealized
# (from the live ``portfolio:{seg}`` snapshot) so the live picture
# matches reality even before any close fires.
with st.sidebar.container(border=True):
    st.markdown("**Combined P&L (today)**")
    _eq_summary  = _cached_daily_summary(date.today(), Segment.EQUITY)
    _fno_summary = (_cached_daily_summary(date.today(), Segment.FNO)
                    if fno_enabled(cfg) else {"net_pnl": 0, "trades": 0})
    eq_net  = float(_eq_summary.get("net_pnl", 0)  or 0)
    fno_net = float(_fno_summary.get("net_pnl", 0) or 0)
    eq_n_closed  = int(_eq_summary.get("trades", 0)  or 0)
    fno_n_closed = int(_fno_summary.get("trades", 0) or 0)

    def _seg_unreal_open(_seg: Segment) -> tuple[float, int]:
        snap = cache.get_json(cache_key("portfolio", _seg)) or {}
        positions = snap.get("positions", []) or []
        return (sum(float(p.get("unrealized_pnl", 0) or 0) for p in positions),
                len([p for p in positions if int(p.get("qty", 0)) != 0]))

    eq_unreal,  eq_n_open  = _seg_unreal_open(Segment.EQUITY)
    fno_unreal, fno_n_open = (_seg_unreal_open(Segment.FNO)
                              if fno_enabled(cfg) else (0.0, 0))

    st.metric("Equity (closed)", f"₹{eq_net:+,.2f}",
              delta=f"{eq_n_closed} trades")
    if eq_n_open or eq_unreal:
        st.caption(f"+ ₹{eq_unreal:+,.2f} unrealized · {eq_n_open} open")
    if fno_enabled(cfg):
        st.metric("F&O (closed)", f"₹{fno_net:+,.2f}",
                  delta=f"{fno_n_closed} trades")
        if fno_n_open or fno_unreal:
            st.caption(f"+ ₹{fno_unreal:+,.2f} unrealized · {fno_n_open} open")
        st.metric("Combined (realized + unrealized)",
                  f"₹{eq_net + fno_net + eq_unreal + fno_unreal:+,.2f}")

# Risk envelope card — capital + caps for the SELECTED segment so the
# operator can answer "what's my worst day look like?" without reading config.
with st.sidebar.container(border=True):
    st.markdown(f"**Risk envelope · {seg.label}**")
    st.caption(
        f"Capital: **₹{seg_capital.total:,.0f}**  ·  "
        f"deployable {int(seg_capital.deployable_pct*100)}%"
    )
    max_loss_inr = seg_capital.total * seg_risk.max_daily_loss_pct / 100.0
    target_inr   = seg_capital.total * seg_risk.daily_profit_target_pct / 100.0
    st.caption(
        f"Daily profit target: **₹{target_inr:,.0f}** ({seg_risk.daily_profit_target_pct}%)  ·  "
        f"loss cutoff: **−₹{max_loss_inr:,.0f}** ({seg_risk.max_daily_loss_pct}%)"
    )
    st.caption(
        f"Per-trade loss cap: {seg_risk.max_loss_per_trade_pct}%  ·  "
        f"max trades/day: {seg_risk.max_trades_per_day}  ·  "
        f"max open: {seg_risk.max_open_positions}"
    )

# Quick actions.
with st.sidebar.container(border=True):
    st.markdown("**Quick actions**")
    if st.button("🔄 Refresh holiday calendar", width="stretch",
                 help="Force a fresh fetch from NSE"):
        cal = refresh_holidays()
        # Bust the dashboard's holiday cache so subsequent renders see
        # the freshly-fetched calendar instead of the 1h-cached copy.
        _cached_get_holidays.clear()
        st.success(f"Refreshed: {cal.last_refresh[:19].replace('T', ' ')} IST")
    st.session_state["autorefresh_paused"] = st.toggle(
        "⏸ Pause auto-refresh",
        value=st.session_state.get("autorefresh_paused", False),
        help="Hold the page steady while reading a chart. Auto-refresh "
             "runs every 60s during market hours, 5 min when closed.",
    )
    st.caption(
        "Health check runs automatically at 09:00 / 11:00 / 13:00 / 15:00 IST. "
        "Manual: `python -m cli healthcheck`."
    )

# ─────────────────────────── Header (status pills) ───────────────────────────
st.markdown("# 📈 Indian Stock Market — Bot Dashboard")

mode = "🔴 LIVE" if env().LIVE_TRADING and env().BROKER != "paper" else "🟢 PAPER"
_today_status = market_status(date.today(), seg, calendar=_cached_get_holidays())
_hb = cache.get_json(cache_key("heartbeat:tick", seg)) or {}

render_header_pills(
    mode=mode, segment=seg, broker=env().BROKER,
    market_status_dict=_today_status, hb=_hb,
)

# ─────────────────────────── Heartbeat-stale banner ──────────────────────────
# Compute heartbeat age unconditionally — used for the in-market alert AND
# the out-of-market staleness note below the hero strip.
_hb_age: float | None = None
_hb_ts = None
if _hb:
    try:
        _hb_ts = datetime.fromisoformat(_hb["ts"])
        _hb_age = (datetime.now(_hb_ts.tzinfo) - _hb_ts).total_seconds()
    except Exception:
        _hb_age = None

if _hb and is_market_open():
    if _hb_age is not None and _hb_age > 180:
        mins = int(_hb_age // 60)
        st.error(
            f"🛑 [{seg.label}] Executor stalled — last tick was {mins} min ago. "
            f"The bot is NOT managing positions right now. "
            f"Check `tail -f logs/bot_*.log` and "
            f"`python -m cli healthcheck --segment {seg.value}`."
        )
    elif _hb_age is not None and _hb_age > 90:
        st.warning(f"⚠️ [{seg.label}] Slow ticks — last tick was {int(_hb_age)}s ago "
                   "(expected <60s).")

# ─────────────────────────── Portfolio snapshot read ─────────────────────────
snap = cache.get_json(cache_key("portfolio", seg)) or {}
positions = snap.get("positions", [])
saved_capital = snap.get("starting_capital")
if saved_capital is not None and abs(saved_capital - seg_capital.total) > 0.5:
    # Capital changed in config since the bot last persisted state. Show a
    # warning so the operator notices, but DO NOT mutate Redis from the
    # render path: the bot's ``_restore_state`` already discards a stale
    # snapshot on next start (regression-pinned by FIX #2 in
    # ``tests/test_fixes.py``). Deleting here from a Streamlit rerun
    # would race the live bot — two browser tabs or a rerun mid-tick
    # could wipe a freshly-written portfolio.
    st.warning(
        f"⚠️ [{seg.label}] Snapshot's starting_capital "
        f"₹{saved_capital:,.0f} ≠ configured ₹{seg_capital.total:,.0f}. "
        "The bot will reconcile on its next start (`_restore_state` "
        "discards mismatched snapshots). Restart the bot OR run "
        "`python -m cli reset --segment {seg}` to clear manually."
    )
    snap, positions = {}, []
cash       = snap.get("cash", seg_capital.total)
realized   = sum(p.get("realized_pnl", 0)   for p in positions)
unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)

_summary       = _cached_daily_summary(date.today(), seg)
_lockin        = cache.get_json(cache_key("profit_lockin", seg)) or {}
_today_pnl_pct = float(snap.get("daily_pnl_pct", 0.0))

# Out-of-market staleness note — explicitly tells the operator the figures
# below come from the bot's last-persisted snapshot, not live ticks. The
# in-market alert above is loud (red error / yellow warning); this one is
# informational so post-session reviews don't mistake last-known state for
# current state. Threshold matches the in-market alert (3 min).
if _hb and not is_market_open() and _hb_age is not None and _hb_age > 180:
    if _hb_ts is not None:
        _stale_ts = _hb_ts.strftime("%Y-%m-%d %H:%M IST")
    else:
        _stale_ts = "unknown"
    st.info(
        f"ℹ️ [{seg.label}] Market closed — figures below are the bot's "
        f"last persisted snapshot (heartbeat at {_stale_ts}, "
        f"{int(_hb_age // 60)} min ago). They are not live."
    )

# ─────────────────────────── Hero KPI strip ──────────────────────────────────
render_hero_kpis(
    cash=cash, realized=realized, unrealized=unrealized,
    n_open=len(positions),
    today_pnl_pct=_today_pnl_pct,
    target_pct=seg_risk.daily_profit_target_pct,
    loss_cutoff_pct=seg_risk.max_daily_loss_pct,
    capital_total=seg_capital.total,
    summary=_summary, lockin=_lockin,
)

# ─────────────────────────── Tabbed body ─────────────────────────────────────
_tab_labels = ["📊 Overview", "📈 Performance", "🕯️ Charts"]
if seg == Segment.FNO:
    _tab_labels.append("Ω F&O Greeks")
_tab_labels.append("🩺 System")
_tabs = st.tabs(_tab_labels)

# Overview tab —— positions on the left, market schedule + picks + signals on the right.
with _tabs[0]:
    left, right = st.columns([1.55, 1.0])
    with left:
        st.markdown("<div class='section-h'>💼 Open positions — net P&L after charges & taxes</div>",
                    unsafe_allow_html=True)
        render_open_positions(positions)
    with right:
        render_market_schedule_compact()
        render_picks_or_underlyings(cache=cache, segment=seg, watchlist=seg_watchlist)
        render_latest_signals(cache=cache, segment=seg)

# Performance tab.
with _tabs[1]:
    render_performance_tab(segment=seg, summary=_summary)

# Charts tab.
with _tabs[2]:
    render_charts_tab(segment=seg, watchlist=seg_watchlist, cfg=cfg)

# F&O Greeks tab — only when F&O selected.
_idx = 3
if seg == Segment.FNO:
    with _tabs[_idx]:
        render_greeks_tab(positions)
    _idx += 1

# System tab.
with _tabs[_idx]:
    render_system_tab(cache, segment=seg)

# ─────────────────────────── Footer ──────────────────────────────────────────
st.divider()
st.caption(
    f"Last updated: {datetime.now().isoformat(timespec='seconds')}  ·  "
    f"segment: {seg.label}  ·  "
    f"backup of prior layout: `dashboard_code_bkp_20260430.py` "
    f"(restore with `cp dashboard_code_bkp_20260430.py app.py`)"
)
