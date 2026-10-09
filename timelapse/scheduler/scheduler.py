"""
Main capture scheduler.

One scheduler thread serves any number of *intents*: schedules that share one
camera (e.g. the same 3D printer every 5 s for a detailed timelapse and every
30 s for a short one).  Each intent has its own rules and state; the
scheduler works out which intents are due next and hands them to `on_due`
together, so the caller can take one photo and file it for each of them.

Behaviour:
  - Waits for the system clock to sync first (a Pi has no RTC).
  - Sleeps in short chunks and re-plans from the wall clock each time, so a
    clock correction moves the schedule instead of being slept through.
  - A slot is marked handled *before* the capture runs, whether the capture
    succeeds or not, so a failing capture never re-triggers the same slot.
  - After downtime, a slot missed by up to `schedule.catch_up` is taken once;
    older misses are skipped.
  - While running, a slot that's already past by the time the camera is free
    (capture slower than the interval) is skipped and counted, never queued.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from timelapse.config.models import LocationConfig, ScheduleConfig
from timelapse.scheduler.clock import wait_for_clock_sync
from timelapse.scheduler.rules import ScheduleError, next_fire
from timelapse.scheduler.state import ProfileState

logger = logging.getLogger(__name__)

# Intents due within this many seconds of each other share one capture
COALESCE_S = 1.0

# A slot this late (seconds) while running counts as skipped, not taken
LATE_TOLERANCE_S = 2.0

# Longest single sleep before re-checking the wall clock
MAX_SLEEP_S = 30.0

# Re-plan interval when no intent has anything scheduled
IDLE_SLEEP_S = 300.0

# How long to wait for NTP at startup before scheduling anyway
CLOCK_SYNC_TIMEOUT_S = 300.0


@dataclass
class Intent:
    """One schedule sharing the camera."""

    name: str
    schedule: ScheduleConfig
    state: ProfileState
    location: Optional[LocationConfig] = None
    # Set once the scheduler has planned this intent since starting
    _started: bool = field(default=False, repr=False)


@dataclass
class PlannedSlot:
    at: datetime
    intent: Intent
    skipped_late: bool = False


OnDue = Callable[[list[Intent], datetime], None]


class Scheduler:
    """
    Runs in its own thread.  Call `start()` and `stop()`.

    `on_due(intents, slot)` is called each time one or more intents are due.
    It may block (the scheduler waits for it); while it runs, no other
    capture is started.
    """

    def __init__(
        self,
        intents: list[Intent],
        on_due: OnDue,
        local_tz,
        *,
        clock_sync_timeout_s: float = CLOCK_SYNC_TIMEOUT_S,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._intents = intents
        self._on_due = on_due
        self._tz = local_tz
        self._clock_sync_timeout_s = clock_sync_timeout_s
        self._now = now_fn
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        logger.info(
            "Scheduler started for %s.", ", ".join(f"'{i.name}'" for i in self._intents)
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info("Scheduler stopped.")

    # ── main loop ──────────────────────────────────────────────────────────

    def _run(self) -> None:
        wait_for_clock_sync(self._stop_event, self._clock_sync_timeout_s)
        while not self._stop_event.is_set():
            try:
                self._stop_event.wait(self.tick())
            except Exception as exc:
                logger.exception("Unhandled scheduler error: %s", exc)
                self._stop_event.wait(30)  # back-off before retry

    def tick(self) -> float:
        """
        Fire whatever is due now; return how long to sleep before the next
        tick.  Public so tests can drive the scheduler without a thread.
        """
        now = self._now()
        slots = self.plan(now)

        for s in slots:
            if s.skipped_late:
                s.intent.state.record_skipped()
                logger.warning(
                    "[%s] Skipped a missed slot (camera still busy, or the clock jumped).",
                    s.intent.name,
                )

        if not slots:
            return IDLE_SLEEP_S

        first = min(s.at for s in slots)
        delay = (first - now).total_seconds()
        if delay > 0:
            if delay > MAX_SLEEP_S:
                logger.debug("Next capture at %s.", first.astimezone(self._tz))
            return min(delay, MAX_SLEEP_S)

        due = [s for s in slots if (s.at - first).total_seconds() <= COALESCE_S]
        for s in due:
            # Mark the slot handled before capturing, success or not.
            s.intent.state.set_last_slot(max(s.at, now))
        intents = [s.intent for s in due]
        logger.info(
            "Capture due for %s (slot %s).",
            ", ".join(i.name for i in intents),
            first.astimezone(self._tz).strftime("%Y-%m-%d %H:%M:%S %Z"),
        )
        try:
            self._on_due(intents, first)
        except Exception as exc:
            logger.exception("Capture callback failed: %s", exc)
        return 0.0

    # ── planning ───────────────────────────────────────────────────────────

    def plan(self, now: datetime) -> list[PlannedSlot]:
        """Next slot for each active intent."""
        slots: list[PlannedSlot] = []
        for intent in self._intents:
            if intent.state.get_status() in ("completed", "paused"):
                continue
            slot = self._plan_intent(intent, now)
            if slot is not None:
                slots.append(slot)
        return slots

    def _plan_intent(self, intent: Intent, now: datetime) -> Optional[PlannedSlot]:
        last = intent.state.get_last_slot_time()
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        # Never plan from the future (e.g. state written before a clock fix)
        if last is not None and last > now:
            last = now

        if not intent._started:
            grace = timedelta(seconds=intent.schedule.catch_up_seconds)
        else:
            grace = timedelta(seconds=LATE_TOLERANCE_S)
        floor = now - grace
        start = max(last, floor) if last is not None else floor

        try:
            nxt = next_fire(intent.schedule, start, self._tz, intent.location)
        except ScheduleError as exc:
            logger.error("[%s] %s", intent.name, exc)
            return None

        skipped_late = False
        if intent._started and last is not None and start > last:
            # Was there a slot between the last one we handled and the floor?
            try:
                missed = next_fire(intent.schedule, last, self._tz, intent.location)
            except ScheduleError:
                missed = None
            skipped_late = missed is not None and missed <= floor
            if skipped_late:
                # Count it once: move the marker past the missed slot
                intent.state.set_last_slot(floor)
        intent._started = True

        if nxt is None:
            return None
        # Keep slots in UTC: same-tzinfo comparisons use wall time and would
        # misorder slots in a repeated DST hour.
        return PlannedSlot(
            at=nxt.astimezone(timezone.utc), intent=intent, skipped_late=skipped_late
        )
