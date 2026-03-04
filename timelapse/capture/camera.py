"""
Camera abstraction over picamera2.

Architecture
────────────
picamera2 is used as the sole camera driver.  We configure it with two
streams:
  main  — full resolution (default 1920×1080) — used for timelapse stills
  lores — low resolution (default 640×360)    — used for MJPEG/HLS preview

The camera runs in video configuration mode so both streams are always
available.  Still captures grab a frame from the already-running `main`
stream (no mode switch needed → no streaming interruption).

If picamera2 is not available (development machine / unit tests) we fall
back to a MockCamera that generates placeholder images.

Thread safety
─────────────
`capture_still()` briefly sets `_capturing` to prevent the preview thread
from competing for the camera at the exact moment of still capture.
In practice, both streams can coexist in video mode; the lock just makes
intent explicit and avoids races on very slow hardware.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ─── availability check ──────────────────────────────────────────────────────


def check_camera_available() -> tuple[bool, str]:
    """
    Returns (available: bool, message: str).
    Call this early and surface the message to the user if available=False.
    """
    try:
        from picamera2 import Picamera2  # noqa: F401

        # Quick probe — does not start the camera
        cams = Picamera2.global_camera_info()
        if not cams:
            return False, (
                "picamera2 is installed but no camera was detected.\n"
                "Checks:\n"
                "  1. Is the ribbon cable seated firmly?\n"
                "  2. Run: rpicam-hello --list-cameras\n"
                "  3. Ensure the camera is enabled: raspi-config → Interface Options → Camera\n"
                "  4. Reboot after enabling."
            )
        model = cams[0].get("Model", "unknown")
        return True, f"Camera detected: {model}"

    except ImportError:
        return False, (
            "picamera2 is not installed.\n"
            "Install it (Raspberry Pi OS Bookworm):\n"
            "  sudo apt install -y python3-picamera2\n"
            "If you are running on a non-Pi machine for development, "
            "the mock camera will be used automatically."
        )
    except Exception as exc:
        return False, f"Camera check failed: {exc}"


# ─── real camera ─────────────────────────────────────────────────────────────


class PiCamera:
    """
    Wraps picamera2 for both still capture and streaming preview.

    Call start() before use, stop() when done.
    """

    def __init__(
        self,
        still_width: int = 1920,
        still_height: int = 1080,
        preview_width: int = 640,
        preview_height: int = 360,
        jpeg_quality: int = 85,
        preview_fps: float = 2.0,
    ) -> None:
        self._sw = still_width
        self._sh = still_height
        self._pw = preview_width
        self._ph = preview_height
        self._quality = jpeg_quality
        self._preview_interval = max(0.1, 1.0 / preview_fps)

        self._picam2 = None
        self._started = False
        self._capturing = threading.Lock()

        # Shared latest preview JPEG (bytes)
        self._preview_lock = threading.Lock()
        self._latest_preview: Optional[bytes] = None

        # Thread that refreshes _latest_preview continuously
        self._preview_thread: Optional[threading.Thread] = None
        self._stop_preview = threading.Event()

    def start(self) -> None:
        from picamera2 import Picamera2

        self._picam2 = Picamera2()

        config = self._picam2.create_video_configuration(
            main={"size": (self._sw, self._sh), "format": "RGB888"},
            lores={"size": (self._pw, self._ph), "format": "YUV420"},
            display=None,  # no local display
            encode="lores",  # default encode on lores stream
        )
        self._picam2.configure(config)
        self._picam2.start()
        self._started = True
        logger.info(
            "Camera started: still=%dx%d  preview=%dx%d",
            self._sw, self._sh, self._pw, self._ph,
        )

        self._stop_preview.clear()
        self._preview_thread = threading.Thread(
            target=self._preview_loop, daemon=True, name="camera-preview"
        )
        self._preview_thread.start()

    def stop(self) -> None:
        self._stop_preview.set()
        if self._preview_thread:
            self._preview_thread.join(timeout=5)
        if self._picam2 and self._started:
            self._picam2.stop()
            self._picam2.close()
            self._started = False
        logger.info("Camera stopped.")

    # ── still capture ──────────────────────────────────────────────────────

    def capture_still(self, path: Path, timeout_s: int = 10) -> bool:
        """
        Save a full-resolution JPEG to `path`.
        Returns True on success, False on error.
        """
        if not self._started or self._picam2 is None:
            logger.error("capture_still called but camera not started.")
            return False

        path.parent.mkdir(parents=True, exist_ok=True)

        # Use a short lock so the preview loop doesn't race on a slow Pi Zero
        with self._capturing:
            try:
                request = self._picam2.capture_request(flush=True)
                request.save("main", str(path))
                request.release()
                logger.debug("Still saved: %s", path)
                return True
            except Exception as exc:
                logger.error("Still capture failed: %s", exc)
                return False

    # ── preview ────────────────────────────────────────────────────────────

    def _preview_loop(self) -> None:
        """Continuously update _latest_preview from the lores stream."""
        while not self._stop_preview.is_set():
            if not self._started or self._picam2 is None:
                time.sleep(0.5)
                continue
            try:
                # Don't grab preview during a still capture — avoids rare glitch
                if self._capturing.locked():
                    time.sleep(0.1)
                    continue

                request = self._picam2.capture_request()
                img = request.make_image("main")
                request.release()
                # Downscale the full-res RGB frame to preview dimensions
                img.thumbnail((self._pw, self._ph))
                buf = io.BytesIO()
                img.save(buf, format="jpeg", quality=70)
                frame = buf.getvalue()

                with self._preview_lock:
                    self._latest_preview = frame

            except Exception as exc:
                logger.debug("Preview frame error (transient): %s", exc)
                time.sleep(1.0)
                continue

            # Sleep between preview frames — respects configured mjpeg_fps
            self._stop_preview.wait(self._preview_interval)

    def get_preview_frame(self) -> Optional[bytes]:
        """Return the latest JPEG preview frame, or None if not yet available."""
        with self._preview_lock:
            return self._latest_preview

    # ── H264 encoder for HLS / RTMP ───────────────────────────────────────

    def start_h264_output(self, ffmpeg_args: list[str], width: int, height: int) -> object:
        """
        Start picamera2's H264 encoder piped into ffmpeg with the given args.
        Returns the encoder object (pass to stop_h264_output).

        Only feasible on Pi Zero 2 W or better.
        """
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import FfmpegOutput

        if not self._started or self._picam2 is None:
            raise RuntimeError("Camera not started")

        bitrate = 1_000_000  # 1 Mbps default; callers can override via ffmpeg_args
        encoder = H264Encoder(bitrate=bitrate)

        # Build ffmpeg command: read raw H264 on stdin, output per ffmpeg_args
        ffmpeg_cmd = " ".join(ffmpeg_args)
        output = FfmpegOutput(ffmpeg_cmd, audio=False)

        self._picam2.start_encoder(encoder, output, name="lores")
        logger.info("H264 encoder started → %s", ffmpeg_cmd)
        return encoder

    def stop_h264_output(self, encoder: object) -> None:
        if self._picam2 and self._started:
            try:
                self._picam2.stop_encoder()
            except Exception as exc:
                logger.warning("Error stopping H264 encoder: %s", exc)


# ─── mock camera (non-Pi development) ────────────────────────────────────────


class MockCamera:
    """
    Drop-in replacement for PiCamera that generates placeholder images.
    Used when picamera2 is not available.
    """

    def __init__(self, still_width: int = 1920, still_height: int = 1080, **_kw) -> None:
        self._w = still_width
        self._h = still_height
        self._started = False
        self._stop_event = threading.Event()
        logger.warning("Using MockCamera — no real camera attached.")

    def start(self) -> None:
        self._started = True
        logger.info("MockCamera started.")

    def stop(self) -> None:
        self._stop_event.set()
        self._started = False
        logger.info("MockCamera stopped.")

    def capture_still(self, path: Path, **_kw) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            img = self._make_test_image()
            img.save(str(path), format="jpeg", quality=85)
            logger.debug("MockCamera: saved test image to %s", path)
            return True
        except Exception as exc:
            logger.error("MockCamera capture failed: %s", exc)
            return False

    def get_preview_frame(self) -> Optional[bytes]:
        try:
            buf = io.BytesIO()
            self._make_test_image(width=640, height=360).save(buf, format="jpeg", quality=70)
            return buf.getvalue()
        except Exception:
            return None

    def _make_test_image(self, width: Optional[int] = None, height: Optional[int] = None):
        """Return a PIL image with timestamp overlay."""
        from PIL import Image, ImageDraw

        w = width or self._w
        h = height or self._h
        img = Image.new("RGB", (w, h), color=(30, 30, 60))
        draw = ImageDraw.Draw(img)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        draw.text((10, 10), f"MockCamera\n{ts}\n{w}×{h}", fill=(220, 220, 80))
        return img


# ─── factory ─────────────────────────────────────────────────────────────────


def create_camera(
    still_width: int = 1920,
    still_height: int = 1080,
    preview_width: int = 640,
    preview_height: int = 360,
    jpeg_quality: int = 85,
    preview_fps: float = 2.0,
) -> "PiCamera | MockCamera":
    """
    Return a PiCamera if picamera2 and a real camera are available,
    otherwise return a MockCamera.
    """
    available, msg = check_camera_available()
    if available:
        return PiCamera(
            still_width=still_width,
            still_height=still_height,
            preview_width=preview_width,
            preview_height=preview_height,
            jpeg_quality=jpeg_quality,
            preview_fps=preview_fps,
        )
    else:
        logger.warning("Camera not available: %s\nUsing MockCamera.", msg)
        return MockCamera(still_width=still_width, still_height=still_height)
