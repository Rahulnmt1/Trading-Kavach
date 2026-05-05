"""Periodic health check for the running bot.

Every 2 hours during the trading day (09:00, 11:00, 13:00, 15:00 IST) the
scheduler invokes :func:`run_healthcheck`, which runs a battery of checks
covering process health, OS sleep prevention, cache state, market data
freshness, risk budget and disk space.

The result is a :class:`HealthReport` with three severity tiers per check
(``OK`` / ``WARN`` / ``FAIL``) and an aggregate ``overall`` status of
``OK`` / ``DEGRADED`` / ``FAILED``. Each report is:

  1. **Published to the cache** under ``healthcheck:latest`` (also
     ``healthcheck:history`` keeps the last ~10 runs as a list) so the
     Streamlit dashboard can render it live.
  2. **Persisted** to ``logs/healthcheck/YYYY-MM-DD.jsonl`` for audit.
  3. **Logged** as a one-line summary in the rotating bot log.

Email delivery is opt-in (CLI ``--notify`` flag) and not used by the
scheduler; the dashboard is the canonical surface for at-a-glance status.

You can also run the same check manually via::

    python -m cli healthcheck            # print to console + publish to dashboard cache
    python -m cli healthcheck --notify   # also email (rare; off by default)
    python -m cli healthcheck --json     # machine-readable
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytz

from .cache import get_cache
from .config import PROJECT_ROOT, env, load_config
from .logger import logger
from .segment import (
    Segment,
    cache_key,
    cfg_capital,
    cfg_risk,
    signal_pattern,
)

IST = pytz.timezone("Asia/Kolkata")

# Aggregate severity ordering — higher == worse.
_SEVERITY_RANK = {"OK": 0, "WARN": 1, "FAIL": 2}
_OVERALL_LABEL = {0: "OK", 1: "DEGRADED", 2: "FAILED"}


# ─── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    name: str
    status: str  # OK | WARN | FAIL
    detail: str

    def emoji(self) -> str:
        return {"OK": "[OK]", "WARN": "[WARN]", "FAIL": "[FAIL]"}.get(self.status, "[?]")


@dataclass
class HealthReport:
    timestamp: datetime
    overall: str
    checks: List[CheckResult]
    summary: Dict[str, Any] = field(default_factory=dict)

    # ---- representations -------------------------------------------------

    def to_subject(self) -> str:
        return (
            f"Health check {self.timestamp.strftime('%H:%M IST')} — "
            f"{self.overall}  "
            f"({sum(1 for c in self.checks if c.status == 'OK')}/"
            f"{len(self.checks)} OK)"
        )

    def to_text(self) -> str:
        lines: List[str] = []
        lines.append("=" * 64)
        lines.append(
            f"  STOCK BOT — HEALTH CHECK  "
            f"{self.timestamp.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )
        lines.append(f"  Overall: {self.overall}")
        lines.append("=" * 64)
        lines.append("")
        for c in self.checks:
            lines.append(f"  {c.emoji():<6} {c.name:<28} {c.detail}")
        lines.append("")
        if self.summary:
            lines.append("Summary")
            lines.append("─" * 32)
            for k, v in self.summary.items():
                lines.append(f"  {k:<22} {v}")
        lines.append("")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "overall": self.overall,
            "checks": [asdict(c) for c in self.checks],
            "summary": self.summary,
        }


# ─── Individual checks ────────────────────────────────────────────────────────


def _bot_process() -> CheckResult:
    """Locate the running ``cli run`` Python process and report its uptime.

    Two paths:
      1. When this check runs *inside* the bot (via the scheduler), we are by
         definition the running process — use ``os.getpid()`` directly. This
         is bullet-proof and avoids a subtle macOS quirk where ``pgrep -f``
         called from an APScheduler thread can occasionally return empty
         stdout despite the target process being alive (the cause of a false
         FAIL on 2026-04-29 at 11:00).
      2. When invoked externally (e.g. ``python -m cli healthcheck`` from
         another terminal), we shell out to ``pgrep -f`` to find the bot.
         If the only match is *our own* PID, that means no separate bot is
         running.
    """
    my_pid = os.getpid()
    my_cmd = _process_cmdline(my_pid)
    i_am_the_bot = "cli run" in my_cmd

    if i_am_the_bot:
        et = _process_etime(my_pid)
        return CheckResult("Bot process", "OK",
                           f"self pid={my_pid}, uptime={et or '?'}")

    try:
        out = subprocess.run(
            ["pgrep", "-f", "cli run"],
            capture_output=True, text=True, timeout=5,
        )
        pids = [int(p) for p in out.stdout.split() if p.strip().isdigit()]
        pids = [p for p in pids if p != my_pid]
        if not pids:
            return CheckResult("Bot process", "FAIL", "no `cli run` process found")
        pid = pids[0]
        et = _process_etime(pid)
        return CheckResult("Bot process", "OK", f"pid={pid}, uptime={et or '?'}")
    except Exception as e:
        return CheckResult("Bot process", "WARN", f"could not inspect ps: {e}")


def _process_cmdline(pid: int) -> str:
    try:
        return subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def _process_etime(pid: int) -> str:
    try:
        return subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def _sleep_lock() -> CheckResult:
    """Confirm caffeinate (or another agent) is preventing macOS idle-sleep."""
    try:
        out = subprocess.run(
            ["pmset", "-g", "assertions"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except FileNotFoundError:
        # Non-macOS host — sleep prevention is irrelevant.
        return CheckResult("Sleep prevention", "OK", "non-macOS host (no pmset)")
    except Exception as e:
        return CheckResult("Sleep prevention", "WARN", f"pmset failed: {e}")

    has_caffeinate = "caffeinate" in out
    prevents_sleep = "PreventUserIdleSystemSleep     1" in out
    if has_caffeinate and prevents_sleep:
        return CheckResult("Sleep prevention", "OK",
                           "caffeinate is holding PreventUserIdleSystemSleep")
    if prevents_sleep:
        return CheckResult("Sleep prevention", "OK",
                           "system-sleep prevented (no caffeinate but lock held by another agent)")
    return CheckResult(
        "Sleep prevention", "FAIL",
        "no sleep-lock — Mac may sleep and silence the scheduler. "
        "Restart the bot via `bash scripts/run_bot.sh`.",
    )


def _redis_health() -> CheckResult:
    cache = get_cache()
    if not getattr(cache, "_is_redis", False):
        return CheckResult(
            "Redis", "WARN",
            "in-memory fallback — cache will not survive bot restart",
        )
    try:
        ok = bool(cache.client.ping())
        n_keys = len(cache.keys("*"))
        return CheckResult("Redis", "OK" if ok else "FAIL",
                           f"PONG, {n_keys} keys")
    except Exception as e:
        return CheckResult("Redis", "FAIL", f"ping failed: {e}")


def _config_check(segment: Segment = Segment.EQUITY) -> CheckResult:
    """Validate the *segment's* config block (was leaking equity values
    into F&O before — both segments showed equity capital/strategies/etc.)."""
    cfg = load_config()
    issues: List[str] = []
    cap = cfg_capital(cfg, segment)
    risk = cfg_risk(cfg, segment)
    if segment == Segment.FNO and (cfg.fno is None or not cfg.fno.enabled):
        return CheckResult("Config", "FAIL",
                           "F&O segment requested but fno.enabled=false in config.yaml")
    strats = cfg.fno.strategies if segment == Segment.FNO and cfg.fno else cfg.strategies
    syms = (cfg.fno.watchlist.get("symbols", []) if segment == Segment.FNO and cfg.fno
            else list(cfg.symbols))

    if cap.total <= 0:
        issues.append("capital.total <= 0")
    if strats.ensemble.min_agree < 1:
        issues.append("ensemble.min_agree < 1")
    if risk.sl_atr_mult <= 0 or risk.tp_atr_mult <= 0:
        issues.append("ATR multipliers must be > 0")
    detail = (
        f"capital=₹{cap.total:,.0f}, min_agree={strats.ensemble.min_agree}, "
        f"sl_atr={risk.sl_atr_mult}, tp_atr={risk.tp_atr_mult}, "
        f"watchlist={len(syms)} syms"
    )
    if issues:
        return CheckResult("Config", "FAIL", f"{detail} — {'; '.join(issues)}")
    return CheckResult("Config", "OK", detail)


def _premarket_caches(segment: Segment = Segment.EQUITY) -> CheckResult:
    """Equity needs both 08:00 watchlist + 08:30 research. F&O Phase 1 ships
    WITHOUT a research agent — for F&O, only the auto-watchlist matters
    (and even that is shared with equity, not F&O-specific yet)."""
    from .research import todays_picks
    from .watchlist_updater import auto_watchlist
    auto = auto_watchlist()
    n_auto = len(auto)
    if segment == Segment.FNO:
        cfg = load_config()
        n_syms = len(cfg.fno.watchlist.get("symbols", [])) if cfg.fno else 0
        if n_syms == 0:
            return CheckResult("Pre-market caches", "FAIL",
                               "fno.watchlist.symbols is empty — F&O bot has nothing to trade")
        return CheckResult("Pre-market caches", "OK",
                           f"{n_syms} F&O underlyings configured "
                           f"(F&O research agent lands in Phase 6; auto-watchlist={n_auto} not used by F&O)")
    picks = todays_picks()
    n_picks = len(picks)
    if n_picks == 0 and n_auto == 0:
        return CheckResult("Pre-market caches", "FAIL",
                           "no research picks AND no auto-watchlist — bot will fall through to static config")
    if n_picks == 0:
        return CheckResult("Pre-market caches", "WARN",
                           f"research picks empty; auto-watchlist has {n_auto} (executor will use those)")
    return CheckResult("Pre-market caches", "OK",
                       f"{n_picks} research picks + {n_auto} auto-watchlist symbols")


def _signal_freshness(segment: Segment = Segment.EQUITY) -> CheckResult:
    """During trading hours signals should be < 90s old (executor.tick fires every minute)."""
    cfg = load_config()
    now = datetime.now(IST)
    cache = get_cache()
    keys = [k for k in cache.keys(signal_pattern(segment))]
    if not keys:
        if _in_trading_window(now, cfg):
            return CheckResult("Signal stream", "FAIL",
                               f"no {signal_pattern(segment)} keys during trading window — "
                               f"executor.tick may not be firing for the {segment.label} bot")
        return CheckResult("Signal stream", "OK", "no signals (outside trading window)")

    today = now.date().isoformat()
    today_keys: List[Dict[str, Any]] = []
    for k in keys:
        v = cache.get_json(k) or {}
        if "ts" in v:
            today_keys.append(v)

    if not today_keys:
        return CheckResult("Signal stream", "WARN",
                           f"{len(keys)} signal keys but none have a 'ts' field")

    latest_ts = max(v["ts"] for v in today_keys)
    try:
        latest_dt = datetime.fromisoformat(latest_ts)
    except Exception:
        return CheckResult("Signal stream", "WARN",
                           f"latest signal has unparsable ts: {latest_ts}")
    age = (now - latest_dt).total_seconds()

    same_day = sum(1 for v in today_keys if v["ts"][:10] == today)
    detail = f"{same_day}/{len(today_keys)} updated today, latest age {age:.0f}s"
    if _in_trading_window(now, cfg):
        if age > 180:
            return CheckResult("Signal stream", "FAIL",
                               detail + " — executor.tick is silent during trading hours")
        if age > 90:
            return CheckResult("Signal stream", "WARN", detail)
    return CheckResult("Signal stream", "OK", detail)


def _data_freshness(segment: Segment = Segment.EQUITY) -> CheckResult:
    """Sample one symbol from the segment's universe and check bar
    freshness. Was probing the equity watchlist for both segments
    before — the F&O check kept failing on ONGC even though the F&O
    bot trades NIFTY/BANKNIFTY only."""
    from .data import intraday_bars
    from .research import todays_picks

    cfg = load_config()
    if segment == Segment.FNO:
        f_syms = cfg.fno.watchlist.get("symbols", []) if cfg.fno else []
        sample = f_syms[0] if f_syms else None
    else:
        picks = todays_picks()
        sample = picks[0].symbol if picks else None
        if not sample:
            sample = cfg.symbols[0] if cfg.symbols else None
    if not sample:
        return CheckResult("Market data", "WARN", "no symbol available to probe")
    try:
        df = intraday_bars(sample, "5m")
    except Exception as e:
        return CheckResult("Market data", "FAIL", f"yfinance error on {sample}: {e}")
    if df.empty:
        # Pre-market is normal — yfinance only returns intraday bars after
        # the 09:15 open. Don't FAIL outside the trading window; just WARN.
        if not _in_trading_window(datetime.now(IST), cfg):
            return CheckResult("Market data", "OK",
                               f"no 5m bars for {sample} (expected — market hasn't opened yet)")
        return CheckResult("Market data", "FAIL", f"no 5m bars for {sample}")
    last_ts = df.index[-1]
    if last_ts.tzinfo is None:
        last_ts = IST.localize(last_ts.to_pydatetime())
    age = (datetime.now(IST) - last_ts).total_seconds() / 60
    detail = f"{sample}: {len(df)} bars, last bar {last_ts.strftime('%H:%M IST')} ({age:.0f}m old)"
    if _in_trading_window(datetime.now(IST), cfg) and age > 10:
        return CheckResult("Market data", "WARN", detail)
    return CheckResult("Market data", "OK", detail)


def _open_position_data(segment: Segment = Segment.EQUITY) -> CheckResult:
    """Make sure every OPEN position has fresh 1m bars.

    Stale 1m bars on a held position is the exact failure mode that left
    NESTLEIND unmanaged on 2026-04-29: the executor's per-tick SL/TP/trail
    loop calls ``intraday_bars(sym, "1m")`` and silently ``continue``s on
    an empty DataFrame. The position rode to EOD because no SL/TP/trail
    check ever executed for it. This guard surfaces that condition before
    it costs you a trade.

    During trading window:
      * No open positions → skip (OK)
      * All positions have 1m bars ≤ 2 min old → OK
      * Any position with 1m bars > 2 min old (or empty) → WARN
      * Any position with NO 1m bars AND NO 5m fallback → FAIL
    """
    from .data import intraday_bars
    cache = get_cache()
    snap = cache.get_json(cache_key("portfolio", segment)) or {}
    positions = snap.get("positions", []) or []
    if not positions:
        return CheckResult("Open-position data", "OK", "no open positions")

    cfg = load_config()
    now = datetime.now(IST)
    if not _in_trading_window(now, cfg):
        return CheckResult("Open-position data", "OK",
                           f"{len(positions)} position(s); outside trading window")

    issues = []
    fully_blind = []
    for p in positions:
        sym = p.get("symbol", "?")
        try:
            df1 = intraday_bars(sym, "1m")
        except Exception as e:
            issues.append(f"{sym}: 1m fetch error ({type(e).__name__})")
            df1 = None
        if df1 is None or df1.empty:
            try:
                df5 = intraday_bars(sym, "5m")
            except Exception:
                df5 = None
            if df5 is None or df5.empty:
                fully_blind.append(sym)
            else:
                issues.append(f"{sym}: 1m EMPTY (5m fallback in use)")
            continue
        last_ts = df1.index[-1]
        if last_ts.tzinfo is None:
            last_ts = IST.localize(last_ts.to_pydatetime())
        age_min = (now - last_ts).total_seconds() / 60
        if age_min > 2:
            issues.append(f"{sym}: 1m last bar {age_min:.1f}m old")

    if fully_blind:
        return CheckResult("Open-position data", "FAIL",
                           f"NO bars for held position(s): {', '.join(fully_blind)} — "
                           f"SL/TP cannot be enforced!")
    if issues:
        return CheckResult("Open-position data", "WARN", "; ".join(issues))
    return CheckResult("Open-position data", "OK", f"{len(positions)} position(s) all fresh (≤2m)")


def _bot_log_errors() -> CheckResult:
    """Count today's ERROR / CRITICAL lines in the rotating bot log.

    Excludes the healthcheck's own self-reported FAIL/DEGRADED lines —
    otherwise a single transient FAIL would self-perpetuate forever
    (every subsequent run would re-WARN about that ERROR being there)."""
    today = date.today().isoformat()
    log = PROJECT_ROOT / "logs" / f"bot_{today}.log"
    if not log.exists():
        return CheckResult("Bot log", "WARN", f"{log.name} not found yet")
    err = warn = 0
    try:
        with log.open() as fh:
            for line in fh:
                # Skip healthcheck's own FAIL/DEGRADED log lines so we
                # don't double-count them as "real" application errors.
                if "bot.healthcheck" in line:
                    continue
                if "| ERROR" in line or "| CRITICAL" in line:
                    err += 1
                elif "| WARNING" in line:
                    warn += 1
    except Exception as e:
        return CheckResult("Bot log", "WARN", f"unreadable: {e}")
    detail = f"{log.name}: {err} errors, {warn} warnings"
    if err > 0:
        return CheckResult("Bot log", "WARN", detail + " — investigate")
    return CheckResult("Bot log", "OK", detail)


def _stale_signal_cleanup(segment: Segment = Segment.EQUITY) -> CheckResult:
    """Auto-clean signal:<segment>:* keys whose 'ts' is from a previous day."""
    cache = get_cache()
    today = date.today().isoformat()
    deleted: List[str] = []
    prefix = f"signal:{segment.value}:"
    for k in cache.keys(signal_pattern(segment)):
        v = cache.get_json(k) or {}
        ts = v.get("ts", "")
        if ts and not ts.startswith(today):
            cache.delete(k)
            deleted.append(k.replace(prefix, ""))
    if deleted:
        return CheckResult("Cache hygiene", "OK",
                           f"removed {len(deleted)} stale signal keys: {', '.join(deleted)}")
    return CheckResult("Cache hygiene", "OK", "no stale keys")


def _portfolio_and_risk(segment: Segment = Segment.EQUITY) -> CheckResult:
    cfg = load_config()
    capital_total = cfg_capital(cfg, segment).total
    risk = cfg_risk(cfg, segment)
    cache = get_cache()
    snap = cache.get_json(cache_key("portfolio", segment)) or {}
    saved_cap = snap.get("starting_capital")
    if saved_cap is not None and abs(saved_cap - capital_total) > 0.5:
        snap = {}
    cash = float(snap.get("cash", capital_total))
    positions = snap.get("positions", []) or []
    long_basis = sum((p.get("avg_price", 0) or 0) * (p.get("qty", 0) or 0)
                     for p in positions if (p.get("qty", 0) or 0) > 0)
    unreal = sum(p.get("unrealized_pnl", 0) or 0 for p in positions)
    equity = cash + long_basis + unreal
    pnl_pct = (equity - capital_total) / capital_total * 100 if capital_total else 0
    detail = (
        f"equity ₹{equity:,.0f} ({pnl_pct:+.2f}%), "
        f"{len(positions)}/{risk.max_open_positions} positions"
    )
    if pnl_pct <= -risk.max_daily_loss_pct:
        return CheckResult("Portfolio & risk", "FAIL",
                           detail + f" — daily-loss cap of -{risk.max_daily_loss_pct}% reached")
    if pnl_pct <= -risk.max_daily_loss_pct * 0.7:
        return CheckResult("Portfolio & risk", "WARN",
                           detail + f" — within 70% of daily-loss cap (-{risk.max_daily_loss_pct}%)")
    return CheckResult("Portfolio & risk", "OK", detail)


def _tick_heartbeat(segment: Segment = Segment.EQUITY) -> CheckResult:
    """Detect silent executor stalls.

    The biggest failure mode we've seen so far is the bot's APScheduler
    stopping ``executor.tick()`` while the *process* itself is alive (e.g.
    macOS App Nap, an uncaught exception in an APScheduler thread that
    silently disables the job, or a Redis blip that hangs the executor in
    cache I/O). Symptom: from the user's perspective a position just sits
    there as price runs past SL or TP, with no logs.

    The executor stamps ``heartbeat:tick`` every minute. We compute its
    age and FAIL during the trading window if it's > 3 minutes stale.
    Outside trading hours we just report the age informationally.
    """
    cache = get_cache()
    hb = cache.get_json(cache_key("heartbeat:tick", segment)) or {}
    if not hb:
        return CheckResult("Tick heartbeat", "WARN",
                           f"no heartbeat yet for the {segment.label} bot — may not have started")

    try:
        last = datetime.fromisoformat(hb["ts"])
    except (KeyError, ValueError):
        return CheckResult("Tick heartbeat", "WARN", f"malformed heartbeat: {hb}")

    now = datetime.now(IST)
    age_s = (now - last).total_seconds()
    age_label = f"{int(age_s)}s ago" if age_s < 90 else f"{int(age_s // 60)}m {int(age_s % 60)}s ago"

    cfg = load_config()
    in_window = (
        cfg.session.t("trade_start") <= now.time() <= cfg.session.t("square_off")
        and now.weekday() < 5
    )

    if in_window:
        if age_s > 180:
            return CheckResult("Tick heartbeat", "FAIL",
                               f"executor stalled — last tick {age_label} during trading window")
        if age_s > 90:
            return CheckResult("Tick heartbeat", "WARN", f"slow ticks — last {age_label}")
        return CheckResult("Tick heartbeat", "OK", f"last tick {age_label}")
    else:
        return CheckResult("Tick heartbeat", "OK",
                           f"last tick {age_label} (outside trading window)")


def _fee_schedule() -> CheckResult:
    """Surface the latest daily fee-audit result.

    The audit itself runs at 09:00 IST (Mon-Fri) via the scheduler — see
    :mod:`bot.fee_audit`. This check just reads the cached result and maps
    it onto the healthcheck status vocabulary.
    """
    cache = get_cache()
    audit = cache.get_json("fee_audit:latest") or {}
    if not audit:
        return CheckResult(
            "Fee schedule", "WARN",
            "no audit run yet today (auto-runs at 09:00 IST; "
            "or run `python -m cli verify-fees`)",
        )
    status = audit.get("status", "WARN")
    summary = audit.get("summary", "")
    ts = audit.get("timestamp", "")[11:19]
    detail = f"{summary} (last verified {ts})" if ts else summary
    return CheckResult("Fee schedule",
                       {"OK": "OK", "WARN": "WARN", "FAIL": "FAIL"}.get(status, "WARN"),
                       detail)


def _disk_space() -> CheckResult:
    try:
        usage = shutil.disk_usage(PROJECT_ROOT)
    except Exception as e:
        return CheckResult("Disk space", "WARN", f"could not stat: {e}")
    pct = usage.used / usage.total * 100
    free_gib = usage.free / 1024 ** 3
    detail = f"{free_gib:.1f} GiB free ({pct:.0f}% used)"
    if pct >= 95:
        return CheckResult("Disk space", "FAIL", detail)
    if pct >= 85:
        return CheckResult("Disk space", "WARN", detail)
    return CheckResult("Disk space", "OK", detail)


# ─── Public entry points ──────────────────────────────────────────────────────


def _in_trading_window(now: datetime, cfg) -> bool:
    if now.weekday() >= 5:
        return False
    return cfg.session.t("trade_start") <= now.time() <= cfg.session.t("square_off")


def _build_summary(checks: List[CheckResult],
                   segment: Segment = Segment.EQUITY) -> Dict[str, Any]:
    """One-line metrics block surfaced beneath the per-check list."""
    cfg = load_config()
    capital_total = cfg_capital(cfg, segment).total
    risk = cfg_risk(cfg, segment)
    cache = get_cache()
    snap = cache.get_json(cache_key("portfolio", segment)) or {}
    if (snap.get("starting_capital") or 0) and abs(snap["starting_capital"] - capital_total) > 0.5:
        snap = {}
    positions = snap.get("positions", []) or []
    cash = float(snap.get("cash", capital_total))
    long_basis = sum((p.get("avg_price", 0) or 0) * (p.get("qty", 0) or 0)
                     for p in positions if (p.get("qty", 0) or 0) > 0)
    unreal = sum(p.get("unrealized_pnl", 0) or 0 for p in positions)
    equity = cash + long_basis + unreal
    return {
        "Segment":          segment.label,
        "Capital":          f"₹{capital_total:,.2f}",
        "Equity":           f"₹{equity:,.2f}  ({(equity - capital_total) / max(capital_total, 1) * 100:+.2f}%)",
        "Cash":             f"₹{cash:,.2f}",
        "Open positions":   f"{len(positions)} / {risk.max_open_positions}",
        "Trading window":   f"{cfg.session.trade_start}–{cfg.session.square_off} IST",
        "Mode":             "LIVE" if env().LIVE_TRADING else "PAPER",
    }


def _aggregate(checks: List[CheckResult]) -> str:
    worst = max((_SEVERITY_RANK[c.status] for c in checks), default=0)
    return _OVERALL_LABEL[worst]


def run_healthcheck(*, segment: Segment = Segment.EQUITY,
                    notify: bool = False) -> HealthReport:
    """Run every check, return a :class:`HealthReport`, and optionally email it.

    Segment-specific checks (signal freshness, portfolio, heartbeat, …)
    are scoped to ``segment``. Global checks (process, redis, sleep,
    config, disk, fee-audit, log errors) run once and are shared.
    The result is published under ``healthcheck:latest:<segment>`` and
    ``healthcheck:history:<segment>`` so each segment has its own
    panel on the dashboard.
    """
    # Segment-aware checks: bind segment via a closure.
    def _seg(fn, label):
        def _wrapped():
            return fn(segment)
        _wrapped.__name__ = label
        return _wrapped

    checks: List[CheckResult] = []
    for fn in (
        _bot_process,
        _seg(_tick_heartbeat,        "tick_heartbeat"),
        _sleep_lock,
        _redis_health,
        _seg(_config_check,          "config_check"),
        _seg(_premarket_caches,      "premarket_caches"),
        _seg(_signal_freshness,      "signal_freshness"),
        _seg(_data_freshness,        "data_freshness"),
        _seg(_open_position_data,    "open_position_data"),
        _bot_log_errors,
        _seg(_stale_signal_cleanup,  "stale_signal_cleanup"),
        _seg(_portfolio_and_risk,    "portfolio_and_risk"),
        _fee_schedule,
        _disk_space,
    ):
        try:
            checks.append(fn())
        except Exception as e:  # pragma: no cover — defence-in-depth
            checks.append(CheckResult(fn.__name__.strip("_"), "WARN", f"check raised: {e}"))

    report = HealthReport(
        timestamp=datetime.now(IST),
        overall=_aggregate(checks),
        checks=checks,
        summary=_build_summary(checks, segment=segment),
    )
    # Stash the segment on the report (used by dashboard rendering).
    report.summary.setdefault("segment", segment.value)

    log_fn = {
        "OK": logger.info,
        "DEGRADED": logger.warning,
        "FAILED": logger.error,
    }.get(report.overall, logger.info)
    log_fn("[healthcheck:{}] {} — {} checks, {}/{} OK", segment.value, report.overall,
           len(checks),
           sum(1 for c in checks if c.status == "OK"),
           len(checks))

    _persist(report, segment=segment)
    _publish_to_cache(report, segment=segment)

    if notify:
        try:
            from .notify import get_notifier
            get_notifier().health(report)
        except Exception as e:
            logger.error("[healthcheck] notify failed: {}", e)

    return report


def _persist(report: HealthReport, segment: Segment = Segment.EQUITY) -> Path:
    """Append the report to ``logs/healthcheck/<segment>/YYYY-MM-DD.jsonl`` for audit."""
    out_dir = PROJECT_ROOT / "logs" / "healthcheck" / segment.value
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{report.timestamp.date().isoformat()}.jsonl"
    with path.open("a") as fh:
        fh.write(json.dumps(report.to_dict(), default=str) + os.linesep)
    return path


# How many past reports to keep in the cache history list (read by the
# dashboard's "System health" panel for the trend mini-chart).
_HEALTH_HISTORY_LEN = 10


def _publish_to_cache(report: HealthReport, segment: Segment = Segment.EQUITY) -> None:
    """Push the report to the cache so the Streamlit dashboard can render it.

    Stores two keys per segment:
      * ``healthcheck:latest:<segment>``  — the most recent report as a dict.
      * ``healthcheck:history:<segment>`` — list of the last ``_HEALTH_HISTORY_LEN``
        reports (oldest first), useful for a sparkline of overall status.

    The dashboard reads each segment's keys and renders one health
    panel per segment.
    """
    latest_key = cache_key("healthcheck:latest", segment)
    history_key = cache_key("healthcheck:history", segment)
    try:
        cache = get_cache()
        payload = report.to_dict()
        cache.set_json(latest_key, payload, ttl=86400)
        history = cache.get_json(history_key) or []
        history.append(payload)
        history = history[-_HEALTH_HISTORY_LEN:]
        cache.set_json(history_key, history, ttl=86400)
    except Exception as e:  # pragma: no cover — never break the scheduler
        logger.warning("[healthcheck:{}] cache publish failed: {}", segment.value, e)
