"""Multi-timeframe wrapper: confirms a fast-frame signal on a slower frame.

Trade philosophy: take a 5-minute setup ONLY when the 15-minute trend agrees.
This kills countertrend noise that plagues pure 5m strategies.

  base signal (5m)  +  confirm filter (15m)  =  final signal
        BUY                  uptrend                 BUY
        BUY                 not up                  HOLD
        SELL                downtrend                SELL
        SELL                not down                HOLD
"""
from __future__ import annotations

import pandas as pd

from ..config import MultiTimeframeCfg
from ..data import history
from ..indicators import ema
from .base import Signal, SignalType, Strategy


class MultiTimeframeStrategy(Strategy):
    """Wraps a base strategy and adds a higher-timeframe trend filter."""

    def __init__(self, base: Strategy, cfg: MultiTimeframeCfg):
        self.base = base
        self.cfg = cfg
        self.name = f"MTF({base.name})"

    def _confirm_uptrend(self, symbol: str) -> bool:
        """Higher timeframe close > EMA21 and EMA9 > EMA21."""
        df = history(symbol, days=3, interval=self.cfg.confirm_interval)
        if df.empty or len(df) < 25:
            return False
        e9 = ema(df["close"], 9).iloc[-1]
        e21 = ema(df["close"], 21).iloc[-1]
        return float(df["close"].iloc[-1]) > e21 and e9 > e21

    def _confirm_downtrend(self, symbol: str) -> bool:
        df = history(symbol, days=3, interval=self.cfg.confirm_interval)
        if df.empty or len(df) < 25:
            return False
        e9 = ema(df["close"], 9).iloc[-1]
        e21 = ema(df["close"], 21).iloc[-1]
        return float(df["close"].iloc[-1]) < e21 and e9 < e21

    def generate(self, symbol: str, df: pd.DataFrame) -> Signal:
        sig = self.base.generate(symbol, df)
        if sig.type == SignalType.HOLD:
            return sig

        if sig.type == SignalType.BUY and not self._confirm_uptrend(symbol):
            return Signal(
                symbol=symbol, type=SignalType.HOLD, price=sig.price,
                strategy=self.name, confidence=0.0,
                reason=f"{self.base.name} BUY rejected — no {self.cfg.confirm_interval} uptrend confirmation",
            )
        if sig.type == SignalType.SELL and not self._confirm_downtrend(symbol):
            return Signal(
                symbol=symbol, type=SignalType.HOLD, price=sig.price,
                strategy=self.name, confidence=0.0,
                reason=f"{self.base.name} SELL rejected — no {self.cfg.confirm_interval} downtrend confirmation",
            )

        sig.confidence = min(1.0, sig.confidence + 0.1)
        sig.strategy = self.name
        sig.reason = f"{sig.reason} [confirmed on {self.cfg.confirm_interval}]"
        return sig
