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

import math

import numpy as np
import pandas as pd
import pytz

from ...config import OptionBuyDirectionalCfg
from ...indicators import atr as _atr, bollinger as _bb, ema, rsi as _rsi
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
        cross_dir = None
        if bullish_cross and spot_now > fast_now and fast_now > slow_now:
            cross_dir = "bullish"
        elif bearish_cross and spot_now < fast_now and fast_now < slow_now:
            cross_dir = "bearish"

        if cross_dir is None:
            return self.hold(
                symbol, spot_now,
                f"no fresh cross (fast={fast_now:.2f}, slow={slow_now:.2f}, "
                f"price={spot_now:.2f})",
                self.name,
            )

        # ── FIX #35 (2026-05-15) — Multi-source price validation ────
        # Cross-check the yfinance spot against NSE's direct REST
        # endpoint before committing to a trade. Defends against the
        # rare-but-recurring yfinance bad-tick scenarios (post-weekend
        # Mondays, exchange-feed lag, scrape pipeline glitches —
        # see FIX #27 postmortem). Fail-open: if NSE is unreachable
        # we proceed on yfinance alone, since blocking the bot on a
        # third-party outage is a worse failure mode than potentially
        # transacting on a slightly-stale yfinance price.
        try:
            from ...data_sources.nse_direct import validate_against_yfinance
            _ms_raw = getattr(self.cfg, "multisource_max_divergence_pct", 1.0)
            ms_max = 1.0 if _ms_raw is None else float(_ms_raw)
            if ms_max > 0:
                ok, div_pct, nse_price = validate_against_yfinance(
                    underlying, spot_now, max_divergence_pct=ms_max,
                )
                if not ok and div_pct is not None:
                    return self.hold(
                        symbol, spot_now,
                        f"FIX #35: yfinance/NSE price divergence "
                        f"{div_pct:.2f}% > {ms_max:.2f}% threshold "
                        f"(yfinance={spot_now:.2f}, NSE={nse_price:.2f}) — "
                        f"refusing trade on suspect data",
                        self.name,
                    )
        except Exception:  # noqa: BLE001
            # Multi-source check is best-effort; never let it crash
            # the strategy. The exception itself is logged elsewhere.
            pass

        # ── FIX #32 (2026-05-15) — Volatility-regime filter ──────────
        # Refuse the trade if realised vol over the last
        # ``realized_vol_lookback_bars`` is below
        # ``min_realized_vol_pct``. Long-option buying needs a
        # trending underlying to overcome theta; in low-vol chop
        # regimes the premium decays even when spot moves in our
        # favor. The 2026-05-15 BANKNIFTY 13:11 entry had 9.32%
        # realised 1h vol (lowest of any journal-recorded entry) and
        # bled ₹22,711 by EOD. A 10% floor would have skipped that
        # entry while preserving every winning trade.
        rv_floor = float(getattr(self.cfg, "min_realized_vol_pct", 0.0) or 0.0)
        rv_lookback = int(getattr(self.cfg, "realized_vol_lookback_bars", 12) or 12)
        if rv_floor > 0 and len(df) > rv_lookback:
            window = df["close"].iloc[-(rv_lookback + 1):]
            log_ret = np.log(window / window.shift(1)).dropna()
            if len(log_ret) >= 5:
                # Annualise: 5m bars → ~75 bars per trading day, 252 days/year.
                annualised_rv = float(log_ret.std() * math.sqrt(75 * 252))
                if annualised_rv < rv_floor:
                    return self.hold(
                        symbol, spot_now,
                        f"realised_vol {annualised_rv*100:.2f}% < "
                        f"floor {rv_floor*100:.2f}% (theta-trap regime — "
                        f"FIX #32: 2026-05-15 BANKNIFTY-style entries skipped)",
                        self.name,
                    )

        # ── FIX #33 (2026-05-15) — RSI extreme filter ──────────────
        # "Buying the top" guard. Today's 13:11 BANKNIFTY entry had
        # RSI(14) = 67.8 — already in near-overbought territory after
        # a 200-pt rally. The EMA cross was a LATE momentum signal
        # firing at the local top; spot reverted 500+ pts over the
        # next 2h. This filter blocks long CE entries when RSI is
        # too high (and long PE when RSI is too low).
        rsi_period = int(getattr(self.cfg, "rsi_period", 14) or 14)
        rsi_ob = float(getattr(self.cfg, "rsi_overbought", 100) or 100)
        rsi_os = float(getattr(self.cfg, "rsi_oversold", 0) or 0)
        if len(df) > rsi_period + 5:
            rsi_now = float(_rsi(df["close"], rsi_period).iloc[-1])
            if not pd.isna(rsi_now):
                if cross_dir == "bullish" and rsi_now >= rsi_ob:
                    return self.hold(
                        symbol, spot_now,
                        f"RSI({rsi_period}) {rsi_now:.1f} ≥ "
                        f"overbought {rsi_ob:.0f} — buying the top "
                        f"(FIX #33: 2026-05-15 BANKNIFTY-style entries skipped)",
                        self.name,
                    )
                if cross_dir == "bearish" and rsi_now <= rsi_os:
                    return self.hold(
                        symbol, spot_now,
                        f"RSI({rsi_period}) {rsi_now:.1f} ≤ "
                        f"oversold {rsi_os:.0f} — selling the bottom "
                        f"(FIX #33: mean-reversion guard)",
                        self.name,
                    )

        # ── FIX #34 (2026-05-15) — Bollinger %B mean-reversion filter ──
        # %B = (close - lower_band) / (upper_band - lower_band). When
        # %B > 0.85, price is hugging the upper Bollinger Band —
        # buying calls there is fading the obvious (the band is
        # statistical resistance). Today's BANKNIFTY 13:11 entry had
        # %B = 90% — strong BLOCK signal. Today's filter would have
        # AGREED with the RSI filter independently.
        bb_period = int(getattr(self.cfg, "bb_period", 20) or 20)
        bb_std = float(getattr(self.cfg, "bb_std", 2.0) or 2.0)
        bb_upper_t = float(getattr(self.cfg, "bb_upper_threshold", 1.0) or 1.0)
        bb_lower_t = float(getattr(self.cfg, "bb_lower_threshold", 0.0) or 0.0)
        if len(df) > bb_period + 2 and (bb_upper_t < 1.0 or bb_lower_t > 0.0):
            upper, mid, lower = _bb(df["close"], bb_period, bb_std)
            u_now, l_now = float(upper.iloc[-1]), float(lower.iloc[-1])
            if not (pd.isna(u_now) or pd.isna(l_now)) and u_now > l_now:
                pct_b = (spot_now - l_now) / (u_now - l_now)
                if cross_dir == "bullish" and pct_b >= bb_upper_t:
                    return self.hold(
                        symbol, spot_now,
                        f"Bollinger %B = {pct_b*100:.0f}% ≥ {bb_upper_t*100:.0f}% — "
                        f"price hugging upper band, fade-vulnerable "
                        f"(FIX #34: mean-reversion guard)",
                        self.name,
                    )
                if cross_dir == "bearish" and pct_b <= bb_lower_t:
                    return self.hold(
                        symbol, spot_now,
                        f"Bollinger %B = {pct_b*100:.0f}% ≤ {bb_lower_t*100:.0f}% — "
                        f"price hugging lower band, fade-vulnerable "
                        f"(FIX #34: mean-reversion guard)",
                        self.name,
                    )

        if cross_dir == "bullish":
            return self._build_long_option_signal(
                df, underlying, spot_now, opt_type="CE", direction="bullish",
            )
        return self._build_long_option_signal(
            df, underlying, spot_now, opt_type="PE", direction="bearish",
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
