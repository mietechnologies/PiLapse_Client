"""
Raspberry Pi model detection.

Used to print hardware-specific guidance at startup.
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
        "  - Untested. Live video uses the hardware encoders, but Python and\n"
        "    picamera2 themselves are heavy for one ARMv6 core.\n"
        "  - Prefer long capture intervals and preview.mode=mjpeg.\n"
    ),
    "zero_2w": (
        "Pi Zero 2 W (quad-core Cortex-A53 @ 1 GHz, 512 MB RAM):\n"
        "  - Live video: one hardware H.264 encode shared by all outputs.\n"
        "    720p @ 15 fps is the recommended starting point.\n"
        "  - Wi-Fi is usually the bottleneck: disable power saving\n"
        "    (sudo iw wlan0 set power_save off) if the stream stalls.\n"
        "  - Use a heatsink; check throttling with: vcgencmd get_throttled\n"
    ),
    "capable": (
        "Pi 3 / 4:\n"
        "  - All features supported; 1080p @ 30 fps live video is feasible.\n"
        "Pi 5 has no hardware H.264 encoder: picamera2 encodes in software (uses CPU).\n"
    ),
    "unknown": (
        "Non-Pi or unknown hardware:\n"
        "  - Camera access via picamera2 may not work.\n"
        "  - All streaming features depend on ffmpeg and available CPU.\n"
    ),
}
