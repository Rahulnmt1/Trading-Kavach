"""Cleanup the 2026-05-04 ADANIENT phantom-short incident.

The bug: at 15:15:00 IST, two scheduled cron jobs fired ``_end_of_day``
concurrently (the dedicated ``end_of_day`` cron and the regular
``executor_tick`` cron whose ``should_square_off`` branch also routes
to the same method). The first call closed the long ADANIENT position
correctly. The second call ran ``square_off_all`` against a now-empty
position dict, but the broker's ``place_order`` had no over-sell guard
and treated the orphan SELL as a new SHORT entry — creating a phantom
−10 ADANIENT position that only debited fees. The auto-cover BUY at
15:16:00 then closed that phantom short via the equity-short-close
cash formula, leaking ~₹25k.

Real market loss: **₹151.57** on a single ADANIENT BUY/SELL round-trip.
Phantom leak:     **₹24,887.05** in broker accounting noise.

This script:
  1. Renames today's equity trade journal aside with a
     ``.corrupted-by-2026-05-04-eod-race`` suffix (so the bug shape is
     preserved for the post-mortem but the journal stops feeding bad
     data into the dashboard).
  2. Writes a clean replacement journal containing only the legitimate
     entries (entries 1, 2, 6 from the original — the BUY, the
     TRADE_OPEN, and the LONG TRADE_CLOSED with correct entry_time).
  3. Resets the ``paper:state:equity`` Redis snapshot so cash reflects
     the real trade outcome (₹100,000 − ₹151.57 = ₹99,848.43).

Safe to run while bots are stopped. Refuses to run if either equity
or F&O bot is currently alive — bots hold the broker state in memory
and would overwrite our cleanup on the next tick.

Usage::

    python scripts/cleanup_equity_2026_05_04.py             # do it
    python scripts/cleanup_equity_2026_05_04.py --dry-run   # show what would change
    python scripts/cleanup_equity_2026_05_04.py --force     # bypass live-bot guard

Companion to ``scripts/cleanup_fno_2026_04_30.py`` (the spread blow-up
fix). Both follow the same template: rename → replace → reset Redis.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IST = pytz.timezone("Asia/Kolkata")

# Constants for today's specific incident (2026-05-04 equity ADANIENT).
INCIDENT_DATE = "2026-05-04"
SUFFIX = ".corrupted-by-2026-05-04-eod-race"
STARTING_CAPITAL = 100_000.0   # configured equity capital
REAL_TRADE_PNL = -151.57        # the only real trade outcome today
EXPECTED_GOOD_CASH = STARTING_CAPITAL + REAL_TRADE_PNL  # ₹99,848.43

JOURNAL_PATH = ROOT / "logs" / "trades" / "equity" / f"{INCIDENT_DATE}.jsonl"


# Indices (0-based) of the LEGITIMATE journal entries from the original
# 8-entry file — keep these, drop the rest.
#   0  FILL BUY  ADANIENT @ 2496.05  (real entry)
#   1  TRADE_OPEN LONG ADANIENT      (real entry record)
#   2  FILL SELL ADANIENT @ 2483.56  (FIRST square-off, real)
#   3  FILL SELL ADANIENT @ 2483.56  ← duplicate (drop)
#   4  TRADE_CLOSED LONG (entry_time=15:15) ← duplicate (drop)
#   5  TRADE_CLOSED LONG (entry_time=14:14) ← real close (KEEP)
#   6  FILL BUY ADANIENT @ 2486.04   ← phantom auto-cover (drop)
#   7  TRADE_CLOSED SHORT ADANIENT   ← phantom close (drop)
LEGIT_INDICES = [0, 1, 2, 5]


def _live_bot_pids() -> list[int]:
    """Return PIDs of any running bot processes (equity or F&O)."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "cli.* run.*--paper"], text=True, timeout=5,
        )
    except subprocess.CalledProcessError:
        return []
    except Exception:
        return []
    return [int(p) for p in out.split() if p.strip().isdigit()]


def _safety_check(force: bool) -> None:
    pids = _live_bot_pids()
    if pids and not force:
        print()
        print("=" * 72)
        print(" REFUSING TO RUN — live bot(s) are still running")
        print("=" * 72)
        print(f"  pid(s): {pids}")
        print()
        print("  This script clears Redis state that the running broker holds in")
        print("  memory. Restart-overwrite would undo the cleanup.")
        print()
        print("  Stop the bots first:  pkill -f 'cli.* run'")
        print("  Or bypass:             python scripts/cleanup_equity_2026_05_04.py --force")
        sys.exit(1)


def _rename_journal_aside(dry_run: bool) -> Path | None:
    if not JOURNAL_PATH.exists():
        print(f"  • {JOURNAL_PATH.relative_to(ROOT)}: not present, nothing to rename")
        return None
    target = JOURNAL_PATH.with_suffix(JOURNAL_PATH.suffix + SUFFIX)
    print(f"  • rename: {JOURNAL_PATH.relative_to(ROOT)}")
    print(f"            → {target.relative_to(ROOT)}")
    if not dry_run:
        shutil.move(str(JOURNAL_PATH), str(target))
    return target


def _write_clean_journal(corrupted_path: Path | None, dry_run: bool) -> None:
    if corrupted_path is None:
        return
    src = corrupted_path if not dry_run else corrupted_path
    if dry_run and not src.exists():
        # In dry-run we haven't moved the file yet; read the original.
        src = JOURNAL_PATH
    raw_lines = [l for l in src.read_text().splitlines() if l.strip()]
    legit = [json.loads(raw_lines[i]) for i in LEGIT_INDICES if i < len(raw_lines)]
    print(f"  • write clean replacement journal with {len(legit)} entries:")
    for e in legit:
        ts = e.get("ts", "?")[11:19]
        ev = e.get("type", "?")
        side = e.get("side", "")
        sym = e.get("symbol", "")
        pnl = e.get("net_pnl", "")
        print(f"      {ts}  {ev:14}  {side:4}  {sym}  pnl={pnl}")
    if not dry_run:
        with JOURNAL_PATH.open("w") as fh:
            for e in legit:
                fh.write(json.dumps(e, default=str) + os.linesep)


def _reset_redis_state(dry_run: bool) -> None:
    """Reset BOTH state caches (paper:state:equity + portfolio:equity).

    The bot has two parallel state caches:
      * ``paper:state:equity``  — broker snapshot, read on bot startup
                                   by ``PaperBroker._restore_state``
      * ``portfolio:equity``    — executor's published snapshot, read by
                                   the Streamlit dashboard for cash,
                                   positions, and daily-P&L % display

    Both must be cleaned together — the 2026-05-05 morning incident
    proved that cleaning only ``paper:state:equity`` (broker side) left
    the dashboard showing yesterday's corruption from
    ``portfolio:equity`` (display side). Customer impact: dashboard
    panic at "₹74,961 / -25%" when the actual broker state was already
    fixed. Fixed permanently here.

    Also clears today's eod_done marker so a re-launch can run EOD
    cleanly tomorrow morning regardless of whether tonight's cron fires.
    """
    from bot.cache import get_cache
    cache = get_cache()
    now_iso = datetime.now(IST).isoformat()

    # ── Read current state for both caches ──────────────────────────
    for key in ("paper:state:equity", "portfolio:equity"):
        snap = cache.get_json(key) or {}
        ts_label = snap.get("saved_at") or snap.get("ts") or "never"
        print(f"  • {key} (current):")
        print(f"      cash=₹{snap.get('cash',0):,.2f}  "
              f"positions={len(snap.get('positions', {}) or [])}  "
              f"ts={str(ts_label)[:19]}")

    # ── Compose the reset payloads ──────────────────────────────────
    # Note: paper:state stores positions as a dict (keyed by symbol),
    # portfolio: stores positions as a list (the published snapshot
    # format). Use the right shape for each.
    paper_state = {
        "cash": EXPECTED_GOOD_CASH,
        "starting_capital": STARTING_CAPITAL,
        "positions": {},
        "saved_at": now_iso,
        "cleanup_marker": "2026-05-04-eod-race-cleanup",
        "cleanup_note": (
            f"Reset to real post-trade cash (₹{STARTING_CAPITAL:,.0f} − "
            f"₹{abs(REAL_TRADE_PNL):.2f} ADANIENT round-trip = "
            f"₹{EXPECTED_GOOD_CASH:,.2f}). The phantom-short ledger leak "
            "of ₹24,887.05 was a sim bug, not a market loss."
        ),
    }
    portfolio = {
        "ts": now_iso,
        "segment": "equity",
        "starting_capital": STARTING_CAPITAL,
        "cash": EXPECTED_GOOD_CASH,
        "profit_locked": False,
        "daily_pnl_pct": round(100.0 * REAL_TRADE_PNL / STARTING_CAPITAL, 3),
        "positions": [],
        "cleanup_marker": "2026-05-04-eod-race-cleanup",
    }

    print(f"  • paper:state:equity (after reset):")
    print(f"      cash=₹{paper_state['cash']:,.2f}  "
          f"starting=₹{paper_state['starting_capital']:,.0f}  positions=0")
    print(f"  • portfolio:equity (after reset):")
    print(f"      cash=₹{portfolio['cash']:,.2f}  "
          f"daily_pnl_pct={portfolio['daily_pnl_pct']}%  positions=0")

    if not dry_run:
        cache.set_json("paper:state:equity", paper_state)
        cache.set_json("portfolio:equity", portfolio)
        cache.delete("eod_done:equity")
    print(f"  • cleared eod_done:equity (so tomorrow's square-off runs fresh)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the live-bot safety check (dangerous)")
    args = parser.parse_args()

    print()
    print("=" * 72)
    print(" 2026-05-04 ADANIENT phantom-short cleanup")
    print("=" * 72)
    print(f"  date           : {INCIDENT_DATE}")
    print(f"  expected cash  : ₹{EXPECTED_GOOD_CASH:,.2f} "
          f"(₹{STARTING_CAPITAL:,.0f} − ₹{abs(REAL_TRADE_PNL):.2f} real loss)")
    print(f"  dry_run        : {args.dry_run}")
    print()

    _safety_check(args.force)

    print("Step 1 — quarantine the corrupted journal")
    corrupted = _rename_journal_aside(args.dry_run)

    print()
    print("Step 2 — write clean replacement journal")
    _write_clean_journal(corrupted, args.dry_run)

    print()
    print("Step 3 — reset Redis broker state")
    _reset_redis_state(args.dry_run)

    print()
    if args.dry_run:
        print("DRY RUN complete — no files or Redis keys were modified.")
    else:
        print("Cleanup complete. Restart the equity bot to pick up the clean state:")
        print("    bash scripts/run_bot.sh run --paper --segment equity")
        print()
        print("Verify with:")
        print("    python -m cli journal --segment equity --tail 5")
        print("    python scripts/system_audit.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
