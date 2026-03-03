"""
HLS preview backend — Pi Zero 2 W and better.

Architecture:
  picamera2 H264Encoder → FfmpegOutput → ffmpeg HLS muxer → .m3u8 + .ts segments
  Small HTTP server serves the HLS directory as static files.

The HTTP server also serves an index.html with an HLS.js player (CDN-loaded),
so any modern browser can view the stream with zero additional software.

Performance notes:
  Pi Zero W   — DO NOT use. Software H264 will saturate the single core.
                Use MJPEGServer instead.
  Pi Zero 2 W — Use h264_v4l2m2m (hardware). Monitor CPU with `vcgencmd`.
                720p@15fps is feasible; 1080p@30fps is not.
  Pi 3/4/5    — Full 1080p@30fps with hardware encoder.

ffmpeg binary must be installed: sudo apt install -y ffmpeg
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_HLS_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PiLapse HLS Preview</title>
  <style>
    body  {{ margin:0; background:#111; display:flex; flex-direction:column;
             align-items:center; justify-content:center; min-height:100vh; color:#eee; }}
    video {{ max-width:100vw; max-height:90vh; border:2px solid #444; }}
    p     {{ font-family:monospace; font-size:.85em; color:#888; margin:8px 0 0; }}
  </style>
</head>
<body>
  <video id="v" controls muted autoplay playsinline></video>
  <p>HLS · PiLapse · {profile}</p>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
  <script>
    const v = document.getElementById('v');
    if (Hls.isSupported()) {{
      const hls = new Hls({{ lowLatencyMode: true, backBufferLength: 10 }});
      hls.loadSource('/hls/stream.m3u8');
      hls.attachMedia(v);
    }} else if (v.canPlayType('application/vnd.apple.mpegurl')) {{
      v.src = '/hls/stream.m3u8';  // Safari native HLS
    }}
  </script>
</body>
</html>
"""


class _HLSHTTPHandler(BaseHTTPRequestHandler):
    hls_dir: Path = Path("/tmp/pilapse_hls")
    profile: str = "default"
    auth_enabled: bool = False
    auth_username: str = "timelapse"
    auth_password_hash: str = ""

    def log_message(self, fmt, *args):
        logger.debug("HLS %s - %s", self.address_string(), fmt % args)

    def _check_auth(self) -> bool:
        if not self.auth_enabled:
            return True
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            user, _, password = decoded.partition(":")
            if user != self.auth_username:
                return False
            return hashlib.sha256(password.encode()).hexdigest() == self.auth_password_hash
        except Exception:
            return False

    def _send_401(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="PiLapse"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._check_auth():
            self._send_401()
            return

        if self.path in ("/", "/index.html"):
            body = _HLS_INDEX_HTML.format(profile=self.profile).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/hls/"):
            rel = self.path[5:]  # strip "/hls/"
            file_path = self.hls_dir / rel
            if not file_path.exists() or not file_path.is_file():
                self.send_response(404)
                self.end_headers()
                return
            content_type = (
                "application/vnd.apple.mpegurl" if rel.endswith(".m3u8")
                else "video/mp2t"
            )
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_response(404)
        self.end_headers()


class HLSManager:
    """
    Manages the ffmpeg HLS encoder subprocess and the HTTP segment server.

    Requires:
      - picamera2 with H264Encoder support (Pi Zero 2 W or better)
      - ffmpeg installed system-wide
    """

    def __init__(
        self,
        camera,
        width: int = 1280,
        height: int = 720,
        fps: int = 15,
        segment_seconds: int = 2,
        list_size: int = 5,
        http_host: str = "127.0.0.1",
        http_port: int = 8080,
        profile: str = "default",
        auth_enabled: bool = False,
        auth_username: str = "timelapse",
        auth_password: str = "",
    ) -> None:
        self._camera = camera
        self._width = width
        self._height = height
        self._fps = fps
        self._seg_s = segment_seconds
        self._list_size = list_size

        self._hls_dir = Path(tempfile.gettempdir()) / f"pilapse_hls_{profile}"
        self._hls_dir.mkdir(parents=True, exist_ok=True)

        pw_hash = hashlib.sha256(auth_password.encode()).hexdigest() if auth_password else ""

        class BoundHandler(_HLSHTTPHandler):
            pass

        BoundHandler.hls_dir = self._hls_dir
        BoundHandler.profile = profile
        BoundHandler.auth_enabled = auth_enabled
        BoundHandler.auth_username = auth_username
        BoundHandler.auth_password_hash = pw_hash

        self._http_server = HTTPServer((http_host, http_port), BoundHandler)
        self._http_thread: Optional[threading.Thread] = None
        self._encoder = None
        self._started = False

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Start both the ffmpeg HLS encoder and the HTTP server. Returns False on error."""
        if not shutil.which("ffmpeg"):
            logger.error("ffmpeg not found. Install: sudo apt install -y ffmpeg")
            return False

        try:
            self._start_encoder()
        except Exception as exc:
            logger.error("HLS encoder failed to start: %s", exc)
            return False

        self._http_thread = threading.Thread(
            target=self._http_server.serve_forever,
            name="hls-http-server",
            daemon=True,
        )
        self._http_thread.start()
        self._started = True
        logger.info(
            "HLS server started: http://%s:%d/ (segments in %s)",
            self._http_server.server_address[0],
            self._http_server.server_address[1],
            self._hls_dir,
        )
        return True

    def stop(self) -> None:
        if self._encoder:
            try:
                self._camera.stop_h264_output(self._encoder)
            except Exception as exc:
                logger.warning("Error stopping HLS encoder: %s", exc)
            self._encoder = None

        self._http_server.shutdown()
        if self._http_thread:
            self._http_thread.join(timeout=5)
        self._started = False
        logger.info("HLS server stopped.")

    # ── internal ───────────────────────────────────────────────────────────

    def _start_encoder(self) -> None:
        manifest = str(self._hls_dir / "stream.m3u8")
        # FFmpeg args after the piped H264 input.
        # h264_v4l2m2m = hardware H264 decoder/pass-through on Pi
        ffmpeg_args = [
            "-c:v", "copy",          # stream is already H264 from picamera2
            "-f", "hls",
            "-hls_time", str(self._seg_s),
            "-hls_list_size", str(self._list_size),
            "-hls_flags", "delete_segments+append_list",
            "-hls_segment_filename", str(self._hls_dir / "seg%05d.ts"),
            manifest,
        ]
        self._encoder = self._camera.start_h264_output(
            ffmpeg_args, self._width, self._height
        )
