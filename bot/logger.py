"""Structured logging via loguru. Writes to stdout + rotating file."""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from .config import env, load_config

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    cfg = load_config()
    level = env().LOG_LEVEL.upper()

    logger.remove()
    logger.add(
        sys.stdout,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    )

    if cfg.logging.to_file:
        log_dir = Path(cfg.logging.dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_dir / "bot_{time:YYYY-MM-DD}.log",
            rotation="00:00",
            retention="14 days",
            level=level,
            enqueue=True,
        )
    _configured = True


setup_logging()

__all__ = ["logger", "setup_logging"]
