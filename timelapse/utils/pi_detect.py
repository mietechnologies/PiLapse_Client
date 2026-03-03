"""
Raspberry Pi model detection.

Used to choose the appropriate streaming backend:
  Pi Zero W   → MJPEG only (single-core 1 GHz, 512 MB)
  Pi Zero 2 W → HLS optional (quad-core 1 GHz, 512 MB)
  Pi 3/4/5    → HLS + RTMP fully supported
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def get_model_string() -> str:
    try:
        return Path("/proc/device-tree/model").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


@lru_cache(maxsize=1)
def is_pi_zero_w() -> bool:
    """True only for the original Pi Zero W (single-core ARMv6)."""
    model = get_model_string().lower()
    return "zero w" in model and "zero 2" not in model


@lru_cache(maxsize=1)
def is_pi_zero_2w() -> bool:
    model = get_model_string().lower()
    return "zero 2" in model


@lru_cache(maxsize=1)
def is_raspberry_pi() -> bool:
    return "raspberry pi" in get_model_string().lower()


def get_tier() -> str:
    """
    Return a capability tier:
      'zero_w'   — Pi Zero W: MJPEG only, no RTMP
      'zero_2w'  — Pi Zero 2 W: HLS + optional RTMP (careful with bitrate)
      'capable'  — Pi 3/4/5 or other: full feature set
      'unknown'  — Not a Pi or detection failed
    """
    if is_pi_zero_w():
        return "zero_w"
    if is_pi_zero_2w():
        return "zero_2w"
    if is_raspberry_pi():
        return "capable"
    return "unknown"


PERFORMANCE_NOTES = {
    "zero_w": (
        "Pi Zero W (single-core ARMv6 @ 1 GHz, 512 MB RAM):\n"
        "  - Still capture: ✓ Full 1920×1080 JPEG via picamera2\n"
        "  - MJPEG preview: ✓ 640×360 @ 1–2 fps (CPU ~15–25%)\n"
        "  - HLS preview:   ✗ Too heavy — software H.264 encoding saturates CPU\n"
        "  - RTMP stream:   ✗ Not feasible at any useful quality\n"
        "  - Video gen:     ✓ ffmpeg (runs as low-priority batch after threshold)\n"
        "  Tip: schedule video generation at night; use --nice 19.\n"
    ),
    "zero_2w": (
        "Pi Zero 2 W (quad-core Cortex-A53 @ 1 GHz, 512 MB RAM):\n"
        "  - Still capture: ✓ Full 1920×1080 JPEG via picamera2\n"
        "  - MJPEG preview: ✓ Easy\n"
        "  - HLS preview:   ✓ 1280×720 @ 15 fps with h264_v4l2m2m HW encoder\n"
        "  - RTMP stream:   ⚠  Possible at 720p + low bitrate; monitor CPU temp\n"
        "  - Video gen:     ✓ ffmpeg in batch mode\n"
        "  Tip: avoid running HLS preview + RTMP simultaneously.\n"
    ),
    "capable": (
        "Pi 3 / 4 / 5:\n"
        "  - All features supported at full quality.\n"
        "  - RTMP at 1080p / 30 fps is feasible on Pi 4/5.\n"
    ),
    "unknown": (
        "Non-Pi or unknown hardware:\n"
        "  - Camera access via picamera2 may not work.\n"
        "  - All streaming features depend on ffmpeg and available CPU.\n"
    ),
}
