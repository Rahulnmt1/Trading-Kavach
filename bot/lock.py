"""Single-instance lock for the bot process.

Today's NESTLEIND incident proved that two bot processes were running
concurrently (two ``_end_of_day`` log lines fired within 2 ms of each
other). When that happens:

* They both poll yfinance and burn through the rate-limit twice as fast.
* They share Redis state but each holds its own in-memory broker → the
  ``paper:state`` cache flips back and forth between divergent snapshots.
* If one ends up with stale price data, it can issue phantom orders
  (the 13:19 ``trail close at ₹2405`` was exactly this — a second bot
  process operating on a corrupt restore).

This module enforces "at most one ``cli run`` per project directory **per
segment**" using ``fcntl.flock`` on a sentinel file at
``<repo>/.bot.lock.<segment>``. ``fcntl`` advisory locks are auto-released
by the kernel on process exit *even on SIGKILL*, so a crashed bot doesn't
permanently jam the lock — the next startup reclaims it cleanly.

Each segment has its own lockfile so the equity bot and the F&O bot can
run simultaneously without blocking each other:

* ``.bot.lock.equity`` — held by ``cli run --segment equity``
* ``.bot.lock.fno``    — held by ``cli run --segment fno``

A second invocation of the same segment is rejected; an invocation of
the other segment proceeds independently.
"""
from __future__ import annotations

import atexit
import fcntl
import os
import signal
from pathlib import Path
from typing import IO, Dict, Optional

from .config import PROJECT_ROOT
from .logger import logger
from .segment import Segment, lock_path

# Module-level handles keyed by segment so the equity and F&O bots
# can each hold their own lock within the same Python process if
# needed (used by the test suite). In production each segment runs
# in its own process and only one entry will be populated.
_lock_handles: Dict[Segment, IO[str]] = {}


class BotAlreadyRunningError(RuntimeError):
    """Raised when another bot instance already holds the singleton lock."""


def _path_for(segment: Segment) -> Path:
    return lock_path(segment, PROJECT_ROOT)


def acquire(segment: Segment = Segment.EQUITY, force: bool = False) -> int:
    """Acquire the singleton lock for ``segment`` in this project.

    Returns the current process's PID on success. Raises
    :class:`BotAlreadyRunningError` if another live process holds the
    lock for the SAME segment. A different segment's lock is independent —
    holding ``.bot.lock.equity`` does NOT block ``.bot.lock.fno``.

    Pass ``force=True`` to break a stale lock — only do this if you're
    certain no other bot is running (e.g. after a hard kill).
    """
    if _lock_handles.get(segment) is not None:
        return os.getpid()

    path = _path_for(segment)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            fh.seek(0)
            holder_pid = (fh.read().strip().splitlines() or ["?"])[0]
        except Exception:
            holder_pid = "?"
        fh.close()
        if not force:
            raise BotAlreadyRunningError(
                f"Another {segment.label} bot instance already holds {path} "
                f"(PID {holder_pid}). Refusing to start a second copy. "
                f"If you're sure that PID is dead, delete {path} "
                f"and retry — or pass --force-lock."
            )
        logger.warning("[lock:{}] --force-lock requested, breaking stale lock at {}",
                       segment.value, path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return acquire(segment=segment, force=False)

    pid = os.getpid()
    fh.seek(0)
    fh.truncate()
    fh.write(f"{pid}\n")
    fh.flush()

    _lock_handles[segment] = fh
    # Register a cleanup that releases ALL segments held by this process.
    atexit.register(release_all)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            prior = signal.getsignal(sig)
            signal.signal(sig, _make_signal_handler(prior))
        except (ValueError, OSError):
            pass

    logger.info("[lock:{}] acquired singleton lock (pid={}, file={})",
                segment.value, pid, path)
    return pid


def release(segment: Segment = Segment.EQUITY) -> None:
    """Release this segment's singleton lock (idempotent)."""
    fh = _lock_handles.pop(segment, None)
    if fh is None:
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass
    path = _path_for(segment)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("[lock:{}] could not delete {}: {}", segment.value, path, e)


def release_all() -> None:
    """Release every segment lock held by this process. Idempotent."""
    for seg in list(_lock_handles.keys()):
        release(seg)


def _make_signal_handler(prior_handler):
    """Wrap an existing signal handler so we release before delegating."""
    def _handler(signum, frame):
        try:
            release_all()
        finally:
            if callable(prior_handler):
                try:
                    prior_handler(signum, frame)
                except Exception:
                    pass
            elif prior_handler == signal.SIG_DFL:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)
    return _handler
