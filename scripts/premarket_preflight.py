"""Pre-market pre-flight — maker/checker harness with logged outputs.

Run BEFORE you start the bots each trading day. Verifies every
precondition in one place so you don't have to remember the 10
manual checks from the README's "Phase 1 — Monday morning" section.

Usage:
    python scripts/premarket_preflight.py                    # interactive run
    python scripts/premarket_preflight.py --auto-cleanup     # auto-run the
                                                             # 2026-04-30
                                                             # cleanup if
                                                             # corruption is
                                                             # detected
    python scripts/premarket_preflight.py --json             # machine-readable

Exit codes:
    0   all checks PASSED — safe to start the bots
    1   at least one CRITICAL check FAILED — DO NOT start the bots
    2   at least one non-critical check WARNED — review and decide

The terminal output is ALSO written to
``logs/preflight/YYYY-MM-DD_HHMMSS.log`` so you have an audit trail
per pre-flight run. The latest pre-flight is symlinked from
``logs/preflight/latest.log`` for quick `tail -f` access.

──────────────────────────── Maker / Checker pattern ─────────────────────────

Every step is one of two kinds:

* **Checker** — read-only inspection of system state. Returns ``PASS``,
  ``WARN``, ``FAIL``, or ``SKIP`` plus a short detail string. Never
  mutates anything.

* **Maker** — performs an action that prepares the environment (e.g.
  refreshes the NSE holiday calendar, runs the regression test suite).
  Returns the same status codes; on ``FAIL`` the harness halts further
  steps that depend on this maker.

Each step's status is appended to a per-run JSON sidecar
(``logs/preflight/YYYY-MM-DD_HHMMSS.json``) for later programmatic
inspection (CI, dashboards, etc.).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import time
import traceback
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ─────────────────────── Result dataclass + helpers ─────────────────────────

class Status:
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


_EMOJI = {Status.PASS: "✅", Status.WARN: "⚠️ ", Status.FAIL: "❌", Status.SKIP: "⏭ "}
_COLOR = {Status.PASS: "\033[32m",  Status.WARN: "\033[33m",
          Status.FAIL: "\033[31m",  Status.SKIP: "\033[90m"}
_RESET = "\033[0m"
_BOLD  = "\033[1m"


@dataclass
class StepResult:
    name: str
    kind: str                # "checker" | "maker"
    critical: bool           # if True and FAIL → exit code 1
    status: str              # PASS | WARN | FAIL | SKIP
    detail: str              # one-line human description
    elapsed_ms: int          # how long the step took
    extras: dict[str, Any] = field(default_factory=dict)


# Each step is a callable returning ``(status, detail, extras)``. The
# harness wraps it with timing + exception handling so a single step's
# crash never bricks the whole pre-flight.
StepFn = Callable[[], tuple[str, str, dict[str, Any]]]


@dataclass
class Step:
    name: str
    kind: str                # "checker" | "maker"
    critical: bool
    fn: StepFn
    depends_on: tuple[str, ...] = ()


def _run_step(step: Step, prior: list[StepResult]) -> StepResult:
    """Execute one step. Honours ``depends_on``: if a dependency FAILed,
    skip this step rather than running it on a broken substrate."""
    deps_failed = [r for r in prior if r.name in step.depends_on and r.status == Status.FAIL]
    if deps_failed:
        return StepResult(
            name=step.name, kind=step.kind, critical=step.critical,
            status=Status.SKIP, elapsed_ms=0,
            detail=f"skipped — dependency failed: {deps_failed[0].name}",
        )
    t0 = time.perf_counter()
    try:
        status, detail, extras = step.fn()
    except Exception as e:                                       # noqa: BLE001
        status, detail, extras = Status.FAIL, f"crashed: {e}", {"traceback": traceback.format_exc()}
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return StepResult(
        name=step.name, kind=step.kind, critical=step.critical,
        status=status, detail=detail, elapsed_ms=elapsed_ms, extras=extras,
    )


# ─────────────────────── Tee logger (terminal + file) ────────────────────────

class TeeLogger:
    """Mirror everything printed via ``self.print()`` to a logfile.

    We can't just redirect stdout because we still want colored output
    on the terminal but PLAIN text in the file (so logs are grep-able).
    """

    _ANSI = re.compile(r"\033\[[0-9;]*m")

    def __init__(self, logfile: Path) -> None:
        self.logfile = logfile
        logfile.parent.mkdir(parents=True, exist_ok=True)
        self._fh = logfile.open("w")

    def print(self, *parts: str, end: str = "\n") -> None:
        line = " ".join(str(p) for p in parts)
        sys.stdout.write(line + end)
        sys.stdout.flush()
        self._fh.write(self._ANSI.sub("", line) + end)
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# ════════════════════════════════════════════════════════════════════════════
#  STEP IMPLEMENTATIONS
#
#  Each step takes no arguments and returns (status, detail, extras_dict).
#  Keep them short — they should be readable top-to-bottom by an operator
#  who doesn't know the codebase.
# ════════════════════════════════════════════════════════════════════════════

# ─── Phase 1 (Checkers) — system & process sanity ───────────────────────────

def check_power_source() -> tuple[str, str, dict]:
    """Warn (non-blocking) if the Mac is on battery power.

    On 2026-05-05 the Mac entered standby between 07:30 and 09:46 IST
    while the bot was supposedly held awake by ``caffeinate -i -m -s``.
    The ``-s`` flag is ignored on battery, and ``-i`` does not prevent
    macOS standby. The bot threads were silently frozen for 2 h 10 m,
    which delayed the watchlist update, the research run, and the F&O
    bot's recovery.

    On battery → WARN with the exact pmset workaround. On AC → PASS.
    Non-darwin or pmset unavailable → SKIP.
    """
    try:
        from bot.power import power_state
    except Exception as e:                                       # noqa: BLE001
        return (Status.SKIP, f"bot.power unavailable: {e}", {})
    source, pct = power_state()
    if source == "ac":
        suffix = f" ({pct}%)" if pct is not None else ""
        return (Status.PASS, f"on AC power{suffix} — sleep prevention is fully effective", {"source": source, "pct": pct})
    if source == "battery":
        suffix = f" ({pct}%)" if pct is not None else ""
        return (Status.WARN,
                f"running on BATTERY{suffix} — caffeinate alone won't prevent macOS standby. "
                "Plug in to AC, or run: sudo pmset -b sleep 0 disablesleep 1 "
                "(restore later with: sudo pmset -b sleep 1 disablesleep 0). "
                "This was the root cause of the 2026-05-05 morning blackout.",
                {"source": source, "pct": pct,
                 "remediation": "sudo pmset -b sleep 0 disablesleep 1"})
    return (Status.SKIP, "power state unknown (non-darwin or pmset unavailable)", {"source": source})


def check_python_venv() -> tuple[str, str, dict]:
    """Are we running inside the project's venv?"""
    in_venv = (hasattr(sys, "real_prefix")
               or (sys.prefix != getattr(sys, "base_prefix", sys.prefix)))
    venv_path = sys.prefix
    expected_venv = (ROOT / ".venv").resolve()
    if not in_venv:
        return (Status.FAIL,
                f"NOT inside a venv — current prefix: {venv_path}",
                {"venv_path": venv_path, "in_venv": False})
    if Path(venv_path).resolve() != expected_venv:
        return (Status.WARN,
                f"venv is at {venv_path}, expected {expected_venv}",
                {"venv_path": venv_path, "expected": str(expected_venv)})
    return (Status.PASS, f"venv active ({venv_path})",
            {"venv_path": venv_path})


def check_no_stale_bot_processes() -> tuple[str, str, dict]:
    """``ps`` for any ``cli.py run`` processes; we want NONE before launch."""
    try:
        out = subprocess.check_output(["ps", "-eo", "pid,command"],
                                      text=True, stderr=subprocess.DEVNULL, timeout=5)
    except Exception as e:                                       # noqa: BLE001
        return (Status.FAIL, f"ps failed: {e}", {})
    live = []
    self_pid = os.getpid()
    for line in out.splitlines():
        s = line.strip()
        if not s or s.startswith("PID"):
            continue
        if (("cli" in s and " run" in s)) and "premarket_preflight" not in s:
            try:
                pid = int(s.split(None, 1)[0])
            except ValueError:
                continue
            if pid == self_pid:
                continue
            live.append((pid, s[:120]))
    if not live:
        return (Status.PASS, "no stale bot processes", {"processes": []})
    summary = ", ".join(f"pid={pid}" for pid, _ in live)
    return (Status.WARN,
            f"{len(live)} bot process(es) already running ({summary}) — "
            "OK if you intend to keep them, FAIL if you forgot to Ctrl-C",
            {"processes": [{"pid": p, "cmd": c} for p, c in live]})


def check_no_orphan_lock_files() -> tuple[str, str, dict]:
    """Lock files should match live PIDs; orphans block bot startup."""
    locks = list(ROOT.glob(".bot.lock*"))
    if not locks:
        return (Status.PASS, "no lock files present", {"locks": []})
    orphans = []
    keepers = []
    for lock in locks:
        try:
            pid = int(lock.read_text().strip())
        except Exception:
            orphans.append({"file": lock.name, "pid": "unreadable"})
            continue
        try:
            os.kill(pid, 0)              # signal 0 = liveness probe
            keepers.append({"file": lock.name, "pid": pid})
        except (OSError, ProcessLookupError):
            orphans.append({"file": lock.name, "pid": pid})
    if orphans:
        return (Status.FAIL,
                f"{len(orphans)} orphan lock(s) — delete before launch: "
                + ", ".join(o["file"] for o in orphans),
                {"orphans": orphans, "keepers": keepers})
    if keepers:
        return (Status.PASS,
                f"{len(keepers)} lock(s) all match live PIDs",
                {"keepers": keepers})
    return (Status.PASS, "no locks", {})


def check_redis_running() -> tuple[str, str, dict]:
    """Redis must be reachable BEFORE the bot starts."""
    try:
        from bot.config import env
        url = env().REDIS_URL
    except Exception as e:                                       # noqa: BLE001
        return (Status.FAIL, f"could not load REDIS_URL from env: {e}", {})
    try:
        import redis
        r = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
        r.ping()
        n_keys = r.dbsize()
        return (Status.PASS, f"Redis OK ({url}) — {n_keys} keys",
                {"url": url, "key_count": n_keys})
    except Exception as e:                                       # noqa: BLE001
        return (Status.FAIL,
                f"Redis NOT reachable at {url}: {e}.  "
                "Start it with: brew services start redis",
                {"url": url, "error": str(e)})


def check_disk_space() -> tuple[str, str, dict]:
    """Need enough space for today's logs + journal."""
    usage = shutil.disk_usage(ROOT)
    free_gib = usage.free / (1024 ** 3)
    if free_gib < 1:
        return (Status.FAIL, f"only {free_gib:.2f} GiB free (need ≥1 GiB)",
                {"free_gib": round(free_gib, 2)})
    if free_gib < 5:
        return (Status.WARN, f"{free_gib:.2f} GiB free — consider housekeeping",
                {"free_gib": round(free_gib, 2)})
    return (Status.PASS, f"{free_gib:.1f} GiB free",
            {"free_gib": round(free_gib, 2)})


def check_logs_writable() -> tuple[str, str, dict]:
    """The bot writes to logs/, journal/, eod/ — confirm we have permission."""
    targets = [ROOT / "logs", ROOT / "logs" / "trades",
               ROOT / "logs" / "eod", ROOT / "logs" / "preflight"]
    for t in targets:
        try:
            t.mkdir(parents=True, exist_ok=True)
            probe = t / ".write-probe"
            probe.write_text("ok")
            probe.unlink()
        except Exception as e:                                   # noqa: BLE001
            return (Status.FAIL, f"cannot write to {t}: {e}", {"path": str(t)})
    return (Status.PASS, f"{len(targets)} log dirs writable",
            {"paths": [str(t) for t in targets]})


def check_config_loads() -> tuple[str, str, dict]:
    """Pydantic-typed config must parse — catches typos in config.yaml early."""
    try:
        from bot.config import load_config
        cfg = load_config()
    except Exception as e:                                       # noqa: BLE001
        return (Status.FAIL, f"config.yaml parse error: {e}", {"error": str(e)})

    eq_cap = cfg.capital.total
    fno_cap = cfg.fno.capital.total if cfg.fno is not None and cfg.fno.capital else None
    eq_max_loss = cfg.risk.max_daily_loss_pct
    fno_enabled = bool(cfg.fno is not None and cfg.fno.enabled)
    msg = (f"equity capital ₹{eq_cap:,.0f}, daily loss cap {eq_max_loss}%, "
           f"F&O {'ON' if fno_enabled else 'OFF'}"
           + (f" cap ₹{fno_cap:,.0f}" if fno_cap else ""))
    return (Status.PASS, msg, {
        "equity_capital": eq_cap,
        "fno_capital": fno_cap,
        "fno_enabled": fno_enabled,
        "max_daily_loss_pct": eq_max_loss,
    })


def check_paper_state_clean() -> tuple[str, str, dict]:
    """Detect known corruption signatures in BOTH state caches.

    The bot has two parallel state caches (which MUST stay in sync):

    * ``paper:state:<seg>``  — broker snapshot, read by
                                ``PaperBroker._restore_state`` on bot startup
    * ``portfolio:<seg>``    — executor's published snapshot, read by
                                the Streamlit dashboard for cash, positions,
                                and daily-P&L % display

    Three corruption patterns we've seen:

    1. **2026-04-30 spread blow-up** — F&O credit spread squared off at
       underlying spot (₹54,842) instead of premium (₹44/share), driving
       cash to −₹8.3M. Signature: ``cash < -1 × starting_capital``.
       Fixed by ``scripts/cleanup_fno_2026_04_30.py``.

    2. **2026-05-04 ADANIENT phantom-short** — duplicate ``_end_of_day``
       call created a phantom equity short, cash leaked ~₹25k. Signature:
       cash gap > ₹1000 with zero positions and no real trades that
       large. Fixed by ``scripts/cleanup_equity_2026_05_04.py``.

    3. **2026-05-05 stale-portfolio dashboard** — the May-04 cleanup
       script only cleaned ``paper:state:equity`` and missed
       ``portfolio:equity`` → dashboard kept showing yesterday's
       corruption (₹74,961, −25%) until 09:15 the next day. Signature:
       ``portfolio:<seg>`` ts is from a PRIOR trading day. Fixed by
       wiping ``portfolio:<seg>`` in ``clean_redis_session.py`` and the
       cleanup script.

    On detect, FAIL with a clear pointer to the right script.
    """
    try:
        from bot.cache import get_cache
        cache = get_cache()
    except Exception as e:                                       # noqa: BLE001
        return (Status.SKIP, f"cache unavailable: {e}", {})

    today = datetime.now().date().isoformat()
    issues_apr30 = []
    issues_may04 = []
    issues_may05 = []   # stale portfolio key from a prior trading day

    for seg in ("equity", "fno"):
        # ── (a) Check paper:state for Apr-30 + May-04 signatures ─────
        ps = cache.get_json(f"paper:state:{seg}") or {}
        cash = ps.get("cash")
        starting = ps.get("starting_capital")
        ps_saved_at = (ps.get("saved_at") or "")[:10]
        ps_is_stale = bool(ps_saved_at and ps_saved_at < today)
        if cash is not None and starting is not None:
            if starting and cash < -abs(starting):
                # Apr-30 catastrophic — ALWAYS FAIL (real corruption,
                # not a normal trading outcome on any timeline).
                issues_apr30.append({
                    "segment": seg, "key": f"paper:state:{seg}",
                    "cash": cash, "starting_capital": starting,
                    "saved_at": ps.get("saved_at", ""),
                })
            else:
                cleanup_marker = ps.get("cleanup_marker", "")
                n_positions = len(ps.get("positions", {}))
                # Only fire May-04 phantom-short FAIL if the snapshot is
                # from TODAY. A stale snapshot with cash < starting−₹1000
                # is just yesterday's legitimate EOD close (e.g. the
                # 2026-05-06 F&O -₹4,864 loss). PaperBroker._restore_state
                # already discards stale snapshots on bot restart.
                if (starting > 0 and (cash - starting) < -1000
                        and n_positions == 0 and not cleanup_marker
                        and not ps_is_stale):
                    issues_may04.append({
                        "segment": seg, "key": f"paper:state:{seg}",
                        "cash": cash, "starting_capital": starting,
                        "gap": cash - starting,
                        "saved_at": ps.get("saved_at", ""),
                    })

        # ── (b) Check portfolio:<seg> for the same signatures + a
        #        stale-timestamp signature (the 2026-05-05 incident) ──
        pf = cache.get_json(f"portfolio:{seg}") or {}
        pf_cash = pf.get("cash")
        pf_starting = pf.get("starting_capital")
        # If the portfolio snapshot's ts is from a PRIOR trading day,
        # route ANY anomaly through the stale-portfolio (WARN) branch
        # below — a real losing day legitimately shows
        # ``cash - starting < -1000`` at EOD and would otherwise
        # false-positive as the May-04 phantom-short signature on the
        # next morning's preflight (caught 2026-05-07 when yesterday's
        # legitimate -₹4,864 F&O loss tripped the gap-FAIL check).
        # The Apr-30 catastrophic signature (``cash < -starting``) is
        # NEVER a normal trading outcome, so we still let that fire
        # FAIL even on stale snapshots — it always needs cleanup.
        ts_raw = (pf.get("ts") or "")[:10]
        pf_is_stale = bool(ts_raw and ts_raw < today)
        if pf_cash is not None and pf_starting is not None:
            if pf_starting and pf_cash < -abs(pf_starting):
                # Apr-30 catastrophic — ALWAYS FAIL (real corruption,
                # not a normal trading outcome on any timeline).
                issues_apr30.append({
                    "segment": seg, "key": f"portfolio:{seg}",
                    "cash": pf_cash, "starting_capital": pf_starting,
                    "ts": pf.get("ts", ""),
                })
            elif (pf_starting > 0 and (pf_cash - pf_starting) < -1000
                    and not pf.get("cleanup_marker")
                    and not pf_is_stale):
                # May-04 phantom-short signature — only fire if the
                # snapshot is from TODAY. A stale snapshot with the
                # same gap is just yesterday's legitimate close;
                # downgraded to WARN by the stale-portfolio branch.
                issues_may04.append({
                    "segment": seg, "key": f"portfolio:{seg}",
                    "cash": pf_cash, "starting_capital": pf_starting,
                    "gap": pf_cash - pf_starting,
                    "ts": pf.get("ts", ""),
                })

        # The 2026-05-05 stale-portfolio signature: portfolio key has
        # a ts from a PRIOR date (i.e. yesterday's snapshot leaked
        # into today's dashboard). Don't fire if the cleanup_marker
        # is already set for the correct day.
        if pf_is_stale and not pf.get("cleanup_marker"):
            issues_may05.append({
                "segment": seg, "key": f"portfolio:{seg}",
                "ts": pf.get("ts", ""), "today": today,
                "cash": pf_cash,
            })

    if issues_apr30:
        keys_list = ", ".join(i["key"] for i in issues_apr30)
        return (Status.FAIL,
                f"2026-04-30 corruption — keys: {keys_list}.  "
                "Run: python scripts/cleanup_fno_2026_04_30.py",
                {"signature": "apr30_spread_blowup", "details": issues_apr30})
    if issues_may04:
        keys_list = ", ".join(i["key"] for i in issues_may04)
        return (Status.FAIL,
                f"2026-05-04 phantom-short signature — keys: {keys_list}.  "
                "Run: python scripts/cleanup_equity_2026_05_04.py",
                {"signature": "may04_phantom_short", "details": issues_may04})
    if issues_may05:
        keys_list = ", ".join(i["key"] for i in issues_may05)
        return (Status.WARN,
                f"2026-05-05 stale-portfolio signature — keys: {keys_list}.  "
                "Dashboard will show yesterday's snapshot until next "
                "publish. Run: python scripts/clean_redis_session.py",
                {"signature": "may05_stale_portfolio", "details": issues_may05})
    return (Status.PASS, "paper:state + portfolio caches are sane",
            {"checked_segments": ["equity", "fno"], "checked_keys": 4})


def check_redis_session_freshness() -> tuple[str, str, dict]:
    """Inspect Redis intraday-session keys; flag any stale leftovers.

    Stale = a JSON value whose ``ts`` / ``saved_at`` field is older than
    12 hours (well past midnight). Stale data is the symptom that caused
    the 2026-05-04 11:46-13:19 trading blind-spot — the executor saw a
    stale signal cached from yesterday and short-circuited the live tick.

    Signals + trail keys are pattern-matched (``signal:<seg>:*``,
    ``trail:<seg>:*``); exact keys (``paper:state:<seg>``,
    ``heartbeat:tick:<seg>``, ``profit_lockin:<seg>``, ``eod_done:<seg>``)
    are individually checked.

    Returns:
      * PASS  — no stale keys
      * WARN  — stale keys present; operator should run cleanup
      * SKIP  — Redis unreachable
    """
    try:
        from bot.cache import get_cache
        cache = get_cache()
    except Exception as e:                                       # noqa: BLE001
        return (Status.SKIP, f"cache unavailable: {e}", {})

    today = datetime.now().date().isoformat()
    stale: list[dict] = []
    inspected = 0

    def _age_hours(val) -> Optional[float]:
        if not isinstance(val, dict):
            return None
        raw = val.get("ts") or val.get("saved_at") or val.get("timestamp")
        if not raw:
            return None
        try:
            s = str(raw).rstrip("Z")
            ts = datetime.fromisoformat(s)
            now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
            return (now - ts).total_seconds() / 3600
        except Exception:
            return None

    for seg in ("equity", "fno"):
        # Exact keys.
        for k in [f"paper:state:{seg}", f"heartbeat:tick:{seg}",
                  f"profit_lockin:{seg}"]:
            v = cache.get_json(k)
            if v is None:
                continue
            inspected += 1
            age = _age_hours(v)
            if age is not None and age > 12:
                stale.append({"key": k, "age_hours": round(age, 1)})

        # eod_done: stale if its ``date`` field is older than today.
        eod = cache.get_json(f"eod_done:{seg}")
        if isinstance(eod, dict):
            inspected += 1
            ed = eod.get("date", "")
            if ed and ed < today:
                stale.append({"key": f"eod_done:{seg}", "age_days": "≥1"})

        # Pattern keys.
        for pat in [f"signal:{seg}:*", f"trail:{seg}:*"]:
            try:
                keys = cache.keys(pat)
            except Exception:
                continue
            for k in keys:
                inspected += 1
                v = cache.get_json(k)
                age = _age_hours(v)
                if age is not None and age > 12:
                    stale.append({"key": k, "age_hours": round(age, 1)})

    if not stale:
        return (Status.PASS,
                f"{inspected} session key(s) inspected — none stale",
                {"inspected": inspected, "stale": []})

    sample = ", ".join(s["key"] for s in stale[:3])
    extra = f" (+{len(stale)-3} more)" if len(stale) > 3 else ""
    return (Status.WARN,
            f"{len(stale)} of {inspected} session key(s) STALE: {sample}{extra}.  "
            "Run: python scripts/clean_redis_session.py",
            {"inspected": inspected, "stale": stale})


# ─── Phase 2 (Makers) — actions that prepare the environment ────────────────

def maker_refresh_holidays() -> tuple[str, str, dict]:
    """Force-refresh NSE holiday calendar; depends on Redis."""
    try:
        from bot.holidays import refresh_holidays
    except Exception as e:                                       # noqa: BLE001
        return (Status.FAIL, f"could not import bot.holidays: {e}", {})
    cal = refresh_holidays()
    n_eq = len(cal.by_segment.get("equity", []))
    n_fo = len(cal.by_segment.get("fno", []))
    if cal.source != "nse":
        return (Status.WARN,
                f"calendar source = {cal.source!r} (NSE was unreachable). "
                "Bot will still work but the calendar may be stale.",
                {"source": cal.source, "equity_count": n_eq, "fno_count": n_fo})
    return (Status.PASS,
            f"NSE refresh OK — equity={n_eq} F&O={n_fo} holidays",
            {"source": cal.source, "equity_count": n_eq, "fno_count": n_fo,
             "last_refresh": cal.last_refresh})


def maker_run_regression_suite() -> tuple[str, str, dict]:
    """Run the live-bot-safe portion of tests/test_fixes.py inline.

    The full test suite refuses to run when bots are live (correctly),
    so we extract the source-level pins for our most painful incidents
    and run them here:

    * **FIX #12** — synthetic-symbol pricing (the 2026-04-30 -₹8.3M
      spread blow-up). yfinance_proxy must NOT map synthetic symbols
      to underlying spot; ``executor._end_of_day`` must mark via
      ``intraday_bars``.

    * **FIX #13** — EOD race + equity over-sell guard (the 2026-05-04
      ADANIENT phantom-short, ~₹25k leak). ``executor._end_of_day``
      must read+write a per-segment Redis idempotency marker
      (``eod_done:{seg}``); the paper broker must reject equity SELL
      with qty greater than held long qty.

    If ANY pin regresses the bot is unsafe to start — this is what the
    user means by "stop encountering bugs every day": once we've paid
    for a fix, we pin it here so it can't sneak back via a refactor.
    """
    try:
        import inspect
        from bot.executor import Executor
        from bot.instruments.fno import yfinance_proxy
        from bot.broker.paper import PaperBroker
    except Exception as e:                                       # noqa: BLE001
        return (Status.FAIL, f"import error: {e}", {})

    # ── FIX #12 — synthetic-symbol pricing ──────────────────────────────
    cases = [
        ("NIFTY",                                  "^NSEI"),
        ("BANKNIFTY",                              "^NSEBANK"),
        ("NIFTY26MAYFUT",                          "^NSEI"),
        ("NIFTY26MAY24600CE",                      None),
        ("NIFTY26MAY24050-23950PESPRD",            None),
        ("BANKNIFTY26MAY55000-54900PESPRD",        None),
        ("NIFTY26MAY24300-24400-24700-24800IC",    None),
    ]
    failures = []
    for sym, expected in cases:
        got = yfinance_proxy(sym)
        if got != expected:
            failures.append({"symbol": sym, "expected": expected, "got": got})
    if failures:
        return (Status.FAIL,
                f"FIX #12 regressed: yfinance_proxy {len(failures)} case(s) wrong — "
                "the 2026-04-30 spot-leak bug may be back",
                {"fix": "12", "failures": failures})
    eod_src = inspect.getsource(Executor._end_of_day)
    if "latest_quote(p.symbol)" in eod_src:
        return (Status.FAIL,
                "FIX #12 regressed: executor._end_of_day reverted to "
                "latest_quote on positions — synthetic spread/IC marks WILL be wrong",
                {"fix": "12", "detected": "latest_quote(p.symbol)"})
    if "intraday_bars(p.symbol" not in eod_src:
        return (Status.FAIL,
                "FIX #12 regressed: executor._end_of_day no longer marks via "
                "intraday_bars — pricing path inconsistent",
                {"fix": "12"})

    # ── FIX #13 — EOD race idempotency ──────────────────────────────────
    if "eod_done:" not in eod_src:
        return (Status.FAIL,
                "FIX #13 regressed: executor._end_of_day no longer writes the "
                "eod_done:{segment} idempotency marker. The 2026-05-04 race "
                "condition (phantom short → ~₹25k leak) can recur.",
                {"fix": "13a"})
    if "set_json(eod_key" not in eod_src or "get_json(eod_key" not in eod_src:
        return (Status.FAIL,
                "FIX #13 regressed: executor._end_of_day must both READ and "
                "WRITE the eod_done marker (read at top to short-circuit, "
                "write at bottom to claim the day). Asymmetric impl detected.",
                {"fix": "13a"})

    # FIX #13a refinement (2026-05-05 PM): the marker check + write must
    # be gated on `mark_done`. Defensive sweeps from `_startup_catchup`
    # and `_shutdown` must opt out by passing `mark_done=False` so they
    # cannot poison the marker for the rest of the day. This was the
    # 2026-05-05 regression: a 10:13 startup sweep on a flat book set
    # the marker, blocking the legitimate 15:15 square-off when real
    # F&O positions opened at 13:26.
    if "mark_done" not in eod_src:
        return (Status.FAIL,
                "FIX #13a refinement regressed: executor._end_of_day no "
                "longer accepts the `mark_done` kwarg. Defensive sweeps "
                "(startup_catchup, SIGTERM) will poison the marker again "
                "— exact 2026-05-05 PM signature.",
                {"fix": "13a-refined"})
    sched_src_text = inspect.getsource(__import__("bot.scheduler", fromlist=["scheduler"]))
    if sched_src_text.count("mark_done=False") < 2:
        return (Status.FAIL,
                "FIX #13a refinement regressed: bot/scheduler.py expected "
                "to pass `mark_done=False` from BOTH _startup_catchup and "
                f"_shutdown (saw {sched_src_text.count('mark_done=False')} occurrence(s)). "
                "Without this, a midday restart's defensive sweep poisons "
                "the eod_done marker and blocks the 15:15 square-off.",
                {"fix": "13a-refined"})

    # ── FIX #13 — equity over-sell guard (refined 2026-05-05) ───────────
    # The guard MUST still reject the precise 2026-05-04 phantom-short
    # signature (is_squareoff orphan SELL on a flat book), AND the
    # genuine over-sell case (SELL qty > held long qty). It MUST NOT
    # reject fresh strategy-driven shorts (the 2026-05-05 regression
    # that blocked every equity short signal for the morning until the
    # guard was narrowed).
    place_src = inspect.getsource(PaperBroker.place_order)
    if "over-sell guard" not in place_src.lower():
        return (Status.FAIL,
                "FIX #13 regressed: paper broker place_order no longer "
                "implements the equity over-sell guard. A duplicate SELL "
                "on a flat book will create a phantom short and leak cash.",
                {"fix": "13b"})
    if "REJECTED" not in place_src or "held_long_qty" not in place_src:
        return (Status.FAIL,
                "FIX #13 regressed: over-sell guard present but not "
                "rejecting (missing REJECTED/held_long_qty markers)",
                {"fix": "13b"})
    # The refined guard MUST gate the orphan-flat-book reject behind
    # ``is_squareoff`` — otherwise it falls back to the over-aggressive
    # 2026-05-04 form that blocks legitimate strategy shorts.
    if "is_squareoff" not in place_src:
        return (Status.FAIL,
                "FIX #13b regressed: over-sell guard no longer scopes the "
                "flat-book reject to ``is_squareoff`` orders. Legitimate "
                "intraday shorts (e.g. MTF(ORB) SELL signals on 2026-05-05) "
                "would be blocked.",
                {"fix": "13b"})
    # And the synthesized square_off_all orders MUST carry the flag —
    # otherwise the May-04 defense path is bypassed entirely.
    sq_src = inspect.getsource(PaperBroker.square_off_all)
    if "is_squareoff=True" not in sq_src:
        return (Status.FAIL,
                "FIX #13b regressed: PaperBroker.square_off_all no longer "
                "tags its orders with is_squareoff=True. The 2026-05-04 "
                "phantom-short defense will not engage.",
                {"fix": "13b"})

    # ── FIX #14 — F&O EMA50 pre-warm via days=7 fetch window ────────────
    try:
        from bot.data import intraday_bars
    except Exception as e:                                       # noqa: BLE001
        return (Status.FAIL, f"could not import bot.data: {e}", {"fix": "14"})
    ib_src = inspect.getsource(intraday_bars)
    if "fetch_days" not in ib_src and "days=7" not in ib_src:
        return (Status.FAIL,
                "FIX #14 regressed: intraday_bars no longer pre-warms F&O "
                "with days=7 → strategies will be blind on Mondays (the "
                "2026-05-04 zero-F&O-trades incident).",
                {"fix": "14"})
    if "is_fno" not in ib_src:
        return (Status.FAIL,
                "FIX #14 regressed: intraday_bars no longer differentiates "
                "F&O vs equity for the fetch window — equity will pull "
                "unnecessary 7-day windows or F&O will starve.",
                {"fix": "14"})

    # ── FIX #15 — daily-loss formula F&O-aware (2026-05-05 PM) ──────────
    # ``RiskManager._equity`` must add ``margin_blocked`` back for futures
    # and ``(margin − credit_received)`` for short credit spreads / iron
    # condors. Without this, a freshly opened F&O credit-spread reports a
    # phantom -2-3 % "loss" (the margin block) the moment it opens, and
    # the kill-switch will halt all trading on a non-event. The
    # ``Executor._publish_state`` snapshot must also include
    # ``instrument_kind`` and ``margin_blocked`` per position so the
    # healthcheck and dashboard can apply the same correction.
    try:
        from bot.risk import RiskManager
    except Exception as e:                                       # noqa: BLE001
        return (Status.FAIL, f"could not import bot.risk: {e}", {"fix": "15"})
    risk_src = inspect.getsource(RiskManager._equity)
    if "SPREAD" not in risk_src or "IRON_CONDOR" not in risk_src:
        return (Status.FAIL,
                "FIX #15 regressed: RiskManager._equity no longer adjusts "
                "equity for SPREAD / IRON_CONDOR instrument kinds. Phantom "
                "margin-block 'loss' will trip the kill-switch on a "
                "freshly-opened credit spread (the 2026-05-05 PM scenario).",
                {"fix": "15"})
    if "margin_blocked" not in risk_src:
        return (Status.FAIL,
                "FIX #15 regressed: RiskManager._equity no longer references "
                "margin_blocked. F&O equity will be undervalued by the "
                "margin held, and the kill-switch will misfire.",
                {"fix": "15"})
    pub_src = inspect.getsource(Executor._publish_state)
    if "instrument_kind" not in pub_src or "margin_blocked" not in pub_src:
        return (Status.FAIL,
                "FIX #15 regressed: Executor._publish_state no longer writes "
                "instrument_kind / margin_blocked into portfolio:<seg>. The "
                "healthcheck and dashboard cannot apply the corrected "
                "equity formula.",
                {"fix": "15"})
    # Mirror check on healthcheck.py (the dashboard's other equity reader).
    try:
        from bot import healthcheck as _hc_mod
        hc_src = inspect.getsource(_hc_mod._portfolio_and_risk)
        if "margin_blocked" not in hc_src or "SPREAD" not in hc_src:
            return (Status.FAIL,
                    "FIX #15 regressed: healthcheck._portfolio_and_risk no "
                    "longer applies the F&O-aware equity correction. The "
                    "Portfolio & risk gate will FAIL on phantom margin "
                    "loss when an F&O credit-spread is open.",
                    {"fix": "15"})
    except Exception as e:                                       # noqa: BLE001
        return (Status.FAIL, f"could not import bot.healthcheck: {e}", {"fix": "15"})

    # ── FIX #27 — yfinance empty-bar retry + stale-cache fallback ───────
    # On 2026-05-11 (post-weekend Mon) the F&O bot emitted ZERO signals
    # because yfinance returned empty payloads for ^NSEI / ^NSEBANK in
    # 1-2 min bursts that compounded across the day (63 "No history"
    # warnings in the bot log). Three-layer fix:
    #   1. bot.data.history retries up to 3× with backoff on empty.
    #   2. bot.data.intraday_bars writes a parallel ":stale" cache copy
    #      and falls back to it when fresh fetch is empty.
    #   3. bot.data._warn_once dedups identical warnings per-hour so a
    #      sustained burst produces ~6 lines/day instead of 63+.
    try:
        from bot.data import history as _history_fn
    except Exception as e:                                       # noqa: BLE001
        return (Status.FAIL, f"could not import bot.data: {e}", {"fix": "27"})
    hist_src = inspect.getsource(_history_fn)
    if "_RETRY_DELAYS" not in hist_src or "time.sleep" not in hist_src:
        return (Status.FAIL,
                "FIX #27 regressed: bot.data.history no longer retries on "
                "empty yfinance responses. The 2026-05-11 F&O HOLD-all-day "
                "scenario can recur — option_buy_directional will return "
                "HOLD on every yfinance hiccup.",
                {"fix": "27"})
    ib_src2 = inspect.getsource(intraday_bars)
    if ":stale" not in ib_src2 or "_STALE_CACHE_TTL" not in ib_src2:
        return (Status.FAIL,
                "FIX #27 regressed: bot.data.intraday_bars no longer writes "
                "the ':stale' cache copy. Strategies will starve on "
                "transient yfinance empty-bar bursts.",
                {"fix": "27"})
    if "cutoff" not in ib_src2 or "interval_td" not in ib_src2:
        return (Status.FAIL,
                "FIX #27 regressed: bot.data.intraday_bars stale-fallback "
                "no longer enforces the bar-age cutoff. Strategies could "
                "trade on materially stale data.",
                {"fix": "27"})
    try:
        from bot import data as _data_mod
    except Exception as e:                                       # noqa: BLE001
        return (Status.FAIL, f"could not import bot.data: {e}", {"fix": "27"})
    if not hasattr(_data_mod, "_warn_once") or not hasattr(_data_mod, "_warning_dedup"):
        return (Status.FAIL,
                "FIX #27 regressed: bot.data._warn_once / _warning_dedup "
                "removed — empty-bar bursts will spam the log (63 "
                "lines/day signature from 2026-05-11).",
                {"fix": "27"})

    # ── FIX #29 — Position management until square_off ──────────────────
    # On 2026-05-15 BANKNIFTY26MAY54200CE rode unmanaged from 13:30
    # (trade_cutoff) to 15:15 (square_off), turning a -₹6,683 SL fill
    # into a -₹22,711.61 forced EOD close. Same pattern killed
    # NESTLEIND on 2026-04-29 and INFY on 2026-05-12. Fix: tick() now
    # uses the wider in_management_window (trade_start..square_off)
    # for SL/TP/trail checks, regardless of whether new entries are
    # still allowed.
    try:
        from bot.executor import Executor as _Executor
        from bot.risk import RiskManager as _RM
    except Exception as e:                                       # noqa: BLE001
        return (Status.FAIL, f"could not import bot.executor/risk: {e}",
                {"fix": "29"})
    tick_src = inspect.getsource(_Executor.tick)
    if "in_management_window" not in tick_src or "FIX #29" not in tick_src:
        return (Status.FAIL,
                "FIX #29 regressed: Executor.tick no longer manages open "
                "positions in the wider trade_start..square_off window. "
                "The 2026-05-15 BANKNIFTY ride-to-EOD scenario can recur — "
                "an open position past 13:30 will not have its SL/TP/trail "
                "checked until forced square-off at 15:15.",
                {"fix": "29"})
    if not hasattr(_RM, "in_management_window"):
        return (Status.FAIL,
                "FIX #29 regressed: RiskManager.in_management_window "
                "removed.", {"fix": "29"})

    # ── FIX #30 — Open-position kill switch ─────────────────────────────
    # On 2026-05-15 F&O closed -6.03% (3× past the -2% kill threshold)
    # because the daily-loss gate only blocked NEW entries, not
    # already-open positions whose premium decayed past the per-trade
    # SL. tick() now calls daily_loss_kill_breached() after the manage
    # pass and force-closes the book via _end_of_day when fired.
    if not hasattr(_RM, "daily_loss_kill_breached"):
        return (Status.FAIL,
                "FIX #30 regressed: RiskManager.daily_loss_kill_breached "
                "removed — open-position kill switch is gone.",
                {"fix": "30"})
    if "daily_loss_kill_breached" not in tick_src or "FIX #30" not in tick_src:
        return (Status.FAIL,
                "FIX #30 regressed: Executor.tick no longer checks "
                "daily_loss_kill_breached. An open position can again "
                "breach the -2% daily kill without force-closing the book.",
                {"fix": "30"})

    # ── FIX #31 — Tighter option-buy SL floor ────────────────────────────
    # 2026-05-15 BANKNIFTY26MAY54200CE -₹22.7K — pre-fix
    # min_sl_premium_pct of 0.30 allowed up to 70% premium drop. New
    # default 0.65 caps max drop at 35%.
    try:
        from bot.config import OptionBuyDirectionalCfg as _ObdCfg
    except Exception as e:                                       # noqa: BLE001
        return (Status.FAIL, f"could not import OptionBuyDirectionalCfg: {e}",
                {"fix": "31"})
    if _ObdCfg().min_sl_premium_pct < 0.60:
        return (Status.FAIL,
                f"FIX #31 regressed: OptionBuyDirectionalCfg default "
                f"min_sl_premium_pct = {_ObdCfg().min_sl_premium_pct} < "
                f"0.60. Option premium can drop > 40% before SL fires "
                f"(2026-05-15 BANKNIFTY scenario).",
                {"fix": "31"})

    # ── FIX #32 — Volatility-regime filter ──────────────────────────────
    # Today's 13:11 BANKNIFTY catastrophe fired the EMA20/50 cross but
    # realised 1h vol was only 9-11% (chop / theta-trap regime).
    # Premium decayed 36% by EOD. Filter: refuse the trade if
    # annualised 1h realised vol < min_realized_vol_pct (default 12%).
    if not hasattr(_ObdCfg(), "min_realized_vol_pct"):
        return (Status.FAIL,
                "FIX #32 regressed: OptionBuyDirectionalCfg.min_realized_vol_pct "
                "removed — theta-trap entries can recur (2026-05-15 BANKNIFTY).",
                {"fix": "32"})
    if _ObdCfg().min_realized_vol_pct < 0.05:
        return (Status.FAIL,
                f"FIX #32 regressed: min_realized_vol_pct = "
                f"{_ObdCfg().min_realized_vol_pct} < 0.05 — filter is "
                f"essentially disabled.",
                {"fix": "32"})
    try:
        from bot.strategies.fno.option_buy import OptionBuyDirectionalStrategy as _Obd
    except Exception as e:                                       # noqa: BLE001
        return (Status.FAIL, f"could not import OptionBuyDirectionalStrategy: {e}",
                {"fix": "32"})
    obd_src = inspect.getsource(_Obd.generate)
    if "min_realized_vol_pct" not in obd_src or "annualised_rv" not in obd_src:
        return (Status.FAIL,
                "FIX #32 regressed: option_buy_directional.generate no longer "
                "applies the realised-vol filter.",
                {"fix": "32"})

    # ── FIX #33 — RSI extreme filter (mean-reversion guard) ────────────
    # Today's 13:11 BANKNIFTY entry had RSI 67.8 (CE buy at the top of
    # a 200-pt rally); the 11:26 PE buy had RSI 32.2 (selling the
    # bottom). Both classic institutional fades. FIX #33 refuses CE
    # buys at RSI ≥ rsi_overbought and PE buys at RSI ≤ rsi_oversold.
    if not hasattr(_ObdCfg(), "rsi_overbought") or not hasattr(_ObdCfg(), "rsi_oversold"):
        return (Status.FAIL,
                "FIX #33 regressed: OptionBuyDirectionalCfg.rsi_overbought/rsi_oversold "
                "removed — buy-the-top entries can recur.",
                {"fix": "33"})
    if _ObdCfg().rsi_overbought > 75.0 or _ObdCfg().rsi_overbought < 60.0:
        return (Status.FAIL,
                f"FIX #33 regressed: rsi_overbought = "
                f"{_ObdCfg().rsi_overbought} outside [60, 75]; calibration drifted.",
                {"fix": "33"})
    if _ObdCfg().rsi_oversold < 25.0 or _ObdCfg().rsi_oversold > 40.0:
        return (Status.FAIL,
                f"FIX #33 regressed: rsi_oversold = "
                f"{_ObdCfg().rsi_oversold} outside [25, 40]; calibration drifted.",
                {"fix": "33"})
    if "FIX #33" not in obd_src or "rsi_overbought" not in obd_src:
        return (Status.FAIL,
                "FIX #33 regressed: option_buy_directional.generate no longer "
                "references the RSI filter.",
                {"fix": "33"})

    # ── FIX #34 — Bollinger %B mean-reversion filter ───────────────────
    # Today's 13:11 BANKNIFTY entry had %B = 90% — price hugging the
    # upper band, statistically resistance-vulnerable. FIX #34 refuses
    # CE buys at %B ≥ bb_upper_threshold and PE buys at %B ≤
    # bb_lower_threshold.
    if not hasattr(_ObdCfg(), "bb_upper_threshold"):
        return (Status.FAIL,
                "FIX #34 regressed: OptionBuyDirectionalCfg.bb_upper_threshold "
                "removed — fade-vulnerable entries can recur.",
                {"fix": "34"})
    if _ObdCfg().bb_upper_threshold < 0.75 or _ObdCfg().bb_upper_threshold > 1.0:
        return (Status.FAIL,
                f"FIX #34 regressed: bb_upper_threshold = "
                f"{_ObdCfg().bb_upper_threshold} outside [0.75, 1.0]; "
                f"calibration drifted.",
                {"fix": "34"})
    if "FIX #34" not in obd_src or "bb_upper_threshold" not in obd_src:
        return (Status.FAIL,
                "FIX #34 regressed: option_buy_directional.generate no longer "
                "references the Bollinger %B filter.",
                {"fix": "34"})

    # ── FIX #35 — Multi-source price validation ────────────────────────
    # Cross-checks yfinance spot against NSE's free public REST endpoint
    # before placing a trade. Fail-open: NSE outage doesn't halt the
    # bot. Refuses trade if divergence > multisource_max_divergence_pct
    # (default 1.0%).
    if not hasattr(_ObdCfg(), "multisource_max_divergence_pct"):
        return (Status.FAIL,
                "FIX #35 regressed: OptionBuyDirectionalCfg.multisource_max_divergence_pct "
                "removed — yfinance-only trade entry is back.",
                {"fix": "35"})
    if _ObdCfg().multisource_max_divergence_pct < 0.5 or _ObdCfg().multisource_max_divergence_pct > 2.0:
        return (Status.FAIL,
                f"FIX #35 regressed: multisource_max_divergence_pct = "
                f"{_ObdCfg().multisource_max_divergence_pct} outside [0.5, 2.0].",
                {"fix": "35"})
    if "FIX #35" not in obd_src or "validate_against_yfinance" not in obd_src:
        return (Status.FAIL,
                "FIX #35 regressed: option_buy_directional.generate no longer "
                "references the multi-source validator.",
                {"fix": "35"})
    # Verify the NSE-direct module exists and exposes the API.
    try:
        from bot.data_sources.nse_direct import (  # noqa: F401
            spot_price as _ns_spot,
            validate_against_yfinance as _ns_validate,
        )
    except Exception as _exc:  # noqa: BLE001
        return (Status.FAIL,
                f"FIX #35 regressed: bot.data_sources.nse_direct import failed: {_exc}",
                {"fix": "35"})

    # ── FIX #36 — Pluggable data-source registry (Phase 5A) ─────────────
    # FIX #36 introduces bot/data_sources/ as the single point of control
    # for which market-data backend serves the bot. Default remains
    # yfinance; Dhan is the Phase 5A target. Resilience: a non-yfinance
    # primary is always wrapped in a FallbackDataSource(primary, yfinance)
    # so a Dhan outage degrades silently to yfinance for that one tick.
    try:
        from bot.data_sources import (  # noqa: F401
            DataSource as _DS,
            FallbackDataSource as _FBDS,
            get_data_source as _get_ds,
            reset_registry as _reset_reg,
        )
        from bot.data_sources.yfinance_source import YFinanceDataSource as _YFDS
        from bot.data_sources.dhan_source import DhanDataSource as _DDS  # noqa: F401
        from bot.data_sources.dhan_resolver import resolve_symbol as _resolve  # noqa: F401
    except Exception as _exc:  # noqa: BLE001
        return (Status.FAIL,
                f"FIX #36 regressed: bot.data_sources import failed: {_exc}",
                {"fix": "36"})

    # Default must remain yfinance — anyone running the bot without
    # configuring DATA_SOURCE sees pre-Phase-5 behaviour.
    _saved_ds = os.environ.pop("DATA_SOURCE", None)
    try:
        _reset_reg()
        _active = _get_ds()
        if not isinstance(_active, _YFDS):
            return (Status.FAIL,
                    f"FIX #36 regressed: default backend is "
                    f"{type(_active).__name__}, expected YFinanceDataSource",
                    {"fix": "36"})
    finally:
        if _saved_ds is not None:
            os.environ["DATA_SOURCE"] = _saved_ds
        _reset_reg()

    # Routing helper must be wired into bot.data.
    from bot import data as _bot_data
    if not hasattr(_bot_data, "_maybe_route_to_alternate_source"):
        return (Status.FAIL,
                "FIX #36 regressed: bot.data._maybe_route_to_alternate_source removed",
                {"fix": "36"})
    _src_intraday = inspect.getsource(_bot_data.intraday_bars)
    _src_history = inspect.getsource(_bot_data.history)
    if "_maybe_route_to_alternate_source" not in _src_intraday:
        return (Status.FAIL,
                "FIX #36 regressed: bot.data.intraday_bars no longer consults the registry",
                {"fix": "36"})
    if "_maybe_route_to_alternate_source" not in _src_history:
        return (Status.FAIL,
                "FIX #36 regressed: bot.data.history no longer consults the registry",
                {"fix": "36"})

    # ── FIX #37 — Fee-aware entry gate ──────────────────────────────────
    # FIX #37 closes the cost-management loop by REJECTING any trade
    # whose expected round-trip fees exceed N% of the expected gross
    # at TP. Without the gate, fees are merely measured (FIX #21r
    # rates are accurate) — not used to make decisions. The gate
    # short-circuits 0.3-0.5% scalp-y signals where the fee drag
    # alone consumes 25%+ of the optimistic gross.
    from bot.risk import RiskManager as _RM37
    from bot.config import RiskCfg as _RiskCfg37, load_config as _lc37
    if not hasattr(_RM37, "_check_fee_edge"):
        return (Status.FAIL,
                "FIX #37 regressed: RiskManager._check_fee_edge missing",
                {"fix": "37"})
    if not hasattr(_RiskCfg37, "model_fields") or "max_fee_pct_of_gross" not in _RiskCfg37.model_fields:
        return (Status.FAIL,
                "FIX #37 regressed: RiskCfg.max_fee_pct_of_gross knob missing",
                {"fix": "37"})
    _src_eval37 = inspect.getsource(_RM37.evaluate)
    for _gate_tag in ("FIX #37 (2026-05-16) — fee-aware gate (options)",
                      "FIX #37 (2026-05-16) — fee-aware gate (futures)",
                      "FIX #37 (2026-05-16) — fee-aware gate (equity)"):
        if _gate_tag not in _src_eval37:
            return (Status.FAIL,
                    f"FIX #37 regressed: RiskManager.evaluate missing approval-path "
                    f"sentinel ({_gate_tag!r})",
                    {"fix": "37"})
    # The live config must have a sane threshold — too low (≤ 0) or too
    # high (≥ 100) silently disables the gate. Equity reads the
    # top-level `risk:` block; F&O reads `fno.risk:` — pin both.
    _cfg37 = _lc37()
    _eq_threshold = float(getattr(_cfg37.risk, "max_fee_pct_of_gross", 0.0) or 0.0)
    _fno_threshold = float(getattr(_cfg37.fno.risk, "max_fee_pct_of_gross", 0.0) or 0.0)
    for seg, val in (("equity", _eq_threshold), ("fno", _fno_threshold)):
        if not (0.0 < val < 100.0):
            return (Status.FAIL,
                    f"FIX #37 regressed: {seg} max_fee_pct_of_gross = {val} "
                    f"out of (0, 100) range — gate disabled",
                    {"fix": "37"})

    return (Status.PASS,
            "FIX #12 (synthetic pricing) + FIX #13 (EOD race + over-sell) + "
            "FIX #14 (F&O EMA50 pre-warm) + FIX #15 (F&O-aware daily-loss) + "
            "FIX #27 (yfinance retry + stale-cache) + FIX #29 (manage until "
            "square_off) + FIX #30 (open-pos kill) + FIX #31 (option-buy SL "
            "floor 0.65) + FIX #32 (theta-trap filter 12%) + FIX #33 (RSI "
            "mean-reversion guard) + FIX #34 (Bollinger %B fade guard) + "
            "FIX #35 (multi-source NSE cross-check) + FIX #36 (Phase 5A "
            "pluggable data-source registry) + FIX #37 (fee-aware entry "
            "gate) pinned",
            {"checks": len(cases) + 2 + 4 + 2 + 4 + 4 + 3 + 1 + 4 + 3 + 4 + 4 + 5,
             "fixes_pinned": ["12", "13a", "13b", "14", "15", "27",
                              "29", "30", "31", "32", "33", "34", "35", "36", "37"]})


# ─── Phase 3 (Decision checkers) — read post-maker state ────────────────────

def check_today_is_trading_day() -> tuple[str, str, dict]:
    """Today must be a trading day (weekday + not on the holiday list).

    If today is a holiday/weekend we WARN instead of FAIL — the operator
    might be running this on a Sunday to test, and we don't want to
    block that.
    """
    try:
        from bot.holidays import market_status, get_holidays
        from bot.segment import Segment
    except Exception as e:                                       # noqa: BLE001
        return (Status.SKIP, f"holiday module unavailable: {e}", {})
    cal = get_holidays(allow_refresh=False)
    today = date.today()
    eq = market_status(today, Segment.EQUITY, calendar=cal)
    fo = market_status(today, Segment.FNO,    calendar=cal)
    if eq["is_open"] and fo["is_open"]:
        return (Status.PASS,
                f"{today.strftime('%A %d %b')} — both segments OPEN today",
                {"equity": eq, "fno": fo})
    if eq["is_open"] or fo["is_open"]:
        closed = "F&O" if not fo["is_open"] else "Equity"
        return (Status.WARN,
                f"{closed} is CLOSED today ({(eq if not eq['is_open'] else fo)['reason']}). "
                f"Other segment is open.",
                {"equity": eq, "fno": fo})
    reason = eq["reason"] or fo["reason"] or "non-trading day"
    return (Status.WARN,
            f"NO trading today — {reason}. Bots will idle.",
            {"equity": eq, "fno": fo})


def check_tomorrow_advisory() -> tuple[str, str, dict]:
    """Heads-up if MIS positions need to be planned around tomorrow's closure."""
    try:
        from bot.holidays import market_status, get_holidays
        from bot.segment import Segment
    except Exception as e:                                       # noqa: BLE001
        return (Status.SKIP, f"holiday module unavailable: {e}", {})
    cal = get_holidays(allow_refresh=False)
    tom = date.today() + timedelta(days=1)
    eq = market_status(tom, Segment.EQUITY, calendar=cal)
    fo = market_status(tom, Segment.FNO,    calendar=cal)
    if eq["is_open"] and fo["is_open"]:
        return (Status.PASS,
                f"tomorrow ({tom.strftime('%a %d %b')}) — both open",
                {"equity": eq, "fno": fo})
    return (Status.WARN,
            f"tomorrow ({tom.strftime('%a %d %b')}) — "
            f"equity {eq['status']}, F&O {fo['status']}. "
            f"All MIS positions will square off today @ 15:15.",
            {"equity": eq, "fno": fo})


# ════════════════════════════════════════════════════════════════════════════
#  HARNESS
# ════════════════════════════════════════════════════════════════════════════

def build_steps() -> list[Step]:
    """Define the pre-flight ordering — checkers first, then makers, then
    post-make decision checkers that read the makers' outputs."""
    return [
        # ── Phase 1: cheap checkers (no I/O outside local fs / ps) ──────
        Step("python_venv",         "checker", critical=False, fn=check_python_venv),
        Step("power_source",        "checker", critical=False, fn=check_power_source),
        Step("disk_space",          "checker", critical=True,  fn=check_disk_space),
        Step("logs_writable",       "checker", critical=True,  fn=check_logs_writable),
        Step("no_stale_processes",  "checker", critical=False, fn=check_no_stale_bot_processes),
        Step("no_orphan_locks",     "checker", critical=True,  fn=check_no_orphan_lock_files),
        Step("config_loads",        "checker", critical=True,  fn=check_config_loads),

        # ── Phase 1: external sanity ─────────────────────────────────────
        Step("redis_running",       "checker", critical=True,  fn=check_redis_running),
        Step("paper_state_clean",   "checker", critical=True,  fn=check_paper_state_clean,
             depends_on=("redis_running",)),
        Step("redis_session_freshness", "checker", critical=False,
             fn=check_redis_session_freshness, depends_on=("redis_running",)),

        # ── Phase 2: makers (require Redis up) ──────────────────────────
        Step("refresh_holidays",    "maker",   critical=True,  fn=maker_refresh_holidays,
             depends_on=("redis_running",)),
        Step("regression_suite",    "maker",   critical=True,  fn=maker_run_regression_suite),

        # ── Phase 3: post-make decisions ────────────────────────────────
        Step("today_is_trading_day", "checker", critical=False,
             fn=check_today_is_trading_day, depends_on=("refresh_holidays",)),
        Step("tomorrow_advisory",    "checker", critical=False,
             fn=check_tomorrow_advisory,   depends_on=("refresh_holidays",)),
    ]


def banner(tee: TeeLogger, title: str, char: str = "═") -> None:
    tee.print()
    tee.print(_BOLD + char * 78 + _RESET)
    tee.print(_BOLD + " " + title + _RESET)
    tee.print(_BOLD + char * 78 + _RESET)


def run_preflight(args) -> int:
    ts = datetime.now()
    log_dir = ROOT / "logs" / "preflight"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{ts.strftime('%Y-%m-%d_%H%M%S')}.log"
    json_path = log_dir / f"{ts.strftime('%Y-%m-%d_%H%M%S')}.json"
    tee = TeeLogger(log_path)

    banner(tee, "🛫 PRE-MARKET PRE-FLIGHT")
    tee.print(f"  date / time     : {ts.strftime('%a %d %b %Y, %H:%M:%S %Z')}")
    tee.print(f"  host            : {socket.gethostname()}")
    tee.print(f"  cwd             : {ROOT}")
    tee.print(f"  log file        : {log_path}")
    tee.print(f"  python          : {sys.executable}  ({sys.version.split()[0]})")
    tee.print(f"  steps to run    : {len(build_steps())}")
    tee.print(f"  --auto-cleanup  : {args.auto_cleanup}")

    steps = build_steps()
    results: list[StepResult] = []

    cur_phase = ""
    PHASE_LABELS = {
        "checker": "── 🩺 CHECKERS — read-only system & state inspection ──",
        "maker":   "── 🛠  MAKERS — actions that prepare the environment ──",
    }
    for step in steps:
        if step.kind != cur_phase:
            cur_phase = step.kind
            banner(tee, PHASE_LABELS[cur_phase], char="─")

        tee.print(f"\n  ▶ {step.kind}.{step.name}"
                  + (f"  {_COLOR[Status.WARN]}(critical){_RESET}" if step.critical else ""))
        result = _run_step(step, results)
        results.append(result)

        emoji = _EMOJI[result.status]
        col = _COLOR[result.status]
        tee.print(f"    {emoji}  {col}{result.status:<4}{_RESET}  "
                  f"{result.detail}  ({result.elapsed_ms}ms)")

        # Auto-cleanup hook — picks the right script based on detected signature.
        if (result.status == Status.FAIL
                and result.name == "paper_state_clean"
                and args.auto_cleanup):
            sig = result.extras.get("signature", "")
            cleanup_script = {
                "apr30_spread_blowup": "cleanup_fno_2026_04_30.py",
                "may04_phantom_short": "cleanup_equity_2026_05_04.py",
            }.get(sig, "cleanup_fno_2026_04_30.py")
            tee.print(f"    {_COLOR[Status.WARN]}--auto-cleanup specified "
                      f"(signature={sig!r}) — invoking {cleanup_script}{_RESET}")
            try:
                cmd = [sys.executable, str(ROOT / "scripts" / cleanup_script)]
                if cleanup_script == "cleanup_equity_2026_05_04.py":
                    cmd.append("--force")  # bypass live-bot guard since we manage that
                proc = subprocess.run(
                    cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=30,
                )
                tee.print(textwrap.indent(proc.stdout, "      | "))
                if proc.returncode == 0:
                    tee.print(f"    {_COLOR[Status.PASS]}cleanup OK — re-running paper_state_clean check{_RESET}")
                    rerun = _run_step(step, results)
                    rerun.detail = "(re-run after auto-cleanup) " + rerun.detail
                    results[-1] = rerun
                    tee.print(f"    {_EMOJI[rerun.status]}  {_COLOR[rerun.status]}{rerun.status:<4}{_RESET}  "
                              f"{rerun.detail}")
            except Exception as e:                               # noqa: BLE001
                tee.print(f"    {_COLOR[Status.FAIL]}cleanup script crashed: {e}{_RESET}")

        # Optional: auto-clean stale Redis session keys on WARN.
        # Fires for both:
        #   * redis_session_freshness — stale intraday keys (signal:*,
        #     trail:*, heartbeat:*, etc.)
        #   * paper_state_clean with the ``may05_stale_portfolio``
        #     signature — yesterday's portfolio:<seg> snapshot leaked
        #     into today's dashboard. The WARN message itself points at
        #     clean_redis_session.py, which already wipes
        #     ``portfolio:<seg>`` via ``_exact_keys``. Without this
        #     branch the user passes --clean-redis and nothing happens
        #     because the script's gating only matched freshness.
        clean_redis_warn = (
            args.clean_redis
            and result.status == Status.WARN
            and (
                result.name == "redis_session_freshness"
                or (result.name == "paper_state_clean"
                    and result.extras.get("signature") == "may05_stale_portfolio")
            )
        )
        if clean_redis_warn:
            tee.print(f"    {_COLOR[Status.WARN]}--clean-redis specified "
                      f"({result.name}) — invoking clean_redis_session.py{_RESET}")
            try:
                proc = subprocess.run(
                    [sys.executable,
                     str(ROOT / "scripts" / "clean_redis_session.py"), "--force"],
                    cwd=str(ROOT), capture_output=True, text=True, timeout=30,
                )
                tee.print(textwrap.indent(proc.stdout, "      | "))
                if proc.returncode == 0:
                    tee.print(f"    {_COLOR[Status.PASS]}stale keys cleared — "
                              f"re-running freshness check{_RESET}")
                    rerun = _run_step(step, results)
                    rerun.detail = "(re-run after clean-redis) " + rerun.detail
                    results[-1] = rerun
                    tee.print(f"    {_EMOJI[rerun.status]}  {_COLOR[rerun.status]}{rerun.status:<4}{_RESET}  "
                              f"{rerun.detail}")
            except Exception as e:                               # noqa: BLE001
                tee.print(f"    {_COLOR[Status.FAIL]}redis cleanup crashed: {e}{_RESET}")

    # ── Final summary ──────────────────────────────────────────────────
    tally = {Status.PASS: 0, Status.WARN: 0, Status.FAIL: 0, Status.SKIP: 0}
    for r in results:
        tally[r.status] += 1
    critical_fails = [r for r in results if r.critical and r.status == Status.FAIL]
    any_fail = any(r.status == Status.FAIL for r in results)
    any_warn = any(r.status == Status.WARN for r in results)

    banner(tee, "📊 SUMMARY")
    tee.print(f"  {_COLOR[Status.PASS]}{tally[Status.PASS]:>3} PASS{_RESET}   "
              f"{_COLOR[Status.WARN]}{tally[Status.WARN]:>3} WARN{_RESET}   "
              f"{_COLOR[Status.FAIL]}{tally[Status.FAIL]:>3} FAIL{_RESET}   "
              f"{_COLOR[Status.SKIP]}{tally[Status.SKIP]:>3} SKIP{_RESET}   "
              f"of {len(results)} steps")

    if critical_fails:
        tee.print(f"\n  {_BOLD}{_COLOR[Status.FAIL]}🛑 DO NOT START THE BOTS.{_RESET}")
        tee.print(f"  {_COLOR[Status.FAIL]}Critical failures:{_RESET}")
        for r in critical_fails:
            tee.print(f"    • {r.name}: {r.detail}")
        verdict = "DO_NOT_START"
        exit_code = 1
    elif any_fail:
        tee.print(f"\n  {_BOLD}{_COLOR[Status.WARN]}⚠ Non-critical failures present — review and decide.{_RESET}")
        verdict = "REVIEW"
        exit_code = 1
    elif any_warn:
        tee.print(f"\n  {_BOLD}{_COLOR[Status.WARN]}⚠ All critical checks passed but some warnings — review:{_RESET}")
        for r in results:
            if r.status == Status.WARN:
                tee.print(f"    • {r.name}: {r.detail}")
        tee.print(f"\n  {_COLOR[Status.PASS]}You CAN start the bots, but read the warnings above first.{_RESET}")
        verdict = "OK_WITH_WARNINGS"
        exit_code = 2
    else:
        tee.print(f"\n  {_BOLD}{_COLOR[Status.PASS]}✅ ALL CHECKS PASSED — safe to launch the bots.{_RESET}")
        verdict = "OK"
        exit_code = 0

    if verdict in ("OK", "OK_WITH_WARNINGS"):
        tee.print()
        tee.print(f"  {_BOLD}Next steps (run in three separate terminals):{_RESET}")
        tee.print( "    Terminal 1:  bash scripts/run_bot.sh run --paper")
        tee.print( "    Terminal 2:  bash scripts/run_bot.sh run --paper --segment fno")
        tee.print( "    Terminal 3:  python -m cli dashboard")

    # Update the latest.log symlink.
    latest = log_dir / "latest.log"
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(log_path.name)
    except OSError:
        pass

    # Persist machine-readable result.
    payload = {
        "timestamp":      ts.isoformat(),
        "verdict":        verdict,
        "exit_code":      exit_code,
        "tally":          tally,
        "critical_fails": [r.name for r in critical_fails],
        "results":        [asdict(r) for r in results],
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    tee.print(f"\n  log:  {log_path}")
    tee.print(f"  json: {json_path}")
    tee.print(f"  latest symlink: {latest}")

    if args.json:
        # Re-emit JSON to stdout (after the human-readable section, in
        # case the operator's terminal pipes both).
        sys.stdout.write("\n--- JSON ---\n")
        sys.stdout.write(json.dumps(payload, indent=2, default=str))
        sys.stdout.write("\n")

    tee.close()
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="premarket_preflight",
        description="Maker/checker harness — validates every precondition "
                    "before the daily bot startup.",
    )
    parser.add_argument(
        "--auto-cleanup", action="store_true",
        help="Auto-run the matching paper-state cleanup script if the "
             "corruption check fails. Picks the right script based on the "
             "detected signature (Apr-30 spread blow-up vs May-04 phantom-"
             "short). Off by default — surface the error first."
    )
    parser.add_argument(
        "--clean-redis", action="store_true",
        help="If the Redis session-freshness check finds stale intraday keys, "
             "OR if paper_state_clean detects the 2026-05-05 stale-portfolio "
             "signature (yesterday's portfolio:<seg> snapshot leaking into "
             "today's dashboard), automatically run "
             "scripts/clean_redis_session.py to wipe the stale keys and "
             "re-check. Off by default — review the WARN first."
    )
    parser.add_argument(
        "--json", action="store_true",
        help="In addition to the pretty terminal output, emit the full result "
             "as JSON on stdout (useful for piping into another tool)."
    )
    args = parser.parse_args()
    return run_preflight(args)


if __name__ == "__main__":
    raise SystemExit(main())
