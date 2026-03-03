"""
MJPEG streaming server — the Pi Zero W preview backend.

Serves:
  GET /          → simple HTML viewer page
  GET /stream    → multipart/x-mixed-replace MJPEG stream
  GET /snapshot  → single JPEG frame (for embedding / health check)

Uses only Python stdlib (http.server + threading) — zero extra deps.

Performance on Pi Zero W:
  - Camera captures lores JPEG at ~1–2 fps in a background thread
  - HTTP thread reads the shared frame buffer and writes to each client
  - At 640×360 / 1 fps, CPU impact is ~10–20% on Zero W
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_BOUNDARY = b"--mjpegframe"
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PiLapse Live Preview</title>
  <style>
    body {{ margin: 0; background: #111; display: flex; flex-direction: column;
            align-items: center; justify-content: center; min-height: 100vh; color: #eee; }}
    img  {{ max-width: 100vw; max-height: 90vh; border: 2px solid #444; }}
    p    {{ font-family: monospace; font-size: 0.85em; color: #888; margin: 8px 0 0; }}
  </style>
</head>
<body>
  <img src="/stream" alt="live preview">
  <p>MJPEG · PiLapse · {profile}</p>
</body>
</html>
"""


class _MJPEGHandler(BaseHTTPRequestHandler):
    # These are injected by MJPEGServer.start()
    get_frame: Callable[[], Optional[bytes]] = lambda: None
    auth_enabled: bool = False
    auth_username: str = "timelapse"
    auth_password_hash: str = ""  # sha256 hex
    profile: str = "default"

    def log_message(self, fmt, *args):  # silence default access log
        logger.debug("MJPEG %s - %s", self.address_string(), fmt % args)

    # ── auth ──────────────────────────────────────────────────────────────

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
            pw_hash = hashlib.sha256(password.encode()).hexdigest()
            return pw_hash == self.auth_password_hash
        except Exception:
            return False

    def _send_401(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="PiLapse"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── routes ────────────────────────────────────────────────────────────

    def do_GET(self):
        if not self._check_auth():
            self._send_401()
            return

        if self.path == "/" or self.path == "/index.html":
            body = _HTML_TEMPLATE.format(profile=self.profile).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/stream":
            self._stream_mjpeg()

        elif self.path == "/snapshot":
            frame = self.get_frame()
            if frame is None:
                self.send_response(503)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(frame)

        else:
            self.send_response(404)
            self.end_headers()

    def _stream_mjpeg(self) -> None:
        self.send_response(200)
        self.send_header(
            "Content-Type", f"multipart/x-mixed-replace; boundary={_BOUNDARY.decode()[2:]}"
        )
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            while True:
                frame = self.get_frame()
                if frame is None:
                    time.sleep(0.2)
                    continue
                header = (
                    _BOUNDARY + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                )
                self.wfile.write(header)
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                time.sleep(0.5)  # ~2 fps — adjust via config if needed
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected — normal


class MJPEGServer:
    """
    Runs the MJPEG HTTP server in a daemon thread.
    """

    def __init__(
        self,
        host: str,
        port: int,
        get_frame_fn: Callable[[], Optional[bytes]],
        profile: str = "default",
        auth_enabled: bool = False,
        auth_username: str = "timelapse",
        auth_password: str = "",
    ) -> None:
        self._host = host
        self._port = port

        # Build a handler class with injected dependencies (avoids globals)
        pw_hash = hashlib.sha256(auth_password.encode()).hexdigest() if auth_password else ""

        class BoundHandler(_MJPEGHandler):
            pass

        BoundHandler.get_frame = staticmethod(get_frame_fn)
        BoundHandler.auth_enabled = auth_enabled
        BoundHandler.auth_username = auth_username
        BoundHandler.auth_password_hash = pw_hash
        BoundHandler.profile = profile

        self._server = HTTPServer((host, port), BoundHandler)
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="mjpeg-server",
            daemon=True,
        )
        self._thread.start()
        logger.info("MJPEG server started at http://%s:%d/", self._host, self._port)

    def stop(self) -> None:
        self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("MJPEG server stopped.")
