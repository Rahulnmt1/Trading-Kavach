"""Opening Range Breakout (ORB) strategy.

Long when price breaks above the high of the first N minutes;
short when it breaks below the low. Stop at the opposite end of the range.
"""
from __future__ import annotations

from datetime import time

import pandas as pd

from ..config import ORBCfg, load_config
from .base import Signal, SignalType, Strategy, atr_levels


class ORBStrategy(Strategy):
    name = "ORB"

    def __init__(self, cfg: ORBCfg):
        self.cfg = cfg

    def generate(self, symbol: str, df: pd.DataFrame) -> Signal:
        if df.empty or len(df) < 5:
            return self.hold(symbol, 0.0, "insufficient bars", self.name)

        cfg = load_config()
        session = cfg.session
        open_t = session.t("market_open")
        cutoff_min = open_t.minute + self.cfg.range_minutes
        cutoff_h = open_t.hour + cutoff_min // 60
        cutoff = time(cutoff_h, cutoff_min % 60)

        opening_range = df[df.index.time <= cutoff]
        if opening_range.empty:
            return self.hold(symbol, float(df["close"].iloc[-1]), "no opening range yet", self.name)

        # Entry-cutoff gate: ORB has follow-through edge mainly in the
        # first ~2 hours of the session. After ``entry_cutoff`` the
        # morning range has been bounded for so long that "breakouts"
        # of it are typically late moves that mean-revert by EOD. A bar
        # at or after the cutoff returns HOLD even if price is breaking
        # out — let other strategies (e.g. EMA-Supertrend) take that
        # later trade if they have edge there.
        h, m = self.cfg.entry_cutoff.split(":")
        entry_cutoff_t = time(int(h), int(m))
        last_bar_time = df.index[-1].time()
        if last_bar_time >= entry_cutoff_t:
            return self.hold(
                symbol, float(df["close"].iloc[-1]),
                f"past entry cutoff {self.cfg.entry_cutoff}", self.name,
            )

        rng_high = float(opening_range["high"].max())
        rng_low = float(opening_range["low"].min())
        ltp = float(df["close"].iloc[-1])
        buf = self.cfg.breakout_buffer_pct / 100.0
        sl_mult = cfg.risk.sl_atr_mult
        tp_mult = cfg.risk.tp_atr_mult

        if ltp > rng_high * (1 + buf):
            sl, tp = atr_levels(df, ltp, is_long=True, sl_mult=sl_mult, tp_mult=tp_mult)
            return Signal(
                symbol=symbol, type=SignalType.BUY, price=ltp,
                stop_loss=sl, take_profit=tp,
                confidence=0.8, strategy=self.name,
                reason=f"break above ORB high {rng_high:.2f}",
            )
        if ltp < rng_low * (1 - buf):
            sl, tp = atr_levels(df, ltp, is_long=False, sl_mult=sl_mult, tp_mult=tp_mult)
            return Signal(
                symbol=symbol, type=SignalType.SELL, price=ltp,
                stop_loss=sl, take_profit=tp,
                confidence=0.8, strategy=self.name,
                reason=f"break below ORB low {rng_low:.2f}",
            )
        return self.hold(symbol, ltp, f"inside range [{rng_low:.2f},{rng_high:.2f}]", self.name)
