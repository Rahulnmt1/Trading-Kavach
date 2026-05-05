"""Notifier — sends email alerts on fills and risk rejections.

Configure via .env: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, NOTIFY_TO.
If SMTP_HOST is empty the notifier becomes a no-op (silent in tests / CI).
"""
from __future__ import annotations

import smtplib
import threading
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional

from .broker.base import Order, OrderStatus
from .config import env
from .logger import logger
from .strategies.base import Signal


@dataclass
class _LevelOrder:
    DEBUG = 0
    INFO = 1
    WARNING = 2
    ERROR = 3

    @classmethod
    def value(cls, name: str) -> int:
        return getattr(cls, name.upper(), cls.INFO)


class Notifier:
    """Thread-safe emailer. send() never raises into the caller."""

    def __init__(self) -> None:
        e_ = env()
        self.host = e_.SMTP_HOST
        self.port = e_.SMTP_PORT
        self.user = e_.SMTP_USER
        self.password = e_.SMTP_PASSWORD
        self.sender = e_.SMTP_FROM or e_.SMTP_USER
        self.recipients: List[str] = e_.notify_recipients
        self.min_level = _LevelOrder.value(e_.NOTIFY_LEVEL)
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.recipients and self.sender)

    def send(self, subject: str, body: str, level: str = "INFO", wait: bool = False) -> bool:
        """Send an email.

        ``wait=False`` (the default) dispatches on a daemon thread so the
        caller — typically ``executor._place()`` on the trading hot-path —
        is never blocked by SMTP latency. ``wait=True`` sends synchronously
        and returns ``True`` only after Gmail/SES/etc. has acknowledged
        delivery; use it from CLI smoke-tests so users see the real verdict.
        Returns ``False`` if the notifier is disabled, the level is below
        ``NOTIFY_LEVEL``, or (in synchronous mode) the SMTP server rejected
        the message.
        """
        if not self.enabled:
            return False
        if _LevelOrder.value(level) < self.min_level:
            return False

        msg = MIMEMultipart()
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg["Subject"] = f"[Bot:{level}] {subject}"
        msg.attach(MIMEText(body, "plain"))

        def _do_send() -> bool:
            try:
                with self._lock, smtplib.SMTP(self.host, self.port, timeout=15) as s:
                    s.starttls()
                    if self.user and self.password:
                        s.login(self.user, self.password)
                    s.send_message(msg)
                logger.info("[notify] sent '{}' to {} recipient(s)", subject, len(self.recipients))
                return True
            except Exception as ex:
                logger.error("[notify] send failed: {}", ex)
                return False

        if wait:
            return _do_send()
        threading.Thread(target=_do_send, daemon=True).start()
        return True

    def fill(self, order: Order) -> None:
        if order.status != OrderStatus.FILLED:
            return
        body = (
            f"Order filled\n\n"
            f"Symbol     : {order.symbol}\n"
            f"Side       : {order.side.value}\n"
            f"Qty        : {order.qty}\n"
            f"Fill price : ₹{order.fill_price:.2f}\n"
            f"Fees       : ₹{order.fees:.2f}\n"
            f"Stop loss  : {order.stop_loss}\n"
            f"Take profit: {order.take_profit}\n"
            f"Time       : {order.created_at.isoformat(timespec='seconds')}\n"
        )
        self.send(f"FILLED {order.side.value} {order.symbol} x{order.qty}", body, "INFO")

    def rejection(self, signal: Signal, reason: str) -> None:
        body = (
            f"Signal rejected by risk manager\n\n"
            f"Symbol  : {signal.symbol}\n"
            f"Side    : {signal.type.value}\n"
            f"Price   : ₹{signal.price:.2f}\n"
            f"Strategy: {signal.strategy}\n"
            f"Reason  : {reason}\n"
        )
        self.send(f"REJECTED {signal.type.value} {signal.symbol}", body, "WARNING")

    def error(self, subject: str, exc: Exception) -> None:
        self.send(subject, f"{type(exc).__name__}: {exc}", "ERROR")

    def health(self, report) -> None:
        """Email a periodic health-check report. ``report`` is a HealthReport."""
        level = {"OK": "INFO", "DEGRADED": "WARNING", "FAILED": "ERROR"}.get(
            report.overall, "INFO",
        )
        self.send(report.to_subject(), report.to_text(), level)


_notifier: Optional[Notifier] = None


def get_notifier() -> Notifier:
    global _notifier
    if _notifier is None:
        _notifier = Notifier()
    return _notifier
