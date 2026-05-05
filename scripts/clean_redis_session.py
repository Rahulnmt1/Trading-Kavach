"""Clean stale Redis session keys — manual on-demand version of ``daily_reset``.

The bot's scheduler runs ``bot.daily_reset.daily_reset`` automatically at
08:55 IST every weekday — but only WHILE the bot is running. If you stop
the bot mid-day (Ctrl-C, crash, manual restart), or start it AFTER 08:55
IST, the daily reset never fires and yesterday's keys can leak into today.

This script is a standalone replacement that runs the same logic for
**both segments** plus a few extra keys that the in-bot reset is too
conservative to clear (heartbeats from prior runs, EOD-done markers from
yesterday, stale signal keys whose timestamps are > 24h old, etc.).

Safe to run while bots are stopped. Refuses to run while bots are live —
the running broker's in-memory state would just re-publish the cleared
keys on the next tick.

Usage::

    python scripts/clean_redis_session.py                # do it
    python scripts/clean_redis_session.py --dry-run      # show what would clear
    python scripts/clean_redis_session.py --status       # report-only, no changes
    python scripts/clean_redis_session.py --force        # skip the live-bot guard

Categories cleared (per segment):

  * ``paper:state:<seg>``       broker positions/cash (intraday only)
  * ``heartbeat:tick:<seg>``    last-tick timestamp
  * ``profit_lockin:<seg>``     daily P&L target halt
  * ``signal:<seg>:*``          per-symbol signal cache
  * ``trail:<seg>:*``           trailing-stop snapshots
  * ``eod_done:<seg>``          EOD idempotency marker (only if older than today)

Categories NOT cleared (by design):

  * ``research:YYYY-MM-DD``     today's pre-market picks (would force re-run)
  * ``watchlist:auto``          today's auto-watchlist
  * ``healthcheck:latest:*``    diagnostic trail
  * ``fee_audit:latest``        7-day TTL artefact
  * ``holidays:*``              NSE holiday calendar (refreshed at 06:00)
  * ``bars:*``                  yfinance cache (60s TTL, self-stale)
  * ``orders``                  trade history hash (audit trail)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, date
from pathlib import Path

import pytz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IST = pytz.timezone("Asia/Kolkata")


# ─── Coloured output ─────────────────────────────────────────────────────────

_C = {
    "ok":   "\033[32m",
    "warn": "\033[33m",
    "fail": "\033[31m",
    "dim":  "\033[2m",
    "bold": "\033[1m",
    "rs":   "\033[0m",
}


def _live_bot_pids() -> list[int]:
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "cli.* run.*--paper"], text=True, timeout=5,
        )
    except subprocess.CalledProcessError:
        return []
    except Exception:
        return []
    return [int(p) for p in out.split() if p.strip().isdigit()]


def _safety_check(force: bool, status_only: bool) -> None:
    if status_only:
        return
    pids = _live_bot_pids()
    if pids and not force:
        print()
        print("=" * 72)
        print(" REFUSING — live bot(s) are still running")
        print("=" * 72)
        print(f"  pid(s): {pids}")
        print()
        print("  Clearing Redis state while the broker holds the same data in")
        print("  memory just causes it to re-publish on the next tick (no-op).")
        print("  Stop the bots first:  pkill -f 'cli.* run'")
        print("  Or use:                python scripts/clean_redis_session.py --force")
        print("  Or report-only:        python scripts/clean_redis_session.py --status")
        sys.exit(1)


# ─── Stale-key inspection ────────────────────────────────────────────────────

def _key_age_seconds(cache, key: str, ts_field: str = "ts") -> float | None:
    """Best-effort age in seconds of a JSON value at ``key`` (looks at
    a timestamp field). Returns ``None`` if not parseable.
    """
    val = cache.get_json(key)
    if not isinstance(val, dict):
        return None
    raw = val.get(ts_field) or val.get("saved_at") or val.get("timestamp")
    if not raw:
        return None
    try:
        # Strip trailing Z if present (treat as UTC).
        s = raw.rstrip("Z")
        ts = datetime.fromisoformat(s)
        if ts.tzinfo is None:
            ts = IST.localize(ts)
        now = datetime.now(IST)
        return (now - ts).total_seconds()
    except Exception:
        return None


def _today_iso() -> str:
    return datetime.now(IST).date().isoformat()


def _is_stale(cache, key: str) -> tuple[bool, str]:
    """Return ``(stale, reason)`` for one key. Stale = older than today
    (IST) or older than 24h, depending on what's available."""
    age = _key_age_seconds(cache, key)
    if age is None:
        return False, "no timestamp"
    if age > 12 * 3600:   # 12h cutoff — generous but catches yesterday's data
        return True, f"age {age/3600:.1f}h"
    return False, f"age {age/3600:.1f}h"


# ─── Categories ──────────────────────────────────────────────────────────────

def _exact_keys(seg: str) -> list[str]:
    # Two parallel state caches:
    #   paper:state:<seg>  — broker's internal snapshot (used by
    #                        PaperBroker._restore_state on bot startup)
    #   portfolio:<seg>    — executor's published snapshot (used by the
    #                        Streamlit dashboard for cash + positions
    #                        + daily P&L %).
    # Both must be cleaned together — the 2026-05-05 dashboard incident
    # (dashboard showed yesterday's −₹25k corruption while broker state
    # was already clean) was caused by cleaning only the broker key and
    # leaving portfolio:<seg> stale. They track the same underlying
    # data, so they MUST be wiped as a pair.
    return [
        f"paper:state:{seg}",
        f"portfolio:{seg}",
        f"heartbeat:tick:{seg}",
        f"profit_lockin:{seg}",
    ]


def _patterns(seg: str) -> list[str]:
    return [f"signal:{seg}:*", f"trail:{seg}:*"]


# ─── Main ────────────────────────────────────────────────────────────────────

def report(cache, dry_run: bool, status_only: bool) -> dict[str, dict]:
    """Walk every category, report what's stale, optionally clear it.

    Returns a per-segment summary dict.
    """
    summary: dict[str, dict] = {}
    today = _today_iso()

    print()
    print("=" * 72)
    print(" Redis session-data inspection")
    print("=" * 72)
    print(f"  now: {datetime.now(IST).strftime('%a %d %b %Y, %H:%M:%S IST')}")
    print(f"  today (IST): {today}")
    print(f"  mode: {'STATUS' if status_only else ('DRY-RUN' if dry_run else 'CLEAN')}")

    total_cleared = 0
    total_stale = 0
    total_keys = 0

    for seg in ("equity", "fno"):
        print(f"\n[{seg.upper()}]")
        seg_summary = {"checked": 0, "stale": 0, "cleared": 0, "items": []}

        for k in _exact_keys(seg):
            seg_summary["checked"] += 1
            total_keys += 1
            if cache.get_json(k) is None:
                print(f"  {_C['dim']}· {k:42}  absent{_C['rs']}")
                continue
            stale, reason = _is_stale(cache, k)
            mark = ("⚠ STALE" if stale else "✓ fresh")
            colour = _C["warn"] if stale else _C["ok"]
            print(f"  {colour}· {k:42}  {mark} ({reason}){_C['rs']}")
            seg_summary["items"].append({"key": k, "stale": stale, "reason": reason})
            if stale:
                seg_summary["stale"] += 1
                total_stale += 1
                if not status_only and not dry_run:
                    cache.delete(k)
                    seg_summary["cleared"] += 1
                    total_cleared += 1
                elif dry_run:
                    print(f"    {_C['dim']}(would delete){_C['rs']}")
            elif not status_only:
                # Even if "fresh" by timestamp, exact intraday keys should
                # be wiped because we're going for a fresh session start.
                if not dry_run:
                    cache.delete(k)
                    seg_summary["cleared"] += 1
                    total_cleared += 1
                else:
                    print(f"    {_C['dim']}(would clear — intraday-only key){_C['rs']}")

        for pat in _patterns(seg):
            keys = cache.keys(pat)
            seg_summary["checked"] += len(keys)
            total_keys += len(keys)
            if not keys:
                print(f"  {_C['dim']}· {pat:42}  no matches{_C['rs']}")
                continue
            print(f"  {_C['warn']}· {pat:42}  {len(keys)} key(s){_C['rs']}")
            for k in keys[:5]:
                stale, reason = _is_stale(cache, k)
                mark = "⚠ STALE" if stale else "  carry"
                print(f"    {_C['dim']}- {k}  {mark} ({reason}){_C['rs']}")
            if len(keys) > 5:
                print(f"    {_C['dim']}- ... and {len(keys)-5} more{_C['rs']}")
            for k in keys:
                stale, _ = _is_stale(cache, k)
                if stale:
                    seg_summary["stale"] += 1
                    total_stale += 1
                if not status_only and not dry_run:
                    cache.delete(k)
                    seg_summary["cleared"] += 1
                    total_cleared += 1

        # eod_done — clear ONLY if from a prior date (today's marker is
        # what protects against the race condition; don't wipe it).
        eod_key = f"eod_done:{seg}"
        eod_val = cache.get_json(eod_key)
        if isinstance(eod_val, dict):
            ed = eod_val.get("date", "")
            if ed and ed < today:
                print(f"  {_C['warn']}· {eod_key:42}  ⚠ STALE (date {ed}){_C['rs']}")
                seg_summary["stale"] += 1
                total_stale += 1
                if not status_only and not dry_run:
                    cache.delete(eod_key)
                    seg_summary["cleared"] += 1
                    total_cleared += 1
            else:
                print(f"  {_C['ok']}· {eod_key:42}  ✓ today ({ed}){_C['rs']}")
        else:
            print(f"  {_C['dim']}· {eod_key:42}  absent{_C['rs']}")

        summary[seg] = seg_summary
        print(f"  {_C['bold']}{seg} total: {seg_summary['checked']} checked, "
              f"{seg_summary['stale']} stale, "
              f"{seg_summary['cleared']} cleared{_C['rs']}")

    print()
    print("=" * 72)
    if status_only:
        print(f" Status: {total_keys} key(s) inspected, {total_stale} stale, 0 cleared")
        print(f" Re-run without --status to clean.")
    elif dry_run:
        print(f" Dry-run: {total_keys} key(s) inspected, {total_stale} stale, "
              f"would-clear={total_cleared}")
    else:
        print(f" Cleaned: {total_keys} inspected, {total_stale} stale, "
              f"{total_cleared} cleared")
    print("=" * 72)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would clear without writing")
    parser.add_argument("--status", action="store_true",
                        help="Read-only report (no changes, no live-bot guard)")
    parser.add_argument("--force", action="store_true",
                        help="Skip the live-bot safety check (dangerous)")
    args = parser.parse_args()

    _safety_check(args.force, args.status)

    from bot.cache import get_cache
    try:
        cache = get_cache()
        # Liveness probe.
        cache.client.ping() if hasattr(cache.client, "ping") else None
    except Exception as e:
        print(f"  {_C['fail']}Redis unreachable: {e}{_C['rs']}")
        print(f"  Start it:  brew services start redis")
        return 1

    summary = report(cache, dry_run=args.dry_run, status_only=args.status)

    # Non-zero exit if stale data found in --status mode (so the
    # preflight harness can detect it via exit code).
    any_stale = any(s["stale"] > 0 for s in summary.values())
    if args.status and any_stale:
        return 2  # 2 = "stale found, no changes made"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
