"""
Streaming controller — wires the camera's hardware encoders to live outputs.

  preview.mode = mediamtx  →  H.264 → ffmpeg (copy) → MediaMTX (RTSP publish)
                               MediaMTX serves viewers over WebRTC / HLS / RTSP
                               and handles their logins.
  preview.mode = mjpeg     →  built-in MJPEG web server, encoder on demand
  rtmp.enabled             →  H.264 → ffmpeg (copy + silent audio) → RTMP ingest

Every H.264 output shares ONE hardware encode: outputs cost a little network
bandwidth and a near-idle ffmpeg copy process each, not encoder time.
Photo capture continues even if streaming fails.
"""

from __future__ import annotations

import ipaddress
import logging
import shutil
from typing import Optional
from urllib.parse import urlsplit

from timelapse.config.models import PreviewConfig, RtmpConfig
from timelapse.streaming.outputs import FfmpegPipeOutput, mediamtx_args, rtmp_args
from timelapse.utils.secrets import get_secret

logger = logging.getLogger(__name__)


class StreamingController:
    """
    Owns the live outputs for one profile.

    Call `start()` after the camera has been started.
    """

    def __init__(self, config, camera, profile: str = "default") -> None:
        self._config = config  # ProfileConfig
        self._camera = camera
        self._profile = profile
        self._mjpeg: Optional[object] = None
        self._h264_outputs: list[FfmpegPipeOutput] = []

    def start(self, preview: bool = True, rtmp: bool = True) -> None:
        cfg = self._config.streaming
        outputs: list[FfmpegPipeOutput] = []

        if preview and cfg.preview.enabled:
            if cfg.preview.mode == "mediamtx":
                outputs.append(self._mediamtx_output(cfg.preview))
            else:
                self._start_mjpeg(cfg.preview)
        elif preview:
            logger.info("Live view disabled in config.")

        if rtmp and cfg.rtmp.enabled:
            out = self._rtmp_output(cfg.rtmp)
            if out:
                outputs.append(out)

        if outputs:
            self._start_h264(outputs)

    def stop(self) -> None:
        if self._h264_outputs:
            try:
                self._camera.stop_h264()  # also stops each output
            except Exception as exc:
                logger.warning("H.264 stop error: %s", exc)
            for out in self._h264_outputs:
                out.stop()  # idempotent; covers a camera that never started them
            self._h264_outputs = []
        if self._mjpeg:
            try:
                self._mjpeg.stop()
            except Exception as exc:
                logger.warning("MJPEG stop error: %s", exc)
            self._mjpeg = None

    def status(self) -> dict:
        return {
            "outputs": {o.name: o.running for o in self._h264_outputs},
            "mjpeg": self._mjpeg is not None,
        }

    # ── H.264 outputs ─────────────────────────────────────────────────────

    def _mediamtx_output(self, cfg: PreviewConfig) -> FfmpegPipeOutput:
        url = cfg.publish_url.format(profile=self._profile)
        path = urlsplit(url).path.lstrip("/")
        logger.info(
            "Live view: publishing to MediaMTX at %s — viewers connect to MediaMTX "
            "(WebRTC http://<pi>:8889/%s/, HLS http://<pi>:8888/%s/).",
            url, path, path,
        )
        return FfmpegPipeOutput("mediamtx", mediamtx_args(url), size=self._stream_size())

    def _rtmp_output(self, cfg: RtmpConfig) -> Optional[FfmpegPipeOutput]:
        key = get_secret(cfg.stream_key_env, cfg.secrets_file)
        if not key:
            logger.error(
                "RTMP stream key not found. Set $%s or configure secrets_file. "
                "RTMP disabled; capture continues.",
                cfg.stream_key_env,
            )
            return None
        url = f"{cfg.rtmp_url.rstrip('/')}/{key}"
        logger.info("RTMP: streaming to %s/***", cfg.rtmp_url.rstrip("/"))
        return FfmpegPipeOutput(
            "rtmp", rtmp_args(url, cfg.silent_audio), size=self._stream_size(), secrets=[key]
        )

    def _stream_size(self) -> tuple[int, int]:
        video = self._config.streaming.video
        return video.width, video.height

    def _start_h264(self, outputs: list[FfmpegPipeOutput]) -> None:
        if not shutil.which("ffmpeg"):
            logger.error("ffmpeg not found — live streaming disabled. Install: sudo apt install -y ffmpeg")
            return
        video = self._config.streaming.video
        try:
            self._camera.start_h264(outputs, video.bitrate_bps, video.keyframe_interval_s)
        except Exception as exc:
            logger.error("H.264 encoder failed to start: %s — capture continues.", exc)
            for out in outputs:
                out.stop()
            return
        self._h264_outputs = outputs

    # ── MJPEG ─────────────────────────────────────────────────────────────

    def _start_mjpeg(self, cfg: PreviewConfig) -> None:
        from timelapse.streaming.mjpeg import MJPEGServer

        password = ""
        if cfg.auth.enabled:
            password = get_secret(cfg.auth.password_env) or ""
            if not password:
                # Fail closed: the user asked for a login, so never serve without one.
                logger.error(
                    "Preview auth is enabled but $%s is not set — refusing to start "
                    "the preview server. Set the variable or disable auth.",
                    cfg.auth.password_env,
                )
                return
        elif not _is_loopback(cfg.host):
            logger.warning(
                "Preview server on %s has NO authentication — anyone who can reach "
                "port %d can watch this camera. Enable streaming.preview.auth.",
                cfg.host,
                cfg.port,
            )

        server = MJPEGServer(
            host=cfg.host,
            port=cfg.port,
            camera=self._camera,
            profile=self._profile,
            auth_enabled=cfg.auth.enabled,
            auth_username=cfg.auth.username,
            auth_password=password,
            mjpeg_fps=cfg.mjpeg_fps,
        )
        server.start()
        self._mjpeg = server
        logger.info("Live view (MJPEG) at http://%s:%d/", cfg.host, cfg.port)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
