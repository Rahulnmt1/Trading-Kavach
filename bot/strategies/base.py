"""Strategy base class, Signal dataclass, and shared ATR-based SL/TP helper."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

import pandas as pd

from ..indicators import atr as _atr


class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class Signal:
    symbol: str
    type: SignalType
    price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    confidence: float = 1.0          # 0..1
    reason: str = ""
    strategy: str = ""
    ts: datetime = field(default_factory=datetime.utcnow)


def atr_levels(
    df: pd.DataFrame,
    ltp: float,
    is_long: bool,
    sl_mult: float,
    tp_mult: float,
    fallback_pct: float = 0.5,
) -> Tuple[float, float]:
    """Compute (stop_loss, take_profit) from price + ATR(14) on the given bars.

    SL distance  =  sl_mult * ATR(14)
    TP distance  =  tp_mult * ATR(14)

    If ATR is NaN (insufficient bars early in the session), fall back to a flat
    ``fallback_pct`` of price for SL and ``fallback_pct * tp_mult/sl_mult`` for TP
    so the function always returns finite levels — never None.
    """
    a_series = _atr(df, 14)
    a = float(a_series.iloc[-1]) if len(a_series) else float("nan")
    if pd.isna(a) or a <= 0:
        a = ltp * fallback_pct / 100.0
    sl_dist = sl_mult * a
    tp_dist = tp_mult * a
    if is_long:
        return ltp - sl_dist, ltp + tp_dist
    return ltp + sl_dist, ltp - tp_dist


class Strategy(ABC):
    name: str = "base"

    @abstractmethod
    def generate(self, symbol: str, df: pd.DataFrame) -> Signal:
        """Examine the latest bar of `df` and return a Signal (BUY/SELL/HOLD)."""
        raise NotImplementedError

    @staticmethod
    def hold(symbol: str, price: float, reason: str, name: str = "base") -> Signal:
        return Signal(symbol=symbol, type=SignalType.HOLD, price=price,
                      reason=reason, strategy=name, confidence=0.0)
