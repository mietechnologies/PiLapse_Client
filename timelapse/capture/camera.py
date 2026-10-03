"""
Camera abstraction over picamera2.

Architecture
────────────
This process is the camera's only owner (libcamera allows one per camera).
It runs the camera continuously in video mode with two YUV420 streams:

  main  — still resolution (default 1920×1080) — timelapse stills
  lores — live resolution  (default 1280×720)  — hardware encoders

Pixel work stays off the CPU wherever the hardware can do it:
  - Live video: ONE hardware H.264 encode of `lores`, fanned out to every
    output (MediaMTX publish, RTMP push).  See start_h264().
  - MJPEG preview: hardware MJPEG encode of `lores`, only while a viewer is
    connected.  See start_mjpeg().
  - Stills: the only software encode — one JPEG straight from the YUV
    planes (no RGB conversion, no PIL) every capture interval.

YUV420 is 1.5 bytes/pixel versus 3 for RGB888, which halves the camera's
reserved (CMA) memory — the scarce resource on a 512 MB Pi Zero 2 W.

If picamera2 is not available (development machine / unit tests) we fall
back to a MockCamera with the same interface.
"""

from __future__ import annotations

import io
import logging
import os
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Optional, Sequence

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


# ─── helpers ─────────────────────────────────────────────────────────────────


def encode_yuv420_jpeg(array, width: int, height: int, quality: int) -> bytes:
    """
    JPEG-encode a picamera2 YUV420 buffer without converting to RGB.

    `array` is the stream's 2-D uint8 view: `height * 3 // 2` rows of
    `stride` bytes — the Y plane, then U and V at half resolution packed two
    half-stride rows per full row.  This mirrors picamera2's JpegEncoder.
    """
    import simplejpeg

    y = array[:height, :width]
    half_stride = array.reshape((array.shape[0] * 2, array.strides[0] // 2))
    u = half_stride[2 * height : 2 * height + height // 2, : width // 2]
    v = half_stride[2 * height + height // 2 :, : width // 2]
    return simplejpeg.encode_jpeg_yuv_planes(y, u, v, quality)


def write_atomic(path: Path, data: bytes) -> None:
    """Write via a temp file + rename so readers never see a partial JPEG."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.part")
    with tmp.open("wb") as f:
        f.write(data)
    os.replace(tmp, path)


# ─── real camera ─────────────────────────────────────────────────────────────


class PiCamera:
    """
    Wraps picamera2 for still capture and hardware-encoded live video.

    Call start() before use, stop() when done.
    """

    def __init__(
        self,
        still_width: int = 1920,
        still_height: int = 1080,
        stream_width: int = 1280,
        stream_height: int = 720,
        stream_fps: int = 15,
        jpeg_quality: int = 85,
        buffer_count: int = 4,
    ) -> None:
        self._sw = still_width
        self._sh = still_height
        self._pw = stream_width
        self._ph = stream_height
        self._fps = stream_fps
        self._quality = jpeg_quality
        # Fewer buffers = less CMA memory.  4 leaves headroom for two encoders
        # plus a still in flight; the picamera2 default for video is 6.
        self._buffer_count = buffer_count

        self._picam2 = None
        self._started = False
        self._still_lock = threading.Lock()
        self._encoder_lock = threading.Lock()
        self._h264 = None
        self._mjpeg = None

    def start(self) -> None:
        from picamera2 import Picamera2

        self._picam2 = Picamera2()
        config = self._picam2.create_video_configuration(
            main={"size": (self._sw, self._sh), "format": "YUV420"},
            lores={"size": (self._pw, self._ph), "format": "YUV420"},
            controls={"FrameRate": self._fps},
            buffer_count=self._buffer_count,
            encode="lores",
            display=None,
        )
        self._picam2.configure(config)
        self._picam2.start()
        self._started = True
        logger.info(
            "Camera started: still=%dx%d  live=%dx%d@%dfps",
            self._sw, self._sh, self._pw, self._ph, self._fps,
        )

    def stop(self) -> None:
        self.stop_h264()
        self.stop_mjpeg()
        if self._picam2 and self._started:
            self._picam2.stop()
            self._picam2.close()
            self._started = False
        logger.info("Camera stopped.")

    # ── still capture ──────────────────────────────────────────────────────

    def capture_still(self, path: Path, timeout_s: int = 10) -> bool:
        """
        Save a full-resolution JPEG to `path`.
        Returns True on success, False on error or timeout.
        """
        if not self._started or self._picam2 is None:
            logger.error("capture_still called but camera not started.")
            return False

        from picamera2 import MappedArray

        with self._still_lock:
            request = self._capture_request(timeout_s)
            if request is None:
                return False
            try:
                with MappedArray(request, "main") as m:
                    jpeg = encode_yuv420_jpeg(m.array, self._sw, self._sh, self._quality)
            except Exception as exc:
                logger.error("Still capture failed: %s", exc)
                return False
            finally:
                request.release()

        try:
            write_atomic(path, jpeg)
        except OSError as exc:
            logger.error("Could not write still %s: %s", path, exc)
            return False
        logger.debug("Still saved: %s (%d KB)", path, len(jpeg) // 1024)
        return True

    def _capture_request(self, timeout_s: float):
        """
        Fetch a fresh request, or None after `timeout_s`.

        On timeout the job is still pending inside picamera2; whichever side
        finishes last releases its request so no camera buffer leaks.
        """
        lock = threading.Lock()
        state = {"abandoned": False, "done": False}

        def on_done(job) -> None:
            with lock:
                state["done"] = True
                if state["abandoned"]:
                    _release_job_result(job)

        job = self._picam2.capture_request(wait=False, flush=True, signal_function=on_done)
        try:
            return job.get_result(timeout=timeout_s)
        except FutureTimeoutError:
            with lock:
                state["abandoned"] = True
                if state["done"]:
                    _release_job_result(job)
            logger.error("Still capture timed out after %s s.", timeout_s)
        except Exception as exc:
            logger.error("Still capture failed: %s", exc)
        return None

    # ── hardware encoders ──────────────────────────────────────────────────

    def start_h264(self, outputs: Sequence[object], bitrate_bps: int, keyframe_interval_s: int) -> None:
        """
        Start the single hardware H.264 encoder on the live stream, writing to
        every output in `outputs` (picamera2 Output objects).
        """
        from picamera2.encoders import H264Encoder

        self._require_started()
        with self._encoder_lock:
            if self._h264 is not None:
                raise RuntimeError("H.264 encoder already running")
            encoder = H264Encoder(
                bitrate=bitrate_bps,
                repeat=True,  # SPS/PPS before every keyframe, so receivers can join late
                iperiod=self._fps * keyframe_interval_s,
            )
            encoder.output = list(outputs)
            self._picam2.start_encoder(encoder, name="lores")
            self._h264 = encoder
        logger.info(
            "H.264 encoder started: %dx%d@%dfps %d kbps, keyframe every %d s, %d output(s)",
            self._pw, self._ph, self._fps, bitrate_bps // 1000, keyframe_interval_s, len(outputs),
        )

    def stop_h264(self) -> None:
        with self._encoder_lock:
            encoder, self._h264 = self._h264, None
        self._stop_encoder(encoder, "H.264")

    def start_mjpeg(self, output: object) -> None:
        """Start the hardware MJPEG encoder on the live stream into `output`."""
        from picamera2.encoders import MJPEGEncoder

        self._require_started()
        with self._encoder_lock:
            if self._mjpeg is not None:
                return
            encoder = MJPEGEncoder()
            self._picam2.start_encoder(encoder, output, name="lores")
            self._mjpeg = encoder
        logger.info("MJPEG encoder started.")

    def stop_mjpeg(self) -> None:
        with self._encoder_lock:
            encoder, self._mjpeg = self._mjpeg, None
        self._stop_encoder(encoder, "MJPEG")

    def _stop_encoder(self, encoder, label: str) -> None:
        if encoder is None or self._picam2 is None:
            return
        try:
            # Always pass the encoder: with no argument picamera2 stops ALL of them.
            self._picam2.stop_encoder(encoder)
            logger.info("%s encoder stopped.", label)
        except Exception as exc:
            logger.warning("Error stopping %s encoder: %s", label, exc)

    def _require_started(self) -> None:
        if not self._started or self._picam2 is None:
            raise RuntimeError("Camera not started")


def _release_job_result(job) -> None:
    try:
        job.get_result(timeout=0).release()
    except Exception:
        pass  # the job failed, so there is no request to release


# ─── mock camera (non-Pi development) ────────────────────────────────────────


class MockCamera:
    """
    Drop-in replacement for PiCamera that generates placeholder images.
    Used when picamera2 is not available.  H.264 is not simulated.
    """

    def __init__(
        self,
        still_width: int = 1920,
        still_height: int = 1080,
        stream_width: int = 640,
        stream_height: int = 360,
        **_kw,
    ) -> None:
        self._w = still_width
        self._h = still_height
        self._pw = stream_width
        self._ph = stream_height
        self._started = False
        self._mjpeg_stop: Optional[threading.Event] = None
        logger.warning("Using MockCamera — no real camera attached.")

    def start(self) -> None:
        self._started = True
        logger.info("MockCamera started.")

    def stop(self) -> None:
        self.stop_mjpeg()
        self._started = False
        logger.info("MockCamera stopped.")

    def capture_still(self, path: Path, **_kw) -> bool:
        try:
            buf = io.BytesIO()
            self._make_test_image().save(buf, format="jpeg", quality=85)
            write_atomic(path, buf.getvalue())
            logger.debug("MockCamera: saved test image to %s", path)
            return True
        except Exception as exc:
            logger.error("MockCamera capture failed: %s", exc)
            return False

    def start_h264(self, outputs, bitrate_bps: int, keyframe_interval_s: int) -> None:
        logger.warning("MockCamera: H.264 streaming is not simulated (%d output(s)).", len(outputs))

    def stop_h264(self) -> None:
        pass

    def start_mjpeg(self, output) -> None:
        if self._mjpeg_stop is not None:
            return
        stop = self._mjpeg_stop = threading.Event()
        output.start()

        def run() -> None:
            while not stop.is_set():
                buf = io.BytesIO()
                self._make_test_image(self._pw, self._ph).save(buf, format="jpeg", quality=70)
                output.outputframe(buf.getvalue())
                stop.wait(0.5)
            output.stop()

        threading.Thread(target=run, name="mock-mjpeg", daemon=True).start()

    def stop_mjpeg(self) -> None:
        if self._mjpeg_stop is not None:
            self._mjpeg_stop.set()
            self._mjpeg_stop = None

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
    stream_width: int = 1280,
    stream_height: int = 720,
    stream_fps: int = 15,
    jpeg_quality: int = 85,
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
            stream_width=stream_width,
            stream_height=stream_height,
            stream_fps=stream_fps,
            jpeg_quality=jpeg_quality,
        )
    logger.warning("Camera not available: %s\nUsing MockCamera.", msg)
    return MockCamera(
        still_width=still_width,
        still_height=still_height,
        stream_width=stream_width,
        stream_height=stream_height,
    )
