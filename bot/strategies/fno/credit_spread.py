"""Credit-spread strategy — F&O segment, Phase 4.

Same EMA20/EMA50 cross trigger as ``futures_trend`` and
``option_buy_directional``, but expresses the directional view by selling
a defined-risk vertical credit spread:

  * Bullish cross → BULL PUT spread (sell ATM PE, buy lower-strike PE)
  * Bearish cross → BEAR CALL spread (sell ATM CE, buy higher-strike CE)

Why credit spreads rather than naked short options?

Naked NIFTY short option ≈ ₹2.5 L margin per lot. Out of reach for a
₹50K F&O budget. Vertical credit spreads CAP the loss at the strike
width minus the credit collected, so margin = max_loss × qty (~₹2-5 K
per lot for ATM weekly NIFTY) — fits the budget.

Why credit spreads rather than long options (Phase 3)?

Theta is on YOUR side. The position profits from time decay even if
the underlying moves sideways. Phase 3 (long options) needs the
underlying to MOVE in your direction by enough to overcome theta;
Phase 4 credit spreads tolerate sideways action and small adverse
moves.

How SL/TP work
==============

The spread is treated as a SINGLE synthetic SHORT position with
``avg_price = net_credit_per_share``. Profit when the net price drops
(theta decay + favourable underlying move).

SL/TP are net-price thresholds:
  * **stop_loss** = entry_credit + max_loss_pct × max_loss_per_share
                    — close when net price climbs above this
  * **take_profit** = entry_credit × (1 − profit_lock_pct)
                      — close when net price drops below this (lock
                        in fraction of credit)

Both default to lock 50% of max-credit (TP) and stop at 70% of
max-loss (SL). These are conservative defaults; real intraday spread
traders often run TP=50%/SL=200% (let losses run to max).
"""
from __future__ import annotations

import pandas as pd

from ...config import CreditSpreadCfg
from ...indicators import atr as _atr, ema
from ...instruments.fno import (
    resolve_credit_spread, underlying_from_tradingsymbol,
)
from ...options.pricing import bs, years_to_expiry
from ...logger import logger
from ..base import Signal, SignalType, Strategy


class CreditSpreadStrategy(Strategy):
    name = "credit_spread"

    def __init__(self, cfg: CreditSpreadCfg) -> None:
        self.cfg = cfg

    def generate(self, symbol: str, df: pd.DataFrame) -> Signal:
        """``symbol`` is the watchlist entry (bare underlying or futures
        tradingsymbol — both proxy to spot in paper mode). ``df`` carries
        underlying spot bars.

        Returns a SELL signal whose ``symbol`` is the SPREAD's synthetic
        tradingsymbol (e.g. ``NIFTY26MAY24500-24400PESPRD``). The broker
        opens it with ``InstrumentKind.SPREAD`` — margin = max_loss × qty,
        cash credit = net_premium × qty.
        """
        underlying = underlying_from_tradingsymbol(symbol)
        need = self.cfg.ema_slow + self.cfg.cross_lookback_bars + 1
        if df.empty or len(df) < need:
            return self.hold(symbol, 0.0,
                             f"need {need} bars, got {len(df)}", self.name)

        df = df.copy()
        df["ema_fast"] = ema(df["close"], self.cfg.ema_fast)
        df["ema_slow"] = ema(df["close"], self.cfg.ema_slow)

        last = df.iloc[-1]
        spot_now = float(last["close"])
        fast_now = float(last["ema_fast"])
        slow_now = float(last["ema_slow"])

        diff = (df["ema_fast"] - df["ema_slow"]).iloc[-(self.cfg.cross_lookback_bars + 1):]
        prev_sign = diff.iloc[:-1]
        curr_sign = diff.iloc[1:]
        bullish_cross = ((prev_sign.values <= 0) & (curr_sign.values > 0)).any()
        bearish_cross = ((prev_sign.values >= 0) & (curr_sign.values < 0)).any()

        if bullish_cross and spot_now > fast_now and fast_now > slow_now:
            return self._build_spread_signal(
                underlying, spot_now, "bull_put", "bullish",
            )
        if bearish_cross and spot_now < fast_now and fast_now < slow_now:
            return self._build_spread_signal(
                underlying, spot_now, "bear_call", "bearish",
            )

        return self.hold(
            symbol, spot_now,
            f"no fresh cross (fast={fast_now:.2f}, slow={slow_now:.2f}, "
            f"price={spot_now:.2f})",
            self.name,
        )

    # ── helpers ─────────────────────────────────────────────────────────

    def _build_spread_signal(
        self, underlying: str, spot_now: float,
        spread_type: str, direction: str,
    ) -> Signal:
        try:
            spread = resolve_credit_spread(
                underlying, spread_type, spot_now,
                width=self.cfg.strike_width,
            )
        except (KeyError, ValueError) as e:
            logger.warning("[credit_spread] cannot resolve {} {}: {}",
                           underlying, spread_type, e)
            return self.hold(underlying, spot_now,
                             f"cannot resolve spread: {e}", self.name)

        # Compute net credit per share at the current spot via BS.
        T = years_to_expiry(spread.expiry)
        short_prem = bs(
            spot_now, spread.short_strike, T, spread.opt_type,
            sigma=self.cfg.iv, r=self.cfg.risk_free_rate,
        )
        long_prem = bs(
            spot_now, spread.long_strike, T, spread.opt_type,
            sigma=self.cfg.iv, r=self.cfg.risk_free_rate,
        )
        net_credit = short_prem - long_prem
        if net_credit <= 0:
            # Theoretically the short strike is closer to spot so its
            # premium dominates; if BS produces zero/negative net, the
            # spread is degenerate (e.g. width too tight or IV too low).
            return self.hold(
                underlying, spot_now,
                f"degenerate spread: net_credit={net_credit:.4f} (short={short_prem:.2f}, long={long_prem:.2f})",
                self.name,
            )

        # Defined-risk max loss bookkeeping.
        max_loss_per_share = spread.max_loss_per_share(net_credit)
        if max_loss_per_share <= 0:
            return self.hold(
                underlying, spot_now,
                f"net credit {net_credit:.2f} ≥ width {spread.width()} (impossible)",
                self.name,
            )

        # SL / TP in NET-PRICE space:
        #   TP (lock profit): close when net drops to (1 − profit_lock_pct) × entry
        #   SL (cap loss):    close when net rises to entry + sl_max_loss_pct × max_loss
        tp_net = net_credit * (1.0 - self.cfg.profit_lock_pct)
        sl_net = net_credit + max_loss_per_share * self.cfg.sl_max_loss_pct

        # Sanity: SL above entry (loss side), TP below entry (profit side),
        # SL bounded by max_loss buffer above entry.
        if not (tp_net < net_credit < sl_net):
            return self.hold(
                underlying, spot_now,
                f"degenerate SL/TP (tp={tp_net:.2f} entry={net_credit:.2f} sl={sl_net:.2f})",
                self.name,
            )

        return Signal(
            symbol=spread.spread_tradingsymbol,
            type=SignalType.SELL,                      # always SELL (we sell for credit)
            price=round(net_credit, 2),
            stop_loss=round(sl_net, 2),
            take_profit=round(tp_net, 2),
            confidence=0.65,
            strategy=self.name,
            reason=(
                f"{direction} cross on {underlying}@{spot_now:.2f} → "
                f"sell {spread_type} {spread.short_strike}/{spread.long_strike}"
                f"{spread.opt_type} (credit ₹{net_credit:.2f}/share, "
                f"max_loss ₹{max_loss_per_share:.2f}/share, width "
                f"{spread.width()}, TP@₹{tp_net:.2f} SL@₹{sl_net:.2f})"
            ),
        )
