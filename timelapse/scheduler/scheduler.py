"""
Main capture scheduler.

Supports four schedule modes:
  interval  — every N seconds/minutes/hours
  times     — one or more fixed HH:MM times per day
  solar     — sunrise / solar_noon / sunset with optional offsets

Design goals:
  - Survives reboots: on restart, computes the correct NEXT time from state
  - No double-captures: 60 s cooldown window around every scheduled slot
  - Interruptible sleep: wakes every 5 s to check for shutdown / schedule changes
  - If solar fetch fails, skips all solar events for that day and retries next day
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from timelapse.config.models import (
    IntervalSchedule,
    ProfileConfig,
    SolarSchedule,
    TimesSchedule,
    parse_interval_seconds,
    parse_hhmm,
)
from timelapse.scheduler.solar import SolarTimes, compute_solar_schedule, get_solar_times
from timelapse.scheduler.state import ProfileState

logger = logging.getLogger(__name__)

# Minimum seconds between captures — prevents double-trigger on restart
CAPTURE_COOLDOWN_S = 60

# How often the sleep loop wakes to check for shutdown
SLEEP_TICK_S = 5


class Scheduler:
    """
    Runs in its own thread.  Call `start()` and `stop()`.

    `capture_fn` is called each time a capture should happen.
    It must be thread-safe (blocking is OK; the scheduler waits for it).
    """

    def __init__(
        self,
        profile: str,
        config: ProfileConfig,
        state: ProfileState,
        capture_fn: Callable[[], bool],
        local_tz,
    ) -> None:
        self._profile = profile
        self._config = config
        self._state = state
        self._capture_fn = capture_fn
        self._tz = local_tz
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"scheduler-{self._profile}", daemon=True
        )
        logger.info("Scheduler started for profile '%s'.", self._profile)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info("Scheduler stopped for profile '%s'.", self._profile)

    # ── main loop ──────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                logger.exception("Unhandled scheduler error: %s", exc)
                self._stop_event.wait(30)  # back-off before retry

    def _tick(self) -> None:
        status = self._state.get_status()
        if status == "completed":
            logger.debug("Profile completed; scheduler idle.")
            self._stop_event.wait(60)
            return

        now = datetime.now(self._tz)
        next_dt = self._next_capture_time(now)

        if next_dt is None:
            # Solar fetch failure — skip the rest of the day
            seconds_until_midnight = self._seconds_until_midnight(now)
            logger.warning(
                "No next capture time (solar fetch failed?). "
                "Sleeping %d s until midnight.", seconds_until_midnight
            )
            self._interruptible_sleep(seconds_until_midnight)
            return

        delay = (next_dt - now).total_seconds()

        if delay > 0:
            logger.info(
                "Next capture at %s (in %.0f s).",
                next_dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
                delay,
            )
            self._interruptible_sleep(delay)
            if self._stop_event.is_set():
                return
            # We slept the full scheduled delay — always capture now.
            # (Cooldown guard only applies to the startup/catchup path below.)
        else:
            # delay <= 0: startup or missed-slot catchup — guard against
            # double-capture if the service was restarted within the cooldown window.
            if self._state.was_recently_captured(CAPTURE_COOLDOWN_S):
                logger.debug(
                    "Skipping — captured within last %d s (restart guard).",
                    CAPTURE_COOLDOWN_S,
                )
                self._interruptible_sleep(CAPTURE_COOLDOWN_S + 1)
                return

        logger.info("Triggering capture.")
        ok = self._capture_fn()
        if ok:
            logger.info("Capture succeeded.")
        else:
            logger.warning("Capture reported failure.")

    # ── next-time logic ────────────────────────────────────────────────────

    def _next_capture_time(self, now: datetime) -> Optional[datetime]:
        schedule = self._config.schedule

        if isinstance(schedule, IntervalSchedule):
            return self._next_interval(now, schedule)

        if isinstance(schedule, TimesSchedule):
            return self._next_times(now, schedule)

        if isinstance(schedule, SolarSchedule):
            return self._next_solar(now, schedule)

        logger.error("Unknown schedule type: %s", type(schedule))
        return None

    # ── interval ───────────────────────────────────────────────────────────

    def _next_interval(self, now: datetime, schedule: IntervalSchedule) -> datetime:
        interval_s = schedule.interval_seconds
        last = self._state.get_last_capture_time()

        if last is None:
            # First ever capture — do it immediately
            return now

        # Ensure aware datetime
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        last_local = last.astimezone(self._tz)

        next_dt = last_local + timedelta(seconds=interval_s)
        if next_dt <= now:
            # We missed a slot (reboot, etc.) — capture as soon as possible
            return now
        return next_dt

    # ── fixed times ────────────────────────────────────────────────────────

    def _next_times(self, now: datetime, schedule: TimesSchedule) -> datetime:
        today = now.date()
        candidates: list[datetime] = []

        for time_str in schedule.times:
            h, m = parse_hhmm(time_str)
            candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
            # Must be strictly after last capture + cooldown
            last = self._state.get_last_capture_time()
            if last:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                cutoff = last.astimezone(self._tz) + timedelta(seconds=CAPTURE_COOLDOWN_S)
                if candidate <= cutoff:
                    continue
            if candidate > now:
                candidates.append(candidate)

        if candidates:
            return min(candidates)

        # All today's slots are past — find first slot tomorrow
        tomorrow = today + timedelta(days=1)
        h, m = parse_hhmm(schedule.times[0])
        return datetime(
            tomorrow.year, tomorrow.month, tomorrow.day, h, m, 0,
            tzinfo=now.tzinfo,
        )

    # ── solar ──────────────────────────────────────────────────────────────

    def _next_solar(self, now: datetime, schedule: SolarSchedule) -> Optional[datetime]:
        today_str = now.date().isoformat()

        # Use cached times if available for today
        cached = self._state.get_solar_cache(today_str)
        if cached:
            solar = SolarTimes.from_dict(cached)
        else:
            solar = get_solar_times(
                lat=schedule.latitude,
                lon=schedule.longitude,
                api_key_env=schedule.owm_api_key_env,
                for_date=now.date(),
            )
            if solar is None:
                return None  # caller will handle
            self._state.set_solar_cache(today_str, solar.as_dict())

        slots = compute_solar_schedule(solar, schedule.events, self._tz)
        last = self._state.get_last_capture_time()

        for slot in slots:
            if slot.tzinfo is None:
                slot = slot.replace(tzinfo=self._tz)
            # Guard against double-capture
            if last:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                cutoff = last.astimezone(self._tz) + timedelta(seconds=CAPTURE_COOLDOWN_S)
                if slot <= cutoff:
                    continue
            if slot >= now:
                return slot

        # All today's solar slots have passed — get first slot tomorrow
        tomorrow = now.date() + timedelta(days=1)
        tomorrow_str = tomorrow.isoformat()
        tomorrow_solar = get_solar_times(
            lat=schedule.latitude,
            lon=schedule.longitude,
            api_key_env=schedule.owm_api_key_env,
            for_date=tomorrow,
        )
        if tomorrow_solar is None:
            # sleep till midnight and retry
            return datetime(
                now.year, now.month, now.day, 23, 59, 0, tzinfo=self._tz
            ) + timedelta(minutes=1)
        self._state.set_solar_cache(tomorrow_str, tomorrow_solar.as_dict())
        tomorrow_slots = compute_solar_schedule(tomorrow_solar, schedule.events, self._tz)
        if tomorrow_slots:
            return tomorrow_slots[0]
        return None

    # ── helpers ────────────────────────────────────────────────────────────

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep for `seconds` but wake on stop_event every SLEEP_TICK_S."""
        end = time.monotonic() + seconds
        while not self._stop_event.is_set():
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            self._stop_event.wait(min(SLEEP_TICK_S, remaining))

    @staticmethod
    def _seconds_until_midnight(now: datetime) -> float:
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return (tomorrow - now).total_seconds()
