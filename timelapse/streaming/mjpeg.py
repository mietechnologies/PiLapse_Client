"""
Built-in MJPEG web server — live view without any extra software.

Serves:
  GET /          → simple HTML viewer page
  GET /stream    → multipart/x-mixed-replace MJPEG stream
  GET /snapshot  → single JPEG frame (for embedding / health check)

Frames come from the Pi's hardware MJPEG encoder on the live stream, and
that encoder only runs while at least one request needs frames (see
OnDemandMjpeg), so an unwatched camera spends nothing on preview.

MJPEG costs far more bandwidth than H.264; prefer preview.mode=mediamtx
(WebRTC/HLS) when MediaMTX is installed.
"""

from __future__ import annotations

import logging
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import urlsplit

from timelapse.streaming.auth import check_basic_auth, hash_password
from timelapse.streaming.outputs import FrameBuffer

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


class OnDemandMjpeg:
    """
    Reference-counts viewers and runs the camera's MJPEG encoder only while
    someone is watching.  The encoder keeps running for `idle_stop_s` after
    the last viewer leaves, so page reloads and snapshot polling don't
    thrash it on and off.
    """

    def __init__(self, camera, idle_stop_s: float = 10.0) -> None:
        self._camera = camera
        self._idle_stop_s = idle_stop_s
        self.buffer = FrameBuffer()
        self._lock = threading.Lock()
        self._viewers = 0
        self._running = False
        self._stop_timer: Optional[threading.Timer] = None

    def acquire(self) -> None:
        with self._lock:
            self._viewers += 1
            if self._stop_timer is not None:
                self._stop_timer.cancel()
                self._stop_timer = None
            if not self._running:
                self._camera.start_mjpeg(self.buffer)
                self._running = True

    def release(self) -> None:
        with self._lock:
            self._viewers = max(0, self._viewers - 1)
            if self._viewers == 0 and self._running and self._stop_timer is None:
                self._stop_timer = threading.Timer(self._idle_stop_s, self._stop_if_idle)
                self._stop_timer.daemon = True
                self._stop_timer.start()

    def _stop_if_idle(self) -> None:
        with self._lock:
            self._stop_timer = None
            if self._viewers == 0 and self._running:
                self._camera.stop_mjpeg()
                self._running = False

    def close(self) -> None:
        with self._lock:
            if self._stop_timer is not None:
                self._stop_timer.cancel()
                self._stop_timer = None
            if self._running:
                self._camera.stop_mjpeg()
                self._running = False

    @property
    def running(self) -> bool:
        return self._running


class _MJPEGHandler(BaseHTTPRequestHandler):
    # These are injected by MJPEGServer
    source: Optional[OnDemandMjpeg] = None
    auth_enabled: bool = False
    auth_username: str = "timelapse"
    auth_password_hash: str = ""  # sha256 hex
    profile: str = "default"
    frame_interval: float = 0.2  # minimum seconds between frames (1 / mjpeg_fps)
    first_frame_timeout: float = 5.0

    def log_message(self, fmt, *args):  # silence default access log
        logger.debug("MJPEG %s - %s", self.address_string(), fmt % args)

    # ── auth ──────────────────────────────────────────────────────────────

    def _check_auth(self) -> bool:
        if not self.auth_enabled:
            return True
        return check_basic_auth(
            self.headers.get("Authorization", ""), self.auth_username, self.auth_password_hash
        )

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

        path = urlsplit(self.path).path
        if path == "/" or path == "/index.html":
            body = _HTML_TEMPLATE.format(profile=self.profile).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/stream":
            self._stream_mjpeg()

        elif path == "/snapshot":
            self.source.acquire()
            try:
                _, frame = self.source.buffer.wait_for_frame(0, self.first_frame_timeout)
            finally:
                self.source.release()
            if frame is None:
                self.send_response(503)
                self.send_header("Content-Length", "0")
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

        self.source.acquire()
        seq = 0
        try:
            while True:
                sent_at = time.monotonic()
                seq, frame = self.source.buffer.wait_for_frame(seq, self.first_frame_timeout)
                if frame is None:
                    if not self.source.running:
                        return  # server shutting down
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
                # Cap the per-client rate; the encoder runs at the stream fps.
                remaining = self.frame_interval - (time.monotonic() - sent_at)
                if remaining > 0:
                    time.sleep(remaining)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected — normal
        finally:
            self.source.release()


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """HTTP server that handles each connection in a dedicated thread.

    Required for MJPEG streaming: the /stream handler loops forever, so a
    single-threaded server would block all other connections (favicon, the
    root page, second client, etc.) causing browsers to hang indefinitely.
    """
    daemon_threads = True  # threads exit when the main process exits


class MJPEGServer:
    """
    Runs the MJPEG HTTP server in a daemon thread.
    """

    def __init__(
        self,
        host: str,
        port: int,
        camera,
        profile: str = "default",
        auth_enabled: bool = False,
        auth_username: str = "timelapse",
        auth_password: str = "",
        mjpeg_fps: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self.source = OnDemandMjpeg(camera)

        # Build a handler class with injected dependencies (avoids globals)
        pw_hash = hash_password(auth_password) if auth_password else ""

        class BoundHandler(_MJPEGHandler):
            pass

        BoundHandler.source = self.source
        BoundHandler.auth_enabled = auth_enabled
        BoundHandler.auth_username = auth_username
        BoundHandler.auth_password_hash = pw_hash
        BoundHandler.profile = profile
        BoundHandler.frame_interval = max(0.1, 1.0 / mjpeg_fps)

        self._server = _ThreadedHTTPServer((host, port), BoundHandler)
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
        self.source.close()
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("MJPEG server stopped.")
