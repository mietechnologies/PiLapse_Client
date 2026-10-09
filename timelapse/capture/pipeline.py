"""
Capture pipeline — ties the scheduler → camera → disk guard → video generator.

On each scheduled tick the pipeline:
  1. Checks disk free space (disk guard)
  2. Asks the camera for a still
  3. Names the file with a timestamp
  4. Updates state (and, if weather recording is on, appends the photo and
     the latest weather reading to `captures.jsonl` in the photos dir)
  5. If batch threshold reached, optionally triggers video generation
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from timelapse.capture.camera import PiCamera, MockCamera
from timelapse.config.models import ProfileConfig
from timelapse.disk.guard import DiskGuard
from timelapse.scheduler.state import ProfileState
from timelapse.weather import WeatherRecorder

logger = logging.getLogger(__name__)

# Per-photo metadata (weather) log, one JSON object per line
CAPTURE_LOG = "captures.jsonl"


def make_filename(dt: datetime, prefix: str = "frame") -> str:
    """Generate a deterministic, sortable filename from a datetime."""
    # e.g. frame_20240115T083012Z.jpg
    ts = dt.strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{ts}.jpg"


class CapturePipeline:
    """
    Callable by the Scheduler.  Also exposes `snapshot()` for manual one-shots.

    `video_trigger_fn` is called when the photo batch threshold is reached.
    It should be non-blocking (e.g., submit to a thread pool).

    `on_captured_fn(path, captured_at)` is called after each successful
    capture (used to queue server uploads). It must also be non-blocking.
    """

    def __init__(
        self,
        config: ProfileConfig,
        state: ProfileState,
        camera: "PiCamera | MockCamera",
        disk_guard: "DiskGuard",
        video_trigger_fn: "callable | None" = None,
        weather: "WeatherRecorder | None" = None,
        on_captured_fn: "callable | None" = None,
    ) -> None:
        self._cfg = config
        self._state = state
        self._camera = camera
        self._disk_guard = disk_guard
        self._video_trigger = video_trigger_fn
        self._weather = weather
        self._on_captured = on_captured_fn

    def __call__(self) -> bool:
        """Callable interface for the Scheduler."""
        return self.capture()

    def capture(self) -> bool:
        """
        Take one timelapse frame.  Returns True on success.
        """
        return self.capture_photo() is not None

    def capture_photo(self) -> "tuple[Path, datetime] | None":
        """
        Take one timelapse frame and file it for this profile.
        Returns (path, captured_at), or None if nothing was captured.
        """
        if not self._ensure_space():
            return None

        now = datetime.now(timezone.utc)
        path = self._cfg.photos_path / make_filename(now)

        ok = self._camera.capture_still(path, timeout_s=self._cfg.capture.capture_timeout_s)
        if not ok:
            return None

        self._record(path, now)
        return path, now

    def adopt(self, source: Path, captured_at: datetime) -> bool:
        """
        File a photo another profile just took (several intents due at the
        same moment share one capture).  Hard-links when possible, so the
        copy costs no extra space.
        """
        if not self._ensure_space():
            return False

        path = self._cfg.photos_path / source.name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source, path)
            except OSError:
                shutil.copy2(source, path)
        except OSError as exc:
            logger.error("Could not file shared photo %s: %s", source.name, exc)
            return False

        self._record(path, captured_at)
        return True

    def _ensure_space(self) -> bool:
        freed = self._disk_guard.enforce()
        if freed:
            logger.info("Disk guard freed %d file(s) to reclaim space.", freed)

        if not self._disk_guard.has_free_space():
            logger.error(
                "Disk full even after cleanup — skipping capture. "
                "Free up space or lower min_free_gb."
            )
            return False
        return True

    def _record(self, path: Path, captured_at: datetime) -> None:
        """Update state and run post-capture steps for a filed photo."""
        filename = path.name
        self._state.record_capture(captured_at)
        self._log_capture(filename, captured_at)
        batch = self._state.get_current_batch()
        logger.info(
            "Captured %s  (batch %d / %d)",
            filename,
            batch,
            self._cfg.video.photos_per_video,
        )

        if self._on_captured:
            try:
                self._on_captured(path, captured_at)
            except Exception as exc:
                logger.error("Post-capture hook failed for %s: %s", filename, exc)

        # Check video threshold
        if batch >= self._cfg.video.photos_per_video:
            logger.info("Video threshold reached — triggering generation.")
            if self._video_trigger:
                self._video_trigger()
            # Reset batch counter (done inside record_video)

    def _log_capture(self, filename: str, captured_at: datetime) -> None:
        """Append one line per photo with the weather at capture time."""
        if self._weather is None or not self._weather.usable:
            return
        reading = self._weather.latest()
        entry = {
            "file": filename,
            "captured_at": captured_at.isoformat(),
            "weather": reading.as_dict() if reading else None,
        }
        try:
            with (self._cfg.photos_path / CAPTURE_LOG).open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as exc:
            logger.warning("Could not write %s: %s", CAPTURE_LOG, exc)

    def snapshot(self, output_path: Path) -> bool:
        """Manual one-shot capture to an explicit path (for `timelapse snapshot`)."""
        ok = self._camera.capture_still(output_path)
        if ok:
            logger.info("Snapshot saved: %s", output_path)
        return ok
