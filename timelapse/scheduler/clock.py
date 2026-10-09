"""
Wall-clock sanity for a Pi with no real-time clock.

After a power cut a Raspberry Pi boots with the time it last shut down
(fake-hwclock) until NTP corrects it.  Planning captures before that would
put them at the wrong times, so the scheduler waits for systemd to report
the clock as synchronised, up to a timeout (an offline Pi still runs).
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

POLL_S = 5


def is_clock_synced() -> Optional[bool]:
    """True/False from `timedatectl`, or None if it can't be determined."""
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip().lower()
    if value in ("yes", "true", "1"):
        return True
    if value in ("no", "false", "0"):
        return False
    return None


def wait_for_clock_sync(stop_event: threading.Event, timeout_s: float) -> bool:
    """
    Block until the clock is synchronised, `timeout_s` passes, or `stop_event`
    is set.  Returns True if the clock is known (or assumed) to be good.
    """
    synced = is_clock_synced()
    if synced is None:
        logger.debug("Clock sync status unavailable (no timedatectl); assuming OK.")
        return True
    if synced:
        return True

    logger.info("Waiting up to %d s for the system clock to sync (NTP)...", timeout_s)
    deadline = time.monotonic() + timeout_s
    while not stop_event.is_set() and time.monotonic() < deadline:
        stop_event.wait(POLL_S)
        if is_clock_synced():
            logger.info("System clock synchronised.")
            return True
    if not stop_event.is_set():
        logger.warning(
            "System clock still not synchronised after %d s; scheduling anyway. "
            "Capture times may be off until NTP syncs.",
            timeout_s,
        )
    return False
