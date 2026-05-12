"""Strategy backtest sweep — answer the "should we run F&O / equity tomorrow?"
question with actual numbers, not hopes.

What this script does:
  1. Pulls 60 days (~58 trading days) of 5-minute bars from yfinance for both
     the equity watchlist and NIFTY/BANKNIFTY underlyings.
  2. Runs each candidate strategy bar-by-bar with the LIVE executor's exit
     logic: ATR-based SL/TP from the strategy's own signal, EOD square-off
     at 15:15 IST, realistic fees from `bot/fees.py`. No fixed 1% SL — we
     use what the strategy actually emits.
  3. Reports per-strategy metrics: trades, win rate, payoff, expectancy/trade,
     net P&L, max drawdown. Plus a daily expectancy projection vs the
     ₹2-5K NET daily target.

Run:  python scripts/strategy_backtest.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable, List, Optional

import pandas as pd
import pytz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.broker.base import OrderSide, InstrumentKind
from bot.config import load_config
from bot.data import history
from bot.fees import compute_fees
from bot.instruments.fno import (
    lot_size as _lot_size,
    margin_pct as _margin_pct,
    parse_iron_condor_tradingsymbol,
    parse_option_tradingsymbol,
    parse_spread_tradingsymbol,
)
from bot.options.pricing import bs as _bs, years_to_expiry
from bot.strategies import (
    CreditSpreadStrategy,
    EMASupertrendStrategy,
    Ensemble,
    FuturesTrendStrategy,
    IronCondorStrategy,
    MultiTimeframeStrategy,
    OptionBuyDirectionalStrategy,
    ORBStrategy,
)
from bot.strategies.base import Signal, SignalType, Strategy

IST = pytz.timezone("Asia/Kolkata")


# ───── Look-ahead patch for the MTF wrapper ─────
# The live MultiTimeframeStrategy._confirm_*trend calls bot.data.history()
# directly, fetching the LATEST 15-min bars. In a backtest those bars would
# be from "today" no matter which historical day the simulator is on — a
# textbook look-ahead bias. We swap in a per-symbol cache of the full 60-day
# 15m series and slice it up to ``_BT_AS_OF`` (the current backtest bar's
# timestamp), so the wrapper sees only data that would have existed at that
# moment. ``_BT_AS_OF`` is updated by the backtest loops below.

_BT_AS_OF: Optional[pd.Timestamp] = None
_MTF_15M_CACHE: dict = {}


def _patched_history(symbol: str, days: int = 5, interval: str = "1m"):
    if interval == "15m" and _BT_AS_OF is not None:
        if symbol not in _MTF_15M_CACHE:
            _MTF_15M_CACHE[symbol] = history(symbol, days=60, interval="15m")
        full = _MTF_15M_CACHE[symbol]
        return full[full.index < _BT_AS_OF]
    return history(symbol, days=days, interval=interval)


import bot.strategies.multitimeframe as _mtm
_mtm.history = _patched_history
# ───── end look-ahead patch ─────


@dataclass
class BTrade:
    symbol: str
    side: str
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    qty: int
    gross_pnl: float
    fees: float
    net_pnl: float
    exit_reason: str


@dataclass
class BTResult:
    label: str
    trades: List[BTrade] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.trades)

    def summary(self, capital: float = 100_000) -> dict:
        if not self.trades:
            return {"label": self.label, "trades": 0}
        net = sum(t.net_pnl for t in self.trades)
        gross = sum(t.gross_pnl for t in self.trades)
        fees = sum(t.fees for t in self.trades)
        wins = [t for t in self.trades if t.net_pnl > 0]
        losses = [t for t in self.trades if t.net_pnl <= 0]
        avg_w = sum(t.net_pnl for t in wins) / len(wins) if wins else 0
        avg_l = sum(t.net_pnl for t in losses) / len(losses) if losses else 0
        wr = len(wins) / len(self.trades)
        expect = wr * avg_w + (1 - wr) * avg_l

        days = sorted({t.entry_time.date() for t in self.trades})
        per_day = defaultdict(float)
        for t in self.trades:
            per_day[t.entry_time.date()] += t.net_pnl
        daily_pnls = sorted(per_day.values())
        median_daily = daily_pnls[len(daily_pnls) // 2] if daily_pnls else 0
        best_daily = max(daily_pnls) if daily_pnls else 0
        worst_daily = min(daily_pnls) if daily_pnls else 0

        # Equity-curve max drawdown
        eq = capital
        peak = capital
        max_dd = 0.0
        for d in days:
            eq += per_day[d]
            peak = max(peak, eq)
            max_dd = min(max_dd, eq - peak)

        return {
            "label": self.label,
            "trades": len(self.trades),
            "wins": len(wins),
            "win_pct": round(wr * 100, 1),
            "gross": round(gross, 0),
            "fees": round(fees, 0),
            "net": round(net, 0),
            "avg_win": round(avg_w, 0),
            "avg_loss": round(avg_l, 0),
            "expectancy/trade": round(expect, 0),
            "trading_days": len(days),
            "trades/day": round(len(self.trades) / max(len(days), 1), 1),
            "median_day": round(median_daily, 0),
            "best_day": round(best_daily, 0),
            "worst_day": round(worst_daily, 0),
            "max_drawdown": round(max_dd, 0),
        }


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _instrument_kind_for(symbol: str) -> InstrumentKind:
    if parse_iron_condor_tradingsymbol(symbol) is not None:
        return InstrumentKind.IRON_CONDOR
    if parse_spread_tradingsymbol(symbol) is not None:
        return InstrumentKind.SPREAD
    if parse_option_tradingsymbol(symbol) is not None:
        return InstrumentKind.OPTION
    if symbol.endswith("FUT"):
        return InstrumentKind.FUTURES
    return InstrumentKind.EQUITY


_KIND_TO_SEGMENT = {
    InstrumentKind.EQUITY: "equity",
    InstrumentKind.OPTION: "options",
    InstrumentKind.SPREAD: "options",
    InstrumentKind.IRON_CONDOR: "options",
    InstrumentKind.FUTURES: "futures",
}


def _fees_for(side: OrderSide, qty: int, price: float, kind: InstrumentKind) -> float:
    """One-leg fees from the live fee schedule (returns total ₹)."""
    seg = _KIND_TO_SEGMENT[kind]
    return float(compute_fees(side=side.value, qty=qty, price=price, segment=seg).total)


def _size_qty(signal: "Signal", capital: float, max_loss_pct: float, lot: int = 1) -> int:
    risk_per_share = abs(signal.price - signal.stop_loss)
    if risk_per_share <= 0:
        return 0
    max_loss = capital * max_loss_pct / 100.0
    qty = int(max_loss // risk_per_share)
    if lot > 1:
        qty = (qty // lot) * lot
    return max(0, qty)


# ────────────────────────────────────────────────────────────────────────
# Equity backtest (uses spot bars for both signal and SL/TP eval)
# ────────────────────────────────────────────────────────────────────────


def backtest_equity(symbol: str, ensemble: Strategy, days: int = 60,
                    interval: str = "5m", capital: float = 100_000,
                    max_loss_pct: float = 1.0,
                    label: str = "Equity") -> BTResult:
    cfg = load_config()
    df = history(symbol, days=days, interval=interval)
    if df.empty:
        return BTResult(label=f"{label}:{symbol}")

    so = cfg.session.t("square_off")
    ts = cfg.session.t("trade_start")
    tc = cfg.session.t("trade_cutoff")

    res = BTResult(label=f"{label}:{symbol}")
    in_pos: Optional[BTrade] = None

    grouped = df.groupby(df.index.date)
    for day, day_df in grouped:
        day_df = day_df.sort_index()
        for i in range(20, len(day_df)):
            window = day_df.iloc[: i + 1]
            bar = day_df.iloc[i]
            t = bar.name.time() if isinstance(bar.name, pd.Timestamp) else time(0, 0)
            high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])

            if in_pos is not None:
                # Check SL/TP — same logic as executor._stop_loss_hit/_take_profit_hit
                hit_sl = (
                    (in_pos.side == "BUY" and low <= in_pos._sl)
                    or (in_pos.side == "SELL" and high >= in_pos._sl)
                )
                hit_tp = (
                    (in_pos.side == "BUY" and high >= in_pos._tp)
                    or (in_pos.side == "SELL" and low <= in_pos._tp)
                )
                exit_price, reason = None, None
                if hit_sl:
                    exit_price, reason = in_pos._sl, "stop_loss"
                elif hit_tp:
                    exit_price, reason = in_pos._tp, "take_profit"
                elif t >= so or i == len(day_df) - 1:
                    exit_price, reason = close, "eod_squareoff"

                if exit_price is not None:
                    if in_pos.side == "BUY":
                        gross = (exit_price - in_pos.entry_price) * in_pos.qty
                    else:
                        gross = (in_pos.entry_price - exit_price) * in_pos.qty
                    exit_side = OrderSide.SELL if in_pos.side == "BUY" else OrderSide.BUY
                    exit_fees = _fees_for(exit_side, in_pos.qty, exit_price, InstrumentKind.EQUITY)
                    in_pos.exit_time = bar.name
                    in_pos.exit_price = exit_price
                    in_pos.gross_pnl = gross
                    in_pos.fees += exit_fees
                    in_pos.net_pnl = gross - in_pos.fees
                    in_pos.exit_reason = reason
                    res.trades.append(in_pos)
                    in_pos = None
                continue

            if t < ts or t > tc:
                continue

            # Set the "as-of" timestamp so the patched MTF history() returns
            # data sliced up to the current bar (no look-ahead).
            global _BT_AS_OF
            _BT_AS_OF = bar.name

            sig = ensemble.generate(symbol, window)
            if sig.type == SignalType.HOLD or sig.stop_loss is None:
                continue

            qty = _size_qty(sig, capital, max_loss_pct, lot=1)
            if qty == 0:
                continue

            entry_side = OrderSide.BUY if sig.type == SignalType.BUY else OrderSide.SELL
            entry_fees = _fees_for(entry_side, qty, float(sig.price), InstrumentKind.EQUITY)
            in_pos = BTrade(
                symbol=symbol, side=sig.type.value,
                entry_time=bar.name, entry_price=float(sig.price),
                exit_time=bar.name, exit_price=0.0,
                qty=qty, gross_pnl=0.0, fees=entry_fees, net_pnl=0.0,
                exit_reason="",
            )
            in_pos._sl = float(sig.stop_loss)
            in_pos._tp = float(sig.take_profit) if sig.take_profit else (
                in_pos.entry_price + 2.0 * abs(in_pos.entry_price - in_pos._sl)
                if sig.type == SignalType.BUY
                else in_pos.entry_price - 2.0 * abs(in_pos.entry_price - in_pos._sl)
            )

    return res


# ────────────────────────────────────────────────────────────────────────
# F&O option_buy_directional backtest (signal on spot, SL/TP in premium)
# ────────────────────────────────────────────────────────────────────────


def backtest_option_buy(underlying: str, strategy: OptionBuyDirectionalStrategy,
                        days: int = 60, interval: str = "5m",
                        capital: float = 100_000, max_loss_pct: float = 1.0,
                        force_min_lot: bool = False) -> BTResult:
    cfg = load_config()
    spot_df = history(underlying, days=days, interval=interval)
    if spot_df.empty:
        return BTResult(label=f"option_buy:{underlying}")

    so = cfg.session.t("square_off")
    ts = cfg.session.t("trade_start")
    tc = cfg.session.t("trade_cutoff")
    lot = _lot_size(underlying)

    res = BTResult(label=f"option_buy:{underlying}")
    in_pos: Optional[BTrade] = None

    spot_df = spot_df.sort_index()
    # Continuous iteration across the WHOLE window so EMA20/EMA50 warm up
    # from prior days — same as the live executor sees from `intraday_bars`
    # (which loads 7 days for F&O). Per-day grouping zeroes the EMAs each
    # morning, which is wrong for F&O strategies.
    for i in range(60, len(spot_df)):  # 60-bar warmup ≈ ema_slow + lookback
            window = spot_df.iloc[: i + 1]
            bar = spot_df.iloc[i]
            t = bar.name.time() if isinstance(bar.name, pd.Timestamp) else time(0, 0)
            today = bar.name.date()

            if in_pos is not None:
                # Re-price the option for THIS bar's spot using the same BS
                # pricer the strategy + executor use.
                option_meta = in_pos._option_meta
                T_now = years_to_expiry(option_meta["expiry"])
                if T_now <= 0:
                    # Expiry passed mid-position — close at intrinsic value
                    intrinsic = (
                        max(0, float(bar["close"]) - option_meta["strike"]) if option_meta["opt_type"] == "CE"
                        else max(0, option_meta["strike"] - float(bar["close"]))
                    )
                    in_pos._mark = intrinsic
                else:
                    high_prem = _bs(float(bar["high"]), option_meta["strike"], T_now, option_meta["opt_type"])
                    low_prem = _bs(float(bar["low"]), option_meta["strike"], T_now, option_meta["opt_type"])
                    close_prem = _bs(float(bar["close"]), option_meta["strike"], T_now, option_meta["opt_type"])
                    if option_meta["opt_type"] == "CE":
                        bar_high, bar_low = high_prem, low_prem
                    else:  # PE — premium falls when spot rises
                        bar_high, bar_low = low_prem, high_prem
                    in_pos._mark = close_prem

                hit_sl = bar_low <= in_pos._sl     # always BUY side for option_buy
                hit_tp = bar_high >= in_pos._tp
                exit_price, reason = None, None
                if hit_sl:
                    exit_price, reason = in_pos._sl, "stop_loss"
                elif hit_tp:
                    exit_price, reason = in_pos._tp, "take_profit"
                elif t >= so or (today != in_pos.entry_time.date() and t >= so):
                    exit_price, reason = in_pos._mark, "eod_squareoff"

                if exit_price is not None:
                    gross = (exit_price - in_pos.entry_price) * in_pos.qty
                    exit_fees = _fees_for(OrderSide.SELL, in_pos.qty, exit_price,
                                          InstrumentKind.OPTION)
                    in_pos.exit_time = bar.name
                    in_pos.exit_price = exit_price
                    in_pos.gross_pnl = gross
                    in_pos.fees += exit_fees
                    in_pos.net_pnl = gross - in_pos.fees
                    in_pos.exit_reason = reason
                    res.trades.append(in_pos)
                    in_pos = None
                continue

            if t < ts or t > tc:
                continue

            sig = strategy.generate(underlying, window)
            if sig.type == SignalType.HOLD or sig.stop_loss is None:
                continue

            opt_meta = parse_option_tradingsymbol(sig.symbol)
            if opt_meta is None:
                continue

            qty = _size_qty(sig, capital, max_loss_pct, lot=lot)
            # Cap by cash (premium × qty must fit)
            premium = float(sig.price)
            cash_cap = int(capital // premium // lot) * lot
            qty = min(qty, cash_cap)
            if force_min_lot and qty == 0 and cash_cap >= lot:
                # See what the strategy WOULD do if we always took 1 lot —
                # used to evaluate strategy edge independent of sizing caps.
                qty = lot
            if qty < lot:
                continue

            entry_fees = _fees_for(OrderSide.BUY, qty, premium, InstrumentKind.OPTION)
            in_pos = BTrade(
                symbol=sig.symbol, side="BUY",
                entry_time=bar.name, entry_price=premium,
                exit_time=bar.name, exit_price=0.0,
                qty=qty, gross_pnl=0.0, fees=entry_fees, net_pnl=0.0,
                exit_reason="",
            )
            in_pos._sl = float(sig.stop_loss)
            in_pos._tp = float(sig.take_profit) if sig.take_profit else premium * 1.5
            in_pos._option_meta = opt_meta
            in_pos._mark = premium

    return res


# ────────────────────────────────────────────────────────────────────────
# F&O futures_trend backtest (signal on spot, SL/TP in spot space)
# ────────────────────────────────────────────────────────────────────────


def backtest_futures(underlying: str, strategy: FuturesTrendStrategy,
                     days: int = 60, interval: str = "5m",
                     capital: float = 100_000, max_loss_pct: float = 1.0,
                     force_min_lot: bool = False) -> BTResult:
    cfg = load_config()
    df = history(underlying, days=days, interval=interval)
    if df.empty:
        return BTResult(label=f"futures:{underlying}")

    so = cfg.session.t("square_off")
    ts = cfg.session.t("trade_start")
    tc = cfg.session.t("trade_cutoff")
    lot = _lot_size(underlying)
    margin_per_unit = lambda p: p * _margin_pct(underlying)

    res = BTResult(label=f"futures:{underlying}")
    in_pos: Optional[BTrade] = None

    df = df.sort_index()
    for i in range(60, len(df)):
            window = df.iloc[: i + 1]
            bar = df.iloc[i]
            t = bar.name.time() if isinstance(bar.name, pd.Timestamp) else time(0, 0)
            today = bar.name.date()
            high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])

            if in_pos is not None:
                hit_sl = (
                    (in_pos.side == "BUY" and low <= in_pos._sl)
                    or (in_pos.side == "SELL" and high >= in_pos._sl)
                )
                hit_tp = (
                    (in_pos.side == "BUY" and high >= in_pos._tp)
                    or (in_pos.side == "SELL" and low <= in_pos._tp)
                )
                exit_price, reason = None, None
                if hit_sl:
                    exit_price, reason = in_pos._sl, "stop_loss"
                elif hit_tp:
                    exit_price, reason = in_pos._tp, "take_profit"
                elif t >= so:
                    exit_price, reason = close, "eod_squareoff"

                if exit_price is not None:
                    if in_pos.side == "BUY":
                        gross = (exit_price - in_pos.entry_price) * in_pos.qty
                    else:
                        gross = (in_pos.entry_price - exit_price) * in_pos.qty
                    exit_side = OrderSide.SELL if in_pos.side == "BUY" else OrderSide.BUY
                    exit_fees = _fees_for(exit_side, in_pos.qty, exit_price,
                                          InstrumentKind.FUTURES)
                    in_pos.exit_time = bar.name
                    in_pos.exit_price = exit_price
                    in_pos.gross_pnl = gross
                    in_pos.fees += exit_fees
                    in_pos.net_pnl = gross - in_pos.fees
                    in_pos.exit_reason = reason
                    res.trades.append(in_pos)
                    in_pos = None
                continue

            if t < ts or t > tc:
                continue

            sig = strategy.generate(underlying, window)
            if sig.type == SignalType.HOLD or sig.stop_loss is None:
                continue

            # Margin-based sizing: 1 lot of NIFTY needs ~₹90K.
            qty_risk = _size_qty(sig, capital, max_loss_pct, lot=lot)
            margin_per_lot = margin_per_unit(float(sig.price)) * lot
            qty_margin_cap = int(capital * 0.80 // margin_per_lot) * lot
            qty = min(qty_risk, qty_margin_cap)
            if force_min_lot and qty == 0:
                # Strategy-edge mode: take 1 lot regardless of sizing caps,
                # so we can observe the raw signal P&L without margin/risk
                # filtering. Useful when the live config is sizing-binding.
                qty = lot
            if qty < lot:
                continue

            entry_side = OrderSide.BUY if sig.type == SignalType.BUY else OrderSide.SELL
            entry_fees = _fees_for(entry_side, qty, float(sig.price),
                                   InstrumentKind.FUTURES)
            in_pos = BTrade(
                symbol=sig.symbol, side=sig.type.value,
                entry_time=bar.name, entry_price=float(sig.price),
                exit_time=bar.name, exit_price=0.0,
                qty=qty, gross_pnl=0.0, fees=entry_fees, net_pnl=0.0,
                exit_reason="",
            )
            in_pos._sl = float(sig.stop_loss)
            in_pos._tp = float(sig.take_profit) if sig.take_profit else (
                in_pos.entry_price + 1.5 * abs(in_pos.entry_price - in_pos._sl)
                if sig.type == SignalType.BUY
                else in_pos.entry_price - 1.5 * abs(in_pos.entry_price - in_pos._sl)
            )

    return res


# ────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────


def _build_equity_ensemble(min_agree_override: Optional[int] = None) -> Ensemble:
    cfg = load_config().strategies
    members: List[Strategy] = []
    if "orb" in cfg.enabled:
        members.append(ORBStrategy(cfg.orb))
    if "ema_supertrend" in cfg.enabled:
        members.append(EMASupertrendStrategy(cfg.ema_supertrend))
    if cfg.multitimeframe.enabled and members:
        members = [MultiTimeframeStrategy(m, cfg.multitimeframe) for m in members]
    return Ensemble(members,
                    min_agree=min_agree_override if min_agree_override is not None
                    else cfg.ensemble.min_agree)


def _print_table(rows: List[dict], title: str) -> None:
    print(f"\n{'=' * 90}\n  {title}\n{'=' * 90}")
    if not rows:
        print("  (no rows)")
        return
    headers = list(rows[0].keys())
    widths = {h: max(len(str(h)), max(len(str(r.get(h, ""))) for r in rows)) for h in headers}
    line = "  ".join(f"{h:<{widths[h]}}" for h in headers)
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(f"{str(r.get(h, '')):<{widths[h]}}" for h in headers))


def main() -> int:
    cfg = load_config()
    days = 60
    interval = "5m"
    capital = float(cfg.capital.total)
    max_loss_pct = float(cfg.risk.max_loss_per_trade_pct)

    print(f"\nStrategy backtest sweep")
    print(f"  history window:  {days} calendar days @ {interval}")
    print(f"  equity capital:  ₹{capital:,.0f}")
    print(f"  per-trade risk:  {max_loss_pct}% (= ₹{capital * max_loss_pct / 100:,.0f})")
    print(f"  trading window:  {cfg.session.trade_start}–{cfg.session.trade_cutoff} entry, "
          f"{cfg.session.square_off} square-off")

    # ── 1. Equity ensemble — three configs side by side ──────────────
    eq_full_watchlist = ["HDFCBANK", "INFY", "RELIANCE", "ICICIBANK", "TCS",
                         "ITC", "SBIN", "AXISBANK", "INDUSINDBK", "BANKBARODA"]

    def _run_eq_pass(label: str, ensemble: Ensemble, watchlist: List[str]) -> List[BTResult]:
        print("\n" + "─" * 90)
        print(f"  Phase 1: Equity ensemble — {label}")
        print(f"  members={[m.name for m in ensemble.members]} min_agree={ensemble.min_agree} "
              f"watchlist={len(watchlist)} stocks")
        print("─" * 90)
        rs: List[BTResult] = []
        for sym in watchlist:
            try:
                r = backtest_equity(sym, ensemble, days=days, interval=interval,
                                    capital=capital, max_loss_pct=max_loss_pct)
                rs.append(r)
                s = r.summary(capital)
                print(f"    {sym:<14} trades={s.get('trades', 0):>3}  "
                      f"win%={s.get('win_pct', 0):>5}  "
                      f"net={s.get('net', 0):>+8}  "
                      f"expect/trade={s.get('expectancy/trade', 0):>+5}")
            except Exception as e:
                print(f"    {sym:<14} ERROR: {e}")
        agg = BTResult(label=f"EQUITY ({label})")
        agg.trades = [t for r in rs for t in r.trades]
        print(f"\n  Aggregate ({label}):")
        for k, v in agg.summary(capital).items():
            print(f"    {k:<22} {v}")
        return rs

    # Pass A: current config (min_agree=1, full watchlist) — what we'd run tomorrow
    ens_a = _build_equity_ensemble(min_agree_override=1)
    eq_a = _run_eq_pass("CONFIG-A (min_agree=1, full watchlist) [current]",
                        ens_a, eq_full_watchlist)

    # Pass B: tighter (min_agree=2, full watchlist) — pre-FIX#25 config
    ens_b = _build_equity_ensemble(min_agree_override=2)
    eq_b = _run_eq_pass("CONFIG-B (min_agree=2, full watchlist) [pre-FIX#25]",
                        ens_b, eq_full_watchlist)

    # Pass C: trim watchlist to historically-profitable stocks (per Pass A)
    profitable_subset = ["HDFCBANK", "INFY", "SBIN", "INDUSINDBK"]
    ens_c = _build_equity_ensemble(min_agree_override=1)
    eq_c = _run_eq_pass("CONFIG-C (min_agree=1, profitable-4 only)",
                        ens_c, profitable_subset)

    # Pass D: tighter + trimmed
    ens_d = _build_equity_ensemble(min_agree_override=2)
    eq_d = _run_eq_pass("CONFIG-D (min_agree=2, profitable-4 only)",
                        ens_d, profitable_subset)

    # Pick the best for the final verdict
    eq_passes = {
        "A: m=1, all 10": eq_a,
        "B: m=2, all 10": eq_b,
        "C: m=1, top 4 ": eq_c,
        "D: m=2, top 4 ": eq_d,
    }
    print("\n" + "─" * 90)
    print("  Equity config comparison:")
    print("─" * 90)
    for k, rs in eq_passes.items():
        agg = BTResult(label=k)
        agg.trades = [t for r in rs for t in r.trades]
        s = agg.summary(capital)
        print(f"    {k}  trades={s.get('trades', 0):>4}  "
              f"win%={s.get('win_pct', 0):>5}  "
              f"net={s.get('net', 0):>+8.0f}  "
              f"expect/trade={s.get('expectancy/trade', 0):>+5}  "
              f"med_day={s.get('median_day', 0):>+6}  "
              f"max_dd={s.get('max_drawdown', 0):>+8}")
    # use best as the main aggregate for the "combined verdict"
    best_label = max(eq_passes,
                     key=lambda k: BTResult(label=k, trades=[t for r in eq_passes[k] for t in r.trades]).summary(capital).get("net", 0))
    eq_agg = BTResult(label=f"EQUITY (best: {best_label})")
    eq_agg.trades = [t for r in eq_passes[best_label] for t in r.trades]
    all_trades = eq_agg.trades
    print(f"\n  → Best pass for combined verdict: {best_label}")

    # ── 2. F&O option_buy_directional ─────────────────────────────────
    print("\n" + "─" * 90)
    print("  Phase 2: F&O option_buy_directional  (long ATM CE/PE on EMA20/50 cross)")
    print("─" * 90)
    fno_cfg = cfg.fno.strategies
    obd_strategy = OptionBuyDirectionalStrategy(fno_cfg.option_buy_directional)
    fno_capital = float(cfg.fno.capital.total)
    fno_max_loss = float(cfg.fno.risk.max_loss_per_trade_pct)
    print(f"  F&O capital: ₹{fno_capital:,.0f}, per-trade risk: {fno_max_loss}%")

    obd_results = []
    print("  -- live-sizing pass (live risk/cash caps applied) --")
    for sym in ["NIFTY", "BANKNIFTY"]:
        try:
            r = backtest_option_buy(sym, obd_strategy, days=days, interval=interval,
                                    capital=fno_capital, max_loss_pct=fno_max_loss)
            obd_results.append(r)
            s = r.summary(fno_capital)
            print(f"    {sym:<14} trades={s.get('trades', 0):>3}  "
                  f"win%={s.get('win_pct', 0):>5}  "
                  f"net={s.get('net', 0):>+8}  "
                  f"expect/trade={s.get('expectancy/trade', 0):>+5}")
        except Exception as e:
            print(f"    {sym:<14} ERROR: {type(e).__name__}: {e}")

    obd_all_trades = [t for r in obd_results for t in r.trades]
    obd_agg = BTResult(label="F&O option_buy (aggregate)")
    obd_agg.trades = obd_all_trades
    print("\n  option_buy_directional aggregate (live-sizing):")
    for k, v in obd_agg.summary(fno_capital).items():
        print(f"    {k:<22} {v}")

    print("\n  -- strategy-edge pass (force 1 lot, ignore risk caps) --")
    obd_edge_results = []
    for sym in ["NIFTY", "BANKNIFTY"]:
        try:
            r = backtest_option_buy(sym, obd_strategy, days=days, interval=interval,
                                    capital=fno_capital, max_loss_pct=fno_max_loss,
                                    force_min_lot=True)
            obd_edge_results.append(r)
            s = r.summary(fno_capital)
            print(f"    {sym:<14} trades={s.get('trades', 0):>3}  "
                  f"win%={s.get('win_pct', 0):>5}  "
                  f"net={s.get('net', 0):>+8}  "
                  f"expect/trade={s.get('expectancy/trade', 0):>+5}")
        except Exception as e:
            print(f"    {sym:<14} ERROR: {type(e).__name__}: {e}")
    obd_edge_trades = [t for r in obd_edge_results for t in r.trades]
    obd_edge_agg = BTResult(label="F&O option_buy edge")
    obd_edge_agg.trades = obd_edge_trades
    print("\n  option_buy_directional aggregate (edge / 1-lot):")
    for k, v in obd_edge_agg.summary(fno_capital).items():
        print(f"    {k:<22} {v}")

    # ── 3. F&O futures_trend ──────────────────────────────────────────
    print("\n" + "─" * 90)
    print("  Phase 3: F&O futures_trend  (long/short index futures on EMA20/50 cross)")
    print("─" * 90)
    ft_strategy = FuturesTrendStrategy(fno_cfg.futures_trend)
    ft_results = []
    print("  -- live-sizing pass (live margin/risk caps applied) --")
    for sym in ["NIFTY", "BANKNIFTY"]:
        try:
            r = backtest_futures(sym, ft_strategy, days=days, interval=interval,
                                 capital=fno_capital, max_loss_pct=fno_max_loss)
            ft_results.append(r)
            s = r.summary(fno_capital)
            print(f"    {sym:<14} trades={s.get('trades', 0):>3}  "
                  f"win%={s.get('win_pct', 0):>5}  "
                  f"net={s.get('net', 0):>+8}  "
                  f"expect/trade={s.get('expectancy/trade', 0):>+5}")
        except Exception as e:
            print(f"    {sym:<14} ERROR: {type(e).__name__}: {e}")

    ft_all_trades = [t for r in ft_results for t in r.trades]
    ft_agg = BTResult(label="F&O futures_trend (aggregate)")
    ft_agg.trades = ft_all_trades
    print("\n  futures_trend aggregate (live-sizing):")
    for k, v in ft_agg.summary(fno_capital).items():
        print(f"    {k:<22} {v}")

    print("\n  -- strategy-edge pass (force 1 lot, ignore margin caps) --")
    ft_edge_results = []
    for sym in ["NIFTY", "BANKNIFTY"]:
        try:
            r = backtest_futures(sym, ft_strategy, days=days, interval=interval,
                                 capital=fno_capital, max_loss_pct=fno_max_loss,
                                 force_min_lot=True)
            ft_edge_results.append(r)
            s = r.summary(fno_capital)
            print(f"    {sym:<14} trades={s.get('trades', 0):>3}  "
                  f"win%={s.get('win_pct', 0):>5}  "
                  f"net={s.get('net', 0):>+8}  "
                  f"expect/trade={s.get('expectancy/trade', 0):>+5}")
        except Exception as e:
            print(f"    {sym:<14} ERROR: {type(e).__name__}: {e}")

    ft_edge_trades = [t for r in ft_edge_results for t in r.trades]
    ft_edge_agg = BTResult(label="F&O futures_trend edge")
    ft_edge_agg.trades = ft_edge_trades
    print("\n  futures_trend aggregate (edge / 1-lot):")
    for k, v in ft_edge_agg.summary(fno_capital).items():
        print(f"    {k:<22} {v}")

    # ── 4. Combined verdict vs ₹2-5K daily target ─────────────────────
    print("\n" + "═" * 90)
    print("  VERDICT — projected daily NET against your ₹2-5K target")
    print("═" * 90)
    eq_s = eq_agg.summary(capital)
    obd_s = obd_agg.summary(fno_capital)
    ft_s = ft_agg.summary(fno_capital)
    rows = [
        {"strategy": "Equity ensemble", **{k: eq_s.get(k, "-") for k in
            ["trades", "win_pct", "net", "median_day", "best_day", "worst_day", "max_drawdown"]}},
        {"strategy": "F&O option_buy_directional", **{k: obd_s.get(k, "-") for k in
            ["trades", "win_pct", "net", "median_day", "best_day", "worst_day", "max_drawdown"]}},
        {"strategy": "F&O futures_trend", **{k: ft_s.get(k, "-") for k in
            ["trades", "win_pct", "net", "median_day", "best_day", "worst_day", "max_drawdown"]}},
    ]
    _print_table(rows, "Per-strategy summary (60-day OOS, after fees)")

    # combined daily
    combined_per_day = defaultdict(float)
    for t in all_trades + obd_all_trades + ft_all_trades:
        combined_per_day[t.entry_time.date()] += t.net_pnl
    if combined_per_day:
        sorted_pnls = sorted(combined_per_day.values())
        median = sorted_pnls[len(sorted_pnls) // 2]
        n_target_2k = sum(1 for v in combined_per_day.values() if v >= 2000)
        n_target_5k = sum(1 for v in combined_per_day.values() if v >= 5000)
        n_loss = sum(1 for v in combined_per_day.values() if v < 0)
        n_loss_2pct = sum(1 for v in combined_per_day.values() if v < -0.02 * (capital + fno_capital))
        total_days = len(combined_per_day)
        print(f"\n  COMBINED daily P&L over {total_days} trading days (Equity + F&O):")
        print(f"    median day:        ₹{median:>+8,.0f}")
        print(f"    days with ≥ ₹2K:   {n_target_2k}/{total_days}  ({n_target_2k/total_days*100:.0f}%)")
        print(f"    days with ≥ ₹5K:   {n_target_5k}/{total_days}  ({n_target_5k/total_days*100:.0f}%)")
        print(f"    losing days:       {n_loss}/{total_days}  ({n_loss/total_days*100:.0f}%)")
        print(f"    catastrophic days: {n_loss_2pct}/{total_days}  (>2% combined loss = -₹{0.02*(capital+fno_capital):,.0f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
