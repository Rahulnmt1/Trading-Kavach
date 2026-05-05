"""Typer CLI for the trading bot.

Examples:
  python -m cli run                              # paper trading (default safe mode)
  python -m cli run --live                       # live trading (requires LIVE_TRADING=true)
  python -m cli login                            # daily Zerodha TOTP login
  python -m cli backtest --days 14
  python -m cli backtest --symbol RELIANCE --days 30
  python -m cli walk-forward --days 60           # walk-forward + Sharpe / Sortino / MDD
  python -m cli research                         # one-off pre-market research run
  python -m cli update-watchlist                 # rebuild watchlist by trend + momentum
  python -m cli notify-test                      # send a test email via SMTP
  python -m cli regen-readme                     # rebuild README module map
  python -m cli dashboard                        # launch the Streamlit UI
  python -m cli status                           # one-shot snapshot: picks, watchlist, risk, positions
  python -m cli journal                          # show today's P&L statement
  python -m cli journal --date 2026-04-25        # show a past day's statement
  python -m cli journal --tail                   # follow today's trade log live
  python -m cli journal --write-eod              # (re)generate the EOD report file
  python -m cli healthcheck                      # run periodic health check now
  python -m cli healthcheck --notify             # also email the report
  python -m cli healthcheck --json               # machine-readable output
  python -m cli verify-fees                      # audit fee/tax rates against Zerodha's published page
  python -m cli verify-fees --json               # machine-readable
  python -m cli holidays                         # show NSE-sourced holiday calendar (next 14 days)
  python -m cli holidays --refresh               # force-refresh from NSE
  python -m cli holidays --json                  # machine-readable
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from bot.backtest import backtest_symbol, backtest_watchlist
from bot.backtest_advanced import (
    compute_metrics,
    walk_forward_symbol,
    walk_forward_watchlist,
)
from bot.config import env, load_config
from bot.logger import logger
from bot.notify import get_notifier
from bot.research import run_research
from bot.watchlist_updater import update_watchlist

app = typer.Typer(add_completion=False, help="Indian Stock Market — automated intraday trading bot.")
console = Console()


@app.command()
def run(
    live: bool = typer.Option(False, "--live", help="Use real broker. Requires LIVE_TRADING=true in .env."),
    paper: bool = typer.Option(False, "--paper", help="Explicit paper-mode flag (default behavior; ignored if --live is also passed)."),
    segment: str = typer.Option("equity", "--segment", help="Trading segment: equity (default) or fno."),
    force_lock: bool = typer.Option(
        False, "--force-lock",
        help="Break a stale singleton lock at .bot.lock.<segment>. Only use if you're certain no other bot is running.",
    ),
):
    """Start the bot scheduler for ONE segment. Runs until Ctrl+C or 15:30 IST.

    The two segments (equity / fno) run in **separate processes** with
    separate locks (.bot.lock.equity vs .bot.lock.fno), separate Redis
    namespaces, separate capital budgets, and separate trade journals.
    Run them concurrently in two terminals if you want both:

        terminal 1:  python -m cli run --segment equity
        terminal 2:  python -m cli run --segment fno

    Refuses to start if another bot is already running for this segment
    (single-instance enforcement). Two concurrent bots in the SAME segment
    cause state corruption and phantom orders — see bot/lock.py post-mortem.
    """
    _ = paper  # accepted for clarity in the README; paper is the default whenever --live isn't set.

    from bot.segment import Segment
    try:
        seg = Segment.parse(segment)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)

    from bot.lock import acquire as acquire_lock, BotAlreadyRunningError
    try:
        acquire_lock(segment=seg, force=force_lock)
    except BotAlreadyRunningError as e:
        console.print(f"[red bold]{e}[/red bold]")
        raise typer.Exit(code=1)

    # Warn loudly if starting on battery — the 2026-05-05 morning sleep
    # blackout was caused by exactly this and ``caffeinate -i`` alone is
    # not sufficient to prevent macOS standby on battery.
    from bot.power import battery_warning_lines
    for line in battery_warning_lines():
        console.print(f"[yellow bold]{line}[/yellow bold]")

    if live:
        if not env().LIVE_TRADING:
            console.print("[red]Set LIVE_TRADING=true in .env first. Aborting.[/red]")
            raise typer.Exit(code=1)
        console.print("[red bold]LIVE MODE — real orders will be placed. You have 5 seconds to abort.[/red bold]")
        import time
        time.sleep(5)
    else:
        os.environ["LIVE_TRADING"] = "false"
        os.environ["BROKER"] = "paper"

    # Apply F&O rollover buffer from config so the futures resolver rolls
    # to the next monthly contract a couple of days BEFORE expiry rather
    # than holding the dying contract until expiry day. Affects only the
    # F&O segment but is harmless to set unconditionally.
    if seg == Segment.FNO:
        from bot.config import load_config
        from bot.instruments.fno import set_rollover_buffer_days
        cfg = load_config()
        if cfg.fno is not None:
            set_rollover_buffer_days(cfg.fno.rollover_buffer_days)

    from bot.scheduler import start
    start(segment=seg)


@app.command()
def research():
    """Run pre-market research now and print top picks."""
    picks = run_research()
    if not picks:
        console.print("[yellow]No picks generated. Check that the watchlist symbols return data.[/yellow]")
        return
    table = Table(title="Pre-market research — top picks")
    table.add_column("Symbol", style="cyan")
    table.add_column("Bias", style="magenta")
    table.add_column("Score", justify="right")
    table.add_column("Rationale", overflow="fold")
    for p in picks:
        table.add_row(p.symbol, p.bias, f"{p.score:.2f}", p.rationale)
    console.print(table)


@app.command()
def backtest(
    symbol: str = typer.Option(None, "--symbol", help="Single symbol to backtest. Omit to test the whole watchlist."),
    days: int = typer.Option(7, "--days", help="Lookback days (yfinance caps intraday at ~7)."),
    interval: str = typer.Option("5m", "--interval", help="Bar interval: 1m, 5m, 15m..."),
):
    """Run the strategy ensemble against historical bars."""
    if symbol:
        result = backtest_symbol(symbol, days, interval)
        console.print(result["summary"])
        return
    result = backtest_watchlist(days, interval)
    table = Table(title=f"Backtest watchlist — {days} days @ {interval}")
    table.add_column("Symbol", style="cyan")
    table.add_column("Trades", justify="right")
    table.add_column("Wins", justify="right")
    table.add_column("Win rate", justify="right")
    table.add_column("Net P&L", justify="right")
    for sym, s in result["symbols"].items():
        wr = f"{s['win_rate']*100:.1f}%"
        pnl = f"₹{s['net_pnl']:,.2f}"
        pnl_style = "green" if s["net_pnl"] >= 0 else "red"
        table.add_row(sym, str(s["trades"]), str(s["wins"]), wr,
                      f"[{pnl_style}]{pnl}[/{pnl_style}]")
    console.print(table)
    tot = result["totals"]
    console.print(f"\n[bold]Total trades:[/bold] {tot['trades']} | "
                  f"[bold]Win rate:[/bold] {tot['win_rate']*100:.1f}% | "
                  f"[bold]Net P&L:[/bold] ₹{tot['net_pnl']:,.2f}")


@app.command()
def dashboard():
    """Launch the Streamlit dashboard."""
    here = Path(__file__).parent
    cmd = [sys.executable, "-m", "streamlit", "run", str(here / "app.py")]
    console.print(f"[green]Launching dashboard:[/green] {' '.join(cmd)}")
    subprocess.run(cmd)


@app.command()
def show_config():
    """Print resolved configuration."""
    cfg = load_config()
    console.print({
        "broker": env().BROKER,
        "live": env().LIVE_TRADING,
        "capital": cfg.capital.model_dump(),
        "risk": cfg.risk.model_dump(),
        "session": cfg.session.model_dump(),
        "strategies_enabled": cfg.strategies.enabled,
        "multitimeframe_enabled": cfg.strategies.multitimeframe.enabled,
        "watchlist_size": len(cfg.symbols),
        "watchlist_updater_enabled": cfg.watchlist_updater.enabled,
        "ws_feed": cfg.feed.use_websocket,
        "notifier": get_notifier().enabled,
    })


@app.command()
def login():
    """Run the daily Zerodha TOTP login and refresh KITE_ACCESS_TOKEN in .env."""
    from scripts.zerodha_login import login as do_login
    token = do_login()
    if token:
        console.print(f"[green]Access token refreshed.[/green] (first 8 chars: {token[:8]}…)")
    else:
        console.print("[red]Login failed. Check logs for details.[/red]")
        raise typer.Exit(code=1)


@app.command("update-watchlist")
def update_watchlist_cmd():
    """Rebuild today's watchlist from the NIFTY 100 universe by trend + momentum."""
    selected = update_watchlist()
    if not selected:
        console.print("[yellow]No symbols cleared the filters.[/yellow]")
        return
    table = Table(title="Updated watchlist")
    table.add_column("Symbol", style="cyan")
    table.add_column("Bias")
    table.add_column("Score", justify="right")
    table.add_column("Momentum %", justify="right")
    table.add_column("SMA-slope %", justify="right")
    table.add_column("Avg vol", justify="right")
    for c in selected:
        table.add_row(c.symbol, c.bias, f"{c.score:.2f}",
                      f"{c.momentum_pct:+.2f}", f"{c.sma20_slope_pct:+.2f}",
                      f"{c.avg_volume:,.0f}")
    console.print(table)


@app.command("walk-forward")
def walk_forward_cmd(
    symbol: str = typer.Option(None, "--symbol", help="Single symbol; omit to walk the whole watchlist."),
    days: int = typer.Option(60, "--days", help="Total history to use."),
    interval: str = typer.Option("5m", "--interval"),
):
    """Run walk-forward analysis with full performance metrics."""
    if symbol:
        result = walk_forward_symbol(symbol, days=days, interval=interval)
        console.print({"symbol": symbol, "aggregate": result["aggregate"]})
        if result["windows"]:
            table = Table(title=f"Per-window metrics: {symbol}")
            table.add_column("OOS window", style="cyan")
            for k in ["trades", "win_rate", "net_pnl", "sharpe", "sortino", "max_drawdown_pct"]:
                table.add_column(k.replace("_", " "), justify="right")
            for w in result["windows"]:
                table.add_row(
                    w["oos"],
                    str(w["trades"]),
                    f"{w['win_rate']*100:.1f}%",
                    f"₹{w['net_pnl']:,.2f}",
                    f"{w['sharpe']:.2f}",
                    f"{w['sortino']:.2f}",
                    f"{w['max_drawdown_pct']:.2f}%",
                )
            console.print(table)
        return

    result = walk_forward_watchlist(days=days, interval=interval)
    table = Table(title=f"Walk-forward — {days}d @ {interval}")
    table.add_column("Symbol", style="cyan")
    for k in ["trades", "win_rate", "net_pnl", "sharpe", "sortino", "max_drawdown_pct", "calmar"]:
        table.add_column(k.replace("_", " "), justify="right")
    for sym, m in result["symbols"].items():
        pnl_style = "green" if m["net_pnl"] >= 0 else "red"
        table.add_row(
            sym,
            str(m["trades"]),
            f"{m['win_rate']*100:.1f}%",
            f"[{pnl_style}]₹{m['net_pnl']:,.2f}[/{pnl_style}]",
            f"{m['sharpe']:.2f}",
            f"{m['sortino']:.2f}",
            f"{m['max_drawdown_pct']:.2f}%",
            f"{m['calmar']:.2f}",
        )
    console.print(table)


@app.command("notify-test")
def notify_test():
    """Send a test email and wait for the SMTP server's real verdict."""
    n = get_notifier()
    if not n.enabled:
        console.print("[yellow]Notifier disabled. Set SMTP_HOST and NOTIFY_TO in .env.[/yellow]")
        raise typer.Exit(code=1)
    console.print(f"[dim]→ contacting {n.host}:{n.port} as {n.user} (synchronous, ~5s)…[/dim]")
    sent = n.send(
        "Smoke test",
        "If you can read this, the bot's email pipeline works.",
        "INFO",
        wait=True,
    )
    if sent:
        console.print(f"[green]✓ Delivered to {len(n.recipients)} recipient(s).[/green]  "
                      f"[dim]Check your inbox.[/dim]")
    else:
        console.print(
            "[red]✗ Send failed.[/red]  "
            "[dim]See logs/bot_*.log for the SMTP error code & message.[/dim]"
        )
        raise typer.Exit(code=1)


@app.command("regen-readme")
def regen_readme():
    """Regenerate the auto-managed Module map section of README.md."""
    from scripts.update_readme import regenerate
    changed = regenerate()
    console.print("[green]README updated.[/green]" if changed else "README already up-to-date.")


@app.command()
def journal(
    date_str: str = typer.Option(None, "--date", help="YYYY-MM-DD (default: today)."),
    tail: bool = typer.Option(False, "--tail", help="Follow today's trade log live (Ctrl+C to stop)."),
    write_eod: bool = typer.Option(False, "--write-eod", help="Regenerate the EOD report file for the given date."),
    segment: str = typer.Option("equity", "--segment", help="Segment: equity (default) or fno."),
):
    """Show per-day live trade log and detailed P&L statement for ONE segment."""
    from datetime import date as _date, datetime as _dt
    import time

    from bot.journal import (
        _hhmmss, daily_summary, eod_report, trades_csv, trades_jsonl,
        write_eod_report,
    )
    from bot.segment import Segment, cfg_capital

    try:
        seg = Segment.parse(segment)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)

    day = _dt.strptime(date_str, "%Y-%m-%d").date() if date_str else _date.today()

    if tail:
        path = trades_jsonl(day, segment=seg)
        console.print(f"[cyan]Tailing[/cyan] {path} ([magenta]{seg.label}[/magenta]) — Ctrl+C to stop.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        with path.open() as fh:
            fh.seek(0, 2)
            try:
                while True:
                    line = fh.readline()
                    if not line:
                        time.sleep(0.5)
                        continue
                    _print_trade_event(line)
            except KeyboardInterrupt:
                console.print("\n[dim]stopped[/dim]")
        return

    if write_eod:
        path = write_eod_report(day, segment=seg)
        console.print(f"[green]EOD report written:[/green] {path}")

    summary = daily_summary(day, segment=seg)
    if summary["trades"] == 0:
        console.print(f"[yellow]No trades recorded for {summary['date']} ({seg.label}).[/yellow]")
        console.print(f"Live log path: {trades_jsonl(day, segment=seg)}")
        return

    header = Table(title=f"Daily P&L — {summary['date']} [{seg.label}]", show_header=False, box=None)
    header.add_column(justify="right", style="bold")
    header.add_column()
    cap = cfg_capital(load_config(), seg).total
    pnl_color = "green" if summary["net_pnl"] >= 0 else "red"
    header.add_row("Capital", f"₹{cap:,.2f}  →  ₹{cap + summary['net_pnl']:,.2f}  "
                              f"([{pnl_color}]{summary['return_pct']:+.3f}%[/{pnl_color}])")
    header.add_row("Trades", f"{summary['trades']}  ({summary['wins']} wins / {summary['losses']} losses, "
                             f"{summary['win_rate']*100:.1f}% win rate)")
    header.add_row("Gross P&L", f"₹{summary['gross_pnl']:+,.2f}")
    header.add_row("Fees",      f"₹{-summary['fees']:+,.2f}")
    header.add_row("Net P&L",   f"[{pnl_color}]₹{summary['net_pnl']:+,.2f}[/{pnl_color}]")
    header.add_row("Avg win / loss", f"₹{summary['avg_win']:+,.2f}  /  ₹{summary['avg_loss']:+,.2f}")
    header.add_row("Profit factor",  str(summary["profit_factor"]))
    if summary["biggest_win"]:
        bw = summary["biggest_win"]
        header.add_row("Biggest win", f"₹{bw['net_pnl']:+,.2f}  ({bw['symbol']})")
    if summary["biggest_loss"]:
        bl = summary["biggest_loss"]
        header.add_row("Biggest loss", f"₹{bl['net_pnl']:+,.2f}  ({bl['symbol']})")
    console.print(header)

    tt = Table(title="Trade-by-trade")
    tt.add_column("Time", style="cyan")
    tt.add_column("Side")
    tt.add_column("Symbol", style="magenta")
    tt.add_column("Qty", justify="right")
    tt.add_column("Entry", justify="right")
    tt.add_column("Exit", justify="right")
    tt.add_column("Net P&L", justify="right")
    tt.add_column("Reason")
    for t in summary["trades_list"]:
        et = _hhmmss(t.get("entry_time"))
        net = t["net_pnl"]
        st = "green" if net >= 0 else "red"
        tt.add_row(et, t["side"], t["symbol"], str(t["qty"]),
                   f"{t['entry_price']:.2f}", f"{t['exit_price']:.2f}",
                   f"[{st}]₹{net:+,.2f}[/{st}]", t["exit_reason"])
    console.print(tt)

    console.print(f"\n[dim]live log:[/dim] {trades_jsonl(day, segment=seg)}")
    console.print(f"[dim]csv:[/dim]      {trades_csv(day, segment=seg)}")
    console.print(f"[dim]eod report:[/dim] {eod_report(day, segment=seg)}")


@app.command()
def status(
    segment: str = typer.Option("equity", "--segment", help="Segment: equity (default) or fno."),
):
    """One-shot snapshot of today's picks, auto-watchlist, risk budget and open positions for ONE segment.

    Runs out-of-process (read-only) — safe to invoke any time, even while the
    bot is live. Reads from the same cache the bot writes to and from the
    on-disk trade journal under ``logs/``.
    """
    from datetime import date as _date, datetime as _dt

    import pytz

    from bot.cache import get_cache
    from bot.journal import _load_events, daily_summary, trades_jsonl
    from bot.research import todays_picks
    from bot.risk import KILL_SWITCH
    from bot.segment import Segment, cache_key, cfg_capital, cfg_risk
    from bot.watchlist_updater import auto_watchlist

    try:
        seg = Segment.parse(segment)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)

    IST = pytz.timezone("Asia/Kolkata")
    cfg = load_config()
    e = env()
    cache = get_cache()
    today = _date.today()
    now = _dt.now(IST)
    seg_capital = cfg_capital(cfg, seg)
    seg_risk = cfg_risk(cfg, seg)

    trade_start = cfg.session.t("trade_start")
    trade_cutoff = cfg.session.t("trade_cutoff")
    square_off = cfg.session.t("square_off")
    if now.weekday() >= 5:
        window_state, window_color = "WEEKEND", "dim"
    elif now.time() < trade_start:
        window_state, window_color = "PRE-MARKET", "yellow"
    elif now.time() <= trade_cutoff:
        window_state, window_color = "TRADING", "green"
    elif now.time() < square_off:
        window_state, window_color = "CUTOFF (no new entries)", "yellow"
    else:
        window_state, window_color = "POST-MARKET", "dim"
    mode = "LIVE" if e.LIVE_TRADING else "PAPER"
    mode_color = "red" if e.LIVE_TRADING else "green"

    console.print()
    console.print(
        f"[bold]Stock Market Bot — Status[/bold]   "
        f"[bold magenta]{seg.label}[/bold magenta]   "
        f"{now.strftime('%Y-%m-%d %H:%M:%S')} IST   "
        f"[{mode_color}]{mode}[/{mode_color}] · broker={e.BROKER} · "
        f"window=[{window_color}]{window_state}[/{window_color}] · "
        f"cache={'redis' if getattr(cache, '_is_redis', False) else 'in-memory'}"
    )

    # ---- Risk budget --------------------------------------------------------
    summary = daily_summary(today, segment=seg)
    realized = summary.get("net_pnl", 0.0)
    closed_count = summary.get("trades", 0)

    portfolio = cache.get_json(cache_key("portfolio", seg)) or {}
    saved_cap = portfolio.get("starting_capital")
    if saved_cap is not None and abs(saved_cap - seg_capital.total) > 0.5:
        portfolio = {}
    positions = portfolio.get("positions", []) or []
    cash = portfolio.get("cash", seg_capital.total)
    unrealized = sum(p.get("unrealized_pnl", 0) or 0 for p in positions)

    open_entries = sum(
        1 for ev in _load_events(today, segment=seg)
        if ev.get("type") == "TRADE_OPEN"
    )
    trades_used = max(closed_count, open_entries)

    capital = seg_capital.total
    day_pnl = realized + unrealized
    daily_drawdown_pct = -day_pnl / capital * 100 if capital and day_pnl < 0 else 0.0

    rb = Table(show_header=False, box=None, pad_edge=False)
    rb.add_column(justify="right", style="bold")
    rb.add_column()
    rb.add_row("Segment",        seg.label)
    rb.add_row("Capital",        f"₹{capital:,.2f}")
    rb.add_row("Cash",           f"₹{cash:,.2f}")
    pnl_color = "green" if day_pnl >= 0 else "red"
    rb.add_row("Realized today", f"[{pnl_color}]₹{realized:+,.2f}[/{pnl_color}]   "
                                 f"({closed_count} closed trade{'s' if closed_count != 1 else ''})")
    rb.add_row("Unrealized",     f"[{pnl_color}]₹{unrealized:+,.2f}[/{pnl_color}]")
    rb.add_row("Day P&L",        f"[{pnl_color}]₹{day_pnl:+,.2f}[/{pnl_color}]   "
                                 f"({day_pnl / capital * 100:+.3f}% of capital)")
    dd_color = "red" if daily_drawdown_pct >= seg_risk.max_daily_loss_pct * 0.7 else "green"
    rb.add_row("Daily loss used",
               f"[{dd_color}]{daily_drawdown_pct:.2f}%[/{dd_color}] / "
               f"{seg_risk.max_daily_loss_pct:.2f}%   "
               f"(per-trade cap {seg_risk.max_loss_per_trade_pct:.2f}%)")
    tu_color = "red" if trades_used >= seg_risk.max_trades_per_day else "green"
    rb.add_row("Trades used",
               f"[{tu_color}]{trades_used}[/{tu_color}] / {seg_risk.max_trades_per_day}")
    op_color = "red" if len(positions) >= seg_risk.max_open_positions else "green"
    rb.add_row("Open positions",
               f"[{op_color}]{len(positions)}[/{op_color}] / {seg_risk.max_open_positions}")
    rb.add_row("Kill switch", "[red]ACTIVE[/red]" if KILL_SWITCH.exists() else "off")
    rb.add_row("Trading window",
               f"{trade_start.strftime('%H:%M')}–{trade_cutoff.strftime('%H:%M')}  •  "
               f"square-off {square_off.strftime('%H:%M')}")
    console.print()
    console.print("[bold cyan]Risk budget[/bold cyan]")
    console.print(rb)

    # ---- Today's research picks --------------------------------------------
    picks = todays_picks()
    console.print()
    console.print("[bold cyan]Today's research picks[/bold cyan]")
    if not picks:
        console.print("  [dim]none cached — run [white]python -m cli research[/white] "
                      "(pre-market job runs daily at "
                      f"{cfg.research.run_at} IST)[/dim]")
    else:
        rp = Table()
        rp.add_column("Symbol", style="cyan")
        rp.add_column("Bias", style="magenta")
        rp.add_column("Score", justify="right")
        rp.add_column("Rationale", overflow="fold")
        for p in picks:
            rp.add_row(p.symbol, p.bias, f"{p.score:.2f}", p.rationale)
        console.print(rp)

    # ---- Watchlist (segment-aware) ----------------------------------------
    if seg == Segment.FNO:
        # F&O: read configured underlyings (no auto-watchlist for F&O).
        from bot.segment import cfg_watchlist_symbols
        wl = cfg_watchlist_symbols(cfg, seg)
        label = "fno.watchlist (config.yaml)"
    else:
        wl = auto_watchlist()
        is_static_fallback = wl == list(cfg.symbols)
        label = "config.yaml fallback" if is_static_fallback else "auto-watchlist (cached)"
    console.print()
    console.print(f"[bold cyan]Watchlist[/bold cyan]  [dim]({label}, {len(wl)} symbols)[/dim]")
    if wl:
        console.print("  " + ", ".join(wl))
    else:
        console.print("  [dim](empty)[/dim]")

    # ---- Open positions — net P&L after charges ----------------------------
    console.print()
    console.print(f"[bold cyan]Open positions[/bold cyan]  ({len(positions)})  "
                  "[dim]net P&L = gross − all fees & taxes (Zerodha-style)[/dim]")
    if not positions:
        console.print("  [dim](none)[/dim]")
    else:
        from bot.fees import position_economics

        op = Table()
        op.add_column("Symbol", style="cyan")
        op.add_column("Side")
        op.add_column("Qty", justify="right")
        op.add_column("Entry", justify="right")
        op.add_column("Breakeven", justify="right")
        op.add_column("SL → Net", justify="right")
        op.add_column("Now → Net", justify="right")
        op.add_column("TP → Net", justify="right")
        op.add_column("RT fees", justify="right")

        for p in positions:
            side_str = p.get("side", "BUY")
            direction = "long" if side_str == "BUY" else "short"
            qty = abs(int(p.get("qty", 0)))
            entry = float(p.get("avg_price", 0))
            sl = p.get("stop_loss")
            tp = p.get("take_profit")
            unreal = float(p.get("unrealized_pnl", 0) or 0)
            curr = entry + (unreal / qty if qty else 0) if direction == "long" \
                   else entry - (unreal / qty if qty else 0)

            econ = position_economics(
                symbol=p.get("symbol", ""), direction=direction,
                qty=qty, entry_price=entry, current_price=curr,
                stop_loss=sl, take_profit=tp,
            )
            sl_cell = "—"
            if econ.if_sl_hit:
                v = econ.if_sl_hit["net_pnl"]
                col = "red" if v < 0 else "green"
                sl_cell = f"₹{sl:.2f}\n[{col}]₹{v:+.2f}[/{col}]"
            now_v = econ.at_current["net_pnl"]
            now_col = "green" if now_v >= 0 else "red"
            now_cell = f"₹{econ.current_price:.2f}\n[{now_col}]₹{now_v:+.2f}[/{now_col}]"
            tp_cell = "—"
            if econ.if_tp_hit:
                v = econ.if_tp_hit["net_pnl"]
                col = "green" if v >= 0 else "red"
                tp_cell = f"₹{tp:.2f}\n[{col}]₹{v:+.2f}[/{col}]"
            rt_cell = f"₹{econ.at_current['fees_total']:.2f}"

            op.add_row(
                econ.symbol, direction.upper(), str(qty),
                f"₹{entry:.2f}", f"₹{econ.breakeven_price:.2f}",
                sl_cell, now_cell, tp_cell, rt_cell,
            )
        console.print(op)

        # Detailed line-item table per position (entry leg + exit leg).
        for p in positions:
            side_str = p.get("side", "BUY")
            direction = "long" if side_str == "BUY" else "short"
            qty = abs(int(p.get("qty", 0)))
            entry = float(p.get("avg_price", 0))
            sl = p.get("stop_loss")
            tp = p.get("take_profit")
            unreal = float(p.get("unrealized_pnl", 0) or 0)
            curr = entry + (unreal / qty if qty else 0) if direction == "long" \
                   else entry - (unreal / qty if qty else 0)
            econ = position_economics(
                symbol=p.get("symbol", ""), direction=direction,
                qty=qty, entry_price=entry, current_price=curr,
                stop_loss=sl, take_profit=tp,
            )
            console.print()
            console.print(f"[bold magenta]{econ.symbol}[/bold magenta] — "
                          f"fee breakdown   "
                          f"[dim](entry fees ₹{econ.entry_fees:.2f}, "
                          f"breakeven ₹{econ.breakeven_price:.2f})[/dim]")
            scenarios = []
            if econ.if_sl_hit:
                scenarios.append((f"If SL hits (₹{sl:.2f})", econ.if_sl_hit, "red"))
            scenarios.append((f"At current (₹{econ.current_price:.2f})", econ.at_current, None))
            if econ.if_tp_hit:
                scenarios.append((f"If TP hits (₹{tp:.2f})", econ.if_tp_hit, "green"))

            bd = Table(show_header=True, box=None, pad_edge=False)
            bd.add_column("Charge", style="dim")
            for lbl, _, _ in scenarios:
                bd.add_column(lbl, justify="right")

            rows = ["Brokerage","STT","Exchange","SEBI","Stamp duty","GST","Leg total"]
            keys = ["brokerage","stt","exchange","sebi","stamp_duty","gst","total"]
            for r, k in zip(rows, keys):
                cells = [r]
                for _, scn, _ in scenarios:
                    e_v = scn["entry_leg"][k]
                    x_v = scn["exit_leg"][k]
                    cells.append(f"₹{e_v:.2f} + ₹{x_v:.2f}")
                bd.add_row(*cells)
            footer = ["Net P&L"]
            for _, scn, color in scenarios:
                v = scn["net_pnl"]
                col = color or ("green" if v >= 0 else "red")
                footer.append(f"[bold {col}]₹{v:+.2f}[/bold {col}]")
            bd.add_row(*footer)
            console.print(bd)

    # ---- Footer -------------------------------------------------------------
    console.print()
    console.print(f"[dim]journal: {trades_jsonl(today, segment=seg)}[/dim]")
    if not getattr(cache, "_is_redis", False):
        console.print(
            "[dim]note: Redis is unavailable — research picks, auto-watchlist and "
            "open positions are read from an in-memory cache that does not survive "
            "process restarts. Data here may differ from a separately-running bot. "
            "Start Redis to share state across processes.[/dim]"
        )


@app.command()
def healthcheck(
    notify: bool = typer.Option(
        False, "--notify",
        help="Also email the report to NOTIFY_TO via the configured SMTP server.",
    ),
    as_json: bool = typer.Option(
        False, "--json",
        help="Emit machine-readable JSON instead of the pretty table.",
    ),
    segment: str = typer.Option(
        "equity", "--segment",
        help="Run the healthcheck for which segment (equity / fno).",
    ),
):
    """Run the periodic health check immediately and print the result.

    This is the same check the scheduler invokes at 09:00, 11:00, 13:00,
    15:00 IST per segment. Always safe to run — read-only apart from
    auto-cleaning stale ``signal:<segment>:*`` cache keys from previous days.
    """
    import json as _json
    from bot.healthcheck import run_healthcheck
    from bot.segment import Segment

    try:
        seg = Segment.parse(segment)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)

    report = run_healthcheck(segment=seg, notify=notify)

    if as_json:
        console.print_json(_json.dumps(report.to_dict(), default=str))
        if report.overall == "FAILED":
            raise typer.Exit(code=2)
        if report.overall == "DEGRADED":
            raise typer.Exit(code=1)
        return

    overall_color = {"OK": "green", "DEGRADED": "yellow", "FAILED": "red"}[report.overall]
    console.print()
    console.print(
        f"[bold]Health check[/bold]  "
        f"{report.timestamp.strftime('%Y-%m-%d %H:%M:%S')} IST   "
        f"overall: [{overall_color} bold]{report.overall}[/{overall_color} bold]"
    )

    table = Table(show_header=True, box=None, pad_edge=False)
    table.add_column("Status", style="bold", width=6)
    table.add_column("Check", style="cyan")
    table.add_column("Detail", overflow="fold")
    for c in report.checks:
        st_color = {"OK": "green", "WARN": "yellow", "FAIL": "red"}[c.status]
        table.add_row(f"[{st_color}]{c.status}[/{st_color}]", c.name, c.detail)
    console.print(table)

    if report.summary:
        s_table = Table(show_header=False, box=None, pad_edge=False)
        s_table.add_column(justify="right", style="bold")
        s_table.add_column()
        for k, v in report.summary.items():
            s_table.add_row(k, str(v))
        console.print()
        console.print("[bold cyan]Summary[/bold cyan]")
        console.print(s_table)

    if notify:
        from bot.notify import get_notifier
        n = get_notifier()
        if n.enabled:
            console.print(f"\n[dim]→ emailed to {len(n.recipients)} recipient(s)[/dim]")
        else:
            console.print("\n[yellow]--notify requested but SMTP is not configured (.env).[/yellow]")

    if report.overall == "FAILED":
        raise typer.Exit(code=2)
    if report.overall == "DEGRADED":
        raise typer.Exit(code=1)


@app.command("verify-fees")
def verify_fees(
    as_json: bool = typer.Option(
        False, "--json",
        help="Emit machine-readable JSON instead of the pretty table.",
    ),
):
    """Audit the fee/tax rates baked into the bot against multiple
    authoritative sources.

    The scheduler runs this automatically at 09:00 IST every weekday —
    invoke this command to run an extra check on demand. It fetches the
    published charges/pricing pages from three independent SEBI-regulated
    brokers (Zerodha, Upstox, Dhan), parses each one, and cross-verifies
    every line-item against ``bot/fees.py::_RATES`` / ``_FUTURES_RATES``
    / ``_OPTIONS_RATES``.

    Verdicts per rate:
      * OK              — every reachable source agrees with the configured value
      * DRIFT_CONFIRMED — ≥2 sources independently agree on a different value
      * DRIFT_SINGLE    — only one source observed a different value
      * AMBIGUOUS       — sources disagree with each other
      * UNVERIFIED      — no source could observe this rate today

    Exit codes:
      * 0 — every rate is OK
      * 1 — any DRIFT / AMBIGUOUS / UNVERIFIED rate, OR any source unreachable
    """
    import json as _json
    from bot.fee_audit import run_fee_audit

    result = run_fee_audit()

    if as_json:
        console.print_json(_json.dumps(result.to_dict(), default=str))
    else:
        st_color = {"OK": "green", "WARN": "yellow", "FAIL": "red"}[result.status]
        console.print()
        console.print(
            f"[bold]Fee schedule audit[/bold]  "
            f"{result.timestamp[:19].replace('T', ' ')} IST   "
            f"status: [{st_color} bold]{result.status}[/{st_color} bold]"
        )
        unreachable = sorted(set(result.sources_checked) - set(result.sources_reachable))
        console.print(
            f"[dim]sources reachable: {', '.join(result.sources_reachable) or 'none'}"
            + (f"   unreachable: {', '.join(unreachable)}" if unreachable else "")
            + "[/dim]"
        )
        console.print(f"[dim]{result.summary}[/dim]")
        table = Table(show_header=True, box=None, pad_edge=False)
        table.add_column("Seg", style="cyan")
        table.add_column("Rate", style="cyan")
        table.add_column("Configured", justify="right")
        table.add_column("Consensus", justify="right")
        table.add_column("Sources", overflow="fold")
        table.add_column("Verdict", overflow="fold")
        # Sort drift rows to the top so the operator's eye lands there first.
        verdict_rank = {"DRIFT_CONFIRMED": 0, "AMBIGUOUS": 1, "DRIFT_SINGLE": 2,
                        "UNVERIFIED": 3, "OK": 4}
        seg_rank = {"equity": 0, "futures": 1, "options": 2}
        sorted_checks = sorted(
            result.checks,
            key=lambda c: (verdict_rank.get(c.verdict, 9),
                           seg_rank.get(c.segment, 9),
                           c.key),
        )
        for c in sorted_checks:
            obs = f"{c.observed}" if c.observed is not None else "—"
            verdict_color = {
                "OK":              "green",
                "DRIFT_CONFIRMED": "red",
                "DRIFT_SINGLE":    "yellow",
                "AMBIGUOUS":       "magenta",
                "UNVERIFIED":      "yellow",
            }.get(c.verdict, "white")
            # Per-source compact view: "z=0.05 u=0.05 d=—"
            src_strs = []
            for s in c.sources:
                src_dict = s if isinstance(s, dict) else (
                    s.to_dict() if hasattr(s, "to_dict") else {})
                src_label = src_dict.get("source", "?")[:1]
                val = src_dict.get("value")
                src_strs.append(f"{src_label}={val if val is not None else '—'}")
            # Strip the per-segment prefix from the label for cleaner display.
            rate_label = c.label.split("] ", 1)[-1] if "] " in c.label else c.label
            table.add_row(
                c.segment.upper(),
                rate_label,
                f"{c.configured}",
                obs,
                " ".join(src_strs) or "(no sources)",
                f"[{verdict_color}]{c.verdict}[/{verdict_color}]",
            )
        console.print(table)

    if result.status == "FAIL":
        raise typer.Exit(code=2)
    if result.status == "WARN":
        raise typer.Exit(code=1)


@app.command("holidays")
def holidays_cmd(
    refresh: bool = typer.Option(
        False, "--refresh",
        help="Force a fresh fetch from NSE (otherwise reads from Redis cache).",
    ),
    days: int = typer.Option(
        14, "--days",
        help="Number of upcoming days to show (default: 14).",
    ),
    segment: str = typer.Option(
        "both", "--segment",
        help="Filter: equity | fno | both (default).",
    ),
    as_json: bool = typer.Option(
        False, "--json",
        help="Emit machine-readable JSON instead of the pretty table.",
    ),
):
    """Show the NSE-sourced trading-holiday calendar for equity and F&O.

    NSE publishes per-segment holiday lists at ``/api/holiday-master?type=trading``
    — this command fetches them, caches the result for 24h in Redis, and prints
    the next ``--days`` days for one or both segments. The dashboard reads
    from the same cache so a single ``--refresh`` updates both surfaces.
    """
    import json as _json
    from datetime import date as _date, timedelta as _td

    from bot.holidays import get_holidays, refresh_holidays, market_status
    from bot.segment import Segment

    cal = refresh_holidays() if refresh else get_holidays(allow_refresh=True)

    seg_filter = segment.lower().strip()
    if seg_filter not in {"equity", "fno", "both"}:
        console.print(f"[red]Invalid --segment: {segment} (use equity | fno | both)[/red]")
        raise typer.Exit(code=2)
    segs = (
        [Segment.EQUITY, Segment.FNO]
        if seg_filter == "both"
        else [Segment.EQUITY if seg_filter == "equity" else Segment.FNO]
    )

    today = _date.today()
    rows = []
    for offset in range(days):
        d = today + _td(days=offset)
        for seg in segs:
            rows.append(market_status(d, seg, calendar=cal))

    if as_json:
        console.print_json(_json.dumps({
            "calendar": {
                "source": cal.source,
                "last_refresh": cal.last_refresh,
                "totals": {seg: len(lst) for seg, lst in cal.by_segment.items()},
            },
            "rows": rows,
        }, default=str))
        return

    src_color = {"nse": "green", "bootstrap": "yellow", "stale": "yellow"}.get(cal.source, "white")
    console.print()
    console.print(
        f"[bold]NSE trading-holiday calendar[/bold]  "
        f"source: [{src_color} bold]{cal.source}[/{src_color} bold]   "
        f"last refresh: {cal.last_refresh[:19].replace('T', ' ')} IST   "
        f"equity={len(cal.by_segment.get('equity', []))} "
        f"f&o={len(cal.by_segment.get('fno', []))}"
    )
    if cal.source == "bootstrap":
        console.print(
            "[yellow]⚠ NSE was unreachable AND Redis cache was empty — "
            "showing the hardcoded 2026 bootstrap list. Run with `--refresh` "
            "once you're back online to pull the real calendar.[/yellow]"
        )

    table = Table(show_header=True, box=None, pad_edge=False)
    table.add_column("Date", style="cyan")
    table.add_column("Day")
    table.add_column("Segment")
    table.add_column("Status", justify="center")
    table.add_column("Reason / Hours", overflow="fold")
    for r in rows:
        st_color = {"OPEN": "green", "HOLIDAY": "red", "WEEKEND": "dim"}[r["status"]]
        if r["is_open"]:
            detail = f"{r.get('open', '09:15')} – {r.get('close', '15:30')} IST"
        else:
            detail = r.get("reason") or ""
        seg_label = "Equity" if r["segment"] == "equity" else "F&O"
        table.add_row(
            r["date"], r["weekday"], seg_label,
            f"[{st_color} bold]{r['status']}[/{st_color} bold]",
            detail,
        )
    console.print(table)


def _print_trade_event(raw: str) -> None:
    """Pretty-print one JSONL line from the trade log (used by --tail)."""
    import json as _json

    from bot.journal import _hhmmss
    try:
        e = _json.loads(raw)
    except Exception:
        console.print(raw.rstrip())
        return
    ts = _hhmmss(e.get("ts"))
    typ = e.get("type")
    if typ == "FILL":
        console.print(f"[cyan]{ts}[/cyan] FILL    {e.get('side','?'):<4} {e.get('symbol',''):<10} "
                      f"{e.get('qty',0):>4}@{e.get('price',0):.2f}  fees ₹{e.get('fees',0):.2f}  "
                      f"[dim]{e.get('strategy','')}[/dim]")
    elif typ == "TRADE_OPEN":
        console.print(f"[yellow]{ts}[/yellow] OPEN    {e.get('side','?'):<5} {e.get('symbol',''):<10} "
                      f"{e.get('qty',0):>4}@{e.get('entry_price',0):.2f}  "
                      f"SL={e.get('stop_loss')} TP={e.get('take_profit')}  "
                      f"[dim]{e.get('strategy','')}[/dim]")
    elif typ == "TRADE_CLOSED":
        net = e.get("net_pnl", 0)
        st = "green" if net >= 0 else "red"
        console.print(f"[{st}]{ts} CLOSED[/{st}]  {e.get('side','?'):<5} {e.get('symbol',''):<10} "
                      f"{e.get('qty',0):>4} {e.get('entry_price',0):.2f}→{e.get('exit_price',0):.2f}  "
                      f"net [{st}]₹{net:+,.2f}[/{st}]  ({e.get('exit_reason','')})")
    else:
        console.print(raw.rstrip())


if __name__ == "__main__":
    app()
