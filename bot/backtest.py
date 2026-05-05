"""Bar-by-bar backtester.

Runs the ensemble strategy over historical intraday bars and reports trade-level P&L.
Slippage and fees are modelled to match `paper.py`. Results are NOT a guarantee of
future performance — they're a sanity check on whether the strategy has any edge.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import List, Optional

import pandas as pd

from .broker.paper import _fees
from .broker.base import OrderSide
from .config import load_config
from .data import history
from .logger import logger
from .strategies import build_default_ensemble
from .strategies.base import SignalType


@dataclass
class Trade:
    symbol: str
    side: str
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: Optional[pd.Timestamp]
    exit_price: Optional[float]
    qty: int
    pnl: float
    fees: float
    reason: str


def backtest_symbol(symbol: str, days: int = 7, interval: str = "5m") -> dict:
    cfg = load_config()
    ensemble = build_default_ensemble()
    df = history(symbol, days=days, interval=interval)
    if df.empty:
        return {"symbol": symbol, "trades": [], "summary": {"net_pnl": 0.0}}

    trades: List[Trade] = []
    in_pos: Optional[Trade] = None
    so = cfg.session.t("square_off")

    grouped = df.groupby(df.index.date)
    for day, day_df in grouped:
        day_df = day_df.sort_index()
        for i in range(20, len(day_df)):
            window = day_df.iloc[: i + 1]
            bar = day_df.iloc[i]
            t = bar.name.time() if isinstance(bar.name, pd.Timestamp) else time(0, 0)

            if in_pos is not None:
                hit_sl = (in_pos.side == "BUY" and bar["low"] <= (in_pos.entry_price * 0.99)) or \
                         (in_pos.side == "SELL" and bar["high"] >= (in_pos.entry_price * 1.01))
                if hit_sl or t >= so or i == len(day_df) - 1:
                    exit_price = float(bar["close"])
                    pnl = (exit_price - in_pos.entry_price) * in_pos.qty if in_pos.side == "BUY" \
                          else (in_pos.entry_price - exit_price) * in_pos.qty
                    fees = _fees(OrderSide(in_pos.side), in_pos.qty, exit_price)
                    in_pos.exit_time = bar.name
                    in_pos.exit_price = exit_price
                    in_pos.pnl = pnl - in_pos.fees - fees
                    in_pos.fees += fees
                    trades.append(in_pos)
                    in_pos = None
                continue

            if t < cfg.session.t("trade_start") or t > cfg.session.t("trade_cutoff"):
                continue

            sig = ensemble.generate(symbol, window)
            if sig.type == SignalType.HOLD or sig.stop_loss is None:
                continue

            risk_per_share = abs(sig.price - sig.stop_loss)
            if risk_per_share <= 0:
                continue
            max_loss = cfg.capital.total * cfg.risk.max_loss_per_trade_pct / 100.0
            qty = max(1, int(max_loss // risk_per_share))
            entry_fees = _fees(
                OrderSide.BUY if sig.type == SignalType.BUY else OrderSide.SELL,
                qty, sig.price,
            )
            in_pos = Trade(
                symbol=symbol, side=sig.type.value,
                entry_time=bar.name, entry_price=float(sig.price),
                exit_time=None, exit_price=None,
                qty=qty, pnl=0.0, fees=entry_fees, reason=sig.reason,
            )

    net = sum(t.pnl for t in trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    summary = {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "net_pnl": round(net, 2),
        "avg_win": round(sum(t.pnl for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(t.pnl for t in losses) / len(losses), 2) if losses else 0.0,
        "total_fees": round(sum(t.fees for t in trades), 2),
    }
    logger.info("Backtest {}: {}", symbol, summary)
    return {"symbol": symbol, "trades": [t.__dict__ for t in trades], "summary": summary}


def backtest_watchlist(days: int = 7, interval: str = "5m") -> dict:
    out = {"symbols": {}, "totals": {"net_pnl": 0.0, "trades": 0, "wins": 0}}
    for sym in load_config().symbols:
        r = backtest_symbol(sym, days, interval)
        out["symbols"][sym] = r["summary"]
        out["totals"]["net_pnl"] += r["summary"]["net_pnl"]
        out["totals"]["trades"] += r["summary"]["trades"]
        out["totals"]["wins"] += r["summary"]["wins"]
    out["totals"]["win_rate"] = (
        out["totals"]["wins"] / out["totals"]["trades"] if out["totals"]["trades"] else 0.0
    )
    return out
