"""Ensemble voter — combines multiple strategies into one final signal.

Final signal is BUY only if at least `min_agree` strategies say BUY (and none say SELL),
SELL only if at least `min_agree` say SELL (and none say BUY), else HOLD.
"""
from __future__ import annotations

from collections import Counter
from typing import List

import pandas as pd

from .base import Signal, SignalType, Strategy


class Ensemble(Strategy):
    name = "Ensemble"

    def __init__(self, members: List[Strategy], min_agree: int = 2):
        self.members = members
        self.min_agree = min_agree

    def generate(self, symbol: str, df: pd.DataFrame) -> Signal:
        signals = [m.generate(symbol, df) for m in self.members]
        votes = Counter(s.type for s in signals)
        ltp = float(df["close"].iloc[-1]) if not df.empty else 0.0

        buys = [s for s in signals if s.type == SignalType.BUY]
        sells = [s for s in signals if s.type == SignalType.SELL]

        if len(buys) >= self.min_agree and len(sells) == 0:
            return self._merge(buys, SignalType.BUY)
        if len(sells) >= self.min_agree and len(buys) == 0:
            return self._merge(sells, SignalType.SELL)

        reasons = "; ".join(f"{s.strategy}={s.type.value}" for s in signals)
        return Signal(
            symbol=symbol, type=SignalType.HOLD, price=ltp,
            reason=f"no consensus ({reasons})", strategy=self.name, confidence=0.0,
        )

    def _merge(self, signals: List[Signal], side: SignalType) -> Signal:
        sym = signals[0].symbol
        price = signals[0].price
        if side == SignalType.BUY:
            stop_loss = max([s.stop_loss for s in signals if s.stop_loss], default=None)
            take_profit = min([s.take_profit for s in signals if s.take_profit], default=None)
        else:
            stop_loss = min([s.stop_loss for s in signals if s.stop_loss], default=None)
            take_profit = max([s.take_profit for s in signals if s.take_profit], default=None)

        confidence = sum(s.confidence for s in signals) / len(signals)
        reason = " | ".join(f"{s.strategy}: {s.reason}" for s in signals)
        return Signal(
            symbol=sym, type=side, price=price,
            stop_loss=stop_loss, take_profit=take_profit,
            confidence=confidence, strategy=self.name,
            reason=f"{len(signals)} agree → {side.value}. {reason}",
        )
