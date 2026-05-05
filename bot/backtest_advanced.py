"""Advanced backtester — performance metrics + walk-forward analysis.

Metrics computed per equity curve:
  - Total return, CAGR
  - Sharpe ratio (annualised, rf=6% — RBI 10y benchmark)
  - Sortino ratio (downside-only deviation)
  - Max drawdown (and drawdown duration)
  - Calmar ratio (CAGR / |MDD|)
  - Win rate, profit factor, expectancy
  - Avg win / avg loss / payoff ratio

Walk-forward analysis splits history into rolling (in-sample, out-of-sample)
windows and reports OOS performance only — the only result that matters,
because it removes overfitting bias.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import List

import numpy as np
import pandas as pd

from .backtest import backtest_symbol
from .config import load_config
from .data import history
from .logger import logger

RISK_FREE_RATE = 0.06           # India 10Y G-Sec ~6%
TRADING_DAYS = 252


@dataclass
class Metrics:
    total_return_pct: float = 0.0
    cagr_pct: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_duration_days: int = 0
    calmar: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    payoff_ratio: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: float = 0.0

    def as_dict(self) -> dict:
        return {k: round(v, 4) if isinstance(v, float) else v for k, v in self.__dict__.items()}


def equity_curve_from_trades(trades: List[dict], starting_cash: float) -> pd.Series:
    if not trades:
        return pd.Series([starting_cash], index=[pd.Timestamp.now()], name="equity")
    df = pd.DataFrame(trades)
    df["exit_time"] = pd.to_datetime(df["exit_time"])
    df = df.dropna(subset=["exit_time"]).sort_values("exit_time")
    eq = starting_cash + df["pnl"].cumsum()
    eq.index = df["exit_time"]
    eq.name = "equity"
    return eq


def compute_metrics(trades: List[dict], starting_cash: float = 100_000) -> Metrics:
    m = Metrics()
    if not trades:
        return m

    pnls = pd.Series([t["pnl"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    m.trades = len(pnls)
    m.wins = len(wins)
    m.losses = len(losses)
    m.net_pnl = float(pnls.sum())
    m.win_rate = len(wins) / len(pnls) if len(pnls) else 0.0
    m.avg_win = float(wins.mean()) if len(wins) else 0.0
    m.avg_loss = float(losses.mean()) if len(losses) else 0.0
    m.payoff_ratio = abs(m.avg_win / m.avg_loss) if m.avg_loss else 0.0
    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = abs(float(losses.sum())) if len(losses) else 0.0
    m.profit_factor = gross_profit / gross_loss if gross_loss else float("inf")
    m.expectancy = m.win_rate * m.avg_win + (1 - m.win_rate) * m.avg_loss

    eq = equity_curve_from_trades(trades, starting_cash)
    if len(eq) < 2:
        return m

    days = max((eq.index[-1] - eq.index[0]).days, 1)
    m.total_return_pct = (eq.iloc[-1] / starting_cash - 1) * 100
    m.cagr_pct = ((eq.iloc[-1] / starting_cash) ** (365 / days) - 1) * 100

    daily = eq.resample("1D").last().ffill().pct_change().dropna()
    if len(daily) > 1:
        excess = daily - RISK_FREE_RATE / TRADING_DAYS
        std = daily.std()
        m.sharpe = float(np.sqrt(TRADING_DAYS) * excess.mean() / std) if std else 0.0
        downside = daily[daily < 0].std()
        m.sortino = float(np.sqrt(TRADING_DAYS) * excess.mean() / downside) if downside else 0.0

    running_max = eq.cummax()
    drawdown = (eq - running_max) / running_max
    m.max_drawdown_pct = float(drawdown.min() * 100)

    in_dd = drawdown < 0
    if in_dd.any():
        groups = (in_dd != in_dd.shift()).cumsum()
        durations = in_dd.groupby(groups).sum()
        m.max_drawdown_duration_days = int(durations.max())

    m.calmar = m.cagr_pct / abs(m.max_drawdown_pct) if m.max_drawdown_pct else 0.0
    return m


@dataclass
class WalkForwardWindow:
    is_start: pd.Timestamp
    is_end: pd.Timestamp
    oos_start: pd.Timestamp
    oos_end: pd.Timestamp
    metrics: Metrics = field(default_factory=Metrics)


def walk_forward_symbol(
    symbol: str,
    days: int = 60,
    interval: str = "5m",
    is_window_days: int = 30,
    oos_window_days: int = 10,
    step_days: int = 10,
) -> dict:
    """Backtest sliding (IS, OOS) windows. Reports OOS-only aggregated metrics."""
    df = history(symbol, days=days, interval=interval)
    if df.empty:
        return {"symbol": symbol, "windows": [], "aggregate": Metrics().as_dict()}

    start = df.index.min()
    end = df.index.max()

    windows: List[WalkForwardWindow] = []
    cursor = start
    while True:
        is_start = cursor
        is_end = is_start + timedelta(days=is_window_days)
        oos_start = is_end
        oos_end = oos_start + timedelta(days=oos_window_days)
        if oos_end > end:
            break
        windows.append(WalkForwardWindow(is_start, is_end, oos_start, oos_end))
        cursor = cursor + timedelta(days=step_days)

    all_oos_trades: List[dict] = []
    for w in windows:
        oos_df = df[(df.index >= w.oos_start) & (df.index < w.oos_end)]
        if oos_df.empty:
            continue
        # Backtest the strategy on the full history then keep only trades whose
        # entry falls within the OOS window. (Strategy is rule-based — no fitting
        # needed; the in-sample window is reserved for future hyper-param search.)
        result = backtest_symbol(symbol, days=days, interval=interval)
        oos_trades = [
            t for t in result["trades"]
            if t.get("entry_time") and pd.Timestamp(t["entry_time"]) >= w.oos_start
            and pd.Timestamp(t["entry_time"]) < w.oos_end
        ]
        w.metrics = compute_metrics(oos_trades)
        all_oos_trades.extend(oos_trades)

    aggregate = compute_metrics(all_oos_trades)
    return {
        "symbol": symbol,
        "windows": [
            {
                "is": f"{w.is_start.date()}→{w.is_end.date()}",
                "oos": f"{w.oos_start.date()}→{w.oos_end.date()}",
                **w.metrics.as_dict(),
            }
            for w in windows
        ],
        "aggregate": aggregate.as_dict(),
    }


def walk_forward_watchlist(days: int = 60, interval: str = "5m") -> dict:
    cfg = load_config()
    wf = cfg.backtest.walk_forward
    out = {"symbols": {}, "totals": {}}
    all_trades: List[dict] = []
    for sym in cfg.symbols:
        r = walk_forward_symbol(
            sym, days=days, interval=interval,
            is_window_days=wf.is_window_days,
            oos_window_days=wf.oos_window_days,
            step_days=wf.step_days,
        )
        out["symbols"][sym] = r["aggregate"]
        logger.info("Walk-forward {}: {}", sym, r["aggregate"])
    return out
