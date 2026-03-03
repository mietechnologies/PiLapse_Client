"""
Disk space guard and retention policy.

Rules:
  - Minimum free space: config.disk.min_free_gb  (default 3 GB)
  - Deletion never touches files outside photos_dir, videos_dir, archive_dir
  - oldest_first:          delete oldest files across all media dirs
  - oldest_videos_first:   delete oldest videos first, then oldest photos

Deleted files are logged individually so the user can audit what was removed.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _free_gb(path: Path) -> float:
    """Return free space in GB for the filesystem containing `path`."""
    try:
        usage = shutil.disk_usage(path)
        return usage.free / (1024 ** 3)
    except OSError:
        return 0.0


def _collect_files(dirs: list[Path], extensions: tuple[str, ...]) -> list[Path]:
    """Return all files with `extensions` from `dirs`, sorted oldest first."""
    files: list[Path] = []
    for d in dirs:
        if d.exists():
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() in extensions:
                    files.append(f)
    files.sort(key=lambda f: f.stat().st_mtime)
    return files


def _safe_to_delete(path: Path, allowed_dirs: list[Path]) -> bool:
    """
    Ensure `path` is inside one of `allowed_dirs`.
    This is a safety net against accidental deletion outside media directories.
    """
    try:
        resolved = path.resolve()
        for d in allowed_dirs:
            if d.exists():
                try:
                    resolved.relative_to(d.resolve())
                    return True
                except ValueError:
                    continue
    except Exception:
        pass
    return False


class DiskGuard:
    def __init__(
        self,
        min_free_gb: float,
        photos_dir: Path,
        videos_dir: Path,
        archive_dir: Path,
        policy: str = "oldest_first",
    ) -> None:
        self._min_gb = min_free_gb
        self._photos_dir = photos_dir
        self._videos_dir = videos_dir
        self._archive_dir = archive_dir
        self._policy = policy
        self._allowed = [photos_dir, videos_dir, archive_dir]

    # ── public API ─────────────────────────────────────────────────────────

    def has_free_space(self) -> bool:
        # Use the photos dir as the reference filesystem
        ref = self._photos_dir if self._photos_dir.exists() else Path.home()
        return _free_gb(ref) >= self._min_gb

    def enforce(self) -> int:
        """
        Delete files until free space >= min_free_gb.
        Returns the number of files deleted.
        """
        if self.has_free_space():
            return 0

        deleted = 0
        candidates = self._deletion_candidates()

        for path in candidates:
            if self.has_free_space():
                break
            if not _safe_to_delete(path, self._allowed):
                logger.warning("SAFE GUARD: refusing to delete %s (outside media dirs)", path)
                continue
            try:
                size_mb = path.stat().st_size / (1024 ** 2)
                os.remove(path)
                logger.warning(
                    "Disk guard deleted %s (%.1f MB) to free space.", path, size_mb
                )
                deleted += 1
            except OSError as exc:
                logger.error("Could not delete %s: %s", path, exc)

        if not self.has_free_space():
            logger.error(
                "Disk guard could not free enough space. "
                "Free space: %.2f GB, minimum: %.2f GB.",
                _free_gb(self._photos_dir if self._photos_dir.exists() else Path.home()),
                self._min_gb,
            )

        return deleted

    # ── deletion order ─────────────────────────────────────────────────────

    def _deletion_candidates(self) -> list[Path]:
        photo_exts = (".jpg", ".jpeg")
        video_exts = (".mp4", ".mkv", ".mov", ".avi")

        if self._policy == "oldest_videos_first":
            videos = _collect_files(
                [self._videos_dir, self._archive_dir], video_exts
            )
            photos = _collect_files([self._photos_dir, self._archive_dir], photo_exts)
            return videos + photos
        else:  # oldest_first
            all_files = _collect_files(self._allowed, photo_exts + video_exts)
            return all_files

    # ── diagnostics ────────────────────────────────────────────────────────

    def status(self) -> dict:
        ref = self._photos_dir if self._photos_dir.exists() else Path.home()
        free = _free_gb(ref)
        return {
            "free_gb": round(free, 2),
            "min_free_gb": self._min_gb,
            "ok": free >= self._min_gb,
            "policy": self._policy,
        }
