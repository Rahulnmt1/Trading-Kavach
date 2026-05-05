"""Zerodha Kite Connect adapter (skeleton).

Requires:
  - Kite Connect subscription (~₹2000/month)
  - Daily TOTP-driven access token refresh (SEBI-mandated)
  - Static IP whitelist with broker

Fill in the access-token bootstrap flow before going live. The class is structured
so business logic (executor.py) does not change between paper and live.

PHASE 5 — going live with F&O
=============================

In paper mode, the bot synthesises options/spreads/iron-condors from
Black-Scholes on the underlying spot. To go LIVE with F&O the broker
needs three additional capabilities, which are all SKELETONED below
(implementation gated by ``LIVE_TRADING=true`` in ``.env``):

1. **Real option-chain bars** — replace ``bot/data.py``'s BS synthesis
   with live LTP from Kite. The strike & expiry come from
   ``parse_option_tradingsymbol`` / ``parse_spread_tradingsymbol`` /
   ``parse_iron_condor_tradingsymbol``; we resolve them to instrument
   tokens via ``kite.instruments("NFO")`` and stream LTPs.
   Implementation: :meth:`fetch_option_chain` / :meth:`fetch_option_ltp`.

2. **Multi-leg orders** — when the strategy emits a SPREAD or
   IRON_CONDOR signal, the broker translates the synthetic
   tradingsymbol into 2 (spread) or 4 (IC) real Kite orders, places
   them atomically (or as close as Kite allows), and reconstitutes the
   synthetic Position object once all legs fill. A partial fill on any
   leg triggers an immediate close of the filled legs (avoids unhedged
   tail risk). Implementation: :meth:`place_spread` /
   :meth:`place_iron_condor`.

3. **Real SPAN+exposure margin** — Kite's :meth:`KiteConnect.margins`
   endpoint computes actual broker margin for any basket of orders
   (factoring in netting between offsetting legs). Replaces the paper
   broker's heuristic ``max_loss × qty`` and the futures
   ``margin_pct`` table. Implementation: :meth:`fetch_real_margin`.

The skeletons are deliberately conservative — they raise
``NotImplementedError`` with a clear message describing the missing
plumbing, so an accidental ``LIVE_TRADING=true`` switch fails fast
rather than placing partial multi-leg orders.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import env
from ..logger import logger
from .base import (
    Broker, InstrumentKind, Order, OrderSide, OrderStatus, OrderType, Position,
)


class ZerodhaBroker(Broker):
    name = "zerodha"

    def __init__(self) -> None:
        try:
            from kiteconnect import KiteConnect
        except ImportError as e:
            raise RuntimeError("kiteconnect not installed. `pip install kiteconnect`") from e

        e_ = env()
        if not (e_.KITE_API_KEY and e_.KITE_ACCESS_TOKEN):
            raise RuntimeError("KITE_API_KEY and KITE_ACCESS_TOKEN required for Zerodha live mode.")

        self.kite = KiteConnect(api_key=e_.KITE_API_KEY)
        self.kite.set_access_token(e_.KITE_ACCESS_TOKEN)
        self._marks: dict[str, float] = {}
        logger.warning("ZerodhaBroker initialised — LIVE ORDERS WILL BE PLACED.")

    @staticmethod
    def _exch_symbol(symbol: str) -> str:
        return f"NSE:{symbol}"

    def place_order(self, order: Order) -> Order:
        from kiteconnect import KiteConnect
        try:
            kite_id = self.kite.place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange=KiteConnect.EXCHANGE_NSE,
                tradingsymbol=order.symbol,
                transaction_type=KiteConnect.TRANSACTION_TYPE_BUY
                    if order.side == OrderSide.BUY
                    else KiteConnect.TRANSACTION_TYPE_SELL,
                quantity=order.qty,
                product=order.product,
                order_type=order.type.value if order.type != OrderType.SLM else "SL-M",
                price=order.price if order.type == OrderType.LIMIT else None,
                trigger_price=order.stop_loss if order.type in (OrderType.SL, OrderType.SLM) else None,
                tag="bot",
            )
            order.id = str(kite_id)
            order.status = OrderStatus.OPEN
            order.created_at = datetime.utcnow()
            logger.info("[zerodha] order placed id={} {} {} {}", kite_id, order.side.value, order.qty, order.symbol)
        except Exception as e:
            order.status = OrderStatus.REJECTED
            logger.error("[zerodha] order rejected for {}: {}", order.symbol, e)
        return order

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.kite.cancel_order(variety="regular", order_id=order_id)
            return True
        except Exception as e:
            logger.error("[zerodha] cancel failed: {}", e)
            return False

    def positions(self) -> List[Position]:
        out: List[Position] = []
        try:
            data = self.kite.positions()
            for p in data.get("net", []):
                if p["quantity"] == 0:
                    continue
                side = OrderSide.BUY if p["quantity"] > 0 else OrderSide.SELL
                out.append(Position(
                    symbol=p["tradingsymbol"], qty=int(p["quantity"]),
                    avg_price=float(p["average_price"]), side=side,
                    realized_pnl=float(p.get("realised", 0.0)),
                    unrealized_pnl=float(p.get("unrealised", 0.0)),
                ))
        except Exception as e:
            logger.error("[zerodha] positions fetch failed: {}", e)
        return out

    def cash(self) -> float:
        try:
            margins = self.kite.margins("equity")
            return float(margins["available"]["cash"])
        except Exception as e:
            logger.error("[zerodha] margin fetch failed: {}", e)
            return 0.0

    def update_marks(self, marks: dict[str, float]) -> None:
        self._marks.update(marks)

    def square_off_all(self) -> List[Order]:
        from kiteconnect import KiteConnect
        out = []
        for p in self.positions():
            opp = OrderSide.SELL if p.side == OrderSide.BUY else OrderSide.BUY
            o = Order(id=str(uuid.uuid4()), symbol=p.symbol, side=opp,
                      qty=abs(p.qty), type=OrderType.MARKET,
                      is_squareoff=True)
            out.append(self.place_order(o))
        return out

    # ─────────────────────────────────────────────────────────────────
    # PHASE 5 — F&O LIVE TRADING SKELETONS
    # ─────────────────────────────────────────────────────────────────
    # All four methods below are inactive until you flip ``LIVE_TRADING=true``
    # in ``.env`` AND complete the implementation marked TODO. They exist
    # here as the ENTRY POINT for the live integration so the rest of
    # the bot can call them through the standard ``Broker`` interface.

    _instrument_cache: Optional[List[Dict[str, Any]]] = None

    def _instruments(self, exchange: str = "NFO") -> List[Dict[str, Any]]:
        """Fetch and cache the NFO instrument master.

        Kite's ``kite.instruments("NFO")`` returns ~40,000 rows once a
        day; we cache in-process for the bot's lifetime. The list is
        keyed by ``tradingsymbol``: e.g. ``NIFTY26MAY24500CE`` →
        ``{"instrument_token": 12345, "lot_size": 75, "expiry": ...}``.
        """
        if self._instrument_cache is None:
            try:
                self._instrument_cache = self.kite.instruments(exchange)
                logger.info("[zerodha:nfo] loaded {} instruments",
                            len(self._instrument_cache))
            except Exception as e:
                logger.error("[zerodha:nfo] instrument-master fetch failed: {}", e)
                self._instrument_cache = []
        return self._instrument_cache

    def fetch_option_chain(self, underlying: str,
                           expiry: Optional[str] = None) -> Dict[int, Dict[str, Any]]:
        """Return the live option chain for ``underlying`` (Phase 5).

        Builds a dict keyed by strike with ``CE`` / ``PE`` LTP, OI,
        IV (if Kite supports it), and instrument_token. The data layer
        (``bot/data.py::_synth_option_bars``) will switch to this
        function once ``LIVE_TRADING=true``, replacing the BS synthesis.

        ``expiry`` is an ISO date string (``"2026-05-28"``); when None,
        the nearest weekly is used.

        TODO(phase5):
        * Filter ``self._instruments()`` to the underlying + expiry +
          ``segment="NFO-OPT"``.
        * Group by strike, attach CE / PE legs.
        * Subscribe a ticker for the strikes in ±5 steps of ATM (the
          typical strategy reach) and return a tick-snapshot.
        * Cache strike-grid in Redis for ~60s; refresh on OI shifts.
        """
        raise NotImplementedError(
            "Phase 5 not yet implemented: live Kite option-chain fetch. "
            "Falling back to BS synthesis in bot/data.py is safe; "
            "remove this raise + implement when Kite Connect is wired."
        )

    def fetch_option_ltp(self, tradingsymbol: str) -> Optional[float]:
        """Fast last-traded-price lookup for one option/spread/IC leg.

        Used by the position-manager when the synthetic bars feed is
        stale. Returns None if Kite has no quote (off-market, illiquid
        strike).

        TODO(phase5):
        * resolve tradingsymbol → instrument_token via :meth:`_instruments`
        * call self.kite.ltp([f"NFO:{tradingsymbol}"]) → return ['last_price'].
        """
        raise NotImplementedError("Phase 5 not yet implemented: live option LTP.")

    def place_spread(self, order: Order) -> Order:
        """Translate a synthetic SPREAD order into 2 real Kite orders.

        The synthetic ``order.symbol`` is e.g.
        ``NIFTY26MAY24500-24400PESPRD``. We:

        1. Parse it via :func:`bot.instruments.fno.parse_spread_tradingsymbol`
           into short_strike / long_strike / opt_type / expiry.
        2. Construct two child Kite orders:
           - SHORT leg:  SELL ATM PE (collects credit)
           - LONG leg:   BUY  OTM PE (caps loss)
        3. Place them in basket-order semantics (Kite's ``BO`` variety
           does NOT cover this directly — we place SEQUENTIALLY and
           cancel/close the first leg if the second leg rejects).
        4. Reconstitute a single SPREAD Position whose ``avg_price`` =
           net credit per share = short_fill − long_fill.

        TODO(phase5):
        * Implement steps 1-4. Step 3's atomicity is the tricky part —
          if leg 2 rejects, we MUST close leg 1 within seconds to avoid
          unhedged short-option exposure (potentially unlimited loss).
        * Persist a "spread_id" → list-of-leg-orders map in Redis so a
          crash mid-placement can be reconciled on restart.
        """
        if order.instrument_kind != InstrumentKind.SPREAD:
            raise ValueError(
                f"place_spread called with kind={order.instrument_kind} "
                "(expected SPREAD)"
            )
        raise NotImplementedError(
            "Phase 5 not yet implemented: multi-leg spread translation. "
            "Use --paper for spread strategies until this is wired."
        )

    def place_iron_condor(self, order: Order) -> Order:
        """Translate a synthetic IRON_CONDOR order into 4 real Kite orders.

        Same shape as :meth:`place_spread` but with FOUR legs (short put,
        long put, short call, long call). Atomicity is even more critical
        here — partial fill of the short legs without their long-OTM
        protective wings = naked short options = unlimited loss.

        TODO(phase5):
        * Place the LONG (protective) legs FIRST so any rejection
          short-circuits before we've sold a naked option.
        * Then place the two SHORT legs. If either rejects, close the
          longs immediately.
        * Reconstitute one IRON_CONDOR Position whose ``avg_price`` =
          net credit per share = (sp + sc) − (lp + lc).
        """
        if order.instrument_kind != InstrumentKind.IRON_CONDOR:
            raise ValueError(
                f"place_iron_condor called with kind={order.instrument_kind} "
                "(expected IRON_CONDOR)"
            )
        raise NotImplementedError(
            "Phase 5 not yet implemented: multi-leg iron-condor translation. "
            "Use --paper for iron-condor strategy until this is wired."
        )

    def fetch_real_margin(self, orders: List[Order]) -> Dict[str, float]:
        """Replace the paper broker's heuristic margin with Kite's SPAN+exposure.

        Kite's :meth:`KiteConnect.basket_order_margins` accepts a list
        of order dicts and returns the actual margin requirement
        (factoring in netting between offsetting legs — e.g. a credit
        spread's margin is automatically reduced by Kite's risk engine,
        no need for our ``vertical_spread_max_loss`` heuristic).

        Returns dict ``{"total": ..., "span": ..., "exposure": ...}``.

        TODO(phase5):
        * Translate each Order → kite-order-dict (variety, exchange,
          tradingsymbol, transaction_type, quantity, product, ...).
        * Pass the list to ``self.kite.basket_order_margins([...])``.
        * Return ``{"total": resp[0]["total"], ...}``.
        * The risk manager's F&O sizing branch (``bot/risk.py``) will
          consult this when ``LIVE_TRADING=true`` instead of computing
          margin client-side.
        """
        raise NotImplementedError(
            "Phase 5 not yet implemented: real SPAN+exposure margin fetch."
        )
