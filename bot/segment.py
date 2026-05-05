"""Segment — equity vs F&O isolation primitive.

The bot operates in two separate "segments" that share zero state:

* :class:`Segment.EQUITY` — intraday cash-equity trading (NSE delivery /
  intraday MIS). The "default" segment. All pre-existing config/cache/log
  paths belong here.
* :class:`Segment.FNO` — Futures & Options. Different lot sizes, fee
  schedule, margin model, expiries, and instrument syntax. Runs in its
  own process with its own lockfile and cache namespace.

The two segments are intentionally **isolated**:

* Each segment has its own ``.bot.lock.{segment}`` file. Equity bot and
  F&O bot can run at the same time on the same machine without colliding
  (different lock paths) and without ever seeing each other's positions.
* All Redis keys are segment-prefixed (``paper:state:equity`` vs
  ``paper:state:fno``, ``signal:equity:RELIANCE`` vs ``signal:fno:NIFTY26500CE``,
  etc.) so the daily reset and healthcheck can scope their work.
* Capital is allocated separately. The risk manager's daily-loss cap and
  position-size cap are computed against the segment's own capital
  budget, not a global pool. That's the answer to "I don't want clashes
  between equity and F&O" — a runaway loss in one segment can never
  bleed into the other's trading budget.
* Trade journals are written to ``logs/trades/{segment}/YYYY-MM-DD.jsonl``
  so EOD reports and CSV summaries don't get mixed.

This module is the single source of truth for those naming conventions.
Every place that previously hard-coded a key like ``"paper:state"`` or a
path like ``logs/trades/...`` should go through one of the helpers below.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional


class Segment(str, Enum):
    """Trading segment.

    Inheriting from ``str`` so ``Segment.EQUITY == "equity"`` works and
    JSON-serialization is trivial (the value is the string).
    """

    EQUITY = "equity"
    FNO = "fno"

    @classmethod
    def parse(cls, raw: Optional[str]) -> "Segment":
        """Liberal parse: ``None`` / empty → :attr:`EQUITY` (backward-compat).

        Accepts upper, lower, or mixed case. Any other value raises
        ``ValueError`` so a typo on the CLI doesn't silently default to
        the wrong segment.
        """
        if raw is None or raw == "":
            return cls.EQUITY
        try:
            return cls(raw.lower())
        except ValueError as e:
            valid = ", ".join(s.value for s in cls)
            raise ValueError(f"unknown segment {raw!r} (valid: {valid})") from e

    @property
    def label(self) -> str:
        """Human-readable label for log lines and the dashboard."""
        return {Segment.EQUITY: "Equity", Segment.FNO: "F&O"}[self]


# --------------------------------------------------------------------------
#  Naming helpers (cache keys, lock files, journal paths).
# --------------------------------------------------------------------------

def cache_key(base: str, segment: Segment) -> str:
    """Return a segment-namespaced cache key.

    The convention is ``<base>:<segment>`` so ``paper:state`` becomes
    ``paper:state:equity`` and per-symbol ``signal:RELIANCE`` becomes
    ``signal:equity:RELIANCE``.

    Per-symbol keys with their own colon-separated suffix are handled
    via the convention "namespace BEFORE the variable suffix":
    ``signal_key("RELIANCE", Segment.EQUITY)`` → ``"signal:equity:RELIANCE"``.
    Use the dedicated helpers for those (``signal_key``, ``trail_key``).
    """
    return f"{base}:{segment.value}"


def signal_key(symbol: str, segment: Segment) -> str:
    """Cache key for a per-symbol signal: ``signal:<segment>:<symbol>``."""
    return f"signal:{segment.value}:{symbol}"


def trail_key(symbol: str, segment: Segment) -> str:
    """Cache key for a per-symbol trailing-stop snapshot."""
    return f"trail:{segment.value}:{symbol}"


def signal_pattern(segment: Segment) -> str:
    """Glob for all signal keys in this segment."""
    return f"signal:{segment.value}:*"


def trail_pattern(segment: Segment) -> str:
    """Glob for all trail keys in this segment."""
    return f"trail:{segment.value}:*"


def lock_path(segment: Segment, project_root: Path) -> Path:
    """Path to the singleton lockfile for this segment."""
    return project_root / f".bot.lock.{segment.value}"


def journal_subdir(segment: Segment) -> str:
    """Sub-directory under ``logs/trades/`` and ``logs/eod/`` for this segment."""
    return segment.value


# --------------------------------------------------------------------------
#  Config-view helpers.
#
#  These read the right slice of AppConfig depending on segment so call
#  sites don't have to repeat the conditional. They tolerate the F&O
#  block being absent (returns the equity-default values), which keeps
#  the equity-only deployment fully backward-compatible.
# --------------------------------------------------------------------------

def cfg_capital(cfg, segment: Segment):
    """Return the :class:`CapitalCfg` for this segment."""
    if segment == Segment.FNO:
        if cfg.fno is not None and cfg.fno.capital is not None:
            return cfg.fno.capital
    return cfg.capital


def cfg_risk(cfg, segment: Segment):
    """Return the :class:`RiskCfg` for this segment."""
    if segment == Segment.FNO:
        if cfg.fno is not None and cfg.fno.risk is not None:
            return cfg.fno.risk
    return cfg.risk


def cfg_watchlist_symbols(cfg, segment: Segment) -> list[str]:
    """Return the watchlist symbols for this segment."""
    if segment == Segment.FNO:
        if cfg.fno is not None:
            return list(cfg.fno.watchlist.get("symbols", []))
        return []
    return list(cfg.watchlist.get("symbols", []))


def cfg_strategies_enabled(cfg, segment: Segment) -> list[str]:
    """Return the list of enabled strategy names for this segment."""
    if segment == Segment.FNO:
        if cfg.fno is not None and cfg.fno.strategies is not None:
            return list(cfg.fno.strategies.enabled)
        return []
    return list(cfg.strategies.enabled)


def fno_enabled(cfg) -> bool:
    """Is the F&O segment opted in via config?"""
    return bool(cfg.fno is not None and cfg.fno.enabled)
