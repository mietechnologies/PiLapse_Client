"""
Streaming controller — picks MJPEG or HLS based on config + hardware tier.

Logic:
  config.streaming.preview.mode == 'auto':
      Pi Zero W   → MJPEG
      Pi Zero 2 W → HLS (with fallback to MJPEG if encoder fails)
      Other       → HLS
  'mjpeg' / 'hls' → forced
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Optional

from timelapse.config.models import PreviewConfig
from timelapse.utils.pi_detect import get_tier, is_pi_zero_w

logger = logging.getLogger(__name__)


class StreamingController:
    """
    Manages one preview server (MJPEG or HLS) and one optional RTMP streamer.

    Call `start()` after the camera has been started.
    Photo capture continues even if streaming fails.
    """

    def __init__(self, config, camera, profile: str = "default") -> None:
        self._config = config  # ProfileConfig
        self._camera = camera
        self._profile = profile
        self._mjpeg: Optional[object] = None
        self._hls: Optional[object] = None
        self._rtmp: Optional[object] = None
        self._active_mode: Optional[str] = None

    def start(self) -> None:
        preview_cfg = self._config.streaming.preview
        if not preview_cfg.enabled:
            logger.info("Preview streaming disabled in config.")
        else:
            self._start_preview(preview_cfg)

        rtmp_cfg = self._config.streaming.rtmp
        if rtmp_cfg.enabled:
            self._start_rtmp(rtmp_cfg)

    def stop(self) -> None:
        if self._mjpeg:
            try:
                self._mjpeg.stop()
            except Exception as exc:
                logger.warning("MJPEG stop error: %s", exc)
        if self._hls:
            try:
                self._hls.stop()
            except Exception as exc:
                logger.warning("HLS stop error: %s", exc)
        if self._rtmp:
            try:
                self._rtmp.stop()
            except Exception as exc:
                logger.warning("RTMP stop error: %s", exc)

    # ── preview selection ──────────────────────────────────────────────────

    def _start_preview(self, cfg: PreviewConfig) -> None:
        mode = cfg.mode
        if mode == "auto":
            mode = "mjpeg" if is_pi_zero_w() else "hls"
            logger.info("Auto preview mode → %s (tier: %s)", mode, get_tier())

        password = self._resolve_password(cfg)
        if cfg.auth.enabled and not password:
            # Fail closed: the user asked for a login, so never serve without one.
            logger.error(
                "Preview auth is enabled but $%s is not set — refusing to start "
                "the preview server. Set the variable or disable auth.",
                cfg.auth.password_env,
            )
            return
        if not cfg.auth.enabled and not _is_loopback(cfg.host):
            logger.warning(
                "Preview server on %s has NO authentication — anyone who can reach "
                "port %d can watch this camera. Enable streaming.preview.auth.",
                cfg.host,
                cfg.port,
            )

        if mode == "mjpeg":
            self._start_mjpeg(cfg, password)
        elif mode == "hls":
            ok = self._start_hls(cfg, password)
            if not ok:
                logger.warning("HLS failed; falling back to MJPEG.")
                self._start_mjpeg(cfg, password)
        else:
            logger.error("Unknown preview mode: %s", mode)

    def _start_mjpeg(self, cfg: PreviewConfig, password: str) -> None:
        from timelapse.streaming.mjpeg import MJPEGServer

        self._mjpeg = MJPEGServer(
            host=cfg.host,
            port=cfg.port,
            get_frame_fn=self._camera.get_preview_frame,
            profile=self._profile,
            auth_enabled=cfg.auth.enabled,
            auth_username=cfg.auth.username,
            auth_password=password,
            mjpeg_fps=cfg.mjpeg_fps,
        )
        self._mjpeg.start()
        self._active_mode = "mjpeg"
        logger.info(
            "Preview (MJPEG) at http://%s:%d/ — "
            "expose securely via Tailscale / Cloudflare Tunnel / nginx+TLS.",
            cfg.host,
            cfg.port,
        )

    def _start_hls(self, cfg: PreviewConfig, password: str) -> bool:
        from timelapse.streaming.hls import HLSManager

        mgr = HLSManager(
            camera=self._camera,
            width=cfg.hls_width,
            height=cfg.hls_height,
            fps=cfg.hls_fps,
            segment_seconds=cfg.hls_segment_seconds,
            list_size=cfg.hls_list_size,
            http_host=cfg.host,
            http_port=cfg.port,
            profile=self._profile,
            auth_enabled=cfg.auth.enabled,
            auth_username=cfg.auth.username,
            auth_password=password,
        )
        ok = mgr.start()
        if ok:
            self._hls = mgr
            self._active_mode = "hls"
            logger.info(
                "Preview (HLS) at http://%s:%d/ — "
                "expose securely via Tailscale / Cloudflare Tunnel / nginx+TLS.",
                cfg.host,
                cfg.port,
            )
        return ok

    # ── RTMP ──────────────────────────────────────────────────────────────

    def _start_rtmp(self, cfg) -> None:
        from timelapse.streaming.rtmp import RTMPStreamer

        tier = get_tier()
        if tier == "zero_w":
            logger.warning(
                "RTMP is NOT supported on Pi Zero W — skipping. "
                "Use Pi Zero 2 W or better for RTMP streaming."
            )
            return

        streamer = RTMPStreamer(
            camera=self._camera,
            rtmp_url=cfg.rtmp_url,
            stream_key_env=cfg.stream_key_env,
            secrets_file=cfg.secrets_file,
            video_bitrate=cfg.video_bitrate,
            width=cfg.width,
            height=cfg.height,
            fps=cfg.fps,
            audio_enabled=cfg.audio_enabled,
        )
        ok = streamer.start()
        if ok:
            self._rtmp = streamer
        else:
            logger.warning(
                "RTMP failed to start — photo capture will continue normally."
            )

    # ── helpers ────────────────────────────────────────────────────────────

    def _resolve_password(self, cfg: PreviewConfig) -> str:
        if not cfg.auth.enabled:
            return ""
        from timelapse.utils.secrets import get_secret

        return get_secret(cfg.auth.password_env) or ""

    def status(self) -> dict:
        return {
            "preview_mode": self._active_mode,
            "rtmp_active": self._rtmp.is_running() if self._rtmp else False,
        }


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
