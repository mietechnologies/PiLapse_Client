"""
Timelapse video generator — uses ffmpeg to stitch JPEG frames into MP4.

Triggered by CapturePipeline when `photos_per_video` threshold is reached.
Runs in a daemon thread so photo capture is not blocked.

Output filename convention:
  timelapse_<first-frame-ts>_to_<last-frame-ts>.mp4
  e.g.  timelapse_20240115T080000Z_to_20240115T200000Z.mp4

After generation, source photos are handled per config.video.after_video:
  move_to_archive  → move to archive_dir / <video-stem>/
  delete           → os.remove each file
  keep             → leave in photos_dir (user manages space manually)

ffmpeg command (all platforms):
  ffmpeg -framerate <fps> -pattern_type glob -i '*.jpg' \
         -c:v libx264 -crf <crf> -pix_fmt yuv420p -y output.mp4

ffmpeg must be installed: sudo apt install -y ffmpeg
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from timelapse.config.models import ProfileConfig
from timelapse.scheduler.state import ProfileState

logger = logging.getLogger(__name__)

# Regex to extract timestamp from filenames like frame_20240115T083012Z.jpg
_FRAME_TS_RE = re.compile(r"frame_(\d{8}T\d{6}Z)")


def _parse_frame_ts(path: Path) -> Optional[datetime]:
    m = _FRAME_TS_RE.search(path.stem)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class VideoGenerator:
    """
    Wraps the ffmpeg call.  `generate()` is synchronous.
    Use `generate_async()` to run in a background thread.
    """

    def __init__(self, config: ProfileConfig, state: ProfileState) -> None:
        self._cfg = config
        self._state = state
        self._lock = threading.Lock()  # one generation at a time

    # ── public API ─────────────────────────────────────────────────────────

    def generate(self) -> Optional[Path]:
        """
        Collect all JPEGs in photos_dir, generate a video, handle source photos.
        Returns the Path of the generated video, or None on failure.
        """
        if not self._lock.acquire(blocking=False):
            logger.warning("Video generation already in progress — skipping duplicate trigger.")
            return None

        try:
            return self._do_generate()
        finally:
            self._lock.release()

    def generate_async(self) -> None:
        """Fire-and-forget wrapper; exceptions are caught and logged."""
        t = threading.Thread(target=self._safe_generate, name="video-gen", daemon=True)
        t.start()

    # ── internals ─────────────────────────────────────────────────────────

    def _safe_generate(self) -> None:
        try:
            self.generate()
        except Exception as exc:
            logger.exception("Video generation error: %s", exc)

    def _do_generate(self) -> Optional[Path]:
        if not shutil.which("ffmpeg"):
            logger.error("ffmpeg not found. Install: sudo apt install -y ffmpeg")
            return None

        photos_dir = self._cfg.photos_path
        videos_dir = self._cfg.videos_path
        videos_dir.mkdir(parents=True, exist_ok=True)

        # Collect frames sorted by name (which is timestamp-based → chronological)
        frames = sorted(
            [f for f in photos_dir.iterdir() if f.suffix.lower() in (".jpg", ".jpeg")],
            key=lambda p: p.name,
        )

        if len(frames) < 2:
            logger.warning("Not enough frames (%d) to generate video.", len(frames))
            return None

        logger.info("Generating video from %d frames.", len(frames))

        # Determine output filename from first/last frame timestamps
        first_ts = _parse_frame_ts(frames[0])
        last_ts = _parse_frame_ts(frames[-1])
        if first_ts and last_ts:
            stem = (
                f"timelapse_{first_ts.strftime('%Y%m%dT%H%M%SZ')}"
                f"_to_{last_ts.strftime('%Y%m%dT%H%M%SZ')}"
            )
        else:
            stem = f"timelapse_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

        output_path = videos_dir / f"{stem}.mp4"

        # Write a temporary file list (safer than glob on all platforms)
        list_file = photos_dir / "_ffmpeg_input.txt"
        try:
            with list_file.open("w") as f:
                for frame in frames:
                    f.write(f"file '{frame.resolve()}'\n")
                    f.write(f"duration {1.0 / self._cfg.video.fps:.6f}\n")

            ok = self._run_ffmpeg(list_file, output_path)
        finally:
            if list_file.exists():
                list_file.unlink()

        if not ok:
            return None

        logger.info("Video generated: %s", output_path)
        self._state.record_video()

        # Handle source photos
        self._handle_source_photos(frames, stem)

        # If repeat=False, mark profile as completed
        if not self._cfg.video.repeat:
            logger.info("repeat=false — marking profile as completed.")
            self._state.set_status("completed")

        return output_path

    def _run_ffmpeg(self, list_file: Path, output: Path) -> bool:
        cmd = [
            "ffmpeg",
            "-y",                              # overwrite without asking
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-c:v", self._cfg.video.video_codec,
            "-crf", str(self._cfg.video.crf),
            "-r", str(self._cfg.video.fps),    # constant frame rate (QuickTime compat)
            "-pix_fmt", "yuv420p",             # broad player compatibility
            "-movflags", "+faststart",         # moov atom at front (QuickTime / web)
            str(output),
        ]
        logger.debug("ffmpeg command: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # 10 min max for large batches
            )
            if result.returncode != 0:
                logger.error("ffmpeg failed (rc=%d):\n%s", result.returncode, result.stderr[-2000:])
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.error("ffmpeg timed out after 600 s.")
            return False
        except Exception as exc:
            logger.error("ffmpeg error: %s", exc)
            return False

    def _handle_source_photos(self, frames: list[Path], video_stem: str) -> None:
        action = self._cfg.video.after_video

        if action == "keep":
            logger.info("after_video=keep — leaving %d photos in place.", len(frames))
            return

        if action == "move_to_archive":
            archive_subdir = self._cfg.archive_path / video_stem
            archive_subdir.mkdir(parents=True, exist_ok=True)
            moved = 0
            for f in frames:
                try:
                    dest = archive_subdir / f.name
                    shutil.move(str(f), str(dest))
                    moved += 1
                except Exception as exc:
                    logger.error("Could not move %s to archive: %s", f, exc)
            logger.info("Moved %d / %d photos to %s.", moved, len(frames), archive_subdir)
            return

        if action == "delete":
            deleted = 0
            for f in frames:
                try:
                    f.unlink()
                    deleted += 1
                except Exception as exc:
                    logger.error("Could not delete %s: %s", f, exc)
            logger.info("Deleted %d / %d source photos.", deleted, len(frames))
