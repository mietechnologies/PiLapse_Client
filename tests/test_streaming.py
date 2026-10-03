"""
Tests for live streaming: auth, the MJPEG server, and the supervised ffmpeg
output.  No camera or real ffmpeg is needed — MockCamera supplies MJPEG
frames and a small Python script stands in for ffmpeg.
"""

from __future__ import annotations

import base64
import http.client
import struct
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from timelapse.capture.camera import MockCamera
from timelapse.config.models import ProfileConfig
from timelapse.streaming.auth import check_basic_auth, hash_password
from timelapse.streaming.controller import StreamingController
from timelapse.streaming.mjpeg import MJPEGServer, OnDemandMjpeg
from timelapse.streaming.outputs import (
    FfmpegPipeOutput,
    FrameBuffer,
    mediamtx_args,
    rtmp_args,
)


def _basic(user: str, pw: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def _wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ─── auth ─────────────────────────────────────────────────────────────────────


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


# ─── frame buffer / on-demand MJPEG ──────────────────────────────────────────


class TestFrameBuffer:
    def test_wait_returns_only_newer_frames(self):
        buf = FrameBuffer()
        assert buf.wait_for_frame(0, timeout=0.01) == (0, None)
        buf.outputframe(b"one")
        seq, frame = buf.wait_for_frame(0, timeout=0.01)
        assert frame == b"one"
        assert buf.wait_for_frame(seq, timeout=0.01) == (seq, None)  # no repeats

    def test_stop_clears_stale_frame(self):
        buf = FrameBuffer()
        buf.outputframe(b"old")
        buf.stop()
        assert buf.wait_for_frame(0, timeout=0.01)[1] is None


class TestOnDemandMjpeg:
    def test_encoder_runs_only_while_watched(self):
        cam = MagicMock()
        src = OnDemandMjpeg(cam, idle_stop_s=0.05)
        src.acquire()
        src.acquire()
        cam.start_mjpeg.assert_called_once_with(src.buffer)
        src.release()
        src.release()
        assert _wait_until(lambda: cam.stop_mjpeg.called)
        assert not src.running

    def test_reacquire_within_grace_period_keeps_encoder(self):
        cam = MagicMock()
        src = OnDemandMjpeg(cam, idle_stop_s=0.2)
        src.acquire()
        src.release()
        src.acquire()  # e.g. a page reload
        time.sleep(0.3)
        cam.stop_mjpeg.assert_not_called()
        assert cam.start_mjpeg.call_count == 1
        src.close()
        cam.stop_mjpeg.assert_called_once()


@pytest.fixture
def mjpeg_server():
    servers = []

    def make(**kw):
        cam = MockCamera(stream_width=160, stream_height=120)
        cam.start()
        srv = MJPEGServer("127.0.0.1", 0, cam, **kw)
        srv.start()
        servers.append((srv, cam))
        return srv

    yield make
    for srv, cam in servers:
        srv.stop()
        cam.stop()


def _get(srv, path, headers=None, read=True):
    host, port = srv._server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    return resp.status, (resp.read() if read else resp)


class TestMJPEGServer:
    def test_snapshot_starts_encoder_on_demand(self, mjpeg_server):
        srv = mjpeg_server()
        assert not srv.source.running
        status, body = _get(srv, "/snapshot")
        assert status == 200
        assert body[:2] == b"\xff\xd8"  # JPEG SOI marker

    def test_stream_delivers_multipart_frames(self, mjpeg_server):
        srv = mjpeg_server()
        status, resp = _get(srv, "/stream", read=False)
        assert status == 200
        assert "multipart/x-mixed-replace" in resp.getheader("Content-Type")
        chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
        assert b"--mjpegframe" in chunk
        resp.close()

    def test_auth_required_when_enabled(self, mjpeg_server):
        srv = mjpeg_server(auth_enabled=True, auth_username="cam", auth_password="pw")
        assert _get(srv, "/snapshot")[0] == 401
        assert _get(srv, "/snapshot", {"Authorization": _basic("cam", "nope")})[0] == 401
        assert _get(srv, "/snapshot", {"Authorization": _basic("cam", "pw")})[0] == 200

    def test_unknown_path_404(self, mjpeg_server):
        assert _get(mjpeg_server(), "/../../etc/passwd")[0] == 404


# ─── ffmpeg argument builders ────────────────────────────────────────────────


class TestFfmpegArgs:
    def test_mediamtx_publishes_copied_video_over_tcp(self):
        args = mediamtx_args("rtsp://127.0.0.1:8554/garden")
        assert args[-1] == "rtsp://127.0.0.1:8554/garden"
        assert ["-c:v", "copy"] == args[0:2]
        assert "tcp" in args

    def test_rtmp_adds_silent_audio_by_default(self):
        args = rtmp_args("rtmp://x/live/KEY")
        assert "anullsrc=channel_layout=stereo:sample_rate=44100" in args
        assert args[args.index("-c:a") + 1] == "aac"
        assert args[-2:] == ["flv", "rtmp://x/live/KEY"]

    def test_rtmp_video_only(self):
        assert "anullsrc" not in " ".join(rtmp_args("rtmp://x/k", silent_audio=False))


# ─── supervised ffmpeg ───────────────────────────────────────────────────────

# Stand-in for ffmpeg: appends stdin to the file named by the last argument.
# If the file "<out>.die" exists, exits after the first chunk (simulating a
# dropped connection); if "<out>.stall" exists, never reads stdin.
_FAKE_FFMPEG = textwrap.dedent(
    """\
    #!{python}
    import os, sys, time
    out = sys.argv[-1]
    if os.path.exists(out + ".stall"):
        time.sleep(60)
    with open(out, "ab") as f:
        while True:
            chunk = os.read(0, 65536)
            if not chunk:
                break
            f.write(chunk)
            f.flush()
            if os.path.exists(out + ".die"):
                os.remove(out + ".die")
                sys.exit(1)
    """
)


@pytest.fixture
def fake_ffmpeg(tmp_path):
    script = tmp_path / "ffmpeg"
    script.write_text(_FAKE_FFMPEG.format(python=sys.executable))
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def pipe_output(fake_ffmpeg, tmp_path):
    outs = []

    def make(**kw):
        sink = tmp_path / f"sink{len(outs)}"
        out = FfmpegPipeOutput("test", [str(sink)], ffmpeg_bin=fake_ffmpeg, **kw)
        out.RESTART_MIN_S = 0.05
        outs.append(out)
        return out, sink

    yield make
    for out in outs:
        out.stop()


def _ivf(path: Path) -> list[tuple[int, bytes]]:
    """Decode what the fake ffmpeg received: (pts, frame) pairs.

    Each (re)started ffmpeg begins with a 32-byte IVF header.
    """
    raw = path.read_bytes() if path.exists() else b""
    frames, i = [], 0
    while i < len(raw):
        if raw.startswith(b"DKIF", i):
            assert struct.unpack_from("<4s", raw, i + 8)[0] == b"H264"
            i += 32
            continue
        size, pts = struct.unpack_from("<IQ", raw, i)
        frames.append((pts, raw[i + 12 : i + 12 + size]))
        i += 12 + size
    return frames


def _read(path: Path) -> bytes:
    return b"".join(f for _, f in _ivf(path))


class TestFfmpegPipeOutput:
    def test_starts_on_keyframe_and_writes_frames(self, pipe_output):
        out, sink = pipe_output()
        out.start()
        out.outputframe(b"P0", keyframe=False)  # before any keyframe: dropped
        out.outputframe(b"K1", keyframe=True)
        out.outputframe(b"P1", keyframe=False)
        assert _wait_until(lambda: _read(sink) == b"K1P1")

    def test_restarts_after_ffmpeg_exits(self, pipe_output):
        out, sink = pipe_output()
        Path(str(sink) + ".die").touch()
        out.start()
        out.outputframe(b"K1", keyframe=True)
        assert _wait_until(lambda: _read(sink) == b"K1")
        _wait_until(lambda: not out.running)
        # Keep feeding until the supervisor has relaunched and a keyframe lands
        deadline = time.monotonic() + 5
        while b"K2" not in _read(sink) and time.monotonic() < deadline:
            out.outputframe(b"P", keyframe=False)
            out.outputframe(b"K2", keyframe=True)
            time.sleep(0.05)
        assert out.restarts >= 1
        assert _read(sink).startswith(b"K1K2")  # restarted on a keyframe, not "P"

    def test_stalled_ffmpeg_never_blocks_encoder_thread(self, pipe_output):
        out, sink = pipe_output(queue_frames=4)
        Path(str(sink) + ".stall").touch()
        out.start()
        frame = b"x" * 200_000  # larger than a pipe buffer
        t0 = time.monotonic()
        for i in range(200):
            out.outputframe(frame, keyframe=(i % 30 == 0))
        assert time.monotonic() - t0 < 1.0
        assert out._dropped > 0

    def test_camera_timestamps_become_ivf_pts_from_zero(self, pipe_output):
        out, sink = pipe_output()
        out.start()
        base = 9_000_000_000
        out.outputframe(b"K", keyframe=True, timestamp=base)
        out.outputframe(b"P", keyframe=False, timestamp=base + 66_667)
        out.outputframe(b"Q", keyframe=False, timestamp=base + 66_667)  # duplicate ts
        assert _wait_until(lambda: len(_ivf(sink)) == 3)
        assert _ivf(sink) == [(0, b"K"), (66_667, b"P"), (66_668, b"Q")]

    def test_secrets_are_redacted(self):
        out = FfmpegPipeOutput("rtmp", rtmp_args("rtmp://x/live/SECRETKEY"), secrets=["SECRETKEY"])
        assert "SECRETKEY" not in out._redact(" ".join(out._cmd))

    def test_stop_is_idempotent(self, pipe_output):
        out, _ = pipe_output()
        out.start()
        out.stop()
        out.stop()


# ─── controller ──────────────────────────────────────────────────────────────


class TestController:
    def _cfg(self, **preview):
        cfg = ProfileConfig()
        for k, v in preview.items():
            setattr(cfg.streaming.preview, k, v)
        return cfg

    def test_mediamtx_and_rtmp_share_one_encoder(self, monkeypatch):
        monkeypatch.setenv("PILAPSE_TEST_KEY", "abc123")
        cfg = self._cfg()
        cfg.streaming.rtmp.enabled = True
        cfg.streaming.rtmp.stream_key_env = "PILAPSE_TEST_KEY"
        cam = MagicMock()
        ctrl = StreamingController(cfg, cam, profile="garden")
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            ctrl.start()
        cam.start_h264.assert_called_once()
        outputs, bitrate, keyframe_s = cam.start_h264.call_args.args
        assert [o.name for o in outputs] == ["mediamtx", "rtmp"]
        assert outputs[0]._cmd[-1] == "rtsp://127.0.0.1:8554/garden"
        assert outputs[1]._cmd[-1] == "rtmp://a.rtmp.youtube.com/live2/abc123"
        assert (bitrate, keyframe_s) == (1_500_000, 2)

    def test_preview_flag_off_keeps_rtmp(self, monkeypatch):
        monkeypatch.setenv("PILAPSE_TEST_KEY", "k")
        cfg = self._cfg()
        cfg.streaming.rtmp.enabled = True
        cfg.streaming.rtmp.stream_key_env = "PILAPSE_TEST_KEY"
        cam = MagicMock()
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            StreamingController(cfg, cam).start(preview=False)
        outputs = cam.start_h264.call_args.args[0]
        assert [o.name for o in outputs] == ["rtmp"]

    def test_missing_rtmp_key_skips_rtmp_only(self, monkeypatch):
        monkeypatch.delenv("PILAPSE_TEST_KEY", raising=False)
        cfg = self._cfg()
        cfg.streaming.rtmp.enabled = True
        cfg.streaming.rtmp.stream_key_env = "PILAPSE_TEST_KEY"
        cam = MagicMock()
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            StreamingController(cfg, cam).start()
        assert [o.name for o in cam.start_h264.call_args.args[0]] == ["mediamtx"]

    def test_encoder_failure_stops_outputs(self):
        cam = MagicMock()
        cam.start_h264.side_effect = RuntimeError("no encoder")
        ctrl = StreamingController(self._cfg(), cam)
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch.object(FfmpegPipeOutput, "stop") as stop:
            ctrl.start()  # must not raise
        stop.assert_called_once()
        assert ctrl.status()["outputs"] == {}

    def test_mjpeg_refuses_to_start_when_auth_enabled_without_password(self, monkeypatch):
        monkeypatch.delenv("PILAPSE_TEST_PW", raising=False)
        cfg = self._cfg(mode="mjpeg")
        cfg.streaming.preview.auth.enabled = True
        cfg.streaming.preview.auth.password_env = "PILAPSE_TEST_PW"
        with patch("timelapse.streaming.mjpeg.MJPEGServer") as server:
            StreamingController(cfg, MagicMock()).start()
        server.assert_not_called()

    def test_mjpeg_starts_with_password(self, monkeypatch):
        monkeypatch.setenv("PILAPSE_TEST_PW", "pw")
        cfg = self._cfg(mode="mjpeg")
        cfg.streaming.preview.auth.enabled = True
        cfg.streaming.preview.auth.password_env = "PILAPSE_TEST_PW"
        cam = MagicMock()
        with patch("timelapse.streaming.mjpeg.MJPEGServer") as server:
            StreamingController(cfg, cam).start()
        server.assert_called_once()
        assert server.call_args.kwargs["auth_password"] == "pw"
        cam.start_h264.assert_not_called()  # MJPEG-only needs no H.264
