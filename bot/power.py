"""macOS power-state helpers.

Used to warn the operator when the bot is being started on battery
power — the conditions under which the 2026-05-05 morning "Mac-sleep
blackout" silently froze both bots for 2 h 10 m. ``caffeinate -i`` does
hold ``PreventUserIdleSystemSleep`` even on battery, but it does NOT
prevent macOS *standby* (deep sleep after ``standbydelaylow``) or
clamshell-mode sleep with the lid closed. The only reliable mitigation
on battery is::

    sudo pmset -b sleep 0 disablesleep 1

which requires user consent. The bot prints a clear actionable warning
when it detects battery operation at startup so the operator can apply
the pmset workaround (or plug in) before the next session.

These helpers are no-ops on non-darwin platforms — they return
``("unknown", None)`` so callers don't need to special-case them.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
from typing import Literal, Optional, Tuple

PowerSource = Literal["ac", "battery", "unknown"]


def power_state() -> Tuple[PowerSource, Optional[int]]:
    """Return ``(source, percent_remaining)``.

    * ``source`` is ``"ac"`` (plugged in), ``"battery"`` (on battery),
      or ``"unknown"`` (non-darwin or pmset unavailable).
    * ``percent_remaining`` is the integer percentage if known.

    Implemented via ``pmset -g ps`` parsing — the same single source of
    truth macOS uses for its menu-bar battery indicator. We avoid the
    IOKit Python bindings to keep this dependency-free.
    """
    if platform.system() != "Darwin":
        return "unknown", None
    pmset = shutil.which("pmset")
    if pmset is None:
        return "unknown", None
    try:
        out = subprocess.run(
            [pmset, "-g", "ps"], capture_output=True, text=True, timeout=2
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return "unknown", None

    source: PowerSource = "unknown"
    pct: Optional[int] = None
    for line in out.splitlines():
        line = line.strip()
        if "AC Power" in line:
            source = "ac"
        elif "Battery Power" in line:
            source = "battery"
        if "%" in line:
            for tok in line.replace(";", " ").split():
                if tok.endswith("%"):
                    try:
                        pct = int(tok.rstrip("%"))
                    except ValueError:
                        pass
                    break
    return source, pct


def battery_warning_lines() -> list[str]:
    """Multi-line warning printed by ``cli run`` and the preflight when
    the bot is starting on battery. Returns ``[]`` on AC / unknown.
    """
    source, pct = power_state()
    if source != "battery":
        return []
    pct_str = f" ({pct}%)" if pct is not None else ""
    return [
        f"⚠  Bot is starting on BATTERY POWER{pct_str}.",
        "    `caffeinate -i` prevents idle sleep but NOT macOS standby or",
        "    lid-close clamshell sleep. On 2026-05-05 the bot was frozen for",
        "    2 h 10 m by exactly this. Plug in to AC, or run:",
        "",
        "        sudo pmset -b sleep 0 disablesleep 1",
        "",
        "    (and `sudo pmset -b sleep 1 disablesleep 0` to restore later)",
    ]
