"""One-shot cleanup of the 2026-04-30 F&O paper-bot ``-₹8.3M`` incident.

Why this exists:
    On 2026-04-30, two NIFTY/BANKNIFTY put credit spreads were opened
    correctly (entry ₹44.25 / ₹43.98 per spread) but the EOD square-off
    closed them at the UNDERLYING SPOT (₹24,002 / ₹54,842) due to
    ``yfinance_proxy()`` matching synthetic ``...-...PESPRD``
    tradingsymbols via ``s.startswith("NIFTY")``. Result: a fake
    ``-₹8.3M`` loss on ₹100k capital.

    The proxy + executor have been fixed in the same commit. This
    script rolls back the corrupted artefacts:

      1. Renames the bad trade journal (``.jsonl`` / ``.csv``) so
         ``daily_summary`` returns 0 trades for today instead of two
         catastrophic losses.
      2. Renames the bad EOD report.
      3. Deletes the corrupted Redis keys (``paper:state:fno``,
         ``portfolio:fno``) so the dashboard stops showing
         ``cash=-₹8.3M``.

    The bot's daily counters (``trades_today``, broker cash) live
    in-memory inside the F&O process — restarting the F&O bot is the
    final step to clear those. We refuse to do anything if the bot is
    still running, so YOU must Ctrl-C it first.

Idempotent: safe to re-run. Does nothing if the artefacts have already
been cleaned.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Stamp added to renamed corrupted artefacts so they're greppable forever.
SUFFIX = ".corrupted-by-2026-04-30-spot-leak-bug"


def _live_fno_bot() -> tuple[int, str] | None:
    """Return ``(pid, cmd)`` if the F&O bot is still running, else ``None``.

    We deliberately scan ``ps`` instead of trusting the lockfile — the
    operator may have killed the bot uncleanly leaving the lockfile
    stale.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,command"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("PID"):
            continue
        if "cli" in line and " run" in line and "--segment fno" in line:
            try:
                pid = int(line.split(None, 1)[0])
                return (pid, line)
            except ValueError:
                continue
    return None


def _rename_aside(p: Path) -> bool:
    """Rename ``p`` to ``p.<SUFFIX>``. Returns True if anything moved."""
    if not p.exists():
        print(f"  (skip) {p.name} — does not exist")
        return False
    target = p.with_suffix(p.suffix + SUFFIX)
    if target.exists():
        # Pre-existing rename means we already cleaned up — leave well alone.
        print(f"  (skip) {p.name} — already moved aside as {target.name}")
        return False
    shutil.move(str(p), str(target))
    print(f"  ✓ moved {p.name} → {target.name}")
    return True


def main() -> int:
    print("=" * 70)
    print("F&O 2026-04-30 spot-leak corruption cleanup")
    print("=" * 70)

    # ── Guardrail: refuse to clean while the F&O bot is running ────────
    live = _live_fno_bot()
    if live:
        pid, cmd = live
        print()
        print(f"REFUSING to clean — F&O bot is still running (pid={pid}).")
        print(f"  cmd: {cmd[:120]}")
        print()
        print("Please Ctrl-C the F&O bot first (the terminal where you ran")
        print("`python -m cli run --paper --segment fno`), then re-run:")
        print()
        print("    python scripts/cleanup_fno_2026_04_30.py")
        print()
        print("Aborting without touching anything.")
        return 2

    today = date.today().isoformat()    # this script is meant to be run on the day-of, but pinning the date string here keeps the print output deterministic for the bug-report.
    today_str = "2026-04-30"

    # ── 1. Move the bad trade journal aside ─────────────────────────────
    print()
    print(f"[1/3] Trade journal — logs/trades/fno/{today_str}.{{jsonl,csv}}")
    moved_any = False
    moved_any |= _rename_aside(ROOT / "logs" / "trades" / "fno" / f"{today_str}.jsonl")
    moved_any |= _rename_aside(ROOT / "logs" / "trades" / "fno" / f"{today_str}.csv")

    # ── 2. Move the bad EOD report aside ────────────────────────────────
    print()
    print(f"[2/3] EOD report — logs/eod/fno/{today_str}.txt")
    moved_any |= _rename_aside(ROOT / "logs" / "eod" / "fno" / f"{today_str}.txt")

    # ── 3. Clear the corrupted Redis state ──────────────────────────────
    print()
    print("[3/3] Redis keys")
    from bot.cache import get_cache
    cache = get_cache()
    REDIS_KEYS_TO_PURGE = (
        "paper:state:fno",        # broker cash + starting_capital snapshot
        "portfolio:fno",          # most recent portfolio snapshot
    )
    purged = 0
    for k in REDIS_KEYS_TO_PURGE:
        v = cache.get_json(k)
        if v is None:
            print(f"  (skip) {k} — already empty")
            continue
        cache.delete(k)
        print(f"  ✓ deleted {k}")
        purged += 1

    print()
    print("=" * 70)
    if moved_any or purged:
        print("Cleanup OK. The dashboard will show clean F&O state on next refresh.")
        print()
        print("Next steps:")
        print("  1. Refresh the dashboard tab in your browser — F&O view should")
        print("     now show: 0 trades today, cash=₹100,000, no open positions.")
        print("  2. Tomorrow morning before 09:00 IST, restart the F&O bot:")
        print("       bash scripts/run_bot.sh run --paper --segment fno")
        print("     (the new code rejects synthetic-symbol yfinance proxies, so")
        print("     square-offs will mark spreads at their net premium correctly)")
        print()
        print("The corrupted artefacts are preserved with the suffix")
        print(f"  '{SUFFIX}' for the post-mortem.")
    else:
        print("Nothing to clean — already in a clean state. No-op.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
