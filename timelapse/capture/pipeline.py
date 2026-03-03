"""
Capture pipeline — ties the scheduler → camera → disk guard → video generator.

On each scheduled tick the pipeline:
  1. Checks disk free space (disk guard)
  2. Asks the camera for a still
  3. Names the file with a timestamp
  4. Updates state
  5. If batch threshold reached, optionally triggers video generation
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from timelapse.capture.camera import PiCamera, MockCamera
from timelapse.config.models import ProfileConfig
from timelapse.disk.guard import DiskGuard
from timelapse.scheduler.state import ProfileState

logger = logging.getLogger(__name__)


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
    """

    def __init__(
        self,
        config: ProfileConfig,
        state: ProfileState,
        camera: "PiCamera | MockCamera",
        disk_guard: "DiskGuard",
        video_trigger_fn: "callable | None" = None,
    ) -> None:
        self._cfg = config
        self._state = state
        self._camera = camera
        self._disk_guard = disk_guard
        self._video_trigger = video_trigger_fn

    def __call__(self) -> bool:
        """Callable interface for the Scheduler."""
        return self.capture()

    def capture(self) -> bool:
        """
        Take one timelapse frame.  Returns True on success.
        """
        # 1. Disk guard — enforce free space before writing
        freed = self._disk_guard.enforce()
        if freed:
            logger.info("Disk guard freed %d file(s) to reclaim space.", freed)

        if not self._disk_guard.has_free_space():
            logger.error(
                "Disk full even after cleanup — skipping capture. "
                "Free up space or lower min_free_gb."
            )
            return False

        # 2. Build path
        now = datetime.now(timezone.utc)
        filename = make_filename(now)
        path = self._cfg.photos_path / filename

        # 3. Capture
        ok = self._camera.capture_still(path, timeout_s=self._cfg.capture.capture_timeout_s)
        if not ok:
            return False

        # 4. Update state
        self._state.record_capture(now)
        batch = self._state.get_current_batch()
        logger.info(
            "Captured %s  (batch %d / %d)",
            filename,
            batch,
            self._cfg.video.photos_per_video,
        )

        # 5. Check video threshold
        if batch >= self._cfg.video.photos_per_video:
            logger.info("Video threshold reached — triggering generation.")
            if self._video_trigger:
                self._video_trigger()
            # Reset batch counter (done inside record_video)

        return True

    def snapshot(self, output_path: Path) -> bool:
        """Manual one-shot capture to an explicit path (for `timelapse snapshot`)."""
        ok = self._camera.capture_still(output_path)
        if ok:
            logger.info("Snapshot saved: %s", output_path)
        return ok
