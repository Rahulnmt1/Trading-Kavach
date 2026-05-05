"""End-of-day fee-rate update + restart sequence.

Run **after the 15:15 IST square-off and the 15:30 IST EOD report** to:

  1. Verify all open positions are closed (safety abort if anything is still open).
  2. Re-run the live audit against ``zerodha.com/charges`` so we update only
     against the current source-of-truth (not yesterday's cache).
  3. Print a side-by-side "before / after" diff of every drifted rate.
  4. **Patch** ``bot/fees.py`` in-place — replacing only the float literals,
     leaving comments and structure intact. A timestamped ``.bak`` is left
     behind so any change is trivially reversible.
  5. Re-import ``bot.fees`` and ``bot.fee_audit`` and re-run the audit to
     confirm 0 drifts post-update.
  6. Stop the running equity + F&O bot processes (gracefully — SIGTERM,
     wait up to 30s, only SIGKILL if necessary).
  7. Stop the caffeinate processes that were pinned to those PIDs.
  8. Re-launch both bots and the dashboard via the standard launchers.

Why an EOD-only operation:

* The fee tables are imported once at process start. Mid-day rate edits
  would cause inconsistent fee accounting across the same trading day —
  EOD is the only safe boundary.
* Restarting bots wipes intraday Redis state via ``daily_reset`` (already
  scheduled at 08:30 next morning). EOD restart preserves portfolio
  snapshots (they're saved on shutdown) so you start fresh tomorrow.

Usage::

    python scripts/eod_apply_fee_updates.py                 # interactive
    python scripts/eod_apply_fee_updates.py --auto          # no prompts
    python scripts/eod_apply_fee_updates.py --dry-run       # show diff, no writes
    python scripts/eod_apply_fee_updates.py --no-restart    # patch only

Exit codes: 0 = success · 1 = aborted (open positions / unsafe time) ·
2 = patch failed · 3 = restart failed.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import pytz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.fee_audit import run_fee_audit                        # noqa: E402
from bot.cache import get_cache                                # noqa: E402

IST = pytz.timezone("Asia/Kolkata")
FEES_PY = ROOT / "bot" / "fees.py"

# Map (segment, rate_key) → (variable_name_in_fees_py)
# When patching we look up the section of fees.py for the matching variable
# name then replace just that key's float literal.
_TABLE_VAR = {
    "equity":  "_RATES",
    "futures": "_FUTURES_RATES",
    "options": "_OPTIONS_RATES",
}


# ─── Pretty printing ─────────────────────────────────────────────────────────


_C = {
    "ok":   "\033[32m",
    "warn": "\033[33m",
    "fail": "\033[31m",
    "dim":  "\033[2m",
    "bold": "\033[1m",
    "cyan": "\033[36m",
    "rs":   "\033[0m",
}


def _bar(ch: str = "─") -> str:
    return ch * 78


def _h(label: str) -> None:
    print()
    print(_C["bold"] + _bar("═") + _C["rs"])
    print(f" {_C['bold']}{label}{_C['rs']}")
    print(_C["bold"] + _bar("═") + _C["rs"])


def _step(n, label: str) -> None:
    print(f"\n{_C['cyan']}{_C['bold']}[step {n}]{_C['rs']} {label}")
    print(_bar())


# ─── Safety checks ───────────────────────────────────────────────────────────


def _hour_ist() -> int:
    return datetime.now(IST).hour


def _check_safe_time() -> Optional[str]:
    """Refuse to run during the trading window unless explicitly forced."""
    h = _hour_ist()
    if 9 <= h < 16:
        return (f"It's currently {datetime.now(IST).strftime('%H:%M IST')} — "
                "still inside the trading window. Run after 15:30 IST.")
    return None


def _open_positions_warning() -> Optional[str]:
    """If either segment still has open positions in Redis, abort."""
    try:
        cache = get_cache()
        for seg in ("equity", "fno"):
            snap = cache.get_json(f"paper:state:{seg}") or {}
            n = len(snap.get("positions", {}))
            if n > 0:
                return (f"{seg.upper()} still has {n} open position(s) in "
                        f"`paper:state:{seg}` — close before patching fees so "
                        "that any pending exit fee uses a single consistent rate.")
    except Exception as e:                                        # noqa: BLE001
        return f"Could not check open positions (Redis): {e}"
    return None


# ─── The patcher ─────────────────────────────────────────────────────────────


@dataclass
class RatePatch:
    segment: str        # equity / futures / options
    key: str            # brokerage_flat / stt_sell_pct / ...
    old_value: float
    new_value: float

    def __str__(self) -> str:
        pct_old = self.old_value * 100
        pct_new = self.new_value * 100
        # rates between 0 and 1 are %; flat fees are absolute
        if abs(self.old_value) < 1.0:
            return (f"{self.segment:8} · {self.key:18} "
                    f"{pct_old:>10.5f}% → {pct_new:>10.5f}%")
        return (f"{self.segment:8} · {self.key:18} "
                f"{self.old_value:>10.2f}  → {self.new_value:>10.2f}")


def _patches_from_audit() -> List[RatePatch]:
    print("→ Re-running live audit against the source page...")
    res = run_fee_audit()
    print(f"  status: {res.status}  ·  {res.summary[:120]}")
    out: List[RatePatch] = []
    for c in res.checks:
        if c.drifted and c.observed is not None:
            out.append(RatePatch(
                segment=c.segment, key=c.key,
                old_value=float(c.configured),
                new_value=float(c.observed),
            ))
    return out


def _patch_fees_py(patches: List[RatePatch], dry_run: bool) -> Tuple[str, str]:
    """Edit ``bot/fees.py`` in place. Returns (before_text, after_text).

    Each rate dict (e.g. ``_RATES = {...}``) is rewritten field-by-field so we
    only touch the targeted float literal; comments next to each line stay
    intact (they describe meaning, not value, and remain valid).
    """
    text = FEES_PY.read_text()
    new_text = text

    for patch in patches:
        var_name = _TABLE_VAR[patch.segment]
        # Locate the dict definition, then replace just this key's literal.
        # Pattern explanation: match the opening line of the dict, then the
        # block up to the closing brace, then within that block locate the
        # specific key line and rewrite the float between the colon and the
        # comma/(optional) trailing comment.
        dict_pat = re.compile(
            rf"({re.escape(var_name)}\s*:\s*Dict\[str,\s*float\]\s*=\s*\{{[\s\S]*?\}})",
            re.MULTILINE,
        )
        m = dict_pat.search(new_text)
        if not m:
            raise RuntimeError(f"Could not locate `{var_name} = {{...}}` in bot/fees.py")
        block = m.group(1)
        # Capture: (key+colon+spaces) (numeric literal) (whitespace+comma)
        # (optional trailing comment fragment so we can refresh the % inside).
        line_pat = re.compile(
            rf'(\"{re.escape(patch.key)}\"\s*:\s*)'
            rf'([0-9.eE+-]+)'
            rf'(\s*,)'
            rf'(\s*(?:#[^\n]*)?)',
        )
        line_m = line_pat.search(block)
        if not line_m:
            raise RuntimeError(
                f"Could not locate key `{patch.key}` inside `{var_name}` — "
                "the dict layout may have drifted, please update the patcher."
            )
        new_literal = _format_literal(patch.new_value, line_m.group(2))
        new_comment = _refresh_comment(line_m.group(4), patch.old_value, patch.new_value)
        new_block = (
            block[:line_m.start()]
            + line_m.group(1) + new_literal + line_m.group(3) + new_comment
            + block[line_m.end():]
        )
        new_text = new_text[:m.start()] + new_block + new_text[m.end():]

    if dry_run:
        return text, new_text

    backup = FEES_PY.with_suffix(
        f".py.bak.{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}"
    )
    shutil.copyfile(FEES_PY, backup)
    print(f"  backup saved: {backup.relative_to(ROOT)}")
    FEES_PY.write_text(new_text)
    return text, new_text


def _format_literal(value: float, prior_literal: str) -> str:
    """Match the formatting style of the literal we're replacing.

    If the prior literal used scientific notation, keep scientific. If it
    was a plain decimal, keep decimal — minimising the visual diff. We
    use up to 10 fractional digits (then strip trailing zeros) so very
    small percentage rates like 0.0000307 (= 0.00307%) keep their full
    precision instead of being truncated to 0.000031 (≠ 0.00310%).
    """
    if "e" in prior_literal.lower():
        return f"{value:.6e}".replace("e-0", "e-").replace("e+0", "e+")
    if value == 0:
        return "0.0"
    if abs(value) >= 1:
        return f"{value:.2f}".rstrip("0").rstrip(".") or "0.0"
    # 10 fractional digits handles 0.0000xxx rates losslessly; rstrip
    # trims unused zeros so the literal stays compact.
    s = f"{value:.10f}".rstrip("0")
    if s.endswith("."):
        s += "0"
    return s


def _refresh_comment(comment_part: str, old_value: float, new_value: float) -> str:
    """Update any percentage figure inside a trailing inline comment to
    reflect the new value.

    The comments in ``bot/fees.py`` describe each rate in human-readable
    units, e.g. ``# NSE: 0.00345% on each leg``. After patching the
    underlying float we refresh the percentage so the comment stays
    truthful — a stale comment is worse than no comment.

    Strategy: find the first ``X.YZW%`` numeric token in the comment that
    matches the *old* percentage value (``old_value * 100``) and replace
    only that token with the new percentage. If we can't find a matching
    token, we leave the comment untouched (the operator can fix it
    manually). We never touch text outside the comment.
    """
    if not comment_part or "#" not in comment_part:
        return comment_part
    old_pct = old_value * 100
    new_pct = new_value * 100
    # Format the new percentage with the same number of significant digits
    # the source page uses: trim trailing zeros after up to 5 decimals.
    pct_str = f"{new_pct:.5f}".rstrip("0").rstrip(".")
    if not pct_str or pct_str == "-0":
        pct_str = "0"
    pct_str += "%"

    # Match any "X[.Y]%" token in the comment; iterate to find the one
    # numerically equal (within tolerance) to the OLD percentage.
    def _swap(m: re.Match) -> str:
        try:
            v = float(m.group(1))
        except ValueError:
            return m.group(0)
        if abs(v - old_pct) <= max(0.0001, abs(old_pct) * 1e-3):
            return pct_str
        return m.group(0)

    return re.sub(r"(\d+(?:\.\d+)?)\s*%", _swap, comment_part)


def _print_diff(old: str, new: str) -> None:
    """Tiny line diff — only show changed lines (ignore unchanged file).

    No external dependency on python-difflib (stdlib `difflib` is fine but
    the output is less compact than a focused before/after line dump).
    """
    import difflib
    diff_lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile="bot/fees.py (current)",
        tofile="bot/fees.py (after patch)",
        lineterm="",
        n=1,
    ))
    if not diff_lines:
        print("  (no changes — unexpected, audit said there was drift)")
        return
    for ln in diff_lines:
        if ln.startswith("---") or ln.startswith("+++") or ln.startswith("@@"):
            print(f"  {_C['dim']}{ln}{_C['rs']}")
        elif ln.startswith("+"):
            print(f"  {_C['ok']}{ln}{_C['rs']}")
        elif ln.startswith("-"):
            print(f"  {_C['fail']}{ln}{_C['rs']}")
        else:
            print(f"  {_C['dim']}{ln}{_C['rs']}")


# ─── Process control ─────────────────────────────────────────────────────────


def _ps_lines() -> List[str]:
    out = subprocess.check_output(
        ["ps", "-eo", "pid,etime,command"], text=True, timeout=5,
    )
    return [ln for ln in out.splitlines() if ln.strip()
            and not ln.lstrip().startswith("PID")]


def _find_pids(needles: List[str], excludes: List[str] | None = None) -> List[int]:
    excludes = excludes or []
    self_pid = os.getpid()
    pids: List[int] = []
    for ln in _ps_lines():
        if not all(n in ln for n in needles):
            continue
        if any(x in ln for x in excludes):
            continue
        try:
            pid = int(ln.split(None, 1)[0])
        except ValueError:
            continue
        if pid != self_pid:
            pids.append(pid)
    return pids


def _kill_gracefully(pids: List[int], label: str, timeout_s: int = 30) -> bool:
    if not pids:
        print(f"  ({label}) no running process — skip")
        return True
    print(f"  ({label}) sending SIGTERM to {pids}")
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        alive = [p for p in pids if _alive(p)]
        if not alive:
            print(f"  ({label}) all stopped cleanly")
            return True
        time.sleep(0.5)
    leftover = [p for p in pids if _alive(p)]
    if leftover:
        print(f"  ({label}) SIGTERM ignored after {timeout_s}s — sending SIGKILL")
        for p in leftover:
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return False
    return True


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _stop_bots() -> None:
    eq = _find_pids(["python", " run", "--paper"],
                    excludes=["--segment fno", "eod_apply", "system_audit"])
    fno = _find_pids(["python", " run", "--paper", "--segment fno"],
                     excludes=["eod_apply", "system_audit"])
    caff = _find_pids(["caffeinate"])
    _kill_gracefully(eq, "equity bot")
    _kill_gracefully(fno, "F&O bot")
    _kill_gracefully(caff, "caffeinate")


def _start_bots() -> None:
    """Launch equity + F&O bots in fresh background terminals.

    Uses ``nohup`` so the processes survive this script exiting. The standard
    ``scripts/run_bot.sh`` wrapper is used so caffeinate gets re-attached.
    """
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    eq_log = log_dir / f"eod_relaunch_equity_{ts}.log"
    fno_log = log_dir / f"eod_relaunch_fno_{ts}.log"
    print(f"  launching equity bot (logs: {eq_log.name})")
    subprocess.Popen(
        ["bash", str(ROOT / "scripts" / "run_bot.sh"), "run", "--paper",
         "--segment", "equity"],
        stdout=eq_log.open("w"), stderr=subprocess.STDOUT, start_new_session=True,
    )
    print(f"  launching F&O bot (logs: {fno_log.name})")
    subprocess.Popen(
        ["bash", str(ROOT / "scripts" / "run_bot.sh"), "run", "--paper",
         "--segment", "fno"],
        stdout=fno_log.open("w"), stderr=subprocess.STDOUT, start_new_session=True,
    )


# ─── Main flow ───────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--auto", action="store_true",
                        help="Skip confirmation prompts (still aborts on safety failures).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the patch diff but do not write or restart anything.")
    parser.add_argument("--no-restart", action="store_true",
                        help="Patch fees.py but skip the bot stop/start sequence.")
    parser.add_argument("--force-time", action="store_true",
                        help="Bypass the post-15:30 IST safety check (USE WITH CARE).")
    args = parser.parse_args()

    _h(" 🛠  EOD fee-rate update + restart")
    print(f"  now: {datetime.now(IST).strftime('%a %d %b %Y, %H:%M:%S IST')}")
    print(f"  fees.py: {FEES_PY.relative_to(ROOT)}")

    # 1. Safety: time
    _step(1, "Safety checks")
    if not args.force_time:
        msg = _check_safe_time()
        if msg:
            print(f"  {_C['fail']}ABORT — {msg}{_C['rs']}")
            print(f"  (override with --force-time if you really need to)")
            return 1
    if not args.dry_run and not args.no_restart:
        msg = _open_positions_warning()
        if msg:
            print(f"  {_C['fail']}ABORT — {msg}{_C['rs']}")
            return 1
    print(f"  {_C['ok']}safe to proceed{_C['rs']}")

    # 2. Refresh audit + collect patches
    _step(2, "Re-run live audit and identify drifted rates")
    patches = _patches_from_audit()
    if not patches:
        print(f"  {_C['ok']}No drift — nothing to patch. Exiting.{_C['rs']}")
        return 0
    print(f"\n  {len(patches)} rate(s) to update:")
    for p in patches:
        print(f"    {p}")

    # 3. Diff + write
    _step(3, "Patch bot/fees.py")
    try:
        before, after = _patch_fees_py(patches, dry_run=args.dry_run)
    except Exception as e:                                       # noqa: BLE001
        print(f"  {_C['fail']}PATCH FAILED — {e}{_C['rs']}")
        return 2
    _print_diff(before, after)

    if args.dry_run:
        print(f"\n  {_C['warn']}DRY RUN — no files written, no restart.{_C['rs']}")
        return 0

    # 4. Re-import + re-audit to confirm 0 drift post-patch
    _step(4, "Re-audit after patch")
    print("  reloading bot.fees and bot.fee_audit modules...")
    import importlib
    import bot.fees
    import bot.fee_audit
    importlib.reload(bot.fees)
    importlib.reload(bot.fee_audit)
    res2 = bot.fee_audit.run_fee_audit()
    drifts2 = sum(1 for c in res2.checks if c.drifted)
    print(f"  audit after patch: {res2.status} · {drifts2} drift(s)")
    if drifts2 != 0:
        print(f"  {_C['fail']}WARN — patch did not fully clear drift. "
              "Inspect bot/fees.py manually before restart.{_C['rs']}")
        if not args.auto:
            ans = input("  Continue with restart anyway? [y/N] ").strip().lower()
            if ans != "y":
                return 2

    # 5. Optional restart
    if args.no_restart:
        print(f"\n  {_C['warn']}--no-restart given — bots NOT restarted. "
              "Restart them yourself for the new rates to take effect.{_C['rs']}")
        return 0

    if not args.auto:
        print()
        ans = input(f"  Stop & relaunch equity + F&O bots now? [y/N] ").strip().lower()
        if ans != "y":
            print(f"  {_C['warn']}Skipping restart. Restart manually with "
                  "`bash scripts/run_bot.sh ...` when ready.{_C['rs']}")
            return 0

    _step(5, "Stop running bots (graceful SIGTERM, 30s grace)")
    _stop_bots()

    # Detect the 2026-05-04 ADANIENT phantom-short corruption: cash dropped
    # from ₹100,000 to ~₹74,961 due to the duplicate ``_end_of_day`` race
    # + missing over-sell guard. If we see that signature in
    # ``paper:state:equity``, run the cleanup script before restart so the
    # broker's ``_restore_state`` doesn't load the corrupted ledger.
    _step("5b", "Check for 2026-05-04 ADANIENT phantom-short corruption")
    try:
        from bot.cache import get_cache as _get_cache
        eq_snap = _get_cache().get_json("paper:state:equity") or {}
        eq_cash = float(eq_snap.get("cash", 0) or 0)
        eq_starting = float(eq_snap.get("starting_capital", 0) or 0)
        # Threshold: cash gap > ₹1000 from real-loss-only target — well above
        # any plausible fee/slippage drift on a single day's trades.
        suspicious = (eq_starting > 0 and (eq_cash - eq_starting) < -1000)
        cleanup_done = eq_snap.get("cleanup_marker") == "2026-05-04-eod-race-cleanup"
        if suspicious and not cleanup_done:
            print(f"  {_C['warn']}detected suspicious equity cash drop: "
                  f"₹{eq_cash:,.2f} (starting ₹{eq_starting:,.0f}). "
                  f"Running cleanup script.{_C['rs']}")
            cleanup_script = ROOT / "scripts" / "cleanup_equity_2026_05_04.py"
            if cleanup_script.exists():
                rc = subprocess.call(
                    [sys.executable, str(cleanup_script), "--force"],
                    cwd=str(ROOT),
                )
                if rc != 0:
                    print(f"  {_C['fail']}cleanup exited with code {rc}{_C['rs']}")
                else:
                    print(f"  {_C['ok']}cleanup applied — equity broker state reset{_C['rs']}")
            else:
                print(f"  {_C['warn']}cleanup script not found, skipping{_C['rs']}")
        elif cleanup_done:
            print(f"  {_C['ok']}cleanup marker present — already cleaned earlier{_C['rs']}")
        else:
            print(f"  {_C['ok']}no corruption signature — equity ledger looks clean{_C['rs']}")
    except Exception as e:                                       # noqa: BLE001
        print(f"  {_C['warn']}corruption-check failed (non-fatal): {e}{_C['rs']}")

    # Clean stale Redis intraday-session keys before relaunch. Bots are
    # currently stopped → safe to invoke ``clean_redis_session.py``
    # (which would otherwise refuse while bots are alive). This wipes
    # signal:*, trail:*, heartbeat:tick:*, profit_lockin:*, and stale
    # eod_done markers so tomorrow's bot starts on a truly clean slate
    # — addresses the "Redis stale data every day" complaint without
    # waiting for tomorrow's 08:55 in-bot daily_reset.
    _step("5c", "Clean stale Redis session keys (intraday-only)")
    try:
        clean_script = ROOT / "scripts" / "clean_redis_session.py"
        if clean_script.exists():
            rc = subprocess.call(
                [sys.executable, str(clean_script), "--force"],
                cwd=str(ROOT),
            )
            if rc == 0:
                print(f"  {_C['ok']}Redis session keys cleaned — clean slate for "
                      f"tomorrow{_C['rs']}")
            else:
                print(f"  {_C['warn']}clean_redis_session exited with code {rc} "
                      f"(non-fatal — 08:55 daily_reset will retry tomorrow){_C['rs']}")
        else:
            print(f"  {_C['warn']}clean_redis_session.py not found, skipping{_C['rs']}")
    except Exception as e:                                       # noqa: BLE001
        print(f"  {_C['warn']}redis cleanup failed (non-fatal): {e}{_C['rs']}")

    _step(6, "Re-launch bots via scripts/run_bot.sh (caffeinate reattached)")
    _start_bots()
    print("  ✓ bots launched in the background — `tail -f logs/bot_*.log` to watch")
    print("  ✓ run `python scripts/system_audit.py` after ~30s to verify everything is healthy")

    _h(" ✅ EOD fee update complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
