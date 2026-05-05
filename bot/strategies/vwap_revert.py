"""VWAP mean-reversion strategy.

Fade extremes when price is far from VWAP and RSI confirms.
- BUY  when price < VWAP * (1 - dev) AND RSI < oversold
- SELL when price > VWAP * (1 + dev) AND RSI > overbought
"""
from __future__ import annotations

import pandas as pd

from ..config import VWAPRevertCfg, load_config
from ..indicators import rsi, vwap
from .base import Signal, SignalType, Strategy, atr_levels


class VWAPRevertStrategy(Strategy):
    name = "VWAP_Revert"

    def __init__(self, cfg: VWAPRevertCfg):
        self.cfg = cfg

    def generate(self, symbol: str, df: pd.DataFrame) -> Signal:
        if df.empty or len(df) < 20:
            return self.hold(symbol, 0.0, "insufficient bars", self.name)

        df = df.copy()
        df["vwap"] = vwap(df)
        df["rsi"] = rsi(df["close"], 14)

        last = df.iloc[-1]
        ltp = float(last["close"])
        vw = float(last["vwap"])
        r = float(last["rsi"]) if not pd.isna(last["rsi"]) else 50.0
        dev = self.cfg.deviation_pct / 100.0
        risk_cfg = load_config().risk
        sl_mult = risk_cfg.sl_atr_mult
        tp_mult = risk_cfg.tp_atr_mult

        if ltp < vw * (1 - dev) and r < self.cfg.rsi_oversold:
            sl, tp = atr_levels(df, ltp, is_long=True, sl_mult=sl_mult, tp_mult=tp_mult)
            return Signal(
                symbol=symbol, type=SignalType.BUY, price=ltp,
                stop_loss=sl, take_profit=tp,
                confidence=0.7, strategy=self.name,
                reason=f"oversold below VWAP {vw:.2f}, RSI={r:.1f}",
            )
        if ltp > vw * (1 + dev) and r > self.cfg.rsi_overbought:
            sl, tp = atr_levels(df, ltp, is_long=False, sl_mult=sl_mult, tp_mult=tp_mult)
            return Signal(
                symbol=symbol, type=SignalType.SELL, price=ltp,
                stop_loss=sl, take_profit=tp,
                confidence=0.7, strategy=self.name,
                reason=f"overbought above VWAP {vw:.2f}, RSI={r:.1f}",
            )
        return self.hold(symbol, ltp, f"price near VWAP (dev={ltp/vw-1:+.3%}, RSI={r:.1f})", self.name)
