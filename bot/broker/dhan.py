"""Dhan API adapter (skeleton).

Requires `pip install dhanhq` and DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN.
Wire in real calls before going live.
"""
from __future__ import annotations

import uuid
from typing import List

from ..config import env
from ..logger import logger
from .base import Broker, Order, OrderSide, OrderStatus, OrderType, Position


class DhanBroker(Broker):
    name = "dhan"

    def __init__(self) -> None:
        try:
            from dhanhq import dhanhq
        except ImportError as e:
            raise RuntimeError("dhanhq not installed. `pip install dhanhq`") from e

        e_ = env()
        if not (e_.DHAN_CLIENT_ID and e_.DHAN_ACCESS_TOKEN):
            raise RuntimeError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN required for Dhan live mode.")
        self.client = dhanhq(e_.DHAN_CLIENT_ID, e_.DHAN_ACCESS_TOKEN)
        self._marks: dict[str, float] = {}
        logger.warning("DhanBroker initialised — LIVE ORDERS WILL BE PLACED.")

    def place_order(self, order: Order) -> Order:
        # NOTE: Dhan needs the security_id (numeric) per scrip — maintain a mapping.
        # This is a stub. Wire the real call before live use.
        try:
            resp = self.client.place_order(
                tag="bot",
                transaction_type=order.side.value,
                exchange_segment="NSE_EQ",
                product_type=order.product,
                order_type=order.type.value,
                validity="DAY",
                quantity=order.qty,
                disclosed_quantity=0,
                price=order.price or 0,
                trigger_price=order.stop_loss or 0,
                security_id="",   # populate via instrument master
            )
            order.id = str(resp.get("data", {}).get("orderId") or uuid.uuid4())
            order.status = OrderStatus.OPEN
            logger.info("[dhan] order placed: {}", order.id)
        except Exception as e:
            order.status = OrderStatus.REJECTED
            logger.error("[dhan] place_order failed: {}", e)
        return order

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel_order(order_id=order_id)
            return True
        except Exception as e:
            logger.error("[dhan] cancel failed: {}", e)
            return False

    def positions(self) -> List[Position]:
        return []  # TODO

    def cash(self) -> float:
        return 0.0  # TODO

    def update_marks(self, marks: dict[str, float]) -> None:
        self._marks.update(marks)

    def square_off_all(self) -> List[Order]:
        return []  # TODO
