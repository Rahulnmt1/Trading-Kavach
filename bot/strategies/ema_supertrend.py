"""EMA crossover with Supertrend filter.

- BUY  when EMA_fast > EMA_slow AND Supertrend direction == +1
- SELL when EMA_fast < EMA_slow AND Supertrend direction == -1
Supertrend line itself becomes the stop-loss.
"""
from __future__ import annotations

import pandas as pd

from ..config import EMASupertrendCfg, load_config
from ..indicators import ema, supertrend
from .base import Signal, SignalType, Strategy, atr_levels


class EMASupertrendStrategy(Strategy):
    name = "EMA_Supertrend"

    def __init__(self, cfg: EMASupertrendCfg):
        self.cfg = cfg

    def generate(self, symbol: str, df: pd.DataFrame) -> Signal:
        need = max(self.cfg.ema_slow, self.cfg.supertrend_period) + 5
        if df.empty or len(df) < need:
            return self.hold(symbol, 0.0, f"need {need} bars, got {len(df)}", self.name)

        df = df.copy()
        df["ema_fast"] = ema(df["close"], self.cfg.ema_fast)
        df["ema_slow"] = ema(df["close"], self.cfg.ema_slow)
        st = supertrend(df, self.cfg.supertrend_period, self.cfg.supertrend_multiplier)
        df = df.join(st)

        last = df.iloc[-1]
        prev = df.iloc[-2]
        ltp = float(last["close"])
        st_dir = int(last["direction"])

        risk_cfg = load_config().risk
        sl_mult = risk_cfg.sl_atr_mult
        tp_mult = risk_cfg.tp_atr_mult

        bullish_cross = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
        bearish_cross = prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]

        if (last["ema_fast"] > last["ema_slow"]) and st_dir == 1 and (bullish_cross or st_dir != prev["direction"]):
            sl, tp = atr_levels(df, ltp, is_long=True, sl_mult=sl_mult, tp_mult=tp_mult)
            return Signal(
                symbol=symbol, type=SignalType.BUY, price=ltp,
                stop_loss=sl, take_profit=tp,
                confidence=0.75, strategy=self.name,
                reason=f"bullish: EMA{self.cfg.ema_fast}>EMA{self.cfg.ema_slow}, Supertrend+",
            )
        if (last["ema_fast"] < last["ema_slow"]) and st_dir == -1 and (bearish_cross or st_dir != prev["direction"]):
            sl, tp = atr_levels(df, ltp, is_long=False, sl_mult=sl_mult, tp_mult=tp_mult)
            return Signal(
                symbol=symbol, type=SignalType.SELL, price=ltp,
                stop_loss=sl, take_profit=tp,
                confidence=0.75, strategy=self.name,
                reason=f"bearish: EMA{self.cfg.ema_fast}<EMA{self.cfg.ema_slow}, Supertrend-",
            )
        return self.hold(symbol, ltp,
                         f"no fresh cross (EMA fast={last['ema_fast']:.2f}, slow={last['ema_slow']:.2f}, dir={st_dir})",
                         self.name)
