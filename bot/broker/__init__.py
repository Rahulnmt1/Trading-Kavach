"""Broker adapters with a uniform interface."""
from __future__ import annotations

from ..segment import Segment
from .base import Broker, Order, OrderSide, OrderStatus, OrderType, Position
from .paper import PaperBroker

__all__ = ["Broker", "Order", "OrderSide", "OrderStatus", "OrderType", "Position", "PaperBroker", "make_broker"]


def make_broker(name: str = "paper", segment: Segment = Segment.EQUITY) -> Broker:
    """Factory: ``name`` in {paper, zerodha, dhan}.

    The ``segment`` is forwarded to brokers that maintain their own
    state (currently only the paper broker — equity vs F&O paper books
    are kept separate so they can run concurrently). Live broker
    adapters today don't take a segment because the broker's own server
    holds the positions; segment-awareness is enforced upstream by the
    risk manager and journal.
    """
    name = (name or "paper").lower()
    if name == "paper":
        return PaperBroker(segment=segment)
    if name == "zerodha":
        from .zerodha import ZerodhaBroker
        return ZerodhaBroker()
    if name == "dhan":
        from .dhan import DhanBroker
        return DhanBroker()
    raise ValueError(f"Unknown broker: {name}")
