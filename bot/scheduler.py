"""APScheduler-based runner with IST market hours.

Resilience notes:
  - Every job is registered with ``misfire_grace_time`` and ``coalesce=True`` so
    that if the host (laptop) sleeps or the scheduler is paused for any reason,
    the next wake-up still fires the most-recent missed run instead of silently
    dropping it. This is how the 2026-04-28 incident (silent 13:59→15:37 gap →
    square-off ran at 15:37 instead of 15:15) is prevented going forward.
  - On startup we catch up explicitly: if the bot starts after ``square_off``
    with open positions, close them immediately and write the EOD report,
    rather than waiting until the next trading day.
"""
from __future__ import annotations

import signal as os_signal
import sys
from datetime import datetime

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import load_config
from .daily_reset import daily_reset
from .executor import Executor
from .fee_audit import run_fee_audit
from .healthcheck import run_healthcheck
from .journal import write_eod_report
from .logger import logger
from .research import run_research
from .segment import Segment
from .watchlist_updater import update_watchlist

IST = pytz.timezone("Asia/Kolkata")

# Misfire grace windows (seconds). If the scheduler missed a fire-time by more
# than this, the job is dropped — set generously so a sleeping laptop doesn't
# cause the day's square-off to be missed entirely.
TICK_GRACE = 120          # per-minute tick: skip stale ticks > 2 min late
PREMARKET_GRACE = 30 * 60  # 30 min — research / watchlist are time-sensitive but tolerate small drift
SQUAREOFF_GRACE = 6 * 60 * 60  # 6 h — fire square-off no matter how late, as long as same day


def start(segment: Segment = Segment.EQUITY) -> None:
    """Build & run the scheduler for ``segment``.

    Each segment runs in its own process (its own ``cli run --segment …``)
    and registers ITS OWN copy of the per-minute tick, the EOD report,
    daily reset, and healthcheck jobs. A few jobs are global and run
    only in the equity scheduler to avoid duplicate work — see comments
    inline.
    """
    cfg = load_config()

    # Refuse to start the F&O scheduler if F&O is disabled in config.
    if segment == Segment.FNO and (cfg.fno is None or not cfg.fno.enabled):
        logger.error("[scheduler:fno] F&O is disabled in config.yaml — set "
                     "`fno.enabled: true` to start the F&O bot.")
        sys.exit(1)

    # Run a scoped Redis cleanup BEFORE creating the executor — otherwise
    # the executor's broker init would re-read whatever stale paper:state
    # is in cache. The freshness gates inside _restore_state would still
    # reject obvious corruption, but a clean slate is simpler to reason
    # about. See bot/daily_reset.py for what is / isn't cleared.
    try:
        daily_reset(segment=segment)
    except Exception as e:
        logger.warning("[scheduler:{}] daily_reset on startup failed: {}",
                       segment.value, e)

    sched = BlockingScheduler(timezone=IST)
    executor = Executor(segment=segment)

    # ── Pre-market jobs (equity only) ─────────────────────────────────
    # The watchlist updater scans NIFTY-100 cash equities and the
    # research agent ranks those — neither concept applies to F&O. The
    # F&O segment uses its own configured underlyings list directly.
    if segment == Segment.EQUITY:
        if cfg.watchlist_updater.enabled:
            wh, wm = map(int, cfg.watchlist_updater.run_at.split(":"))
            sched.add_job(
                update_watchlist,
                CronTrigger(day_of_week="mon-fri", hour=wh, minute=wm, timezone=IST),
                id="watchlist_updater",
                replace_existing=True,
                misfire_grace_time=PREMARKET_GRACE,
                coalesce=True,
            )
            logger.info("[{}] Scheduled watchlist updater at {}", segment.value, cfg.watchlist_updater.run_at)

        if cfg.research.enabled:
            rh, rm = map(int, cfg.research.run_at.split(":"))
            sched.add_job(
                run_research,
                CronTrigger(day_of_week="mon-fri", hour=rh, minute=rm, timezone=IST),
                id="pre_market_research",
                replace_existing=True,
                misfire_grace_time=PREMARKET_GRACE,
                coalesce=True,
            )
            logger.info("[{}] Scheduled pre-market research at {}", segment.value, cfg.research.run_at)

    so = cfg.session.t("square_off")
    sched.add_job(
        executor.tick,
        CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/1", timezone=IST),
        id="executor_tick",
        replace_existing=True,
        misfire_grace_time=TICK_GRACE,
        coalesce=True,
    )

    sched.add_job(
        executor._end_of_day,
        CronTrigger(day_of_week="mon-fri", hour=so.hour, minute=so.minute, timezone=IST),
        id="end_of_day",
        replace_existing=True,
        misfire_grace_time=SQUAREOFF_GRACE,
        coalesce=True,
    )

    # Write the daily P&L statement 2 minutes after square-off so all closing
    # fills are journaled first. Each segment writes its OWN EOD report.
    eod_minute = (so.minute + 2) % 60
    eod_hour = so.hour + (1 if so.minute + 2 >= 60 else 0)

    def _segment_write_eod_report():
        return write_eod_report(segment=segment)

    sched.add_job(
        _segment_write_eod_report,
        CronTrigger(day_of_week="mon-fri", hour=eod_hour, minute=eod_minute, timezone=IST),
        id="eod_report",
        replace_existing=True,
        misfire_grace_time=SQUAREOFF_GRACE,
        coalesce=True,
    )
    logger.info("[{}] Scheduled EOD P&L report at {:02d}:{:02d}",
                segment.value, eod_hour, eod_minute)

    # Daily Redis cleanup at 08:55 IST (Mon-Fri). Wipes intraday-only keys
    # FOR THIS SEGMENT only — never touches the other segment's state.
    def _segment_daily_reset():
        return daily_reset(segment=segment)
    sched.add_job(
        _segment_daily_reset,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=55, second=0, timezone=IST),
        id="daily_reset",
        replace_existing=True,
        misfire_grace_time=30 * 60,
        coalesce=True,
    )
    logger.info("[{}] Scheduled daily Redis reset at 08:55 IST", segment.value)

    # Daily fee-schedule audit at 09:00 IST (Mon-Fri). This is GLOBAL —
    # only the equity scheduler registers it so the F&O bot doesn't
    # redundantly hit Zerodha's charges page. Both segments' healthchecks
    # read the same shared `fee_audit:latest` cache key.
    if segment == Segment.EQUITY:
        sched.add_job(
            run_fee_audit,
            CronTrigger(day_of_week="mon-fri", hour=9, minute=0, second=0, timezone=IST),
            id="fee_audit",
            replace_existing=True,
            misfire_grace_time=30 * 60,
            coalesce=True,
        )
        logger.info("[{}] Scheduled daily fee/tax audit at 09:00 IST", segment.value)

        # Daily NSE holiday-calendar refresh at 08:00 IST (was 06:00 — moved
        # 2026-05-04 to align with the standard 08:00–08:30 pre-market
        # warm-up window so all premarket fetches happen in the same band
        # rather than scattered across the early-morning hours). Single
        # global job (equity scheduler only) — both bots and the dashboard
        # read from the same Redis snapshot. Runs on EVERY day-of-week so
        # we still refresh on Saturdays, ensuring Monday morning's
        # dashboard is always sourced from a fresh NSE pull.
        from .holidays import refresh_holidays
        sched.add_job(
            refresh_holidays,
            CronTrigger(hour=8, minute=0, second=0, timezone=IST),
            id="nse_holidays_refresh",
            replace_existing=True,
            misfire_grace_time=60 * 60,
            coalesce=True,
        )
        logger.info("[{}] Scheduled NSE holiday-calendar refresh at 08:00 IST (daily)", segment.value)

    # Periodic health-check (Mon-Fri at 09:00, 11:00, 13:00, 15:00 IST).
    # Each segment runs its own healthcheck against its own keys; the
    # results are published to ``healthcheck:latest:<segment>`` for the
    # dashboard. Runs at 09:00:30 (after fee_audit at 09:00:00) so the
    # 09:00 cycle sees the freshest audit result.
    def _segment_healthcheck():
        return run_healthcheck(segment=segment)
    sched.add_job(
        _segment_healthcheck,
        CronTrigger(day_of_week="mon-fri", hour="9,11,13,15", minute=0, second=30, timezone=IST),
        id="healthcheck",
        replace_existing=True,
        misfire_grace_time=30 * 60,
        coalesce=True,
    )
    logger.info("[{}] Scheduled health-check at 09:00, 11:00, 13:00, 15:00 IST (visible on dashboard)",
                segment.value)

    _startup_catchup(executor, cfg, segment=segment)

    def _shutdown(*_):
        logger.warning("[{}] Shutdown signal received — squaring off and exiting.", segment.value)
        try:
            # mark_done=False: SIGTERM is a defensive sweep, not the
            # scheduled 15:15 EOD. Setting the eod_done marker here
            # would poison the rest of the day if the bot is restarted
            # mid-session — exactly the 2026-05-05 PM regression.
            executor._end_of_day(mark_done=False)
            try:
                write_eod_report(segment=segment)
            except Exception as e:
                logger.error("[{}] EOD report on shutdown failed: {}", segment.value, e)
        finally:
            sched.shutdown(wait=False)
            sys.exit(0)

    os_signal.signal(os_signal.SIGINT, _shutdown)
    os_signal.signal(os_signal.SIGTERM, _shutdown)

    logger.info("[{}] Bot started at {} IST. Press Ctrl+C to stop.",
                segment.value, datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"))
    sched.start()


def _startup_catchup(executor: Executor, cfg, segment: Segment = Segment.EQUITY) -> None:
    """If the bot is started past the square-off time on a weekday, close any
    open positions immediately and emit the EOD report — don't wait until the
    next day. Idempotent: if there's nothing open, both calls are no-ops.
    """
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return
    if now.time() < cfg.session.t("square_off"):
        return
    open_positions = [p for p in executor.broker.positions() if p.qty != 0]
    if open_positions:
        logger.warning(
            "[startup-catchup:{}] Bot started at {} IST (after square_off {}) "
            "with {} open position(s) — squaring off now.",
            segment.value, now.strftime("%H:%M:%S"), cfg.session.square_off,
            len(open_positions),
        )
        # mark_done=False: defensive recovery sweep, not the legitimate
        # 15:15 cron path. On 2026-05-05 the bot restarted at 10:13
        # after a Mac-sleep blackout; this branch ran on an empty book
        # and used to set the eod_done marker, which then blocked the
        # legitimate 15:15 square-off when real F&O positions opened
        # at 13:26. The marker is now reserved for scheduled paths.
        executor._end_of_day(mark_done=False)
    try:
        write_eod_report(segment=segment)
    except Exception as e:
        logger.error("[startup-catchup:{}] EOD report failed: {}", segment.value, e)
