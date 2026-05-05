"""Index-futures trend-following strategy — F&O segment, Phase 2.

The simplest viable trend-follower on 5-minute bars of an index future:

* Compute fast (20) and slow (50) EMAs on the close.
* On a freshly-formed bullish cross (fast crosses *above* slow within
  the last ``cross_lookback_bars`` bars) AND price still above the fast
  EMA, emit a BUY at last close with ATR-based SL / TP.
* Symmetric for bearish cross → SELL.

Why this strategy first?

Index futures (NIFTY, BANKNIFTY) trend strongly intraday once a
direction is established — the wider ranges that make them risky also
make trend-following more rewarding than mean-reversion. EMA20/EMA50
on 5m bars is the canonical "professional intraday futures" baseline;
we'll layer on multi-timeframe confirmation (15m trend filter) and a
volume/momentum gate in Phase 2.5 if the paper results warrant it.

The ATR multipliers are *wider* than the equity ``EMA_Supertrend``
strategy (2.0× SL / 3.0× TP vs 1.0× / 1.5×) because:

1. Index futures have larger absolute INR ranges per bar (50-150 points
   on NIFTY, 200-500 on BANKNIFTY), so a 1.0× ATR stop would be too
   tight and noise out frequently.
2. Lot-size means each bar's noise translates to more INR P&L volatility
   — a 2.0× SL gives the trade more room to breathe before the trailing
   stop kicks in.
3. The reward:risk ratio (1.5R) matches the equity strategies, so risk
   sizing math in :mod:`bot.risk` produces an apples-to-apples 1%-of-
   capital max loss.
"""
from __future__ import annotations

import pandas as pd

from ..base import Signal, SignalType, Strategy, atr_levels
from ...config import FuturesTrendCfg
from ...indicators import ema


class FuturesTrendStrategy(Strategy):
    name = "futures_trend"

    def __init__(self, cfg: FuturesTrendCfg) -> None:
        self.cfg = cfg

    def generate(self, symbol: str, df: pd.DataFrame) -> Signal:
        # Need at least slow-EMA worth of bars + a small buffer for the
        # cross-lookback check. Index intraday data starts at 09:15 IST
        # so on 5m bars we have ≤ 78 bars/day — the 50-EMA is borderline
        # at session open, which is fine: we'd rather miss the first
        # 30 minutes than fire on a half-warm EMA.
        need = self.cfg.ema_slow + self.cfg.cross_lookback_bars + 1
        if df.empty or len(df) < need:
            return self.hold(
                symbol, 0.0,
                f"need {need} bars, got {len(df)}",
                self.name,
            )

        df = df.copy()
        df["ema_fast"] = ema(df["close"], self.cfg.ema_fast)
        df["ema_slow"] = ema(df["close"], self.cfg.ema_slow)

        last = df.iloc[-1]
        ltp = float(last["close"])
        fast_now = float(last["ema_fast"])
        slow_now = float(last["ema_slow"])

        # Did the fast EMA cross above the slow EMA at any point within
        # the last ``cross_lookback_bars`` bars? We look at the SIGN of
        # (fast - slow) and see if it flipped from non-positive to
        # positive (bullish) or non-negative to negative (bearish).
        diff = (df["ema_fast"] - df["ema_slow"]).iloc[-(self.cfg.cross_lookback_bars + 1):]
        prev_sign = diff.iloc[:-1]
        curr_sign = diff.iloc[1:]
        bullish_cross = ((prev_sign.values <= 0) & (curr_sign.values > 0)).any()
        bearish_cross = ((prev_sign.values >= 0) & (curr_sign.values < 0)).any()

        # Bullish: recent cross + price still above fast EMA = trend intact.
        if bullish_cross and ltp > fast_now and fast_now > slow_now:
            sl, tp = atr_levels(
                df, ltp, is_long=True,
                sl_mult=self.cfg.sl_atr_mult,
                tp_mult=self.cfg.tp_atr_mult,
            )
            return Signal(
                symbol=symbol, type=SignalType.BUY, price=ltp,
                stop_loss=sl, take_profit=tp,
                confidence=0.70, strategy=self.name,
                reason=(f"bullish trend: EMA{self.cfg.ema_fast}({fast_now:.2f}) > "
                        f"EMA{self.cfg.ema_slow}({slow_now:.2f}), "
                        f"recent cross + price above fast EMA"),
            )

        # Bearish: recent cross + price still below fast EMA = trend intact.
        if bearish_cross and ltp < fast_now and fast_now < slow_now:
            sl, tp = atr_levels(
                df, ltp, is_long=False,
                sl_mult=self.cfg.sl_atr_mult,
                tp_mult=self.cfg.tp_atr_mult,
            )
            return Signal(
                symbol=symbol, type=SignalType.SELL, price=ltp,
                stop_loss=sl, take_profit=tp,
                confidence=0.70, strategy=self.name,
                reason=(f"bearish trend: EMA{self.cfg.ema_fast}({fast_now:.2f}) < "
                        f"EMA{self.cfg.ema_slow}({slow_now:.2f}), "
                        f"recent cross + price below fast EMA"),
            )

        return self.hold(
            symbol, ltp,
            f"no fresh cross (fast={fast_now:.2f}, slow={slow_now:.2f}, "
            f"price={ltp:.2f})",
            self.name,
        )
