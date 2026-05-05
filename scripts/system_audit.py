"""System audit — comprehensive read-only investigation of the running bots.

Run **AFTER** ``premarket_preflight.py`` and **AFTER** the equity + F&O
bots + dashboard are up. Verifies that everything wired up by the
preflight is actually behaving end-to-end. Where the preflight asks
"can we start?", this audit asks "are we running cleanly and is today
likely to be a good trading day?".

Output structure mirrors the manual investigation report from chat —
each section is a dedicated finding with status (OK / WARN / FAIL) and
a one-line detail. The full report is also written to
``logs/audit/YYYY-MM-DD_HHMMSS.{log,json}`` for the post-mortem.

Usage:
    python scripts/system_audit.py                # human-readable report
    python scripts/system_audit.py --json         # machine-readable JSON
    python scripts/system_audit.py --quiet        # only summary line

Exit codes:
    0   GO       — all checks OK or only expected pre-market WARNs
    2   CONCERNS — non-critical WARNs that warrant a glance
    1   NO-GO    — at least one CRITICAL FAIL — do NOT trust the bots

Sections (in order):
    1.  Live processes (bots, dashboard, caffeinate)
    2.  Latest pre-market preflight result
    3.  Capital & risk caps (per-segment table)
    4.  Critical fix verifications (the 2026-04-30 spot-leak guards)
    5.  Corrupted artefacts quarantine (.corrupted-by-2026-04-30-*)
    6.  Redis hygiene (paper:state, portfolio, orders, holiday cache)
    7.  Trading-day status (today + tomorrow + next 14 days)
    8.  Scheduler jobs registered (parsed from today's bot log)
    9.  Strategy readiness (live smoke test on equity + F&O)
    10. Healthcheck dry-run (both segments)
    11. Dashboard reachability (HTTP probe localhost:8501)
    12. Cleanup advisories (stale orders, deprecation warnings)

Every section is read-only — this script never deletes, writes, or
shuts anything down. Safe to run during the trading window.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import textwrap
import time
import traceback
import urllib.error
import urllib.request
import warnings
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ─────────────────────────── Result types + ANSI ────────────────────────────


class Status:
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


_EMOJI = {Status.OK: "✅", Status.WARN: "⚠️ ", Status.FAIL: "❌", Status.SKIP: "⏭ "}
_COLOR = {Status.OK: "\033[32m",  Status.WARN: "\033[33m",
          Status.FAIL: "\033[31m", Status.SKIP: "\033[90m"}
_RESET = "\033[0m"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"
_CYAN  = "\033[36m"


@dataclass
class Finding:
    """One audit observation."""
    name: str
    status: str
    detail: str
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class Section:
    """A group of findings (a report chapter)."""
    title: str
    findings: list[Finding] = field(default_factory=list)
    intro: str = ""

    def overall(self) -> str:
        ranks = {Status.OK: 0, Status.WARN: 1, Status.FAIL: 2, Status.SKIP: 0}
        worst = max((ranks[f.status] for f in self.findings), default=0)
        return {0: Status.OK, 1: Status.WARN, 2: Status.FAIL}.get(worst, Status.OK)


# Section runner signature: returns ``Section`` populated with findings.
SectionFn = Callable[[], Section]


# ─────────────────────── Tee logger (terminal + file) ───────────────────────

class _Tee:
    """Mirror everything printed via ``tee.print`` to a logfile.

    Strips ANSI from the file copy so the log is grep-able.
    """
    _ANSI = re.compile(r"\033\[[0-9;]*m")

    def __init__(self, logfile: Optional[Path], quiet: bool = False) -> None:
        self.quiet = quiet
        self._fh = None
        if logfile is not None:
            logfile.parent.mkdir(parents=True, exist_ok=True)
            self._fh = logfile.open("w")

    def print(self, *parts: str, end: str = "\n", force: bool = False) -> None:
        line = " ".join(str(p) for p in parts)
        if not self.quiet or force:
            sys.stdout.write(line + end)
            sys.stdout.flush()
        if self._fh is not None:
            self._fh.write(self._ANSI.sub("", line) + end)
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()


# ════════════════════════════════════════════════════════════════════════════
#  SECTION IMPLEMENTATIONS
#
#  Each section is independent — failures inside one section don't break
#  the rest. We swallow exceptions per-finding and surface them as FAIL.
# ════════════════════════════════════════════════════════════════════════════


# ─── Section 1 — Live processes ─────────────────────────────────────────────

def _ps_lines() -> list[str]:
    """Return ``ps -eo pid,etime,command`` output lines (skipping header)."""
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,etime,command"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
    except Exception:
        return []
    return [ln for ln in out.splitlines() if ln.strip() and not ln.lstrip().startswith("PID")]


def _match_proc(needles: list[str], excludes: list[str] | None = None) -> list[dict]:
    """Find processes whose command contains ALL needles and NONE of ``excludes``."""
    excludes = excludes or []
    self_pid = os.getpid()
    out = []
    for ln in _ps_lines():
        if not all(n in ln for n in needles):
            continue
        if any(x in ln for x in excludes):
            continue
        parts = ln.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == self_pid:
            continue
        out.append({"pid": pid, "etime": parts[1], "command": parts[2][:140]})
    return out


def section_live_processes() -> Section:
    sec = Section(
        "Live processes",
        intro="Both bots, the dashboard, and the macOS sleep blockers must be alive.",
    )
    equity = _match_proc(["python", " run", "--paper"], excludes=["--segment fno", "system_audit"])
    fno    = _match_proc(["python", " run", "--paper", "--segment fno"], excludes=["system_audit"])
    dash   = _match_proc(["streamlit", "run", "app.py"])
    caff   = _match_proc(["caffeinate"])

    if equity:
        e = equity[0]
        sec.findings.append(Finding(
            "Equity bot", Status.OK,
            f"pid={e['pid']} uptime={e['etime']} (cli run --paper)",
            {"pid": e["pid"], "etime": e["etime"]},
        ))
    else:
        sec.findings.append(Finding(
            "Equity bot", Status.FAIL,
            "no `cli run --paper` (equity) process found — start with `bash scripts/run_bot.sh run --paper`",
        ))

    if fno:
        f = fno[0]
        sec.findings.append(Finding(
            "F&O bot", Status.OK,
            f"pid={f['pid']} uptime={f['etime']} (cli run --paper --segment fno)",
            {"pid": f["pid"], "etime": f["etime"]},
        ))
    else:
        sec.findings.append(Finding(
            "F&O bot", Status.FAIL,
            "no `cli run --paper --segment fno` process — start with `bash scripts/run_bot.sh run --paper --segment fno`",
        ))

    if dash:
        d = dash[0]
        sec.findings.append(Finding(
            "Dashboard", Status.OK,
            f"pid={d['pid']} uptime={d['etime']} (streamlit on app.py)",
            {"pid": d["pid"], "etime": d["etime"]},
        ))
    else:
        sec.findings.append(Finding(
            "Dashboard", Status.WARN,
            "no streamlit process found — `python -m cli dashboard` to launch",
        ))

    pinned = []
    for c in caff:
        m = re.search(r"-w\s+(\d+)", c["command"])
        if m:
            pinned.append((c["pid"], int(m.group(1)), c["etime"]))
    expected_pids = {x["pid"] for x in equity + fno}
    pinned_pids = {p[1] for p in pinned}
    missing = expected_pids - pinned_pids
    if not expected_pids:
        sec.findings.append(Finding("Sleep prevention", Status.SKIP, "no bot to caffeinate"))
    elif missing:
        sec.findings.append(Finding(
            "Sleep prevention", Status.WARN,
            f"caffeinate is NOT pinned to bot pid(s) {sorted(missing)} — Mac may sleep mid-trade",
            {"pinned_to": list(pinned_pids), "expected": sorted(expected_pids)},
        ))
    else:
        sec.findings.append(Finding(
            "Sleep prevention", Status.OK,
            f"caffeinate pinned to {len(pinned)} bot pid(s) — Mac sleep blocked",
            {"pinned": [{"caff_pid": cp, "bot_pid": bp, "etime": et} for cp, bp, et in pinned]},
        ))
    return sec


# ─── Section 2 — Latest preflight ───────────────────────────────────────────

def section_preflight() -> Section:
    sec = Section(
        "Pre-market preflight",
        intro="The maker/checker harness from `scripts/premarket_preflight.py`. "
              "Should be GREEN before bots are launched.",
    )
    pre_dir = ROOT / "logs" / "preflight"
    if not pre_dir.exists():
        sec.findings.append(Finding(
            "Preflight log", Status.WARN,
            f"no {pre_dir} directory — `python scripts/premarket_preflight.py` was never run",
        ))
        return sec
    today = date.today().isoformat()
    json_today = sorted(pre_dir.glob(f"{today}_*.json"))
    json_latest = sorted(pre_dir.glob("*.json"))
    if json_today:
        chosen = json_today[-1]
        same_day = True
    elif json_latest:
        chosen = json_latest[-1]
        same_day = False
    else:
        sec.findings.append(Finding(
            "Preflight log", Status.WARN,
            "no preflight JSON output found — run `python scripts/premarket_preflight.py`",
        ))
        return sec

    try:
        data = json.loads(chosen.read_text())
    except Exception as e:                                       # noqa: BLE001
        sec.findings.append(Finding(
            "Preflight log", Status.FAIL, f"unparseable preflight JSON {chosen.name}: {e}",
        ))
        return sec
    # Preflight schema: {timestamp, verdict, exit_code, tally, results: [...]}
    verdict = data.get("verdict", "?")
    tally = data.get("tally") or {}
    results = data.get("results", [])
    n_pass = int(tally.get("PASS", sum(1 for r in results if r.get("status") == "PASS")))
    n_warn = int(tally.get("WARN", sum(1 for r in results if r.get("status") == "WARN")))
    n_fail = int(tally.get("FAIL", sum(1 for r in results if r.get("status") == "FAIL")))
    n_skip = int(tally.get("SKIP", sum(1 for r in results if r.get("status") == "SKIP")))
    n_total = n_pass + n_warn + n_fail + n_skip
    age = chosen.stat().st_mtime
    age_str = datetime.fromtimestamp(age).strftime("%Y-%m-%d %H:%M:%S")

    status = (Status.OK if verdict == "OK" and same_day else
              Status.WARN if verdict == "OK" else Status.FAIL)
    sec.findings.append(Finding(
        "Preflight log", status,
        f"{chosen.name}: verdict={verdict}, {n_pass}P/{n_warn}W/{n_fail}F/{n_skip}S of {n_total}, ran {age_str}"
        + ("" if same_day else "  ⚠ NOT FROM TODAY"),
        {"file": str(chosen), "verdict": verdict, "pass": n_pass, "warn": n_warn,
         "fail": n_fail, "skip": n_skip, "same_day": same_day},
    ))
    failed = [r for r in results if r.get("status") == "FAIL"]
    if failed:
        sec.findings.append(Finding(
            "Preflight failures", Status.FAIL,
            "FAILED steps: " + ", ".join(f"{r['name']} ({r.get('detail','')[:60]})" for r in failed),
            {"failed_steps": [r["name"] for r in failed]},
        ))
    return sec


# ─── Section 3 — Capital & risk caps ────────────────────────────────────────

def section_risk_caps() -> Section:
    sec = Section(
        "Capital & risk caps",
        intro="Per-segment risk levers as loaded from config.yaml. Daily-loss "
              "cap halts trading; profit-lock target stops once met.",
    )
    try:
        from bot.config import load_config
        cfg = load_config()
    except Exception as e:                                       # noqa: BLE001
        sec.findings.append(Finding("Config load", Status.FAIL, f"{e}"))
        return sec
    eq_cap = cfg.capital.total
    eq_risk = cfg.risk
    sec.findings.append(Finding(
        "Equity caps", Status.OK,
        f"capital ₹{eq_cap:,.0f} · loss_cap {eq_risk.max_daily_loss_pct}% "
        f"(₹{eq_cap*eq_risk.max_daily_loss_pct/100:,.0f}) · "
        f"profit_lock {eq_risk.daily_profit_target_pct}% "
        f"(₹{eq_cap*eq_risk.daily_profit_target_pct/100:,.0f}) · "
        f"trades/day {eq_risk.max_trades_per_day} · max_open {eq_risk.max_open_positions} · "
        f"per-trade SL {eq_risk.max_loss_per_trade_pct}%",
        {"capital": eq_cap, "max_daily_loss_pct": eq_risk.max_daily_loss_pct,
         "daily_profit_target_pct": eq_risk.daily_profit_target_pct},
    ))
    if cfg.fno is None or not cfg.fno.enabled:
        sec.findings.append(Finding(
            "F&O caps", Status.WARN,
            "F&O is DISABLED in config (fno.enabled=false) — F&O bot will refuse to start",
        ))
    else:
        fno_cap = cfg.fno.capital.total
        fno_risk = cfg.fno.risk
        sec.findings.append(Finding(
            "F&O caps", Status.OK,
            f"capital ₹{fno_cap:,.0f} · loss_cap {fno_risk.max_daily_loss_pct}% "
            f"(₹{fno_cap*fno_risk.max_daily_loss_pct/100:,.0f}) · "
            f"profit_lock {fno_risk.daily_profit_target_pct}% "
            f"(₹{fno_cap*fno_risk.daily_profit_target_pct/100:,.0f}) · "
            f"trades/day {fno_risk.max_trades_per_day} · max_open {fno_risk.max_open_positions} · "
            f"per-trade SL {fno_risk.max_loss_per_trade_pct}% · "
            f"strategies {cfg.fno.strategies.enabled}",
            {"capital": fno_cap, "max_daily_loss_pct": fno_risk.max_daily_loss_pct,
             "strategies": cfg.fno.strategies.enabled},
        ))
    daily_target = eq_risk.daily_profit_target_pct
    user_target_low, user_target_high = 3000, 5000
    target_inr = eq_cap * daily_target / 100
    if target_inr < user_target_low or target_inr > user_target_high * 2:
        sec.findings.append(Finding(
            "Daily target alignment", Status.WARN,
            f"profit_lock at ₹{target_inr:,.0f}/day — operator's stated objective is "
            f"₹{user_target_low}-{user_target_high}/day. Re-tune `daily_profit_target_pct`.",
        ))
    else:
        sec.findings.append(Finding(
            "Daily target alignment", Status.OK,
            f"profit_lock @ ₹{target_inr:,.0f}/day fits operator's ₹{user_target_low}-{user_target_high} objective",
        ))
    return sec


# ─── Section 4 — Critical fix verifications ─────────────────────────────────

def section_critical_fixes() -> Section:
    sec = Section(
        "Critical fix verifications",
        intro="Pin the 2026-04-30 -₹8.3M spread blow-up regressions. If any of "
              "these go red, STOP trading until investigated.",
    )
    try:
        from bot.instruments.fno import yfinance_proxy
    except Exception as e:                                       # noqa: BLE001
        sec.findings.append(Finding("yfinance_proxy import", Status.FAIL, f"{e}"))
        return sec

    # Fix 1: yfinance_proxy rejects synthetic instruments
    cases = [
        ("NIFTY",                                  "^NSEI"),
        ("BANKNIFTY",                              "^NSEBANK"),
        ("NIFTY26MAYFUT",                          "^NSEI"),
        ("NIFTY26MAY24600CE",                      None),
        ("BANKNIFTY26MAY55000-54900PESPRD",        None),
        ("NIFTY26MAY24300-24400-24700-24800IC",    None),
    ]
    failures = [(s, exp, yfinance_proxy(s)) for s, exp in cases if yfinance_proxy(s) != exp]
    if failures:
        sec.findings.append(Finding(
            "yfinance_proxy spot-leak guard", Status.FAIL,
            f"{len(failures)} regression(s): " +
            ", ".join(f"{s}→{got!r} (want {exp!r})" for s, exp, got in failures),
            {"failures": [{"sym": s, "expected": exp, "got": got} for s, exp, got in failures]},
        ))
    else:
        sec.findings.append(Finding(
            "yfinance_proxy spot-leak guard", Status.OK,
            f"{len(cases)} cases: synthetic option/spread/IC tradingsymbols correctly map to None "
            "(would have leaked spot price → -₹8.3M on 04-30)",
        ))

    # Fix 2: executor._end_of_day uses intraday_bars not latest_quote
    try:
        import inspect
        from bot.executor import Executor
        eod_src = inspect.getsource(Executor._end_of_day)
    except Exception as e:                                       # noqa: BLE001
        sec.findings.append(Finding("_end_of_day inspect", Status.FAIL, f"{e}"))
    else:
        if "latest_quote(p.symbol)" in eod_src:
            sec.findings.append(Finding(
                "_end_of_day pricing path", Status.FAIL,
                "REGRESSED — _end_of_day calls latest_quote(p.symbol). Synthetic spreads "
                "will be marked at SPOT again. The 04-30 fix has been undone.",
            ))
        elif "intraday_bars(p.symbol" not in eod_src:
            sec.findings.append(Finding(
                "_end_of_day pricing path", Status.FAIL,
                "_end_of_day no longer marks via intraday_bars — pricing path inconsistent",
            ))
        else:
            sec.findings.append(Finding(
                "_end_of_day pricing path", Status.OK,
                "uses intraday_bars(p.symbol) for marks — synthetic spreads route through "
                "Black-Scholes synthesis, no spot leak possible",
            ))

    # Fix 3: _manage_open_positions has 1m → 5m → broker-mark fallback chain
    try:
        mgmt_src = inspect.getsource(Executor._manage_open_positions)
    except Exception as e:                                       # noqa: BLE001
        sec.findings.append(Finding("_manage_open_positions inspect", Status.FAIL, f"{e}"))
    else:
        chain_present = ('intraday_bars(pos.symbol, "1m")' in mgmt_src
                         and 'intraday_bars(pos.symbol, "5m")' in mgmt_src
                         and 'fallback_mark' in mgmt_src)
        if chain_present:
            sec.findings.append(Finding(
                "Position-mgmt fallback chain", Status.OK,
                "1m → 5m → last-broker-mark fallback present (prevents the 04-29 NESTLEIND "
                "ride-to-EOD: silent skips on empty bars are gone)",
            ))
        else:
            sec.findings.append(Finding(
                "Position-mgmt fallback chain", Status.FAIL,
                "_manage_open_positions does NOT have full 1m→5m→broker-mark fallback — "
                "open positions may go unmanaged on yfinance hiccups",
            ))
    return sec


# ─── Section 5 — Corrupted artefacts quarantine ─────────────────────────────

def section_quarantine() -> Section:
    sec = Section(
        "Corrupted-artefacts quarantine",
        intro="The 2026-04-30 spot-leak bug produced fake -₹8.3M trades. The cleanup "
              "script renamed those artefacts aside; this confirms they're still quarantined.",
    )
    suffix = ".corrupted-by-2026-04-30-spot-leak-bug"
    quarantined = []
    leaked = []
    for sub in ("logs/trades", "logs/eod"):
        for path in (ROOT / sub).rglob("*"):
            if path.is_file() and path.name.endswith(suffix):
                quarantined.append(path.relative_to(ROOT))
    # Real (un-renamed) 04-30 F&O journal would be the leak
    bad_journals = [
        ROOT / "logs" / "trades" / "fno" / "2026-04-30.jsonl",
        ROOT / "logs" / "trades" / "fno" / "2026-04-30.csv",
    ]
    for bj in bad_journals:
        if bj.exists():
            leaked.append(bj.relative_to(ROOT))

    if leaked:
        sec.findings.append(Finding(
            "Re-emerged corruption", Status.FAIL,
            f"{len(leaked)} corrupted 04-30 file(s) re-appeared: " +
            ", ".join(str(p) for p in leaked) +
            " — re-run `python scripts/cleanup_fno_2026_04_30.py`",
            {"files": [str(p) for p in leaked]},
        ))
    if quarantined:
        sec.findings.append(Finding(
            "Quarantined files", Status.OK,
            f"{len(quarantined)} corrupted 04-30 artefact(s) safely renamed aside",
            {"files": [str(p) for p in quarantined]},
        ))
    else:
        sec.findings.append(Finding(
            "Quarantined files", Status.SKIP,
            "no .corrupted-by-2026-04-30 artefacts found (either never happened or already wiped)",
        ))
    return sec


# ─── Section 6 — Redis hygiene ──────────────────────────────────────────────

def section_redis_state() -> Section:
    sec = Section(
        "Redis hygiene",
        intro="Verify per-segment paper:state, portfolio snapshots, holiday cache, "
              "and that no stale orders are confusing dashboards.",
    )
    try:
        from bot.cache import get_cache
        cache = get_cache()
    except Exception as e:                                       # noqa: BLE001
        sec.findings.append(Finding("Redis connect", Status.FAIL, f"{e}"))
        return sec
    try:
        import redis
        from bot.config import env
        r = redis.Redis.from_url(env().REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        r.ping()
        keys = sorted(r.keys("*"))
    except Exception as e:                                       # noqa: BLE001
        sec.findings.append(Finding("Redis keys", Status.FAIL, f"{e}"))
        return sec

    sec.findings.append(Finding(
        "Redis connectivity", Status.OK,
        f"PONG, {len(keys)} keys total",
        {"key_count": len(keys), "sample": keys[:20]},
    ))

    for seg in ("equity", "fno"):
        snap = cache.get_json(f"paper:state:{seg}") or {}
        if not snap:
            sec.findings.append(Finding(
                f"paper:state:{seg}", Status.OK,
                "empty (broker will boot fresh from configured capital on first tick)",
            ))
            continue
        cash = snap.get("cash")
        starting = snap.get("starting_capital")
        n_pos = len(snap.get("positions", {}))
        saved_at = snap.get("saved_at", "?")
        if cash is None:
            sec.findings.append(Finding(
                f"paper:state:{seg}", Status.WARN,
                f"snapshot has no cash field (saved_at={saved_at})",
            ))
        elif starting and cash < -abs(starting):
            sec.findings.append(Finding(
                f"paper:state:{seg}", Status.FAIL,
                f"CORRUPTED: cash=₹{cash:,.0f} < -starting_capital ₹{starting:,.0f}. "
                "Run `python scripts/cleanup_fno_2026_04_30.py`.",
            ))
        else:
            today = datetime.now().date().isoformat()
            same_day = saved_at[:10] == today
            sec.findings.append(Finding(
                f"paper:state:{seg}",
                Status.OK if same_day else Status.WARN,
                f"cash=₹{cash:,.2f} starting=₹{starting:,.0f} positions={n_pos} saved_at={saved_at}"
                + ("" if same_day else "  ⚠ stale (not from today — broker will discard on restart)"),
            ))

    holiday_snap = cache.get_json("nse:holidays:v2") or {}
    if not holiday_snap:
        sec.findings.append(Finding(
            "Holiday cache", Status.WARN,
            "nse:holidays:v2 missing — run `python scripts/premarket_preflight.py` to refresh",
        ))
    else:
        eq_n = len(holiday_snap.get("by_segment", {}).get("equity", []))
        fo_n = len(holiday_snap.get("by_segment", {}).get("fno", []))
        src = holiday_snap.get("source", "?")
        last = holiday_snap.get("last_refresh", "?")
        sec.findings.append(Finding(
            "Holiday cache", Status.OK if src == "nse" else Status.WARN,
            f"source={src}  equity={eq_n} F&O={fo_n} holidays  last_refresh={last}",
            {"source": src, "equity": eq_n, "fno": fo_n, "last_refresh": last},
        ))

    try:
        n_orders = r.hlen("orders") if r.type("orders") == "hash" else 0
    except Exception:
        n_orders = 0
    if n_orders > 0:
        sec.findings.append(Finding(
            "Stale `orders` audit hash", Status.WARN,
            f"{n_orders} entries — write-only audit log, NO functional impact, "
            "but cleaner with `redis-cli del orders`",
            {"count": n_orders},
        ))
    else:
        sec.findings.append(Finding(
            "Stale `orders` audit hash", Status.OK, "empty",
        ))
    return sec


# ─── Section 7 — Trading day status ─────────────────────────────────────────

def section_trading_days() -> Section:
    sec = Section(
        "Trading-day status",
        intro="Today + tomorrow + the next 14 calendar days. Sourced from "
              "the live NSE holiday calendar in Redis.",
    )
    try:
        from bot.holidays import get_holidays, is_trading_day, market_status
        from bot.segment import Segment
    except Exception as e:                                       # noqa: BLE001
        sec.findings.append(Finding("holidays import", Status.FAIL, f"{e}"))
        return sec
    try:
        cal = get_holidays(allow_refresh=False)
    except Exception as e:                                       # noqa: BLE001
        sec.findings.append(Finding("holidays cache", Status.FAIL, f"{e}"))
        return sec
    today = date.today()
    tomorrow = today + timedelta(days=1)

    for d, label in [(today, "Today"), (tomorrow, "Tomorrow")]:
        eq = market_status(d, Segment.EQUITY, calendar=cal)
        fo = market_status(d, Segment.FNO, calendar=cal)
        if eq["is_open"] and fo["is_open"]:
            sec.findings.append(Finding(
                f"{label} ({d.strftime('%a %d %b')})", Status.OK,
                "both equity and F&O OPEN",
                {"equity": eq, "fno": fo},
            ))
        elif eq["is_open"] or fo["is_open"]:
            closed = "F&O" if not fo["is_open"] else "Equity"
            sec.findings.append(Finding(
                f"{label} ({d.strftime('%a %d %b')})", Status.WARN,
                f"{closed} CLOSED ({(fo if not fo['is_open'] else eq)['reason']}) — other segment open",
                {"equity": eq, "fno": fo},
            ))
        else:
            reason = eq["reason"] or fo["reason"]
            sec.findings.append(Finding(
                f"{label} ({d.strftime('%a %d %b')})", Status.WARN,
                f"BOTH CLOSED — {reason}",
                {"equity": eq, "fno": fo},
            ))

    upcoming = []
    for i in range(0, 14):
        d = today + timedelta(days=i)
        eq_open = is_trading_day(d, Segment.EQUITY, calendar=cal)
        upcoming.append({
            "date": d.isoformat(),
            "weekday": d.strftime("%a"),
            "equity_open": eq_open,
            "fno_open": is_trading_day(d, Segment.FNO, calendar=cal),
        })
    open_count = sum(1 for u in upcoming if u["equity_open"])
    sec.findings.append(Finding(
        "Next 14 days", Status.OK,
        f"{open_count} trading day(s) of 14 — "
        + " ".join(f"{u['date'][5:]}{'✓' if u['equity_open'] else '✗'}" for u in upcoming),
        {"calendar": upcoming},
    ))
    return sec


# ─── Section 8 — Scheduler jobs ─────────────────────────────────────────────

_SCHED_LINE = re.compile(r"\| INFO\s+\| bot\.scheduler:start[\d:]* - \[(\w+)\] (Scheduled .+|Bot started .+)")


def section_scheduler_jobs() -> Section:
    sec = Section(
        "Scheduler jobs registered",
        intro="Every cron job that's scheduled for today, parsed from today's bot log. "
              "F&O correctly omits watchlist/research/fee_audit (those are equity-side global jobs).",
    )
    log = ROOT / "logs" / f"bot_{date.today().isoformat()}.log"
    if not log.exists():
        sec.findings.append(Finding(
            "Bot log", Status.WARN,
            f"no {log.name} — bot has not started today",
        ))
        return sec
    text = log.read_text()
    by_seg: dict[str, list[str]] = {}
    for m in _SCHED_LINE.finditer(text):
        seg, msg = m.group(1), m.group(2)
        by_seg.setdefault(seg, []).append(msg)
    if not by_seg:
        sec.findings.append(Finding(
            "Bot log", Status.WARN,
            f"{log.name} present but no scheduler entries — bot may have crashed before starting",
        ))
        return sec
    for seg in ("equity", "fno"):
        msgs = by_seg.get(seg, [])
        if not msgs:
            sec.findings.append(Finding(
                f"{seg} scheduler", Status.WARN,
                f"no [{seg}] scheduler entries in today's log — bot for this segment may not be running",
            ))
            continue
        started = any(m.startswith("Bot started") for m in msgs)
        scheduled = [m for m in msgs if m.startswith("Scheduled")]
        sec.findings.append(Finding(
            f"{seg} scheduler", Status.OK if started else Status.WARN,
            f"{len(scheduled)} job(s) registered" + (" — bot fully started" if started else " — Bot started line MISSING"),
            {"jobs": scheduled, "started": started},
        ))
    return sec


# ─── Section 9 — Strategy readiness (live smoke test) ───────────────────────

def section_strategy_readiness() -> Section:
    sec = Section(
        "Strategy readiness",
        intro="Live smoke test: pull the freshest bars and run each segment's "
              "ensemble. Reports current EMA/cross state per F&O underlying.",
    )
    try:
        from bot.config import load_config
        from bot.data import intraday_bars
        from bot.strategies import build_default_ensemble
        from bot.segment import Segment
        cfg = load_config()
    except Exception as e:                                       # noqa: BLE001
        sec.findings.append(Finding("imports", Status.FAIL, f"{e}"))
        return sec

    eq_ens = build_default_ensemble(segment=Segment.EQUITY)
    sec.findings.append(Finding(
        "Equity ensemble", Status.OK,
        f"{len(eq_ens.members)} members: {[m.name for m in eq_ens.members]}  "
        f"min_agree={cfg.strategies.ensemble.min_agree}",
        {"members": [m.name for m in eq_ens.members], "min_agree": cfg.strategies.ensemble.min_agree},
    ))

    if cfg.fno is not None and cfg.fno.enabled:
        fno_ens = build_default_ensemble(segment=Segment.FNO)
        sec.findings.append(Finding(
            "F&O ensemble", Status.OK,
            f"{len(fno_ens.members)} members: {[m.name for m in fno_ens.members]}  "
            f"min_agree={cfg.fno.strategies.ensemble.min_agree}",
            {"members": [m.name for m in fno_ens.members]},
        ))
        # Cross-leak guard
        eq_names = {m.name for m in eq_ens.members}
        fno_names = {m.name for m in fno_ens.members}
        if eq_names & fno_names:
            sec.findings.append(Finding(
                "Strategy cross-leak", Status.FAIL,
                f"strategies appear in BOTH ensembles: {sorted(eq_names & fno_names)}",
            ))
        else:
            sec.findings.append(Finding(
                "Strategy cross-leak", Status.OK,
                "equity and F&O ensembles share no strategies (correct)",
            ))

    if cfg.fno is not None and cfg.fno.enabled and "credit_spread" in cfg.fno.strategies.enabled:
        try:
            from bot.strategies.fno.credit_spread import CreditSpreadStrategy
            cs = CreditSpreadStrategy(cfg.fno.strategies.credit_spread)
            need = cfg.fno.strategies.credit_spread.ema_slow + cfg.fno.strategies.credit_spread.cross_lookback_bars + 1
            for u in cfg.fno.watchlist.get("symbols", []):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    df = intraday_bars(u, "5m")
                if df.empty:
                    sec.findings.append(Finding(
                        f"{u} 5m bars", Status.WARN,
                        "no 5m bars from yfinance (expected before 09:15 IST)",
                    ))
                    continue
                last_ts = df.index[-1]
                if len(df) < need:
                    sec.findings.append(Finding(
                        f"{u} cs warm-up", Status.WARN,
                        f"only {len(df)} bars (need {need} for EMA50 + cross lookback). "
                        f"Strategy will HOLD until enough bars accumulate.",
                    ))
                    continue
                sig = cs.generate(u, df)
                sec.findings.append(Finding(
                    f"{u} credit_spread signal", Status.OK,
                    f"latest_bar={last_ts.strftime('%H:%M IST')}  type={sig.type.value}  "
                    f"reason={sig.reason[:100]}",
                    {"signal_type": sig.type.value, "reason": sig.reason},
                ))
        except Exception as e:                                   # noqa: BLE001
            sec.findings.append(Finding(
                "credit_spread smoke test", Status.FAIL,
                f"{e}\n{traceback.format_exc()[:500]}",
            ))
    return sec


# ─── Section 10 — Healthcheck dry-run ───────────────────────────────────────

def section_healthcheck_dryrun() -> Section:
    sec = Section(
        "Healthcheck dry-run",
        intro="Same battery the scheduled 09:00/11:00/13:00/15:00 IST cron runs. "
              "WARNs are normal pre-market (heartbeat / research not yet fired).",
    )
    try:
        from bot.healthcheck import run_healthcheck
        from bot.segment import Segment
    except Exception as e:                                       # noqa: BLE001
        sec.findings.append(Finding("healthcheck import", Status.FAIL, f"{e}"))
        return sec
    for seg in (Segment.EQUITY, Segment.FNO):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                rep = run_healthcheck(segment=seg)
        except Exception as e:                                   # noqa: BLE001
            sec.findings.append(Finding(
                f"healthcheck:{seg.value}", Status.FAIL, f"crashed: {e}",
            ))
            continue
        n_ok = sum(1 for c in rep.checks if c.status == "OK")
        n_warn = sum(1 for c in rep.checks if c.status == "WARN")
        n_fail = sum(1 for c in rep.checks if c.status == "FAIL")
        status = (Status.OK if rep.overall == "OK" else
                  Status.FAIL if rep.overall == "FAILED" else Status.WARN)
        non_ok = [(c.name, c.status, c.detail) for c in rep.checks if c.status != "OK"]
        sec.findings.append(Finding(
            f"healthcheck:{seg.value}", status,
            f"overall={rep.overall}  {n_ok} OK / {n_warn} WARN / {n_fail} FAIL of {len(rep.checks)} checks",
            {"overall": rep.overall, "checks": [{"name": c.name, "status": c.status, "detail": c.detail}
                                                for c in rep.checks]},
        ))
        for n, s, d in non_ok:
            sec.findings.append(Finding(
                f"  └ {seg.value} · {n}",
                Status.WARN if s == "WARN" else Status.FAIL,
                d,
            ))
    return sec


# ─── Section 11 — Dashboard reachability ────────────────────────────────────

def section_dashboard_reachable() -> Section:
    sec = Section(
        "Dashboard reachability",
        intro="HTTP probe of the Streamlit dashboard. The browser-facing URL "
              "should return HTTP 200 within ~2s.",
    )
    url = "http://localhost:8501/"
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            code = resp.getcode()
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            sec.findings.append(Finding(
                "HTTP probe", Status.OK if code == 200 else Status.WARN,
                f"GET {url} → HTTP {code} in {elapsed_ms}ms",
                {"url": url, "code": code, "elapsed_ms": elapsed_ms},
            ))
    except urllib.error.URLError as e:
        sec.findings.append(Finding(
            "HTTP probe", Status.WARN,
            f"GET {url} failed: {e}. Launch with `python -m cli dashboard`.",
        ))
    except Exception as e:                                       # noqa: BLE001
        sec.findings.append(Finding(
            "HTTP probe", Status.WARN, f"unexpected: {e}",
        ))
    return sec


# ─── Section 12 — Cleanup advisories ────────────────────────────────────────

def section_cleanup_advisories() -> Section:
    sec = Section(
        "Cleanup advisories (non-blocking)",
        intro="Hygiene items that don't impact today's trading but are worth fixing soon.",
    )
    # Stale orders
    try:
        import redis
        from bot.config import env
        r = redis.Redis.from_url(env().REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        n_orders = r.hlen("orders") if r.type("orders") == "hash" else 0
        if n_orders > 0:
            sec.findings.append(Finding(
                "Stale Redis orders hash", Status.WARN,
                f"{n_orders} historical orders in `orders` Redis hash. Audit-only — never read "
                "by the executor. Wipe with `redis-cli del orders`.",
            ))
    except Exception:
        pass

    # Streamlit deprecation
    app = ROOT / "app.py"
    if app.exists():
        n = app.read_text().count("use_container_width")
        if n > 0:
            sec.findings.append(Finding(
                "Streamlit deprecation", Status.WARN,
                f"{n} `use_container_width` calls in app.py — Streamlit will remove this after "
                "2025-12-31 (already past). Migrate to `width='stretch'` / `width='content'`.",
                {"occurrences": n, "file": "app.py"},
            ))

    # Healthcheck `Bot process` segment-awareness (known minor)
    try:
        import inspect
        from bot.healthcheck import _bot_process  # noqa: WPS433
        sig = inspect.signature(_bot_process)
        if "segment" not in sig.parameters:
            sec.findings.append(Finding(
                "Healthcheck bot-process segment-awareness", Status.WARN,
                "`Bot process` healthcheck is global (not per-segment) — F&O panel may show "
                "the equity bot's PID. Cosmetic only.",
            ))
    except Exception:
        pass

    if not sec.findings:
        sec.findings.append(Finding("Hygiene", Status.OK, "no advisories"))
    return sec


# ════════════════════════════════════════════════════════════════════════════
#  HARNESS
# ════════════════════════════════════════════════════════════════════════════


SECTIONS: list[tuple[str, SectionFn]] = [
    ("1.  Live processes",              section_live_processes),
    ("2.  Pre-market preflight",        section_preflight),
    ("3.  Capital & risk caps",         section_risk_caps),
    ("4.  Critical fix verifications",  section_critical_fixes),
    ("5.  Corrupted-artefacts quarantine", section_quarantine),
    ("6.  Redis hygiene",               section_redis_state),
    ("7.  Trading-day status",          section_trading_days),
    ("8.  Scheduler jobs registered",   section_scheduler_jobs),
    ("9.  Strategy readiness",          section_strategy_readiness),
    ("10. Healthcheck dry-run",         section_healthcheck_dryrun),
    ("11. Dashboard reachability",      section_dashboard_reachable),
    ("12. Cleanup advisories",          section_cleanup_advisories),
]


def _safe_run(label: str, fn: SectionFn) -> Section:
    try:
        return fn()
    except Exception as e:                                       # noqa: BLE001
        sec = Section(label)
        sec.findings.append(Finding("section crash", Status.FAIL,
                                    f"{e}\n{traceback.format_exc()[:600]}"))
        return sec


def _print_section(tee: _Tee, label: str, sec: Section) -> None:
    overall = sec.overall()
    color = _COLOR.get(overall, "")
    tee.print()
    tee.print(_BOLD + "─" * 78 + _RESET)
    tee.print(f"{_BOLD}{label}{_RESET}   {color}[{overall}]{_RESET}")
    if sec.intro:
        for line in textwrap.wrap(sec.intro, 76):
            tee.print(f"  {_DIM}{line}{_RESET}")
    tee.print(_BOLD + "─" * 78 + _RESET)
    if not sec.findings:
        tee.print(f"  {_DIM}(no findings){_RESET}")
        return
    name_w = max(len(f.name) for f in sec.findings) + 2
    for f in sec.findings:
        emoji = _EMOJI.get(f.status, "?")
        col   = _COLOR.get(f.status, "")
        # Wrap the detail to remaining width, with hanging indent.
        prefix = f"  {emoji}  {col}{f.status:<4}{_RESET}  {f.name:<{name_w}}"
        body_w = max(78 - 4 - 4 - 2 - name_w - 2, 30)
        wrapped = textwrap.wrap(f.detail, body_w) or [""]
        tee.print(f"{prefix}  {wrapped[0]}")
        indent = " " * (len(prefix) - len(_RESET) - len(col) - 2)
        for cont in wrapped[1:]:
            tee.print(f"{indent}    {cont}")


def _summarize(tee: _Tee, sections: list[tuple[str, Section]], elapsed_s: float) -> tuple[int, str]:
    """Print final summary and return (exit_code, verdict_string)."""
    n_ok = n_warn = n_fail = 0
    fails: list[tuple[str, str, str]] = []
    warns: list[tuple[str, str, str]] = []
    for label, sec in sections:
        for f in sec.findings:
            if f.status == Status.OK: n_ok += 1
            elif f.status == Status.WARN:
                n_warn += 1
                warns.append((label, f.name, f.detail))
            elif f.status == Status.FAIL:
                n_fail += 1
                fails.append((label, f.name, f.detail))
    if n_fail > 0:
        verdict = "🛑 NO-GO"
        col = _COLOR[Status.FAIL]; code = 1
    elif n_warn > 0:
        verdict = "⚠️  CONCERNS — review before trusting"
        col = _COLOR[Status.WARN]; code = 2
    else:
        verdict = "✅ GO — system clean"
        col = _COLOR[Status.OK]; code = 0

    tee.print()
    tee.print(_BOLD + "═" * 78 + _RESET, force=True)
    tee.print(f"{_BOLD} 📊 SUMMARY{_RESET}", force=True)
    tee.print(_BOLD + "═" * 78 + _RESET, force=True)
    tee.print(f"  {n_ok} OK · {n_warn} WARN · {n_fail} FAIL  ({elapsed_s:.1f}s elapsed)", force=True)
    tee.print(f"  {col}{_BOLD}{verdict}{_RESET}", force=True)
    if fails:
        tee.print(f"\n  {_COLOR[Status.FAIL]}Critical failures:{_RESET}", force=True)
        for label, name, detail in fails:
            tee.print(f"    ❌  {label} → {name}: {detail[:120]}", force=True)
    if warns:
        tee.print(f"\n  {_COLOR[Status.WARN]}Warnings:{_RESET}", force=True)
        for label, name, detail in warns[:10]:
            tee.print(f"    ⚠   {label} → {name}: {detail[:120]}", force=True)
        if len(warns) > 10:
            tee.print(f"    {_DIM}…and {len(warns)-10} more (see full report){_RESET}", force=True)
    return code, verdict


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only system audit of the running stock-bot.")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON report to stdout (skips the human report)")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress per-section output; print only the final summary")
    parser.add_argument("--no-log", action="store_true",
                        help="Do not write a logfile under logs/audit/")
    args = parser.parse_args()

    ts = datetime.now()
    log_path: Optional[Path] = None
    json_path: Optional[Path] = None
    if not args.no_log:
        log_dir = ROOT / "logs" / "audit"
        log_path = log_dir / f"{ts.strftime('%Y-%m-%d_%H%M%S')}.log"
        json_path = log_dir / f"{ts.strftime('%Y-%m-%d_%H%M%S')}.json"

    tee = _Tee(log_path, quiet=args.quiet or args.json)

    # Header
    tee.print()
    tee.print(_BOLD + "═" * 78 + _RESET)
    tee.print(f"{_BOLD} 🔍 SYSTEM AUDIT — running bot health{_RESET}")
    tee.print(_BOLD + "═" * 78 + _RESET)
    tee.print(f"  date / time     : {ts.strftime('%a %d %b %Y, %H:%M:%S')}")
    tee.print(f"  host            : {socket.gethostname()}")
    tee.print(f"  cwd             : {ROOT}")
    if log_path:
        tee.print(f"  log file        : {log_path}")
    tee.print(f"  python          : {sys.executable}  ({sys.version.split()[0]})")
    tee.print(f"  sections to run : {len(SECTIONS)}")

    t0 = time.perf_counter()
    results: list[tuple[str, Section]] = []
    for label, fn in SECTIONS:
        sec = _safe_run(label, fn)
        results.append((label, sec))
        _print_section(tee, label, sec)
    elapsed_s = time.perf_counter() - t0

    code, verdict = _summarize(tee, results, elapsed_s)

    # JSON sidecar
    if json_path is not None or args.json:
        payload = {
            "timestamp": ts.isoformat(),
            "host": socket.gethostname(),
            "elapsed_s": round(elapsed_s, 2),
            "exit_code": code,
            "verdict": verdict,
            "sections": [
                {
                    "title": label,
                    "intro": sec.intro,
                    "overall": sec.overall(),
                    "findings": [asdict(f) for f in sec.findings],
                }
                for label, sec in results
            ],
        }
        if json_path is not None:
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(payload, indent=2, default=str))
            tee.print(f"\n  json: {json_path}", force=True)
        if args.json:
            print(json.dumps(payload, indent=2, default=str))

    if log_path is not None:
        tee.print(f"  log:  {log_path}", force=True)
    tee.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
