"""Directional option-buying strategy — F&O segment, Phase 3.

The same EMA20 / EMA50 crossover engine as ``futures_trend`` (Phase 2),
but instead of buying the future on a bullish cross we buy the ATM CE,
and instead of selling the future on a bearish cross we buy the ATM PE.

Why option-BUYING first?

1. **Limited risk.** Worst-case loss = full premium paid. With ATM
   weekly NIFTY options around ₹100-200 / lot of 75, that's ₹7,500 –
   ₹15,000 of capital per trade. Compatible with the default ₹50,000
   F&O budget — futures need ₹90,000+ margin per lot.
2. **Convex payoff.** A 100-pt favourable spot move on a fresh ATM
   weekly call moves the premium by ~50 pts (delta ≈ 0.5), so 1
   lot earns ~₹3,750 — better risk-reward than the equivalent futures
   trade at the same lot size, ON A DIRECTIONAL VIEW (the catch:
   theta decay punishes range-bound days and IV crush punishes
   post-event days).
3. **No margin model.** Phase 3 paper broker can fill these without the
   SPAN+exposure plumbing that option SELLING (Phase 4) requires.

How SL/TP work
==============

The strategy sees the UNDERLYING (spot) bars — same data feed as the
futures strategy. It picks an ATR-based stop-loss and take-profit in
**spot space**:

    sl_spot  = entry_spot − sl_atr_mult * ATR
    tp_spot  = entry_spot + tp_atr_mult * ATR     (long CE; flipped for PE)

Then it translates those spot levels to **premium space** via
Black-Scholes at the SL spot and TP spot:

    sl_premium = BS(sl_spot, K, T, σ, r, opt_type)
    tp_premium = BS(tp_spot, K, T, σ, r, opt_type)

The signal returns the OPTION's tradingsymbol with sl/tp denominated in
premium. The position manager (executor) then evaluates SL/TP against
the **synthetic option bars** that ``bot/data.py::intraday_bars``
produces from the underlying spot bars + Black-Scholes.

The chain is internally consistent: a spot move past sl_spot will
produce a synthetic premium past sl_premium because both sides use the
same BS pricer.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional, Tuple

import pandas as pd
import pytz

from ...config import OptionBuyDirectionalCfg
from ...indicators import atr as _atr, ema
from ...instruments.fno import (
    current_expiry, resolve_atm_option, underlying_from_tradingsymbol,
)
from ...options.pricing import bs, years_to_expiry
from ...logger import logger
from ..base import Signal, SignalType, Strategy

IST = pytz.timezone("Asia/Kolkata")


class OptionBuyDirectionalStrategy(Strategy):
    name = "option_buy_directional"

    def __init__(self, cfg: OptionBuyDirectionalCfg) -> None:
        self.cfg = cfg

    def generate(self, symbol: str, df: pd.DataFrame) -> Signal:
        """``symbol`` is the watchlist entry — bare underlying ("NIFTY")
        or futures tradingsymbol ("NIFTY26APRFUT"). ``df`` carries that
        symbol's bars (spot via the yfinance proxy in paper mode).

        Returns a Signal whose ``symbol`` is the **option tradingsymbol**
        we want to BUY (e.g. "NIFTY26MAY24600CE"), with price/SL/TP all
        in option-premium space.
        """
        underlying = underlying_from_tradingsymbol(symbol)
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
        spot_now = float(last["close"])
        fast_now = float(last["ema_fast"])
        slow_now = float(last["ema_slow"])

        # Same cross-detection as futures_trend: did fast cross above/below
        # slow within the last `cross_lookback_bars + 1` window?
        diff = (df["ema_fast"] - df["ema_slow"]).iloc[-(self.cfg.cross_lookback_bars + 1):]
        prev_sign = diff.iloc[:-1]
        curr_sign = diff.iloc[1:]
        bullish_cross = ((prev_sign.values <= 0) & (curr_sign.values > 0)).any()
        bearish_cross = ((prev_sign.values >= 0) & (curr_sign.values < 0)).any()

        # Trend-confirmation gate: don't fire on fading momentum.
        if bullish_cross and spot_now > fast_now and fast_now > slow_now:
            return self._build_long_option_signal(
                df, underlying, spot_now, opt_type="CE", direction="bullish",
            )
        if bearish_cross and spot_now < fast_now and fast_now < slow_now:
            return self._build_long_option_signal(
                df, underlying, spot_now, opt_type="PE", direction="bearish",
            )

        return self.hold(
            symbol, spot_now,
            f"no fresh cross (fast={fast_now:.2f}, slow={slow_now:.2f}, "
            f"price={spot_now:.2f})",
            self.name,
        )

    # ── helpers ─────────────────────────────────────────────────────────

    def _build_long_option_signal(
        self,
        df: pd.DataFrame,
        underlying: str,
        spot_now: float,
        opt_type: str,
        direction: str,
    ) -> Signal:
        """Resolve the ATM option, compute SL/TP in premium space, return Signal."""
        try:
            inst = resolve_atm_option(underlying, opt_type, spot_now)
        except (KeyError, ValueError) as e:
            logger.warning("[option_buy] cannot resolve {} {}: {}",
                           underlying, opt_type, e)
            return self.hold(
                underlying, spot_now,
                f"cannot resolve ATM option: {e}",
                self.name,
            )

        # SL/TP in spot space (ATR-based).
        atr_series = _atr(df, 14)
        atr_val = float(atr_series.iloc[-1]) if len(atr_series) else 0.0
        if atr_val <= 0:
            atr_val = spot_now * 0.005      # 0.5% fallback
        sl_dist = self.cfg.sl_atr_mult * atr_val
        tp_dist = self.cfg.tp_atr_mult * atr_val

        if opt_type == "CE":          # long-call: bullish on underlying
            spot_sl = spot_now - sl_dist
            spot_tp = spot_now + tp_dist
        else:                          # PE: bearish on underlying
            spot_sl = spot_now + sl_dist
            spot_tp = spot_now - tp_dist

        # Translate to premium space at constant T (we ignore the ~minutes
        # difference between SL/TP exit times — second-order vs the
        # intra-day move).
        T = years_to_expiry(inst.expiry)
        prem_now = bs(
            spot_now, inst.strike, T, opt_type,
            sigma=self.cfg.iv, r=self.cfg.risk_free_rate,
        )
        if prem_now <= 0:
            return self.hold(
                underlying, spot_now,
                f"BS premium {prem_now:.2f} non-positive — option too far OTM",
                self.name,
            )
        prem_sl = bs(
            spot_sl, inst.strike, T, opt_type,
            sigma=self.cfg.iv, r=self.cfg.risk_free_rate,
        )
        prem_tp = bs(
            spot_tp, inst.strike, T, opt_type,
            sigma=self.cfg.iv, r=self.cfg.risk_free_rate,
        )

        # Floor the SL premium so a single down-spike doesn't take 90%+
        # of the premium before the position manager reacts (and so the
        # risk manager has a meaningful stop distance to size against).
        sl_floor = prem_now * self.cfg.min_sl_premium_pct
        prem_sl = max(prem_sl, sl_floor)

        # Sanity: SL must be below entry, TP above (for long premium).
        if not (prem_sl < prem_now < prem_tp):
            return self.hold(
                underlying, spot_now,
                f"degenerate SL/TP (sl={prem_sl:.2f} entry={prem_now:.2f} tp={prem_tp:.2f})",
                self.name,
            )

        return Signal(
            symbol=inst.tradingsymbol,
            type=SignalType.BUY,                  # always BUY in option-buying
            price=round(prem_now, 2),
            stop_loss=round(prem_sl, 2),
            take_profit=round(prem_tp, 2),
            confidence=0.65,                       # see Phase-3 README note
            strategy=self.name,
            reason=(
                f"{direction} cross on {underlying}@{spot_now:.2f} → "
                f"buy ATM {inst.opt_type} {inst.strike} "
                f"(SL@spot={spot_sl:.2f}→prem={prem_sl:.2f}, "
                f"TP@spot={spot_tp:.2f}→prem={prem_tp:.2f})"
            ),
        )
