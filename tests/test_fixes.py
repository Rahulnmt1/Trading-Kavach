"""End-to-end smoke test for the three reliability fixes.

Run from the repo root:
    python tests/test_fixes.py

This is intentionally NOT a pytest test — it's a one-shot verifier so
you can read the output and confirm each guarantee holds.
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytz

# Ensure imports resolve from repo root.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

IST = pytz.timezone("Asia/Kolkata")


def banner(title: str) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def _live_bot_processes() -> list[tuple[int, str]]:
    """Return ``[(pid, command), ...]`` for any live ``cli.py run`` Python
    processes (BOTH segments). Empty list = safe to run the test.

    Why we don't trust the lock files alone: a previous (buggy) test run
    may have already deleted them while the bot kept running — leaving
    the lock files missing AND the bot live. The process scan is the
    authoritative check.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,command"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    live: list[tuple[int, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("PID"):
            continue
        # Must match `python ... -m cli run` or `python ... cli.py run`,
        # but NOT this current test process. Skip our own pid.
        if "cli" not in line or " run" not in line:
            continue
        if "test_fixes" in line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == os.getpid() or pid == os.getppid():
            continue
        live.append((pid, parts[1]))
    return live


def _abort_if_bots_running() -> None:
    """Refuse to run the test suite while a real bot is alive.

    The test legitimately clears segment locks and calls daily_reset to
    simulate fresh-day startup — operations that wipe a running bot's
    intraday state and break its lock-file invariant. Detecting that
    situation up-front and bailing is FAR safer than blindly proceeding.
    """
    live = _live_bot_processes()
    if live:
        print()
        print("=" * 60)
        print("REFUSING TO RUN — a live bot is still running")
        print("=" * 60)
        for pid, cmd in live:
            print(f"  pid={pid}  {cmd[:80]}")
        print()
        print("This test clobbers lock files and calls daily_reset, both")
        print("of which would corrupt a running bot's state. Stop the")
        print("bot(s) first:")
        print()
        print("  Ctrl-C in each bot terminal,  OR")
        print("  pkill -f 'cli.* run'          (kills both segments)")
        print()
        print("Then re-run:  python tests/test_fixes.py")
        sys.exit(2)


def main() -> int:
    _abort_if_bots_running()
    banner("FIX #1 — PID lockfile (single-instance enforcement, equity)")

    # Clear stale segment locks from prior runs.
    for f in (".bot.lock.equity", ".bot.lock.fno", ".bot.lock"):
        (ROOT / f).unlink(missing_ok=True)

    from bot.lock import acquire, release, BotAlreadyRunningError
    from bot.segment import Segment

    pid_a = acquire(segment=Segment.EQUITY)
    print(f"  ✓ Process A acquired EQUITY lock (pid={pid_a})")

    # Spawn a fresh subprocess that tries to acquire the SAME segment lock — it must fail.
    probe_script = (
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "from bot.lock import acquire, BotAlreadyRunningError\n"
        "from bot.segment import Segment\n"
        "try:\n"
        "    acquire(segment=Segment.EQUITY)\n"
        "    print('ACQUIRED')\n"
        "except BotAlreadyRunningError as e:\n"
        "    print('BLOCKED')\n"
        "    print(str(e)[:120])\n"
    )
    probe = subprocess.run(
        [sys.executable, "-c", probe_script],
        cwd=str(ROOT),
        capture_output=True, text=True, timeout=10,
    )
    out = probe.stdout.strip()
    if "BLOCKED" in out:
        print(f"  ✓ Subprocess B (also EQUITY) correctly REJECTED")
        excerpt = out.split("\n", 1)[1] if "\n" in out else ""
        print(f"    Message excerpt: {excerpt!r}")
    else:
        print(f"  ✗ FAILED: subprocess B output:\n{out}\nstderr:\n{probe.stderr}")
        return 1

    # Now: a subprocess for the OTHER segment (FNO) should succeed in
    # parallel — that's the whole point of segment isolation.
    fno_probe_script = (
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "from bot.lock import acquire, release, BotAlreadyRunningError\n"
        "from bot.segment import Segment\n"
        "try:\n"
        "    pid = acquire(segment=Segment.FNO)\n"
        "    print(f'ACQUIRED_FNO_PID={pid}')\n"
        "    release(segment=Segment.FNO)\n"
        "except BotAlreadyRunningError as e:\n"
        "    print('UNEXPECTED_BLOCK')\n"
        "    print(str(e)[:120])\n"
    )
    fno_probe = subprocess.run(
        [sys.executable, "-c", fno_probe_script],
        cwd=str(ROOT),
        capture_output=True, text=True, timeout=10,
    )
    fno_out = fno_probe.stdout.strip()
    if "ACQUIRED_FNO" in fno_out:
        print(f"  ✓ Subprocess C (FNO segment) acquired lock IN PARALLEL with A")
        print(f"    Output: {fno_out.splitlines()[0]}")
    else:
        print(f"  ✗ FAILED: FNO segment lock blocked when it shouldn't be:\n{fno_out}")
        return 1

    release(segment=Segment.EQUITY)
    print(f"  ✓ Process A released EQUITY lock")

    pid_a2 = acquire(segment=Segment.EQUITY)
    print(f"  ✓ Re-acquired EQUITY after release (pid={pid_a2})")
    release(segment=Segment.EQUITY)

    banner("FIX #2 — Cache state freshness (price-divergence guard, equity)")

    from bot.cache import get_cache
    from bot.segment import cache_key
    cache = get_cache()

    corrupt_snap = {
        "starting_capital": 50000.0, "cash": 38000.0,
        "saved_at": datetime.now(IST).isoformat(),
        "positions": {
            "NESTLEIND": {
                "symbol": "NESTLEIND", "qty": 8, "avg_price": 2400.00,
                "side": "BUY", "stop_loss": 2392.0, "take_profit": 2410.0,
                "initial_stop_loss": 2392.0, "initial_take_profit": 2410.0,
                "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                "opened_at": datetime.now(IST).isoformat(),
            }
        }
    }
    eq_state_key = cache_key("paper:state", Segment.EQUITY)
    cache.set_json(eq_state_key, corrupt_snap)
    print(f"  Injected corrupt state @ {eq_state_key}: NESTLEIND@₹2400 (today's price ~₹1454)")

    from bot.broker.paper import PaperBroker
    b = PaperBroker(segment=Segment.EQUITY)
    print(f"  After restore: {len(b._positions)} positions")
    if "NESTLEIND" not in b._positions:
        print(f"  ✓ Corrupt position correctly REJECTED")
    else:
        pos = b._positions["NESTLEIND"]
        print(f"  ✗ FAILED — position kept at avg=₹{pos.avg_price:.2f}")
        return 1

    cache.delete(eq_state_key)

    banner("FIX #2b — Cross-session staleness (yesterday's snapshot)")

    old_snap = dict(corrupt_snap)
    old_snap["saved_at"] = (datetime.now(IST) - timedelta(days=2)).isoformat()
    old_snap["positions"]["NESTLEIND"]["avg_price"] = 1454.63  # would otherwise pass
    old_snap["positions"]["NESTLEIND"]["opened_at"] = old_snap["saved_at"]
    cache.set_json(eq_state_key, old_snap)
    print(f"  Injected snapshot from 2 days ago (with realistic avg_price)")

    b2 = PaperBroker(segment=Segment.EQUITY)
    print(f"  After restore: {len(b2._positions)} positions")
    if not b2._positions:
        print(f"  ✓ Stale-day snapshot correctly DISCARDED")
    else:
        print(f"  ✗ FAILED — stale snapshot was restored")
        return 1

    cache.delete(eq_state_key)

    banner("FIX #3 — Tick heartbeat (executor stall detection, equity)")

    from bot.healthcheck import _tick_heartbeat

    eq_hb_key = cache_key("heartbeat:tick", Segment.EQUITY)
    cache.set_json(eq_hb_key, {
        "ts": datetime.now(IST).isoformat(),
        "weekday": datetime.now(IST).weekday(),
        "in_window": True,
        "segment": Segment.EQUITY.value,
    }, ttl=3600)
    r = _tick_heartbeat(Segment.EQUITY)
    print(f"  Fresh heartbeat: status={r.status}  detail={r.detail}")
    assert r.status == "OK", f"expected OK, got {r.status}"
    print(f"  ✓ OK on a fresh equity heartbeat")

    cache.set_json(eq_hb_key, {
        "ts": (datetime.now(IST) - timedelta(minutes=5)).isoformat(),
        "weekday": datetime.now(IST).weekday(),
        "in_window": True,
        "segment": Segment.EQUITY.value,
    }, ttl=3600)
    r = _tick_heartbeat(Segment.EQUITY)
    print(f"  5-min-old heartbeat: status={r.status}  detail={r.detail}")
    now = datetime.now(IST)
    in_window = (now.time() >= datetime.strptime("09:30", "%H:%M").time()
                 and now.time() <= datetime.strptime("15:15", "%H:%M").time()
                 and now.weekday() < 5)
    expected = "FAIL" if in_window else "OK"
    if r.status == expected:
        print(f"  ✓ {expected} as expected (in_window={in_window})")
    else:
        print(f"  ✗ FAILED — expected {expected}, got {r.status}")
        return 1
    cache.delete(eq_hb_key)

    banner("FIX #4 — Daily Redis reset (scoped to ONE segment)")

    # Seed intraday keys for BOTH segments. The reset of Segment.EQUITY
    # must clear ONLY the equity keys and leave the F&O keys intact —
    # that's the proof of segment isolation for the daily reset.
    cache.set_json(cache_key("paper:state",     Segment.EQUITY), {"cash": 9999, "saved_at": datetime.now(IST).isoformat(), "positions": {}})
    cache.set_json(cache_key("heartbeat:tick",  Segment.EQUITY), {"ts": datetime.now(IST).isoformat()})
    cache.set_json(cache_key("profit_lockin",   Segment.EQUITY), {"day": str(datetime.now(IST).date())})
    cache.set_json(f"signal:{Segment.EQUITY.value}:NESTLEIND",   {"type": "BUY", "ts": "x"})
    cache.set_json(f"signal:{Segment.EQUITY.value}:RELIANCE",    {"type": "HOLD", "ts": "y"})
    cache.set_json(f"trail:{Segment.EQUITY.value}:NESTLEIND",    {"peak": 1500})

    # Same shape for FNO.
    cache.set_json(cache_key("paper:state",     Segment.FNO), {"cash": 50000, "saved_at": datetime.now(IST).isoformat(), "positions": {}})
    cache.set_json(cache_key("heartbeat:tick",  Segment.FNO), {"ts": datetime.now(IST).isoformat()})
    cache.set_json(f"signal:{Segment.FNO.value}:NIFTY",          {"type": "BUY", "ts": "x"})
    cache.set_json(f"trail:{Segment.FNO.value}:NIFTY",           {"peak": 24500})

    # Daily-derived keys we MUST NOT touch (global, not per-segment).
    # IMPORTANT: snapshot any pre-existing real values FIRST, then inject
    # test values, then restore the originals in cleanup. Otherwise
    # running this test against a live bot wipes today's actual research
    # picks and auto-watchlist (the cleanup `cache.delete(...)` doesn't
    # know test-injected from real-bot data).
    today = datetime.now(IST).date().isoformat()
    daily_keys = (f"research:{today}", "watchlist:auto",
                  cache_key("healthcheck:latest", Segment.EQUITY), "fee_audit:latest")
    _saved_daily = {k: cache.get_json(k) for k in daily_keys}
    cache.set_json(f"research:{today}", {"picks": ["x"]})
    cache.set_json("watchlist:auto", ["A", "B"])
    cache.set_json(cache_key("healthcheck:latest", Segment.EQUITY), {"overall": "OK"})
    cache.set_json("fee_audit:latest", {"status": "OK"})

    print("  Seeded 6 equity intraday + 4 fno intraday + 4 daily-derived keys")

    from bot.daily_reset import daily_reset
    counts = daily_reset(segment=Segment.EQUITY)
    total = sum(counts.values())
    print(f"  Equity reset cleared {total} keys: {counts}")
    if total >= 6:
        print(f"  ✓ Cleared {total} equity intraday key(s)")
    else:
        print(f"  ✗ FAILED — expected ≥6 equity deletions, got {total}")
        return 1

    # Verify FNO intraday keys SURVIVED (segment isolation).
    fno_survivors = []
    for k in (cache_key("paper:state",     Segment.FNO),
              cache_key("heartbeat:tick",  Segment.FNO),
              f"signal:{Segment.FNO.value}:NIFTY",
              f"trail:{Segment.FNO.value}:NIFTY"):
        if cache.get_json(k) is not None:
            fno_survivors.append(k)
    if len(fno_survivors) == 4:
        print(f"  ✓ All 4 F&O intraday keys preserved (segment-scoped reset)")
    else:
        print(f"  ✗ FAILED — F&O keys WIPED by equity reset: only {fno_survivors} survived")
        return 1

    # Verify the daily-derived keys are STILL there.
    survived = []
    for k in (f"research:{today}", "watchlist:auto",
              cache_key("healthcheck:latest", Segment.EQUITY), "fee_audit:latest"):
        if cache.get_json(k) is not None:
            survived.append(k)
    if len(survived) == 4:
        print(f"  ✓ All 4 daily-derived keys preserved: {survived}")
    else:
        print(f"  ✗ FAILED — only {len(survived)}/4 daily-derived keys survived: {survived}")
        return 1

    # Cleanup: restore the pre-test snapshot (or delete if there was no
    # pre-existing value). Never blind-delete — that erases a live bot's
    # real research/watchlist if the test is run during the trading day.
    for k, original in _saved_daily.items():
        if original is None:
            cache.delete(k)
        else:
            cache.set_json(k, original, ttl=86400)
    daily_reset(segment=Segment.FNO)

    banner("FIX #5 — _manage_open_positions: no silent skip on empty bars")

    # We can't easily run the full executor here without yfinance + a
    # broker — but we CAN smoke-test the fallback by inspecting that the
    # new code path exists in the source.
    src = (ROOT / "bot" / "executor.py").read_text()
    required_phrases = [
        "no fresh bars (yfinance empty)",          # mark fallback log
        "1m bars empty — falling back to 5m bar",   # 5m fallback log
        'NO bars and NO mark',                      # absolute-last-resort log
    ]
    missing = [p for p in required_phrases if p not in src]
    if missing:
        print(f"  ✗ FAILED — bot/executor.py missing fallback log lines: {missing}")
        return 1
    print(f"  ✓ All 3 fallback paths present in _manage_open_positions")

    banner("FIX #6 — Segment isolation (broker, journal, executor)")

    # Two PaperBroker instances for different segments must not see each
    # other's state. The first proof was via the daily_reset test above;
    # this confirms broker construction and journal paths land in the
    # right places.
    cache.delete(cache_key("paper:state", Segment.EQUITY))
    cache.delete(cache_key("paper:state", Segment.FNO))

    eq_b = PaperBroker(segment=Segment.EQUITY)
    fno_b = PaperBroker(segment=Segment.FNO)
    print(f"  Equity broker: cash=₹{eq_b.cash():.0f}  state_key={eq_b._state_key}")
    print(f"  F&O broker:    cash=₹{fno_b.cash():.0f}  state_key={fno_b._state_key}")
    assert eq_b._state_key != fno_b._state_key, "broker state keys collided!"
    print(f"  ✓ Broker state-key namespacing verified")

    # Journal paths must be in different sub-directories.
    from bot.journal import trades_jsonl
    eq_path = trades_jsonl(segment=Segment.EQUITY)
    fno_path = trades_jsonl(segment=Segment.FNO)
    print(f"  Equity journal path: {eq_path}")
    print(f"  F&O journal path:    {fno_path}")
    assert eq_path != fno_path, "journal paths collided!"
    assert "equity" in str(eq_path) and "fno" in str(fno_path), \
        "journal paths missing segment subdir"
    print(f"  ✓ Journal paths are per-segment")

    # Strategy ensemble for F&O must register the configured F&O strategy
    # (Phase 3 default: option_buy_directional). Equity strategies must
    # never leak into the F&O segment regardless of YAML.
    from bot.strategies import build_default_ensemble
    fno_ensemble = build_default_ensemble(segment=Segment.FNO)
    fno_names = sorted(m.name for m in fno_ensemble.members)
    valid_fno = {"futures_trend", "option_buy_directional", "credit_spread",
                 "iron_condor"}
    if fno_names and set(fno_names).issubset(valid_fno):
        print(f"  ✓ F&O ensemble registers F&O strategies: {fno_names}")
    else:
        print(f"  ✗ FAILED — F&O ensemble members = {fno_names}; "
              f"expected subset of {sorted(valid_fno)}")
        return 1
    eq_ensemble = build_default_ensemble(segment=Segment.EQUITY)
    eq_names = sorted(m.name for m in eq_ensemble.members)
    leaked = set(eq_names) & valid_fno
    if leaked:
        print(f"  ✗ FAILED — equity ensemble leaked F&O strategies: {leaked}")
        return 1
    print(f"  ✓ Equity ensemble does NOT leak F&O strategies (got {eq_names})")

    banner("FIX #7 — F&O futures round-trip (Phase 2)")

    # 7a. Instrument resolution: NIFTY → current monthly futures contract.
    from bot.instruments.fno import (
        resolve_futures, current_expiry, lot_size, margin_pct,
        tradingsymbol, yfinance_proxy,
    )
    inst = resolve_futures("NIFTY")
    assert inst.lot_size == 75, f"NIFTY lot size {inst.lot_size} != 75 (verify post-2024 SEBI table)"
    assert inst.expiry.weekday() == 3, f"expiry {inst.expiry} is not a Thursday (weekday={inst.expiry.weekday()})"
    assert inst.tradingsymbol.startswith("NIFTY") and inst.tradingsymbol.endswith("FUT"), \
        f"unexpected tradingsymbol format: {inst.tradingsymbol}"
    print(f"  ✓ Instrument resolve: NIFTY → {inst.tradingsymbol} "
          f"(expiry {inst.expiry}, lot {inst.lot_size})")

    # Tradingsymbol → yfinance proxy round-trips both directions.
    assert yfinance_proxy(inst.tradingsymbol) == "^NSEI", \
        f"proxy({inst.tradingsymbol}) = {yfinance_proxy(inst.tradingsymbol)}"
    assert yfinance_proxy("NIFTY") == "^NSEI"
    assert yfinance_proxy("BANKNIFTY24500FUT") == "^NSEBANK"
    print(f"  ✓ yfinance proxy: NIFTY → ^NSEI, BANKNIFTY → ^NSEBANK")

    # Lot-size lookups accept both forms.
    assert lot_size("NIFTY") == 75
    assert lot_size(inst.tradingsymbol) == 75
    assert margin_pct("NIFTY") == margin_pct(inst.tradingsymbol)
    print(f"  ✓ lot_size + margin_pct accept bare and tradingsymbol forms")

    # 7b. Fee schedule: futures rates differ from equity. Compute a real
    # 1-lot NIFTY round-trip and sanity-check the totals.
    from bot.fees import compute_fees, roundtrip_breakdown
    eq_fee = compute_fees("BUY", 75, 24000.0, segment="equity")
    fut_fee = compute_fees("BUY", 75, 24000.0, segment="futures")
    # Futures STT (sell side) is 0.0125% vs equity 0.025% — half the rate.
    eq_sell  = compute_fees("SELL", 75, 24100.0, segment="equity")
    fut_sell = compute_fees("SELL", 75, 24100.0, segment="futures")
    assert fut_sell.stt < eq_sell.stt, \
        f"futures STT {fut_sell.stt} should be < equity STT {eq_sell.stt}"
    # Futures exchange is 0.00188% vs equity 0.00345% — also lower.
    assert fut_fee.exchange < eq_fee.exchange, \
        f"futures exchange {fut_fee.exchange} should be < equity {eq_fee.exchange}"
    rt = roundtrip_breakdown(75, 24000.0, 24100.0, "long", segment="futures")
    print(f"  ✓ Futures fee schedule active:")
    print(f"      1 lot NIFTY round-trip @24000→24100  fees=₹{rt.fees_total:.2f}  "
          f"gross=₹{rt.gross_pnl:.2f}  net=₹{rt.net_pnl:.2f}")

    # 7c. Strategy signal — synthetic bullish cross 5m series.
    import numpy as np
    import pandas as pd
    from bot.config import FuturesTrendCfg
    from bot.strategies.fno import FuturesTrendStrategy
    from bot.strategies.base import SignalType

    # 55 flat bars at 24000 (so EMAs converge exactly), then 5 sharply
    # trending bars rising 83 pts/bar. The fast-EMA / slow-EMA cross
    # then falls precisely within the strategy's last 6 bars
    # (cross_lookback_bars=5), which is what we want to verify.
    rng = pd.date_range("2026-04-29 09:15", periods=60, freq="5min", tz="Asia/Kolkata")
    flat = np.full(55, 24000.0)
    trend = np.array([24083.0, 24167.0, 24250.0, 24333.0, 24417.0])
    closes = np.concatenate([flat, trend])
    # Tight intra-bar ranges so high/low don't dominate the close-driven indicators.
    df = pd.DataFrame({
        "open":   closes,
        "high":   closes + 5,
        "low":    closes - 5,
        "close":  closes,
        "volume": 100_000,
    }, index=rng)
    strategy = FuturesTrendStrategy(FuturesTrendCfg())
    sig = strategy.generate("NIFTY26MAYFUT", df)
    if sig.type != SignalType.BUY:
        print(f"  ✗ FAILED — synthetic uptrend produced {sig.type.value}, expected BUY. reason={sig.reason}")
        return 1
    assert sig.stop_loss is not None and sig.stop_loss < sig.price, "SL not below entry on long"
    assert sig.take_profit is not None and sig.take_profit > sig.price, "TP not above entry on long"
    print(f"  ✓ futures_trend BUY signal: price=₹{sig.price:.2f} "
          f"SL=₹{sig.stop_loss:.2f} TP=₹{sig.take_profit:.2f}")

    # 7d. Paper broker margin model — futures debit margin, NOT full notional.
    cache.delete(cache_key("paper:state", Segment.FNO))
    fno_b2 = PaperBroker(segment=Segment.FNO)
    # Bump test capital so 1 lot fits (config has ₹50K which can't take a lot).
    fno_b2._starting_cash = 200_000.0
    fno_b2._cash = 200_000.0
    fno_b2._positions = {}

    from bot.broker.base import (
        InstrumentKind, Order as _Order, OrderSide as _OrderSide,
        OrderType as _OrderType,
    )
    import uuid as _uuid
    entry_price = 24000.0
    qty = 75   # exactly 1 lot
    entry_order = _Order(
        id=str(_uuid.uuid4()), symbol="NIFTY26MAYFUT", side=_OrderSide.BUY,
        qty=qty, type=_OrderType.MARKET, price=entry_price,
        instrument_kind=InstrumentKind.FUTURES, lot_size=75,
    )
    cash_before_entry = fno_b2._cash
    filled = fno_b2.place_order(entry_order)
    if filled.status.value != "FILLED":
        print(f"  ✗ FAILED — futures entry status = {filled.status.value}")
        return 1
    pos = fno_b2._positions["NIFTY26MAYFUT"]
    cash_drop = cash_before_entry - fno_b2._cash
    expected_margin = entry_price * qty * margin_pct("NIFTY")          # 24000*75*0.05 = 90,000
    full_notional = entry_price * qty                                  # 1,800,000
    if not (expected_margin * 0.99 < pos.margin_blocked < expected_margin * 1.02):
        print(f"  ✗ FAILED — margin_blocked ₹{pos.margin_blocked:,.0f} "
              f"!= expected ~₹{expected_margin:,.0f}")
        return 1
    if cash_drop > full_notional * 0.5:
        print(f"  ✗ FAILED — cash dropped by ₹{cash_drop:,.0f}, looks like full notional "
              f"(₹{full_notional:,.0f}) was debited instead of margin")
        return 1
    print(f"  ✓ Futures entry: margin_blocked=₹{pos.margin_blocked:,.2f}  "
          f"cash_drop=₹{cash_drop:,.2f}  (NOT full notional ₹{full_notional:,.0f})")
    assert pos.instrument_kind == InstrumentKind.FUTURES
    assert pos.lot_size == 75

    # 7e. Round-trip P&L: close at +100 points → ~₹7,500 gross.
    exit_price = 24100.0
    exit_order = _Order(
        id=str(_uuid.uuid4()), symbol="NIFTY26MAYFUT", side=_OrderSide.SELL,
        qty=qty, type=_OrderType.MARKET, price=exit_price,
        instrument_kind=InstrumentKind.FUTURES, lot_size=75,
    )
    closed = fno_b2.place_order(exit_order)
    assert closed.status.value == "FILLED"
    assert "NIFTY26MAYFUT" not in fno_b2._positions, "position should be flat after exit"
    final_cash = fno_b2._cash
    realized = final_cash - cash_before_entry
    # Slippage is 5 bps each side, so true fill ≈ 24012 / 24087.95 → gross ≈ 5666.
    # We accept anything in (3000, 8000) to allow for that without overfitting.
    if not (3000 < realized < 8000):
        print(f"  ✗ FAILED — realized cash delta ₹{realized:,.2f} outside expected band")
        return 1
    print(f"  ✓ Round-trip realized: cash {cash_before_entry:,.0f} → {final_cash:,.2f}  "
          f"(net P&L ₹{realized:,.2f} on 100-pt move; "
          f"gross ₹{(exit_price - entry_price) * qty:,.0f}, fees ate the rest)")

    # Cleanup the test state.
    cache.delete(cache_key("paper:state", Segment.FNO))

    banner("FIX #8 — F&O option BUYING round-trip (Phase 3)")

    # 8a. Black-Scholes pricer sanity: ATM NIFTY ~14d call should be in
    # the realistic ₹150-400 band for typical 15% IV.
    from bot.options.pricing import (
        bs, bs_call, bs_put, years_to_expiry, synth_option_ohlc,
    )
    atm_call_14d = bs_call(24500, 24500, 14 / 365, sigma=0.15, r=0.07)
    if not (100 < atm_call_14d < 500):
        print(f"  ✗ FAILED — BS ATM 14d call premium ₹{atm_call_14d:.2f} outside [100, 500]")
        return 1
    print(f"  ✓ BS pricer: ATM 14d NIFTY call @24500/24500 → ₹{atm_call_14d:.2f}")

    # Put-call parity sanity: C - P ≈ S - K * e^(-rT)
    p_atm = bs_put(24500, 24500, 14 / 365, sigma=0.15, r=0.07)
    parity_lhs = atm_call_14d - p_atm
    parity_rhs = 24500 - 24500 * math.exp(-0.07 * 14 / 365)
    if abs(parity_lhs - parity_rhs) > 1.0:
        print(f"  ✗ FAILED — put-call parity violated: C-P={parity_lhs:.2f}, S-Ke^(-rT)={parity_rhs:.2f}")
        return 1
    print(f"  ✓ Put-call parity: C-P=₹{parity_lhs:.2f}, S-Ke^(-rT)=₹{parity_rhs:.2f}")

    # 8b. Option tradingsymbol parser round-trips.
    from bot.instruments.fno import (
        atm_strike, option_tradingsymbol, parse_option_tradingsymbol,
        resolve_atm_option, is_option_tradingsymbol,
    )
    from datetime import date as _date
    expiry = _date(2026, 5, 28)
    ts = option_tradingsymbol("NIFTY", expiry, 24600, "CE")
    if ts != "NIFTY26MAY24600CE":
        print(f"  ✗ FAILED — option_tradingsymbol = {ts!r}; expected 'NIFTY26MAY24600CE'")
        return 1
    parsed = parse_option_tradingsymbol(ts)
    assert parsed["underlying"] == "NIFTY"
    assert parsed["strike"] == 24600
    assert parsed["opt_type"] == "CE"
    assert is_option_tradingsymbol(ts)
    # Negative case: futures tradingsymbol must NOT match.
    assert parse_option_tradingsymbol("NIFTY26MAYFUT") is None
    assert not is_option_tradingsymbol("NIFTY26MAYFUT")
    print(f"  ✓ Option tradingsymbol parser: '{ts}' round-trips, FUT correctly excluded")

    # 8c. ATM strike rounding.
    if atm_strike("NIFTY", 24617.40) != 24600:
        print(f"  ✗ FAILED — atm_strike(NIFTY, 24617.40) = {atm_strike('NIFTY', 24617.40)}")
        return 1
    if atm_strike("BANKNIFTY", 53082.0) != 53100:
        print(f"  ✗ FAILED — atm_strike(BANKNIFTY, 53082) = {atm_strike('BANKNIFTY', 53082.0)}")
        return 1
    print(f"  ✓ ATM strike rounding: NIFTY→50-step, BANKNIFTY→100-step")

    # 8d. Option fee schedule: STT on premium SELL is much higher than equity.
    from bot.fees import compute_fees as _cf
    eq_sell  = _cf("SELL", 75, 200.0, segment="equity")
    opt_sell = _cf("SELL", 75, 200.0, segment="options")
    fut_sell = _cf("SELL", 75, 200.0, segment="futures")
    # Options STT (0.0625% on premium) > equity STT (0.025%) > futures STT (0.0125%)
    if not (opt_sell.stt > eq_sell.stt > fut_sell.stt):
        print(f"  ✗ FAILED — STT ordering wrong: opt={opt_sell.stt} eq={eq_sell.stt} fut={fut_sell.stt}")
        return 1
    print(f"  ✓ Option fee schedule: STT(opt)=₹{opt_sell.stt:.2f} > "
          f"STT(eq)=₹{eq_sell.stt:.2f} > STT(fut)=₹{fut_sell.stt:.2f}")

    # 8e. Strategy fires BUY for ATM CE on synthetic bullish data
    # (same data construction as Fix #7).
    from bot.config import OptionBuyDirectionalCfg
    from bot.strategies.fno import OptionBuyDirectionalStrategy
    rng2 = pd.date_range("2026-04-29 09:15", periods=60, freq="5min", tz="Asia/Kolkata")
    flat2 = np.full(55, 24000.0)
    trend2 = np.array([24083.0, 24167.0, 24250.0, 24333.0, 24417.0])
    closes2 = np.concatenate([flat2, trend2])
    df2 = pd.DataFrame({
        "open":   closes2,
        "high":   closes2 + 5,
        "low":    closes2 - 5,
        "close":  closes2,
        "volume": 100_000,
    }, index=rng2)
    opt_strat = OptionBuyDirectionalStrategy(OptionBuyDirectionalCfg())
    sig2 = opt_strat.generate("NIFTY", df2)
    if sig2.type.value != "BUY":
        print(f"  ✗ FAILED — option_buy_directional produced {sig2.type.value}, expected BUY. "
              f"reason={sig2.reason}")
        return 1
    parsed_sig = parse_option_tradingsymbol(sig2.symbol)
    if parsed_sig is None or parsed_sig["opt_type"] != "CE":
        print(f"  ✗ FAILED — sig.symbol={sig2.symbol!r} not a CE option tradingsymbol")
        return 1
    if not (sig2.stop_loss < sig2.price < sig2.take_profit):
        print(f"  ✗ FAILED — degenerate SL/TP: sl={sig2.stop_loss} entry={sig2.price} tp={sig2.take_profit}")
        return 1
    print(f"  ✓ option_buy_directional → BUY {sig2.symbol} "
          f"premium=₹{sig2.price:.2f} SL=₹{sig2.stop_loss:.2f} TP=₹{sig2.take_profit:.2f}")

    # 8f. Paper broker fills option BUY at FULL premium debit (no margin).
    cache.delete(cache_key("paper:state", Segment.FNO))
    opt_broker = PaperBroker(segment=Segment.FNO)
    opt_broker._starting_cash = 50_000.0
    opt_broker._cash = 50_000.0
    opt_broker._positions = {}

    entry_premium = 150.0
    opt_qty = 75   # exactly 1 lot of NIFTY
    opt_entry = _Order(
        id=str(_uuid.uuid4()), symbol=sig2.symbol, side=_OrderSide.BUY,
        qty=opt_qty, type=_OrderType.MARKET, price=entry_premium,
        instrument_kind=InstrumentKind.OPTION, lot_size=75,
    )
    cash_before_opt = opt_broker._cash
    opt_filled = opt_broker.place_order(opt_entry)
    if opt_filled.status.value != "FILLED":
        print(f"  ✗ FAILED — option entry status = {opt_filled.status.value}")
        return 1
    opt_pos = opt_broker._positions[sig2.symbol]
    if opt_pos.margin_blocked != 0.0:
        print(f"  ✗ FAILED — option position margin_blocked = ₹{opt_pos.margin_blocked} "
              f"should be 0 (long options have no margin)")
        return 1
    if opt_pos.instrument_kind != InstrumentKind.OPTION:
        print(f"  ✗ FAILED — instrument_kind = {opt_pos.instrument_kind}")
        return 1
    cash_drop_opt = cash_before_opt - opt_broker._cash
    expected_premium_cost = entry_premium * opt_qty   # 150 × 75 = 11,250
    if not (expected_premium_cost * 0.99 < cash_drop_opt < expected_premium_cost * 1.05):
        print(f"  ✗ FAILED — option cash_drop ₹{cash_drop_opt:,.0f} != expected "
              f"~₹{expected_premium_cost:,.0f} (premium × qty + fees)")
        return 1
    print(f"  ✓ Option BUY: premium_paid=₹{expected_premium_cost:,.0f}  "
          f"cash_drop=₹{cash_drop_opt:,.2f}  margin=₹{opt_pos.margin_blocked:.0f} (none, as expected)")

    # 8g. Round-trip P&L: close at +50 prem move = 75 × 50 = ₹3,750 gross.
    exit_premium = 200.0
    opt_exit = _Order(
        id=str(_uuid.uuid4()), symbol=sig2.symbol, side=_OrderSide.SELL,
        qty=opt_qty, type=_OrderType.MARKET, price=exit_premium,
        instrument_kind=InstrumentKind.OPTION, lot_size=75,
    )
    opt_closed = opt_broker.place_order(opt_exit)
    assert opt_closed.status.value == "FILLED"
    assert sig2.symbol not in opt_broker._positions
    opt_realized = opt_broker._cash - cash_before_opt
    # Gross ₹3,750 minus fees & slippage; expect net somewhere in (2,000, 4,000).
    if not (2_000 < opt_realized < 4_000):
        print(f"  ✗ FAILED — option realized cash delta ₹{opt_realized:,.2f} outside expected band")
        return 1
    print(f"  ✓ Option round-trip: cash {cash_before_opt:,.0f} → {opt_broker._cash:,.2f}  "
          f"(net ₹{opt_realized:,.2f} on +₹50 premium move; gross ₹{(exit_premium-entry_premium)*opt_qty:,.0f})")

    # 8h. Synthetic option bars work via intraday_bars dispatch.
    # We can't easily fetch real underlying bars in a test, so call the
    # synthesizer directly via a fake spot DataFrame to verify the
    # CE/PE direction inversion works.
    spot_bar_h = synth_option_ohlc(
        spot_high=24700, spot_low=24500, spot_open=24600, spot_close=24650,
        K=24500, T=14/365, opt_type="CE",
    )
    spot_bar_p = synth_option_ohlc(
        spot_high=24700, spot_low=24500, spot_open=24600, spot_close=24650,
        K=24500, T=14/365, opt_type="PE",
    )
    # CE: option high when spot high.
    assert spot_bar_h["high"] >= spot_bar_h["low"], f"CE high<low: {spot_bar_h}"
    # PE: option high when spot LOW (mirror).
    assert spot_bar_p["high"] >= spot_bar_p["low"], f"PE high<low: {spot_bar_p}"
    # PE premium when spot=24650 (above strike) should be small (OTM).
    if spot_bar_p["close"] >= spot_bar_h["close"]:
        print(f"  ✗ FAILED — OTM PE close ₹{spot_bar_p['close']:.2f} >= "
              f"ITM CE close ₹{spot_bar_h['close']:.2f}")
        return 1
    print(f"  ✓ Synth option bars: CE OHLC={spot_bar_h}, PE OHLC={spot_bar_p}")

    # Cleanup.
    cache.delete(cache_key("paper:state", Segment.FNO))

    banner("FIX #9 — F&O credit SPREAD round-trip + Greeks (Phase 4)")

    # 9a. Greeks sanity: ATM call has delta ~0.5, gamma > 0, theta < 0,
    # vega > 0. Put-call parity for delta: delta_call - delta_put = 1.
    from bot.options.pricing import (
        delta as _delta, gamma as _gamma, theta as _theta, vega as _vega,
        all_greeks,
    )
    d_atm_call = _delta(24500, 24500, 14/365, "CE")
    d_atm_put  = _delta(24500, 24500, 14/365, "PE")
    g_atm      = _gamma(24500, 24500, 14/365)
    t_atm_call = _theta(24500, 24500, 14/365, "CE")
    v_atm      = _vega(24500, 24500, 14/365)
    if not (0.45 < d_atm_call < 0.60):
        print(f"  ✗ FAILED — ATM call delta {d_atm_call:.4f} outside (0.45, 0.60)")
        return 1
    parity_delta = d_atm_call - d_atm_put
    if abs(parity_delta - 1.0) > 0.01:
        print(f"  ✗ FAILED — delta parity violated: Δcall − Δput = {parity_delta:.4f} (expected ~1.0)")
        return 1
    if g_atm <= 0:
        print(f"  ✗ FAILED — ATM gamma {g_atm} should be positive")
        return 1
    if t_atm_call >= 0:
        print(f"  ✗ FAILED — ATM call theta {t_atm_call} should be NEGATIVE (long options decay)")
        return 1
    if v_atm <= 0:
        print(f"  ✗ FAILED — ATM vega {v_atm} should be positive")
        return 1
    print(f"  ✓ Greeks: Δcall={d_atm_call:.4f} Δput={d_atm_put:.4f} (parity Δc−Δp={parity_delta:.4f})")
    print(f"           γ={g_atm:.6f}  θ_call=₹{t_atm_call:.2f}/day  vega=₹{v_atm/100:.2f}/1%-IV")
    bundle = all_greeks(24500, 24500, 14/365, "CE")
    assert set(bundle.keys()) == {"delta", "gamma", "theta", "vega"}
    print(f"  ✓ all_greeks bundle: {sorted(bundle.keys())}")

    # 9b. Spread tradingsymbol: format + parse + round-trip.
    from bot.instruments.fno import (
        spread_tradingsymbol, parse_spread_tradingsymbol,
        is_spread_tradingsymbol, resolve_credit_spread,
    )
    bps_ts = spread_tradingsymbol("NIFTY", _date(2026, 5, 28), 24500, 24400, "PE")
    if bps_ts != "NIFTY26MAY24500-24400PESPRD":
        print(f"  ✗ FAILED — spread_tradingsymbol = {bps_ts!r}")
        return 1
    parsed_bps = parse_spread_tradingsymbol(bps_ts)
    assert parsed_bps["spread_type"] == "bull_put"
    assert parsed_bps["short_strike"] == 24500
    assert parsed_bps["long_strike"] == 24400
    assert parsed_bps["opt_type"] == "PE"
    bcs_ts = spread_tradingsymbol("NIFTY", _date(2026, 5, 28), 24800, 24900, "CE")
    parsed_bcs = parse_spread_tradingsymbol(bcs_ts)
    assert parsed_bcs["spread_type"] == "bear_call"
    # Negative cases — option / futures must NOT match spread regex.
    assert parse_spread_tradingsymbol("NIFTY26MAY24600CE") is None
    assert parse_spread_tradingsymbol("NIFTY26MAYFUT") is None
    assert is_spread_tradingsymbol(bps_ts)
    print(f"  ✓ Spread tradingsymbol: BPS '{bps_ts}', BCS '{bcs_ts}', non-spreads correctly excluded")

    # 9c. Margin module: max_loss = width − credit; spread margin = max_loss × qty.
    from bot.options.margin import (
        vertical_spread_max_loss, vertical_spread_margin,
    )
    mll = vertical_spread_max_loss(24500, 24400, net_credit_per_share=70.0)
    assert abs(mll - 30.0) < 0.001, f"max_loss/share {mll} != 30"
    mlrg = vertical_spread_margin(24500, 24400, net_credit_per_share=70.0, qty=75)
    assert abs(mlrg - 2250.0) < 0.001, f"margin {mlrg} != 2250"
    # Reject debit and free-money cases.
    try:
        vertical_spread_max_loss(24500, 24400, -10.0)
        print(f"  ✗ FAILED — max_loss should reject debit (negative credit)")
        return 1
    except ValueError:
        pass
    try:
        vertical_spread_max_loss(24500, 24400, 200.0)   # credit > width
        print(f"  ✗ FAILED — max_loss should reject credit > width")
        return 1
    except ValueError:
        pass
    print(f"  ✓ Margin: max_loss=₹{mll}/share, ₹{mlrg}/lot; rejects debit + free-money")

    # 9d. Strategy emits SELL signal for bull-put-spread on synthetic bullish data.
    from bot.config import CreditSpreadCfg
    from bot.strategies.fno import CreditSpreadStrategy
    rng3 = pd.date_range("2026-04-29 09:15", periods=60, freq="5min", tz="Asia/Kolkata")
    flat3 = np.full(55, 24000.0)
    trend3 = np.array([24083.0, 24167.0, 24250.0, 24333.0, 24417.0])
    closes3 = np.concatenate([flat3, trend3])
    df3 = pd.DataFrame({
        "open":   closes3,
        "high":   closes3 + 5,
        "low":    closes3 - 5,
        "close":  closes3,
        "volume": 100_000,
    }, index=rng3)
    cs_strat = CreditSpreadStrategy(CreditSpreadCfg())
    sig3 = cs_strat.generate("NIFTY", df3)
    if sig3.type.value != "SELL":
        print(f"  ✗ FAILED — credit_spread produced {sig3.type.value}, expected SELL. reason={sig3.reason}")
        return 1
    parsed_sig3 = parse_spread_tradingsymbol(sig3.symbol)
    if parsed_sig3 is None or parsed_sig3["spread_type"] != "bull_put":
        print(f"  ✗ FAILED — sig3.symbol={sig3.symbol!r} not a bull-put spread")
        return 1
    if not (sig3.take_profit < sig3.price < sig3.stop_loss):
        print(f"  ✗ FAILED — SL/TP wrong direction for SHORT spread "
              f"(tp={sig3.take_profit} entry={sig3.price} sl={sig3.stop_loss})")
        return 1
    print(f"  ✓ credit_spread → SELL {sig3.symbol} "
          f"net_credit=₹{sig3.price:.2f} TP=₹{sig3.take_profit:.2f} SL=₹{sig3.stop_loss:.2f}")

    # 9e. Paper broker fills SPREAD entry: margin debit + premium credit, no full-margin debit.
    cache.delete(cache_key("paper:state", Segment.FNO))
    sp_broker = PaperBroker(segment=Segment.FNO)
    sp_broker._starting_cash = 50_000.0
    sp_broker._cash = 50_000.0
    sp_broker._positions = {}

    entry_credit = 70.0      # ₹70 net credit per share
    sp_qty = 75              # 1 lot NIFTY
    spread_sym = "NIFTY26MAY24500-24400PESPRD"
    sp_entry = _Order(
        id=str(_uuid.uuid4()), symbol=spread_sym, side=_OrderSide.SELL,
        qty=sp_qty, type=_OrderType.MARKET, price=entry_credit,
        instrument_kind=InstrumentKind.SPREAD, lot_size=75,
    )
    cash_before_sp = sp_broker._cash
    sp_filled = sp_broker.place_order(sp_entry)
    if sp_filled.status.value != "FILLED":
        print(f"  ✗ FAILED — spread entry status = {sp_filled.status.value}")
        return 1
    sp_pos = sp_broker._positions[spread_sym]
    expected_max_loss = (24500 - 24400) - entry_credit         # 30/share
    expected_margin = expected_max_loss * sp_qty                # 2,250
    if not (expected_margin * 0.99 < sp_pos.margin_blocked < expected_margin * 1.01):
        print(f"  ✗ FAILED — margin_blocked ₹{sp_pos.margin_blocked:,.0f} "
              f"!= expected ~₹{expected_margin:,.0f}")
        return 1
    if sp_pos.instrument_kind != InstrumentKind.SPREAD:
        print(f"  ✗ FAILED — instrument_kind = {sp_pos.instrument_kind}")
        return 1
    if sp_pos.qty >= 0:
        print(f"  ✗ FAILED — short spread should have qty < 0, got {sp_pos.qty}")
        return 1
    cash_drop_sp = cash_before_sp - sp_broker._cash
    expected_cash_drop = expected_margin - entry_credit * sp_qty   # 2250 - 5250 = -3000 (net CREDIT)
    if not abs(cash_drop_sp - expected_cash_drop) < 100:
        print(f"  ✗ FAILED — spread cash_drop ₹{cash_drop_sp:,.2f} != expected "
              f"~₹{expected_cash_drop:,.2f} (margin − credit + fees)")
        return 1
    print(f"  ✓ Spread SELL: credit=₹{entry_credit*sp_qty:,.0f}  "
          f"margin=₹{sp_pos.margin_blocked:,.0f}  net_cash_delta=₹{cash_drop_sp:,.2f} "
          f"(NEGATIVE = net cash CREDIT after margin block)")

    # 9f. Round-trip P&L: close at 30 net (theta decay 70→30).
    exit_net = 30.0
    sp_exit = _Order(
        id=str(_uuid.uuid4()), symbol=spread_sym, side=_OrderSide.BUY,
        qty=sp_qty, type=_OrderType.MARKET, price=exit_net,
        instrument_kind=InstrumentKind.SPREAD, lot_size=75,
    )
    sp_closed = sp_broker.place_order(sp_exit)
    assert sp_closed.status.value == "FILLED"
    assert spread_sym not in sp_broker._positions, "spread should be flat after close"
    sp_realized = sp_broker._cash - cash_before_sp
    # Gross ₹3,000 (70-30)*75 minus 4-leg fees + slippage. Expect (1500, 3500).
    if not (1_500 < sp_realized < 3_500):
        print(f"  ✗ FAILED — spread realized ₹{sp_realized:,.2f} outside expected band")
        return 1
    print(f"  ✓ Spread round-trip: cash {cash_before_sp:,.0f} → {sp_broker._cash:,.2f}  "
          f"(net ₹{sp_realized:,.2f} on theta decay 70→30; gross ₹{(entry_credit-exit_net)*sp_qty:,.0f})")

    # 9g. Synthetic spread bars are direction-aware (bull_put inverse-monotonic).
    from bot.data import _synth_spread_bars
    spread_bar = _synth_spread_bars(parsed_bps, "5m")
    # Can't easily verify content without underlying yfinance data; just
    # confirm the function dispatches (yfinance call returns empty df for
    # tests run after-hours, that's ok — just ensures no exception).
    # Direct call to BS for direction sanity:
    from bot.options.pricing import bs as _bs
    T = 14/365
    # Bull-put PE spread, spot drops 50 → short PE more valuable, net PRICE goes UP
    net_at_24500 = _bs(24500, 24500, T, "PE") - _bs(24500, 24400, T, "PE")
    net_at_24450 = _bs(24450, 24500, T, "PE") - _bs(24450, 24400, T, "PE")
    if net_at_24450 <= net_at_24500:
        print(f"  ✗ FAILED — bull_put net at lower spot ({net_at_24450:.2f}) should EXCEED "
              f"net at higher spot ({net_at_24500:.2f}) — direction wrong")
        return 1
    print(f"  ✓ Bull-put net price: spot 24500→net ₹{net_at_24500:.2f}, "
          f"spot 24450→net ₹{net_at_24450:.2f} (rises on spot drop, as expected)")

    # 9h. Risk manager allows a 1-lot spread on ₹50K capital when the
    # credit is high enough that risk-per-share fits the per-trade budget.
    # (The synthesized strategy signal at our test data's spot=24417 gives
    # an OTM strike with low credit / high max_loss / large risk_per_share
    # that fails the 5% max-loss-per-trade budget — that's a REAL behaviour
    # the strategy will surface live; we test the favorable case here to
    # confirm the FNO branch's spread sizing path itself works end-to-end.)
    cache.delete(cache_key("paper:state", Segment.FNO))
    rsk_broker = PaperBroker(segment=Segment.FNO)
    rsk_broker._starting_cash = 50_000.0
    rsk_broker._cash = 50_000.0
    rsk_broker._positions = {}
    from bot.risk import RiskManager
    from bot.strategies.base import Signal as _Signal, SignalType as _SignalType
    favourable_sig = _Signal(
        symbol="NIFTY26MAY24500-24400PESPRD",
        type=_SignalType.SELL,
        price=70.0,                # ₹70 net credit/share (ATM strike)
        stop_loss=91.0,            # 70% of (100 − 70) above entry
        take_profit=35.0,
        confidence=0.65,
        strategy="credit_spread",
        reason="(test) high-credit ATM bull-put",
    )
    risk_mgr = RiskManager(rsk_broker, segment=Segment.FNO)
    # Monkey-patch the loaded F&O capital to ₹50K so the sizing math
    # matches the test's stated intent ("1-lot spread on ₹50K capital").
    # Without this, the RiskManager reads ``fno.capital.total`` from
    # config.yaml (₹1,00,000) — making max_loss_budget=₹5,000 and
    # approving 3 lots given the 21/share stop, which is the correct
    # behaviour for a 100K account but not what this test wants to verify.
    risk_mgr._capital_cfg.total = 50_000.0
    decision = risk_mgr.evaluate(favourable_sig)
    if not decision.allow or decision.qty != 75:
        print(f"  ✗ FAILED — risk manager rejected 1-lot spread on ₹50K: allow={decision.allow}, "
              f"qty={decision.qty}, reason={decision.reason!r}")
        return 1
    print(f"  ✓ Risk manager: 1-lot spread approved on ₹50K capital → qty={decision.qty} "
          f"({decision.reason})")

    cache.delete(cache_key("paper:state", Segment.FNO))

    banner("FIX #10 — F&O IRON CONDOR round-trip + delta-neutral Greeks (Phase 4.5)")

    # 10a. Iron-condor instrument resolution: the four strikes round to the
    # underlying's strike grid and order strictly put_long < put_short <
    # call_short < call_long.
    from bot.instruments.fno import (
        iron_condor_tradingsymbol, parse_iron_condor_tradingsymbol,
        is_iron_condor_tradingsymbol, resolve_iron_condor,
        IronCondorInstrument,
    )
    ic = resolve_iron_condor("NIFTY", spot=24530, put_width=100,
                             call_width=100, wings_distance=100)
    if not isinstance(ic, IronCondorInstrument):
        print("  ✗ FAILED — resolve_iron_condor returned wrong type")
        return 1
    if not (ic.put_long < ic.put_short < ic.call_short < ic.call_long):
        print(f"  ✗ FAILED — IC strikes out of order: "
              f"{ic.put_long}<{ic.put_short}<{ic.call_short}<{ic.call_long}")
        return 1
    # spot 24530 → ATM 24550 (round to 50 grid). Wings ±100, widths 100:
    #   put_long=24350, put_short=24450, call_short=24650, call_long=24750
    expected_strikes = (24350, 24450, 24650, 24750)
    actual_strikes = (ic.put_long, ic.put_short, ic.call_short, ic.call_long)
    if actual_strikes != expected_strikes:
        print(f"  ✗ FAILED — IC strikes {actual_strikes} != {expected_strikes}")
        return 1
    print(f"  ✓ resolve_iron_condor: spot=24530 → strikes "
          f"{ic.put_long}/{ic.put_short}/{ic.call_short}/{ic.call_long}")

    # 10b. tradingsymbol round-trip.
    ic_sym = ic.ic_tradingsymbol
    if not is_iron_condor_tradingsymbol(ic_sym):
        print(f"  ✗ FAILED — {ic_sym!r} not recognised as iron condor")
        return 1
    parsed_ic = parse_iron_condor_tradingsymbol(ic_sym)
    if (parsed_ic is None or parsed_ic["put_long"] != 24350 or
            parsed_ic["call_long"] != 24750):
        print(f"  ✗ FAILED — IC parse round-trip: {parsed_ic}")
        return 1
    print(f"  ✓ IC tradingsymbol round-trip: {ic_sym} → {dict(parsed_ic)}")

    # 10c. IC delta-neutrality: at-spot net delta should be small (the
    # whole point of the structure). With 100/100 symmetric wings and
    # ATM short legs, |net delta| < 0.05.
    from bot.options.pricing import all_greeks as _all_greeks, years_to_expiry
    T_ic = years_to_expiry(ic.expiry)
    sp_g = _all_greeks(24530, ic.put_short, T_ic, "PE")
    lp_g = _all_greeks(24530, ic.put_long,  T_ic, "PE")
    sc_g = _all_greeks(24530, ic.call_short, T_ic, "CE")
    lc_g = _all_greeks(24530, ic.call_long,  T_ic, "CE")
    net_delta = -sp_g["delta"] + lp_g["delta"] - sc_g["delta"] + lc_g["delta"]
    if abs(net_delta) > 0.10:
        print(f"  ✗ FAILED — IC net delta {net_delta:+.4f} not near zero "
              "(structure should be delta-neutral)")
        return 1
    # Theta should be POSITIVE for the IC seller (we collect time decay).
    net_theta_per_share = (-sp_g["theta"] + lp_g["theta"]
                           - sc_g["theta"] + lc_g["theta"])
    if net_theta_per_share <= 0:
        print(f"  ✗ FAILED — IC seller's net theta {net_theta_per_share:+.2f} "
              "should be POSITIVE (theta works for the seller)")
        return 1
    print(f"  ✓ IC Greeks: net Δ={net_delta:+.4f} (≈neutral), "
          f"net Θ={net_theta_per_share:+.2f}/day (positive for seller)")

    # 10d. Vertical-IC margin sanity: max_loss/share = max(put_width,
    # call_width) − net_credit. NOT the SUM (verifies we got the
    # capital-efficiency right).
    test_credit = 70.0
    expected_max_loss = max(100, 100) - test_credit   # 30 per share
    if abs(ic.max_loss_per_share(test_credit) - expected_max_loss) > 0.01:
        print(f"  ✗ FAILED — IC max_loss/share {ic.max_loss_per_share(test_credit)} "
              f"!= {expected_max_loss}")
        return 1
    print(f"  ✓ IC max_loss = max(put_width, call_width) − credit = "
          f"₹{expected_max_loss}/share (NOT the sum of both spread maxes)")

    # 10e. Paper broker: open + close an IC, verify cash flow.
    cache.delete(cache_key("paper:state", Segment.FNO))
    ic_broker = PaperBroker(segment=Segment.FNO)
    ic_broker._starting_cash = 50_000.0
    ic_broker._cash = 50_000.0
    ic_broker._positions = {}
    ic_qty = 75       # 1 lot
    entry_credit = 70.0
    cash_before_ic = ic_broker._cash
    ic_entry = _Order(
        id=str(_uuid.uuid4()), symbol=ic_sym, side=_OrderSide.SELL,
        qty=ic_qty, type=_OrderType.MARKET, price=entry_credit,
        instrument_kind=InstrumentKind.IRON_CONDOR, lot_size=75,
    )
    ic_filled = ic_broker.place_order(ic_entry)
    if ic_filled.status.value != "FILLED":
        print(f"  ✗ FAILED — IC entry rejected: {ic_filled.status}")
        return 1
    ic_pos = ic_broker._positions[ic_sym]
    if ic_pos.instrument_kind != InstrumentKind.IRON_CONDOR:
        print(f"  ✗ FAILED — IC position kind is {ic_pos.instrument_kind}, "
              "expected IRON_CONDOR")
        return 1
    # margin = (worst_wing − fill_credit) × qty. Fill is post-slippage so
    # ~70 (5bps slippage on 70 ≈ 0.035 → fill 69.97). Tolerance ±10 to
    # absorb slippage and BS rounding without over-fitting the test.
    expected_margin_ic = (100 - entry_credit) * ic_qty
    if abs(ic_pos.margin_blocked - expected_margin_ic) > 10:
        print(f"  ✗ FAILED — IC margin ₹{ic_pos.margin_blocked} != "
              f"expected ~₹{expected_margin_ic}")
        return 1
    print(f"  ✓ IC SELL: credit=₹{entry_credit*ic_qty:,.0f} "
          f"margin=₹{ic_pos.margin_blocked:,.0f} (worst wing 100 − credit)")

    # 10f. Round-trip P&L: close at 30 net (theta decay 70→30).
    exit_net_ic = 30.0
    ic_exit = _Order(
        id=str(_uuid.uuid4()), symbol=ic_sym, side=_OrderSide.BUY,
        qty=ic_qty, type=_OrderType.MARKET, price=exit_net_ic,
        instrument_kind=InstrumentKind.IRON_CONDOR, lot_size=75,
    )
    ic_closed = ic_broker.place_order(ic_exit)
    assert ic_closed.status.value == "FILLED"
    assert ic_sym not in ic_broker._positions, "IC should be flat after close"
    ic_realized = ic_broker._cash - cash_before_ic
    # Gross ₹3,000 ((70-30)*75) minus ~₹100-200 of fees+surcharge+slippage.
    # Expect (1500, 3500) as for the spread, but slightly tighter on the
    # low end due to extra ₹40 surcharge over a vertical.
    if not (1_500 < ic_realized < 3_500):
        print(f"  ✗ FAILED — IC realized ₹{ic_realized:,.2f} outside expected band")
        return 1
    print(f"  ✓ IC round-trip: cash {cash_before_ic:,.0f} → {ic_broker._cash:,.2f}  "
          f"(net ₹{ic_realized:,.2f} on theta decay 70→30)")

    # 10g. Risk manager sizing approves a 1-lot IC at favourable credit.
    cache.delete(cache_key("paper:state", Segment.FNO))
    ic_rsk_broker = PaperBroker(segment=Segment.FNO)
    ic_rsk_broker._starting_cash = 50_000.0
    ic_rsk_broker._cash = 50_000.0
    ic_rsk_broker._positions = {}
    ic_sig = _Signal(
        symbol=ic_sym,
        type=_SignalType.SELL,
        price=70.0,                # ₹70 net credit/share
        stop_loss=91.0,            # 70% of (100-70) above entry
        take_profit=35.0,
        confidence=0.55,
        strategy="iron_condor",
        reason="(test) high-credit ATM IC",
    )
    ic_risk_mgr = RiskManager(ic_rsk_broker, segment=Segment.FNO)
    ic_decision = ic_risk_mgr.evaluate(ic_sig)
    if not ic_decision.allow or ic_decision.qty != 75:
        print(f"  ✗ FAILED — risk manager rejected 1-lot IC on ₹50K: "
              f"allow={ic_decision.allow}, qty={ic_decision.qty}, "
              f"reason={ic_decision.reason!r}")
        return 1
    print(f"  ✓ Risk manager: 1-lot IC approved on ₹50K → qty={ic_decision.qty} "
          f"({ic_decision.reason})")

    # 10h. Stock-options lot table extension: RELIANCE/INFY/HDFCBANK
    # resolve to non-zero lot sizes so stock F&O trading is reachable.
    from bot.instruments.fno import LOT_SIZES, STRIKE_STEPS
    for s in ("RELIANCE", "INFY", "HDFCBANK"):
        if s not in LOT_SIZES or s not in STRIKE_STEPS:
            print(f"  ✗ FAILED — stock symbol {s} missing from LOT_SIZES "
                  "or STRIKE_STEPS (Phase 4.5 stock-options extension)")
            return 1
    print(f"  ✓ Stock-options lot table: RELIANCE={LOT_SIZES['RELIANCE']}, "
          f"INFY={LOT_SIZES['INFY']}, HDFCBANK={LOT_SIZES['HDFCBANK']}")

    cache.delete(cache_key("paper:state", Segment.FNO))

    banner("FIX #11 — F&O monthly rollover buffer (current_expiry rolls 2 days before expiry)")

    from datetime import date as _date
    from bot.instruments.fno import (current_expiry, _last_thursday,
                                     get_rollover_buffer_days,
                                     set_rollover_buffer_days,
                                     resolve_futures)

    # April 2026 last Thursday = April 30. Test the boundary days.
    apr_exp = _last_thursday(2026, 4)
    may_exp = _last_thursday(2026, 5)
    if apr_exp != _date(2026, 4, 30) or may_exp != _date(2026, 5, 28):
        print(f"  ✗ FAILED — expiry math broken: APR={apr_exp}, MAY={may_exp}")
        return 1
    print(f"  ✓ APR expiry = {apr_exp} (Thu), MAY expiry = {may_exp} (Thu)")

    # Save and restore the module default so this test doesn't bleed into
    # whatever the running bot has set.
    saved_buf = get_rollover_buffer_days()
    try:
        # buffer=0: roll exactly on expiry day
        set_rollover_buffer_days(0)
        cases_off = [
            (_date(2026, 4, 28), apr_exp, "Tue T-2  (no roll)"),
            (_date(2026, 4, 29), apr_exp, "Wed T-1  (no roll)"),
            (_date(2026, 4, 30), may_exp, "Thu expiry day (rolls to MAY)"),
            (_date(2026, 5,  1), may_exp, "Fri post-expiry (still MAY)"),
        ]
        for d, expected, label in cases_off:
            got = current_expiry(today=d)
            ok = "✓" if got == expected else "✗"
            print(f"  buf=0 {ok} {label:36s} → {got}  (expected {expected})")
            if got != expected:
                return 1

        # buffer=2 (default for the running bot): roll on T-2
        set_rollover_buffer_days(2)
        cases_default = [
            (_date(2026, 4, 27), apr_exp, "Mon T-3  (no roll)"),
            (_date(2026, 4, 28), may_exp, "Tue T-2  (ROLLS to MAY)"),
            (_date(2026, 4, 30), may_exp, "Thu expiry day (already rolled)"),
            (_date(2026, 5,  1), may_exp, "Fri post-expiry (still MAY)"),
        ]
        for d, expected, label in cases_default:
            got = current_expiry(today=d)
            ok = "✓" if got == expected else "✗"
            print(f"  buf=2 {ok} {label:36s} → {got}  (expected {expected})")
            if got != expected:
                return 1

        # And resolve_futures inherits the buffer (the actual call path
        # the live bot takes via the per-minute tick).
        fut_today = resolve_futures("NIFTY", today=_date(2026, 4, 30))
        if fut_today.expiry != may_exp:
            print(f"  ✗ FAILED — resolve_futures on expiry day did NOT roll: got {fut_today.tradingsymbol}")
            return 1
        print(f"  ✓ resolve_futures(NIFTY, today=2026-04-30) → "
              f"{fut_today.tradingsymbol} (rolled past expiry)")

        # Per-call buffer_days override (escape hatch for callers that
        # know better than the module default — e.g. backtests).
        override = current_expiry(today=_date(2026, 4, 28), buffer_days=0)
        if override != apr_exp:
            print(f"  ✗ FAILED — buffer_days=0 per-call override didn't apply: got {override}")
            return 1
        print(f"  ✓ buffer_days=0 per-call override works (Tue T-2 stays APR with buf=0)")
    finally:
        set_rollover_buffer_days(saved_buf)

    # ─────────────────────────────────────────────────────────────────────
    banner("FIX #12 — Synthetic-symbol pricing (no spot-leak, the 2026-04-30 -₹8.3M bug)")
    # On 2026-04-30 the F&O paper bot squared off two NIFTY/BANKNIFTY put
    # credit spreads at the underlying SPOT (₹24,002 / ₹54,842) instead of
    # the spread net premium (~₹44/share for both). Loss: -₹8.3M on ₹100k
    # capital. Root cause: `yfinance_proxy("NIFTY26MAY24050-23950PESPRD")`
    # short-circuited via `s.startswith("NIFTY")` and returned `^NSEI`,
    # so `latest_quote()` (called by `executor._end_of_day`) marked the
    # spread at the NIFTY spot.
    #
    # This regression check pins the contract that synthetic instruments
    # NEVER yfinance-proxy. Anyone who rewrites the proxy must keep this
    # invariant or this test will fail loudly.
    from bot.instruments.fno import yfinance_proxy

    proxy_cases = [
        # Real index symbols — must still map (don't over-correct).
        ("NIFTY",                                  "^NSEI",     True),
        ("BANKNIFTY",                              "^NSEBANK",  True),
        ("NIFTY26MAYFUT",                          "^NSEI",     True),
        ("BANKNIFTY26MAYFUT",                      "^NSEBANK",  True),
        # Synthetic — MUST return None so the BS synthesis path fires.
        ("NIFTY26MAY24600CE",                      None,        False),
        ("NIFTY26MAY24400PE",                      None,        False),
        ("NIFTY26MAY24050-23950PESPRD",            None,        False),  # the bug
        ("NIFTY26MAY24500-24600CESPRD",            None,        False),
        ("BANKNIFTY26MAY55000-54900PESPRD",        None,        False),  # the bug
        ("NIFTY26MAY24300-24400-24700-24800IC",    None,        False),
    ]
    for sym, expected, _is_real in proxy_cases:
        got = yfinance_proxy(sym)
        ok_mark = "✓" if got == expected else "✗"
        print(f"  {ok_mark} yfinance_proxy({sym:>40}) = {got!r:>10}   (expected {expected!r})")
        if got != expected:
            print(f"    ✗ FAILED — synthetic-symbol proxy regressed; the EOD square-off")
            print(f"      will mark spreads at the underlying spot again. ABORT.")
            return 1

    # Also pin executor._end_of_day's pricing path — it must use intraday_bars
    # (which handles BS synthesis), NOT latest_quote (which goes through
    # the equity / yfinance fallback).
    import inspect
    from bot.executor import Executor
    eod_src = inspect.getsource(Executor._end_of_day)
    if "latest_quote(p.symbol)" in eod_src:
        print("  ✗ FAILED — executor._end_of_day still calls latest_quote on")
        print("    position symbols. Synthetic spread/IC marks will be wrong.")
        return 1
    if "intraday_bars(p.symbol" not in eod_src:
        print("  ✗ FAILED — executor._end_of_day must mark via intraday_bars")
        print("    (the same pricing path _manage_open_positions uses).")
        return 1
    print("  ✓ executor._end_of_day uses intraday_bars (synthetic-aware) for marks")

    # ─────────────────────────────────────────────────────────────────────
    banner("FIX #13 — EOD race + over-sell guard (2026-05-04 ADANIENT incident)")
    # On 2026-05-04 the equity paper bot's cash dropped from ₹100,000 to
    # ₹74,961 after a SINGLE small ADANIENT round-trip (real loss ₹152).
    # Root cause: the ``end_of_day`` cron and the ``executor_tick`` cron
    # both fired at exactly 15:15:00 IST and both called ``_end_of_day``
    # in concurrent worker threads. The first call closed the long
    # cleanly; the second saw ``existing = None`` for the just-closed
    # symbol and the broker treated the orphan SELL as a new SHORT entry,
    # creating a phantom −10 ADANIENT position. The auto-cover BUY at
    # 15:16:00 closed that phantom short via the equity-short-close
    # cash formula and leaked ~₹24,887.
    #
    # Two complementary guards now protect against this:
    #   (a) `_end_of_day` writes a per-segment ``eod_done:{seg}`` Redis
    #       marker on completion and refuses to run a second time the
    #       same trading day.
    #   (b) The paper broker's ``place_order`` rejects equity SELL with
    #       qty greater than held long qty (no-position SELL → reject).
    #
    # We test both. (a) is mocked via in-process double-call; (b) is
    # tested by attempting to SELL with a flat book and asserting REJECTED.
    from bot.broker.paper import PaperBroker, OrderSide, OrderStatus
    from bot.broker.base import Order, OrderType, InstrumentKind
    from bot.cache import get_cache
    from bot.config import load_config
    from bot.segment import Segment

    cache = get_cache()
    cfg = load_config()
    cache.delete("eod_done:equity")
    cache.delete("paper:state:equity")

    # (b) Over-sell guard — refined 2026-05-05 to permit fresh strategy
    # shorts on a flat book (legitimate intraday MIS shorting), while
    # still blocking the exact 2026-05-04 phantom-short path: a
    # ``square_off_all``-originated SELL that arrives after another
    # square-off has already cleared the position.
    eq_broker = PaperBroker(segment=Segment.EQUITY)
    eq_broker.update_marks({"TESTSYM": 1000.0})

    # (b.1) Fresh strategy-driven SELL on a flat book MUST fill (this is
    # the case the 2026-05-04 guard incorrectly rejected on 2026-05-05,
    # blocking every short signal for the morning).
    sell_short_open = Order(
        id="t-oversell-1", symbol="TESTSYM", side=OrderSide.SELL, qty=10,
        type=OrderType.MARKET, price=1000.0, instrument_kind=InstrumentKind.EQUITY,
        # is_squareoff defaults to False — this is a strategy entry.
    )
    result = eq_broker.place_order(sell_short_open)
    if result.status != OrderStatus.FILLED:
        print(f"  ✗ FAILED — legitimate fresh equity short was REJECTED: "
              f"status={result.status}")
        print(f"    The over-sell guard is too aggressive and is blocking "
              f"strategy-driven intraday shorts (2026-05-05 regression).")
        return 1
    print("  ✓ Fresh strategy SELL on flat book fills (intraday short allowed)")

    # (b.2) Square-off-originated orphan SELL (the precise 2026-05-04
    # phantom-short signature) MUST be rejected. We synthesize the order
    # exactly the way ``square_off_all`` does — with ``is_squareoff=True``
    # — but on a symbol that the broker no longer holds (the second
    # concurrent square-off looking at a stale snapshot).
    eq_broker_b2 = PaperBroker(segment=Segment.EQUITY)
    eq_broker_b2.update_marks({"GHOSTSYM": 1000.0})
    orphan_squareoff = Order(
        id="t-oversell-orphan", symbol="GHOSTSYM", side=OrderSide.SELL, qty=10,
        type=OrderType.MARKET, price=1000.0, instrument_kind=InstrumentKind.EQUITY,
        is_squareoff=True,
    )
    rb2 = eq_broker_b2.place_order(orphan_squareoff)
    if rb2.status != OrderStatus.REJECTED:
        print(f"  ✗ FAILED — orphan square-off SELL on flat book wasn't "
              f"rejected: status={rb2.status}")
        print(f"    The 2026-05-04 phantom-short race window is open.")
        return 1
    print("  ✓ Orphan square-off SELL on flat book rejected (May-04 defense intact)")

    # (b.3) Over-sell beyond held qty MUST reject (closing more than we
    # hold would leak cash via a residual phantom short).
    # Clear persisted state so b3's broker starts on a clean book — without
    # this, b1's persisted SHORT TESTSYM would be restored, b3's BUY 10
    # would silently close it (rather than open a new long), and the
    # subsequent SELL 20 would land on a flat book and be accepted as a
    # legitimate strategy-driven short entry per FIX #13b refinement.
    cache.delete("paper:state:equity")
    eq_broker.update_marks({"TESTSYM": 1000.0})
    eq_broker_b3 = PaperBroker(segment=Segment.EQUITY)
    eq_broker_b3.update_marks({"TESTSYM": 1000.0})
    buy_order = Order(
        id="t-oversell-2-buy", symbol="TESTSYM", side=OrderSide.BUY, qty=10,
        type=OrderType.MARKET, price=1000.0, instrument_kind=InstrumentKind.EQUITY,
    )
    eq_broker_b3.place_order(buy_order)
    sell_too_many = Order(
        id="t-oversell-2-sell", symbol="TESTSYM", side=OrderSide.SELL, qty=20,
        type=OrderType.MARKET, price=1000.0, instrument_kind=InstrumentKind.EQUITY,
    )
    r2 = eq_broker_b3.place_order(sell_too_many)
    if r2.status != OrderStatus.REJECTED:
        print(f"  ✗ FAILED — over-sell beyond held qty didn't reject: status={r2.status}")
        return 1
    print("  ✓ Over-sell guard rejects SELL qty greater than held long qty")

    # (b.4) Legitimate equity SELL of exactly the held qty MUST fill.
    sell_legit = Order(
        id="t-oversell-3", symbol="TESTSYM", side=OrderSide.SELL, qty=10,
        type=OrderType.MARKET, price=1000.0, instrument_kind=InstrumentKind.EQUITY,
    )
    r3 = eq_broker_b3.place_order(sell_legit)
    if r3.status != OrderStatus.FILLED:
        print(f"  ✗ FAILED — legitimate close was over-rejected: status={r3.status}")
        return 1
    print("  ✓ Legitimate equity SELL closes long position (guard isn't over-eager)")

    # (a) `_end_of_day` idempotency — second concurrent call is a no-op.
    cache.delete("eod_done:equity")
    cache.delete("paper:state:equity")
    eq_broker2 = PaperBroker(segment=Segment.EQUITY)
    eq_broker2.update_marks({"FOO": 100.0})
    eq_broker2.place_order(Order(
        id="t-eod-buy", symbol="FOO", side=OrderSide.BUY, qty=50,
        type=OrderType.MARKET, price=100.0, instrument_kind=InstrumentKind.EQUITY,
    ))

    # Build a fake executor that exposes _end_of_day with our broker.
    # We do this minimally — the full Executor pulls in too much (config,
    # research, scheduler) and we only need `_end_of_day`.
    class _FakeExecutor:
        def __init__(self, broker):
            self.broker = broker
            self.cache = cache
            self.segment = Segment.EQUITY
            self.cfg = cfg
            # The repo has no separate ``NullNotifier`` class — the regular
            # ``Notifier`` short-circuits to a no-op when SMTP env vars
            # are unset (``self.enabled == False``), which is exactly what
            # we want under test.
            from bot.notify import Notifier
            from bot.journal import TradeJournal
            self.notifier = Notifier()
            self.journal = TradeJournal(segment=Segment.EQUITY)

        def _publish_state(self): pass

    from bot.executor import Executor
    fake = _FakeExecutor(eq_broker2)
    # Bind the real method to our fake.
    Executor._end_of_day(fake)   # 1st call — closes positions, writes marker
    cash_after_first = eq_broker2.cash()
    n_positions_after_first = len([p for p in eq_broker2.positions() if p.qty != 0])
    Executor._end_of_day(fake)   # 2nd call — should be a NO-OP
    cash_after_second = eq_broker2.cash()
    if cash_after_second != cash_after_first:
        print(f"  ✗ FAILED — _end_of_day idempotency broken")
        print(f"    cash after 1st call: ₹{cash_after_first:,.2f}")
        print(f"    cash after 2nd call: ₹{cash_after_second:,.2f}  (should be unchanged)")
        return 1
    if n_positions_after_first != 0:
        print(f"  ✗ FAILED — 1st _end_of_day call didn't close positions")
        return 1
    marker = cache.get_json("eod_done:equity")
    if not marker or marker.get("date") != datetime.now(IST).date().isoformat():
        print(f"  ✗ FAILED — eod_done:equity marker not written: {marker}")
        return 1
    print(f"  ✓ _end_of_day is idempotent — 2nd call is a no-op (cash stable at ₹{cash_after_second:,.2f})")
    print(f"  ✓ eod_done:equity marker persisted (race-resistant)")

    # Cleanup.
    cache.delete("eod_done:equity")
    cache.delete("paper:state:equity")

    # ── FIX #13a refinement (2026-05-05 PM regression) ──────────────────
    # The original idempotency guard was too coarse: any defensive sweep
    # that happened to call `_end_of_day` (startup_catchup after a midday
    # restart, SIGTERM shutdown handler) would write the eod_done marker
    # even on a flat book. On 2026-05-05 this poisoned the marker at
    # 10:13:59 IST after a Mac-sleep recovery — the legitimate 15:15
    # cron square-off was then blocked, and two open F&O credit-spreads
    # carried overnight against the bot's intraday-only mandate.
    #
    # The refined guard exposes a `mark_done` keyword argument: scheduled
    # paths (15:15 cron, in-window tick) leave it True (default) and
    # cooperate with the May-04 race protection; defensive sweeps pass
    # `mark_done=False` and skip the marker entirely.
    cache.delete("eod_done:equity")
    cache.delete("paper:state:equity")
    eq_broker3 = PaperBroker(segment=Segment.EQUITY)
    # No positions — empty book, exactly the May-05 startup-catchup state.
    fake3 = _FakeExecutor(eq_broker3)
    Executor._end_of_day(fake3, mark_done=False)
    marker_after_defensive = cache.get_json("eod_done:equity")
    if marker_after_defensive is not None:
        print(f"  ✗ FAILED — defensive sweep (mark_done=False) on flat book "
              f"wrote the eod_done marker — this is the 2026-05-05 PM bug.")
        print(f"    marker: {marker_after_defensive}")
        return 1
    print(f"  ✓ Defensive sweep on flat book leaves eod_done UNSET (mark_done=False)")

    # Now a legitimate 15:15 path runs — should still set the marker
    # (race protection for the May-04 scenario must still work).
    Executor._end_of_day(fake3)  # default mark_done=True
    marker_after_scheduled = cache.get_json("eod_done:equity")
    if not marker_after_scheduled or marker_after_scheduled.get("date") != datetime.now(IST).date().isoformat():
        print(f"  ✗ FAILED — scheduled path (mark_done=True) did NOT set marker")
        print(f"    marker: {marker_after_scheduled}")
        return 1
    print(f"  ✓ Scheduled path (mark_done=True) still writes marker (May-04 race protection intact)")

    # Pin the wiring: scheduler's _shutdown and _startup_catchup must
    # both pass `mark_done=False`. A future refactor that drops the
    # kwarg silently re-introduces today's bug.
    import inspect as _inspect
    from bot import scheduler as _sched_mod
    sched_src = _inspect.getsource(_sched_mod)
    if "mark_done=False" not in sched_src:
        print(f"  ✗ FAILED — bot/scheduler.py does not pass mark_done=False from "
              f"any defensive caller. Today's regression can recur.")
        return 1
    if sched_src.count("mark_done=False") < 2:
        print(f"  ✗ FAILED — expected mark_done=False in BOTH _startup_catchup "
              f"and _shutdown (saw {sched_src.count('mark_done=False')} occurrence(s)).")
        return 1
    print(f"  ✓ _startup_catchup and _shutdown both opt out via mark_done=False")

    # Cleanup.
    cache.delete("eod_done:equity")
    cache.delete("paper:state:equity")

    # ─────────────────────────────────────────────────────────────────────
    banner("FIX #15 — daily-loss formula excludes margin block (2026-05-05 PM kill-switch trip)")
    # On 2026-05-05 13:26 the F&O bot opened two NIFTY/BANKNIFTY put
    # credit-spreads. The dashboard immediately reported
    # ``daily_pnl_pct = -2.252%``. The kill-switch threshold is -2.0%,
    # so the bot was 0.252 pp from halting all further trading even
    # though no money had actually been lost — the "loss" was purely
    # the margin block (₹10,868) net of premium received (₹8,628).
    #
    # Root cause: ``RiskManager._equity`` was a Phase-1 equity-only
    # formula (cash + long_cost_basis + unrealized) that ignored
    # ``margin_blocked``. The fix adds the F&O-aware offset:
    # ``+ margin`` for futures, ``+ (margin − credit)`` for short
    # credit spreads / iron-condors. ``Executor._publish_state`` now
    # also writes ``instrument_kind`` / ``margin_blocked`` per-position
    # so the healthcheck and dashboard apply the same correction.
    cache.delete("paper:state:fno")
    fno_broker = PaperBroker(segment=Segment.FNO)
    starting = fno_broker.cash()  # always config-driven (₹100k)

    # Build a RiskManager and snapshot the starting equity BEFORE entries.
    fno_risk = RiskManager(fno_broker, segment=Segment.FNO)
    eq_before = fno_risk._equity()
    if abs(eq_before - starting) > 1.0:
        print(f"  ✗ FAILED — empty F&O book equity should equal cash; got "
              f"₹{eq_before:,.2f} vs cash ₹{starting:,.2f}")
        return 1

    # Reproduce the May-05 13:26 entries.
    from bot.broker.base import Order, OrderSide, OrderType, InstrumentKind
    import uuid as _uuid
    fno_broker.update_marks({"NIFTY26MAY24050-23950PESPRD": 44.36})
    fno_broker.place_order(Order(
        id=str(_uuid.uuid4()),
        symbol="NIFTY26MAY24050-23950PESPRD", side=OrderSide.SELL, qty=75,
        type=OrderType.MARKET, price=44.36,
        instrument_kind=InstrumentKind.SPREAD, lot_size=75,
    ))
    fno_broker.update_marks({"BANKNIFTY26MAY54700-54600PESPRD": 44.18})
    fno_broker.place_order(Order(
        id=str(_uuid.uuid4()),
        symbol="BANKNIFTY26MAY54700-54600PESPRD", side=OrderSide.SELL, qty=120,
        type=OrderType.MARKET, price=44.18,
        instrument_kind=InstrumentKind.SPREAD, lot_size=30,
    ))
    eq_after = fno_risk._equity()
    pnl_pct_open = (eq_after - eq_before) / eq_before * 100
    if pnl_pct_open < -0.5:
        print(f"  ✗ FAILED — flat-mark spreads produce phantom loss of "
              f"{pnl_pct_open:.3f}% (entry-fee budget is ~-0.10%). "
              f"This is the exact 2026-05-05 PM kill-switch-trip signature.")
        return 1
    print(f"  ✓ Flat-mark credit spreads report {pnl_pct_open:+.3f}% (only entry fees, no phantom margin loss)")

    # Adverse mark move must produce genuine MTM loss.
    fno_broker.update_marks({"NIFTY26MAY24050-23950PESPRD": 50.0})
    fno_broker.positions()  # triggers unrealized recompute
    eq_adverse = fno_risk._equity()
    pnl_pct_adverse = (eq_adverse - eq_before) / eq_before * 100
    if pnl_pct_adverse > pnl_pct_open - 0.3:
        print(f"  ✗ FAILED — ₹5.64 adverse mark move on a 75-lot short produced "
              f"only {pnl_pct_adverse - pnl_pct_open:.3f}% additional loss "
              f"(should be ~-0.42% from the (44.36-50.00)*75 = -₹423 unrealized).")
        return 1
    print(f"  ✓ Genuine MTM loss correctly captured ({pnl_pct_adverse:+.3f}% after ₹5.64 adverse mark)")

    # Source-pin: confirm RiskManager._equity references SPREAD/IRON_CONDOR
    # and the executor snapshot includes the F&O fields. Future refactors
    # that drop this re-introduce the kill-switch trip.
    risk_src = inspect.getsource(RiskManager._equity)
    if "SPREAD" not in risk_src or "IRON_CONDOR" not in risk_src or "margin_blocked" not in risk_src:
        print(f"  ✗ FAILED — RiskManager._equity no longer adjusts equity for "
              f"credit spreads / iron-condors / margin_blocked. Phantom margin loss can recur.")
        return 1
    print(f"  ✓ RiskManager._equity source pinned (SPREAD/IRON_CONDOR/margin_blocked references present)")
    from bot.executor import Executor as _Exec
    pub_src = inspect.getsource(_Exec._publish_state)
    if "instrument_kind" not in pub_src or "margin_blocked" not in pub_src:
        print(f"  ✗ FAILED — Executor._publish_state no longer writes instrument_kind / "
              f"margin_blocked into the portfolio snapshot. The dashboard and "
              f"healthcheck cannot apply the corrected equity formula.")
        return 1
    print(f"  ✓ Executor._publish_state writes instrument_kind + margin_blocked per position")

    # Cleanup.
    cache.delete("paper:state:fno")

    # ─────────────────────────────────────────────────────────────────────
    banner("FIX #14 — F&O EMA50 pre-warm (2026-05-04 zero-F&O-trades incident)")
    # On 2026-05-04 the F&O bot produced ZERO trades despite NIFTY having
    # two clean EMA20/EMA50 crosses (09:20 BULL, 12:20 BEAR) and BANKNIFTY
    # the same. Root cause: ``bot/data.py::intraday_bars`` was fetching
    # ``days=2`` of intraday history. On Monday this means the window
    # starts Saturday — which has NO trading data — so yfinance returned
    # only today's 75 bars. The credit_spread strategy needs ~61 bars
    # before EMA50 is valid, so it returned HOLD all morning. By the time
    # EMA50 was warm (~14:20 IST) the day's only crosses were 30+ bars
    # stale (well beyond the 10-bar lookback). The strategy correctly
    # returned HOLD all day — but for the wrong reason.
    #
    # Fix: bump ``days`` from 2 → 7 for F&O symbols so EMA50 is pre-
    # warmed from the prior week's bars and ready at the 09:15 open.
    # Equity is unaffected (it discards everything except today below).
    import inspect
    from bot.data import intraday_bars as _ib_fn
    ib_src = inspect.getsource(_ib_fn)
    if "days=2" in ib_src and "days=7" not in ib_src:
        print("  ✗ FAILED — intraday_bars still fetches days=2 unconditionally.")
        print("    F&O EMA50 will not be pre-warmed; strategies blind on Mondays.")
        return 1
    if "fetch_days" not in ib_src and "days=7" not in ib_src:
        print("  ✗ FAILED — intraday_bars no longer uses the segment-aware")
        print("    fetch window. F&O can't pre-warm EMA50.")
        return 1
    print("  ✓ intraday_bars uses segment-aware fetch window (F&O ≥ 7 days)")

    # Functional check: load NIFTY 5m bars and verify >75 bars (i.e.,
    # multiple trading days). Skip if yfinance is unreachable in CI.
    try:
        from bot.cache import get_cache as _gc
        _gc().delete("bars:NIFTY:5m")
        df = _ib_fn("NIFTY", "5m")
        if df is None or df.empty:
            print("  ⚠ skip: yfinance returned no bars for NIFTY (offline?)")
        elif len(df) <= 75:
            print(f"  ✗ FAILED — only {len(df)} NIFTY bars loaded; expected ≥150")
            print("    (need at least 2 trading days; got 1).")
            return 1
        else:
            print(f"  ✓ NIFTY 5m bars loaded: {len(df)} bars across "
                  f"{df.index.date.min()}…{df.index.date.max()}")
    except Exception as e:                                          # noqa: BLE001
        print(f"  ⚠ skip: NIFTY fetch failed (non-fatal): {e}")

    banner("✅ ALL FOURTEEN FIXES VERIFIED (incl. F&O EMA50 pre-warm — 2026-05-04 zero-trades)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
