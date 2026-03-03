"""
RTMP restreaming — Pi Zero 2 W and better only.

Architecture:
  picamera2 H264Encoder (hardware) → FfmpegOutput → ffmpeg FLV muxer → RTMP server

Stream key handling:
  Primary: environment variable (TIMELAPSE_RTMP_KEY_<PROFILE>)
  Fallback: secrets file (~/.config/timelapse/secrets/<profile>.env, must be 0600)

Pi Zero W warning:
  Even at 360p / 500 kbps the Zero W cannot encode H264 in real time.
  If you must stream from a Zero W, consider a relay host or a
  Cloudflare Stream + Workers setup that transcodes for you.

Pi Zero 2 W feasibility:
  720p / 15fps / 1000 kbps is the recommended maximum.
  Monitor with: watch -n1 vcgencmd measure_temp
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from timelapse.utils.secrets import get_secret

logger = logging.getLogger(__name__)


class RTMPStreamer:
    """
    Manages the RTMP restream using picamera2's H264Encoder + FfmpegOutput.
    """

    def __init__(
        self,
        camera,
        rtmp_url: str,
        stream_key_env: str,
        secrets_file: Optional[str] = None,
        video_bitrate: str = "1000k",
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        audio_enabled: bool = False,
    ) -> None:
        self._camera = camera
        self._rtmp_url = rtmp_url.rstrip("/")
        self._key_env = stream_key_env
        self._secrets_file = secrets_file
        self._bitrate = video_bitrate
        self._width = width
        self._height = height
        self._fps = fps
        self._audio = audio_enabled
        self._encoder = None
        self._started = False

    def start(self) -> bool:
        """Resolve key, start encoder.  Returns False on failure."""
        key = get_secret(self._key_env, self._secrets_file)
        if not key:
            logger.error(
                "RTMP stream key not found. Set $%s or configure secrets_file.", self._key_env
            )
            return False

        stream_url = f"{self._rtmp_url}/{key}"
        # Log with key redacted
        safe_url = f"{self._rtmp_url}/***"
        logger.info("Starting RTMP stream to %s (%s fps, %s)", safe_url, self._fps, self._bitrate)

        try:
            ffmpeg_args = self._build_ffmpeg_args(stream_url)
            self._encoder = self._camera.start_h264_output(
                ffmpeg_args, self._width, self._height
            )
            self._started = True
            logger.info("RTMP stream active.")
            return True
        except Exception as exc:
            logger.error("Failed to start RTMP stream: %s", exc)
            return False

    def stop(self) -> None:
        if self._encoder:
            try:
                self._camera.stop_h264_output(self._encoder)
            except Exception as exc:
                logger.warning("Error stopping RTMP encoder: %s", exc)
            self._encoder = None
        self._started = False
        logger.info("RTMP stream stopped.")

    def is_running(self) -> bool:
        return self._started

    def _build_ffmpeg_args(self, stream_url: str) -> list[str]:
        """
        Build the ffmpeg output arguments for RTMP.

        Input is piped H264 from picamera2 (already encoded).
        We copy the video stream and mux to FLV for RTMP.
        """
        args = [
            "-c:v", "copy",       # pass through H264 from hardware encoder
            "-f", "flv",
            stream_url,
        ]
        return args

    @property
    def started(self) -> bool:
        return self._started
