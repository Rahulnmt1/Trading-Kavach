"""Broker interface (ABC) + Order / Position dataclasses."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SLM = "SL-M"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class InstrumentKind(str, Enum):
    """What kind of instrument this order/position is for.

    The paper broker uses this to dispatch the cash/margin/fee model
    (equity debits full notional + equity fees; futures debits only
    margin + futures fees). Live brokers ignore this and read the
    tradingsymbol's exchange/segment from the broker API instead.

    SPREAD is a Phase 4 paper-mode-only synthetic kind — a vertical
    credit spread modelled as a SINGLE position (avg_price = net credit
    per share, margin_blocked = max_loss × qty). Going live (Phase 5)
    will translate spread tradingsymbols into two real Kite orders, at
    which point the spread Position is reconstituted from those legs;
    the InstrumentKind remains SPREAD for risk-manager / dashboard /
    journal purposes.

    IRON_CONDOR is a Phase 4.5 synthetic kind — a 4-leg defined-risk
    neutral structure (bull-put-spread + bear-call-spread on the same
    underlying & expiry). Modelled as a single Position with
    avg_price = net credit / share and margin_blocked = max-wing-loss
    × qty (NOT the sum of both spread margins, since spot can hit at
    most one wing at expiry — iron condors are ~50% more
    capital-efficient than running two verticals separately).
    """
    EQUITY = "EQUITY"
    FUTURES = "FUTURES"
    OPTION = "OPTION"
    SPREAD = "SPREAD"
    IRON_CONDOR = "IRON_CONDOR"


@dataclass
class Order:
    id: str
    symbol: str
    side: OrderSide
    qty: int
    type: OrderType = OrderType.MARKET
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    product: str = "MIS"
    status: OrderStatus = OrderStatus.PENDING
    fill_price: Optional[float] = None
    fees: float = 0.0
    created_at: datetime = field(default_factory=datetime.utcnow)
    # Phase 2: instrument classification. Defaults to EQUITY so legacy
    # callers (which build orders without setting this field) keep their
    # old behaviour unchanged.
    instrument_kind: InstrumentKind = InstrumentKind.EQUITY
    lot_size: int = 1                # 1 for equity; 75 for NIFTY fut, etc.
    # Phase 3 (FIX #13b refinement, 2026-05-05):
    # Set to True ONLY by ``square_off_all`` when synthesizing the
    # opposite-side order to flatten an existing position. The paper
    # broker's over-sell guard is then narrowed to fire ONLY when this
    # flag is set AND the position is already gone (the exact 2026-05-04
    # ADANIENT race-condition signature). Fresh strategy-driven SELL
    # signals on a flat book (legitimate intraday MIS shorts) leave this
    # False and pass through the guard untouched.
    is_squareoff: bool = False


@dataclass
class Position:
    symbol: str
    qty: int                 # +ve long, -ve short
    avg_price: float
    side: OrderSide
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    # Immutable snapshot of the SL/TP set at entry time. The PositionManager
    # mutates `stop_loss` upward (long) / downward (short) when trailing —
    # `initial_stop_loss` lets us recover the original 1R distance without
    # losing it. `None` for legacy positions loaded from cache.
    initial_stop_loss: Optional[float] = None
    initial_take_profit: Optional[float] = None
    opened_at: datetime = field(default_factory=datetime.utcnow)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    # Phase 2: F&O fields. For equity these stay at their defaults and the
    # P&L / cash math is unchanged. For futures the broker debits
    # ``margin_blocked`` from cash on entry (instead of full notional)
    # and refunds it on close.
    instrument_kind: InstrumentKind = InstrumentKind.EQUITY
    lot_size: int = 1
    margin_blocked: float = 0.0      # ₹ debited from cash on entry (futures only)


class Broker(ABC):
    name: str = "base"

    @abstractmethod
    def place_order(self, order: Order) -> Order: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    def positions(self) -> List[Position]: ...

    @abstractmethod
    def cash(self) -> float: ...

    @abstractmethod
    def square_off_all(self) -> List[Order]: ...

    @abstractmethod
    def update_marks(self, marks: dict[str, float]) -> None:
        """Update last-traded-price for each held symbol so unrealized P&L is correct."""
