"""Scoped daily Redis reset.

Run at bot startup and at 08:55 IST every weekday. Wipes only the
**intraday-stateful** keys for the given segment — the keys whose
contents would lie about today's trading state if they survived from
a prior day.

What we **DO** clear (per segment):

* ``paper:state:<segment>`` — paper broker positions / cash. Intraday by
  definition. The freshness-gate guards in :func:`PaperBroker._restore_state`
  would reject stale state anyway, but this is defense in depth.
* ``signal:<segment>:*`` — per-symbol signals. ``_stale_signal_cleanup``
  healthcheck also cleans these but we drop them up-front for a clean slate.
* ``heartbeat:tick:<segment>`` — last-tick timestamp. New process, new clock.
* ``profit_lockin:<segment>`` — daily P&L target halt. Resets per day.
* ``trail:<segment>:*`` — per-position trailing-stop snapshots.

What we **DO** clear (global, equity-segment only — see note below):

* ``orders`` — the executor's live-state order hash (written in
  ``Executor._submit``). Audit trail of today's order attempts. The
  journal files in ``logs/trades/<seg>/YYYY-MM-DD.jsonl`` are the
  durable audit, so the hash is purely a convenience. Without this
  cleanup the hash grows unbounded across days (3 stale Apr-30
  entries were still present on 2026-05-05 morning). Equity-segment
  only because the hash is global; running it twice a day from both
  bots would still be correct but redundant.

What we **DO NOT** clear (intentionally):

* ``research:YYYY-MM-DD`` — produced by the 08:30 pre-market agent
  *for today*. Clearing this would force a wasted re-run.
* ``watchlist:auto`` — produced by the 08:00 watchlist updater for today.
* ``healthcheck:latest`` and ``healthcheck:history`` — diagnostic trail.
* ``fee_audit:latest`` — a 7-day TTL artefact; clearing it would
  obscure the most recent audit.
* ``bars:*`` — yfinance cache with a 60s TTL; auto-stale.

The function takes a ``segment`` argument so the equity reset never
clears the F&O bot's state (and vice versa). It is safe to call
repeatedly; returns a dict of keys deleted by category.
"""
from __future__ import annotations

from typing import Dict

from .cache import get_cache
from .logger import logger
from .segment import Segment, cache_key, signal_pattern, trail_pattern


def daily_reset(segment: Segment = Segment.EQUITY) -> Dict[str, int]:
    """Wipe intraday-only keys for ``segment``. Returns ``{key_or_pattern: n_deleted}``.

    Errors are logged and counted as 0 — this function never raises so
    a Redis blip on startup doesn't keep the bot from coming up.
    """
    cache = get_cache()
    counts: Dict[str, int] = {}

    exact_keys = [
        cache_key("paper:state",     segment),
        cache_key("heartbeat:tick",  segment),
        cache_key("profit_lockin",   segment),
    ]
    patterns = [
        signal_pattern(segment),
        trail_pattern(segment),
    ]

    for k in exact_keys:
        try:
            existed = cache.get_json(k) is not None
            cache.delete(k)
            counts[k] = 1 if existed else 0
        except Exception as e:
            logger.warning("[daily-reset:{}] could not delete {}: {}", segment.value, k, e)
            counts[k] = 0

    for pat in patterns:
        try:
            keys = cache.keys(pat)
            for key in keys:
                cache.delete(key)
            counts[pat] = len(keys)
        except Exception as e:
            logger.warning("[daily-reset:{}] could not delete pattern {}: {}", segment.value, pat, e)
            counts[pat] = 0

    # Global ``orders`` hash cleanup — equity bot only, to avoid both
    # bots redundantly clearing the same global key. The hash is an
    # audit-only convenience (journal files are the durable trail) so
    # nuking it daily is safe and prevents stale-day accumulation.
    if segment == Segment.EQUITY:
        try:
            client = cache.client
            n_before = 0
            try:
                n_before = client.hlen("orders") if client.type("orders") == "hash" else 0
            except Exception:
                n_before = 0
            if n_before > 0:
                cache.delete("orders")
                counts["orders"] = int(n_before)
            else:
                counts["orders"] = 0
        except Exception as e:
            logger.warning("[daily-reset:{}] could not clear global orders hash: {}",
                           segment.value, e)
            counts["orders"] = 0

    total = sum(counts.values())
    if total > 0:
        breakdown = ", ".join(f"{k}={v}" for k, v in counts.items() if v > 0)
        logger.info("[daily-reset:{}] cleared {} intraday key(s): {}",
                    segment.value, total, breakdown)
    else:
        logger.info("[daily-reset:{}] no intraday keys to clear (already clean)", segment.value)
    return counts
