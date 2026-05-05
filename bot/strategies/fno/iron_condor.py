"""Iron-condor strategy — F&O segment, Phase 4.5.

A 4-leg defined-risk **neutral** structure (sell ATM±wd PUT + ATM±wd CALL,
buy further-OTM put & call as protection). Profits when the underlying
stays inside the wings; theta decay does the work.

Why iron condors over credit spreads (Phase 4)?
================================================

* **Direction-neutral** — credit spreads need a directional view (cross
  signal). Iron condors profit in CHOP, when no clean trend exists.
* **Capital efficient** — margin = ``max(put_width, call_width) − net_credit``
  per share, NOT the sum of both spread maxes (spot can hit at most one
  wing at expiry). For NIFTY 100/100 wings @ ~₹70 net credit, margin is
  ~₹3,000 per lot — fits ₹50K F&O budget comfortably.

* **Higher win rate at the cost of lower payoff** — the structure
  collects less credit than either single vertical but wins on a much
  wider spot range.

When does it fire?
==================

* The directional strategies (``futures_trend`` / ``option_buy_directional`` /
  ``credit_spread``) all trigger on EMA20/EMA50 CROSSES. The iron condor
  is the **anti-cross strategy** — it triggers on EMA *convergence*
  (the absence of trend).

Trigger condition:

  * |fast_ema − slow_ema| / spot < ``ema_flat_threshold`` (default 0.30%)
    → the trend has paused or rolled over flat → consolidation regime
    → time to harvest theta with an iron condor.

How SL/TP work
==============

The IC is treated as a SINGLE synthetic SHORT position with
``avg_price = net_credit_per_share``. Profit when net price drops, loss
when it rises (toward max_loss = worst_wing − net_credit).

* **take_profit** = entry_credit × (1 − profit_lock_pct)
                    — close when net decays to half the credit (default
                      50% — the canonical IC target)
* **stop_loss** = entry_credit + max_loss_per_share × sl_max_loss_pct
                  — close at the configured % of structural max loss
                    (default 70% — give the position room to breathe).
"""
from __future__ import annotations

import pandas as pd

from ...config import IronCondorCfg
from ...indicators import ema
from ...instruments.fno import (
    resolve_iron_condor, underlying_from_tradingsymbol,
)
from ...options.pricing import bs, years_to_expiry
from ...logger import logger
from ..base import Signal, SignalType, Strategy


class IronCondorStrategy(Strategy):
    name = "iron_condor"

    def __init__(self, cfg: IronCondorCfg) -> None:
        self.cfg = cfg

    def generate(self, symbol: str, df: pd.DataFrame) -> Signal:
        """``symbol`` is the watchlist entry (bare underlying or futures
        tradingsymbol). ``df`` carries underlying spot bars.

        Returns a SELL signal whose ``symbol`` is the iron-condor's
        synthetic tradingsymbol (e.g.
        ``NIFTY26MAY24300-24400-24700-24800IC``). The broker opens it
        with ``InstrumentKind.IRON_CONDOR`` — margin = worst-wing-loss
        × qty, cash credit = net_premium × qty.
        """
        underlying = underlying_from_tradingsymbol(symbol)
        need = self.cfg.ema_slow + 2
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

        if spot_now <= 0:
            return self.hold(symbol, spot_now, "spot=0", self.name)

        # Flatness gauge: how close is the fast EMA to the slow EMA, as
        # a fraction of spot. Trend regimes show |fast - slow|/spot in
        # the 0.5%-2% range; consolidation drops below ~0.30%.
        flatness = abs(fast_now - slow_now) / spot_now
        if flatness >= self.cfg.ema_flat_threshold:
            return self.hold(
                symbol, spot_now,
                f"trend present (flatness={flatness:.4f} ≥ "
                f"{self.cfg.ema_flat_threshold:.4f}); skipping IC",
                self.name,
            )

        return self._build_ic_signal(underlying, spot_now, flatness)

    # ── helpers ─────────────────────────────────────────────────────────

    def _build_ic_signal(self, underlying: str, spot_now: float,
                         flatness: float) -> Signal:
        try:
            ic = resolve_iron_condor(
                underlying, spot_now,
                put_width=self.cfg.put_width,
                call_width=self.cfg.call_width,
                wings_distance=self.cfg.wings_distance,
            )
        except (KeyError, ValueError) as e:
            logger.warning("[iron_condor] cannot resolve {}: {}", underlying, e)
            return self.hold(underlying, spot_now,
                             f"cannot resolve iron condor: {e}", self.name)

        # Compute the net credit at current spot. All four legs at
        # current spot via BS.
        T = years_to_expiry(ic.expiry)

        def _prem(K: int, opt_type: str) -> float:
            return bs(spot_now, K, T, opt_type,
                      sigma=self.cfg.iv, r=self.cfg.risk_free_rate)

        sp_prem = _prem(ic.put_short, "PE")
        lp_prem = _prem(ic.put_long,  "PE")
        sc_prem = _prem(ic.call_short, "CE")
        lc_prem = _prem(ic.call_long,  "CE")
        # Net credit collected on entry (SELL the structure).
        net_credit = (sp_prem - lp_prem) + (sc_prem - lc_prem)
        if net_credit <= 0:
            return self.hold(
                underlying, spot_now,
                f"degenerate IC: net_credit=₹{net_credit:.2f} "
                f"(put-side ₹{sp_prem - lp_prem:.2f}, call-side "
                f"₹{sc_prem - lc_prem:.2f})",
                self.name,
            )

        max_loss_per_share = ic.max_loss_per_share(net_credit)
        if max_loss_per_share <= 0:
            return self.hold(
                underlying, spot_now,
                f"net credit ₹{net_credit:.2f} ≥ worst wing "
                f"{max(ic.put_width(), ic.call_width())} (impossible)",
                self.name,
            )

        # SL / TP in NET-PRICE space — same shape as credit_spread.
        tp_net = net_credit * (1.0 - self.cfg.profit_lock_pct)
        sl_net = net_credit + max_loss_per_share * self.cfg.sl_max_loss_pct

        if not (tp_net < net_credit < sl_net):
            return self.hold(
                underlying, spot_now,
                f"degenerate SL/TP (tp={tp_net:.2f} entry={net_credit:.2f} "
                f"sl={sl_net:.2f})",
                self.name,
            )

        return Signal(
            symbol=ic.ic_tradingsymbol,
            type=SignalType.SELL,           # always SELL (sell IC for credit)
            price=round(net_credit, 2),
            stop_loss=round(sl_net, 2),
            take_profit=round(tp_net, 2),
            confidence=0.55,                # neutral structure → modest conf
            strategy=self.name,
            reason=(
                f"flat regime on {underlying}@{spot_now:.2f} "
                f"(flatness={flatness:.4f} < {self.cfg.ema_flat_threshold:.4f}) → "
                f"sell IC {ic.put_long}/{ic.put_short}/{ic.call_short}/{ic.call_long} "
                f"(credit ₹{net_credit:.2f}/share, max_loss ₹{max_loss_per_share:.2f}/share, "
                f"breakeven [{ic.put_short - net_credit:.0f}, {ic.call_short + net_credit:.0f}], "
                f"TP@₹{tp_net:.2f} SL@₹{sl_net:.2f})"
            ),
        )
