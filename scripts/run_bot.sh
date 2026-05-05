#!/usr/bin/env bash
# ==============================================================================
# Stock-Market-Bot launcher — wraps `python -m cli run --paper` with macOS's
# built-in `caffeinate` so the Mac cannot sleep while the bot is running.
#
# Why: macOS's idle-sleep can suspend the python process for hours (we saw a
# 13:59 → 15:37 silent gap on 2026-04-28). When the system wakes, scheduled
# cron jobs that fire while sleeping are dropped (mitigated by APScheduler's
# misfire_grace_time, but a missed square-off should never even be possible
# in the first place).
#
# `caffeinate` flags used:
#   -i  prevent idle-sleep   (the most important one for a long-running tick)
#   -m  prevent disk-sleep   (Redis / file logging stay responsive)
#   -s  prevent system-sleep when on AC power (laptop shut lid → still runs)
#   -w PID  caffeinate exits when the bot exits — Mac can sleep again
#
# Usage:
#   bash scripts/run_bot.sh                                  # defaults: run --paper --segment equity
#   bash scripts/run_bot.sh run --paper                      # explicit equity paper
#   bash scripts/run_bot.sh run --paper --segment fno        # F&O paper bot
#   bash scripts/run_bot.sh run --segment equity             # live equity (LIVE_TRADING in .env)
#
# To run BOTH segments concurrently, open two terminals:
#   terminal 1:  bash scripts/run_bot.sh run --paper --segment equity
#   terminal 2:  bash scripts/run_bot.sh run --paper --segment fno
# They have separate locks (.bot.lock.equity / .bot.lock.fno) so they
# won't refuse each other.
#
# Ctrl+C in the foreground terminates the bot AND releases the sleep lock.
# ==============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# Default to paper-mode equity if no args supplied. Explicitly include
# --segment equity so the lock file used is .bot.lock.equity (and a
# parallel F&O launcher lands on .bot.lock.fno without colliding).
ARGS=("$@")
if [ "${#ARGS[@]}" -eq 0 ]; then
  ARGS=("run" "--paper" "--segment" "equity")
fi

echo "[run_bot] $(date '+%Y-%m-%d %H:%M:%S %Z')  starting:  python -m cli ${ARGS[*]}"
echo "[run_bot] caffeinate is preventing idle/disk/system sleep while this bot runs."
echo "[run_bot] press Ctrl+C to stop (the Mac will be allowed to sleep again immediately)."
echo

# Launch the bot in the background, then attach caffeinate to that PID with -w
# so caffeinate exits the moment the bot does (no orphaned sleep lock).
python -m cli "${ARGS[@]}" &
BOT_PID=$!

# Forward signals so Ctrl+C reaches the bot, not just caffeinate.
trap 'kill -TERM "$BOT_PID" 2>/dev/null || true' INT TERM

caffeinate -i -m -s -w "$BOT_PID" &
CAFF_PID=$!

wait "$BOT_PID"
EXIT=$?
kill "$CAFF_PID" 2>/dev/null || true

echo
echo "[run_bot] $(date '+%Y-%m-%d %H:%M:%S %Z')  bot exited with status $EXIT — sleep lock released."
exit "$EXIT"
