"""
Tests for the preview HTTP servers: auth, path safety, and port handling.

Servers bind to an ephemeral loopback port; no camera or ffmpeg is needed.
"""

from __future__ import annotations

import base64
import http.client
import threading
from unittest.mock import MagicMock, patch

import pytest

from timelapse.streaming.auth import check_basic_auth, hash_password
from timelapse.streaming.hls import HLSManager


def _basic(user: str, pw: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


class TestBasicAuth:
    def test_valid_credentials(self):
        assert check_basic_auth(_basic("cam", "s3cret"), "cam", hash_password("s3cret"))

    @pytest.mark.parametrize(
        "header",
        [
            "",
            "Bearer abc",
            "Basic !!!not-base64!!!",
            "Basic " + base64.b64encode(b"no-colon").decode(),
            _basic("cam", "wrong"),
            _basic("other", "s3cret"),
        ],
    )
    def test_rejects_bad_headers(self, header):
        assert not check_basic_auth(header, "cam", hash_password("s3cret"))

    def test_empty_password_hash_never_matches(self):
        """An unset password must not authenticate an empty password."""
        assert not check_basic_auth(_basic("cam", ""), "cam", "")


@pytest.fixture
def hls_server(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    mgr = HLSManager(MagicMock(), http_host="127.0.0.1", http_port=0, profile="t")
    (mgr._hls_dir / "stream.m3u8").write_text("#EXTM3U\n")
    (tmp_path / "secret.ts").write_text("outside the hls dir")
    t = threading.Thread(target=mgr._http_server.serve_forever, daemon=True)
    t.start()
    yield mgr
    mgr._http_server.shutdown()
    mgr._http_server.server_close()


def _get(mgr, path):
    host, port = mgr._http_server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    return resp.status, resp.read()


class TestHLSPathSafety:
    def test_serves_playlist(self, hls_server):
        assert _get(hls_server, "/hls/stream.m3u8") == (200, b"#EXTM3U\n")

    @pytest.mark.parametrize(
        "path",
        [
            "/hls/" + "../" * 30 + "etc/passwd",
            "/hls/../secret.ts",
            "/hls/%2e%2e%2fsecret.ts",
            "/hls/..%2fsecret.ts",
            "/hls/",
            "/hls/stream.m3u8/..",
        ],
    )
    def test_rejects_traversal(self, hls_server, path):
        status, body = _get(hls_server, path)
        assert status == 404
        assert b"outside" not in body and b"root:" not in body

    def test_rejects_non_media_files(self, hls_server):
        (hls_server._hls_dir / "notes.txt").write_text("x")
        assert _get(hls_server, "/hls/notes.txt")[0] == 404


class TestHLSStartFailure:
    def test_failed_start_releases_port_for_mjpeg_fallback(self, tmp_path, monkeypatch):
        from timelapse.streaming.mjpeg import MJPEGServer

        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        cam = MagicMock()
        cam.start_h264_output.side_effect = RuntimeError("no encoder")
        mgr = HLSManager(cam, http_host="127.0.0.1", http_port=0, profile="t")
        port = mgr._http_server.server_address[1]

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            assert mgr.start() is False

        fallback = MJPEGServer("127.0.0.1", port, lambda: None)  # must not raise
        fallback._server.server_close()


class TestControllerAuth:
    def _controller(self, auth_enabled, host="127.0.0.1"):
        from timelapse.config.models import ProfileConfig
        from timelapse.streaming.webserver import StreamingController

        cfg = ProfileConfig()
        cfg.streaming.preview.mode = "mjpeg"
        cfg.streaming.preview.host = host
        cfg.streaming.preview.auth.enabled = auth_enabled
        cfg.streaming.preview.auth.password_env = "PILAPSE_TEST_PW"
        return StreamingController(cfg, MagicMock())

    def test_refuses_to_start_when_auth_enabled_without_password(self, monkeypatch):
        monkeypatch.delenv("PILAPSE_TEST_PW", raising=False)
        ctrl = self._controller(auth_enabled=True)
        with patch.object(ctrl, "_start_mjpeg") as start:
            ctrl._start_preview(ctrl._config.streaming.preview)
        start.assert_not_called()

    def test_starts_with_password(self, monkeypatch):
        monkeypatch.setenv("PILAPSE_TEST_PW", "pw")
        ctrl = self._controller(auth_enabled=True)
        with patch.object(ctrl, "_start_mjpeg") as start:
            ctrl._start_preview(ctrl._config.streaming.preview)
        start.assert_called_once()
