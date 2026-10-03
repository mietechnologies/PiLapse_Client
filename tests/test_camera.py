"""
Tests for the camera layer.

The YUV→JPEG path runs for real (numpy + simplejpeg).  picamera2 itself is
replaced by a fake module so the encoder wiring and timeout handling can be
checked without a Pi.
"""

from __future__ import annotations

import io
import sys
import threading
import types
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from timelapse.capture.camera import PiCamera, encode_yuv420_jpeg


def _yuv420_buffer(width, height, stride, y, u, v):
    """Build a buffer laid out like picamera2's YUV420 view (with row padding)."""
    buf = np.zeros((height * 3 // 2, stride), dtype=np.uint8)
    buf[:height, :width] = y
    chroma = buf.reshape(height * 3, stride // 2)  # half-stride rows
    chroma[2 * height : 2 * height + height // 2, : width // 2] = u
    chroma[2 * height + height // 2 :, : width // 2] = v
    # Garbage in the padding must never reach the image
    buf[:height, width:] = 7
    return buf


class TestYuvJpeg:
    def test_round_trip_size_and_neutral_grey(self):
        buf = _yuv420_buffer(64, 48, stride=96, y=200, u=128, v=128)
        img = Image.open(io.BytesIO(encode_yuv420_jpeg(buf, 64, 48, 90)))
        assert img.size == (64, 48)
        r, g, b = img.convert("RGB").getpixel((32, 24))
        assert abs(r - 200) < 6 and abs(g - 200) < 6 and abs(b - 200) < 6

    def test_chroma_planes_are_not_swapped(self):
        # High V (Cr) = red; if U and V were swapped this would come out blue.
        buf = _yuv420_buffer(64, 48, stride=64, y=100, u=128, v=220)
        r, g, b = Image.open(io.BytesIO(encode_yuv420_jpeg(buf, 64, 48, 90))).convert(
            "RGB"
        ).getpixel((32, 24))
        # Expected R = Y + 1.402·(V-128) ≈ 229, B = Y ≈ 100; swapped would be R≈100, B≈255
        assert r > 200 and b < 150


# ─── fake picamera2 ───────────────────────────────────────────────────────────


class _FakeEncoder:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.output = None


class _FakeMappedArray:
    def __init__(self, request, stream):
        self.array = request.array

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeJob:
    def __init__(self):
        self._future = Future()

    def get_result(self, timeout=None):
        return self._future.result(timeout=timeout)


@pytest.fixture
def fake_picamera2(monkeypatch):
    picam2 = MagicMock()
    mod = types.ModuleType("picamera2")
    mod.Picamera2 = MagicMock(return_value=picam2)
    mod.MappedArray = _FakeMappedArray
    enc = types.ModuleType("picamera2.encoders")
    enc.H264Encoder = lambda **kw: _FakeEncoder(kind="h264", **kw)
    enc.MJPEGEncoder = lambda **kw: _FakeEncoder(kind="mjpeg", **kw)
    monkeypatch.setitem(sys.modules, "picamera2", mod)
    monkeypatch.setitem(sys.modules, "picamera2.encoders", enc)
    return picam2


def _started_camera(**kw):
    cam = PiCamera(still_width=64, still_height=48, stream_width=32, stream_height=24, **kw)
    cam.start()
    return cam


class TestPiCamera:
    def test_configures_yuv420_streams_with_few_buffers(self, fake_picamera2):
        _started_camera(stream_fps=12)
        kwargs = fake_picamera2.create_video_configuration.call_args.kwargs
        assert kwargs["main"] == {"size": (64, 48), "format": "YUV420"}
        assert kwargs["lores"] == {"size": (32, 24), "format": "YUV420"}
        assert kwargs["controls"] == {"FrameRate": 12}
        assert kwargs["buffer_count"] == 4

    def test_one_h264_encoder_feeds_all_outputs(self, fake_picamera2):
        cam = _started_camera(stream_fps=15)
        outs = [object(), object()]
        cam.start_h264(outs, bitrate_bps=1_500_000, keyframe_interval_s=2)
        encoder, = fake_picamera2.start_encoder.call_args.args
        assert fake_picamera2.start_encoder.call_args.kwargs == {"name": "lores"}
        assert encoder.output == outs
        assert encoder.kwargs == {"kind": "h264", "bitrate": 1_500_000, "repeat": True, "iperiod": 30}
        with pytest.raises(RuntimeError):
            cam.start_h264(outs, 1, 1)

    def test_stopping_one_encoder_never_stops_the_other(self, fake_picamera2):
        cam = _started_camera()
        cam.start_h264([object()], 1_000_000, 2)
        h264 = fake_picamera2.start_encoder.call_args.args[0]
        cam.start_mjpeg(object())
        cam.stop_mjpeg()
        stopped = fake_picamera2.stop_encoder.call_args.args
        assert len(stopped) == 1 and stopped[0] is not h264  # always pass the encoder
        cam.stop_h264()
        assert fake_picamera2.stop_encoder.call_args.args == (h264,)

    def test_capture_still_writes_jpeg_and_releases_request(self, fake_picamera2, tmp_path):
        cam = _started_camera(jpeg_quality=80)
        request = MagicMock()
        request.array = _yuv420_buffer(64, 48, 64, 120, 128, 128)
        job = _FakeJob()
        job._future.set_result(request)
        fake_picamera2.capture_request.return_value = job

        out = tmp_path / "sub" / "frame.jpg"
        assert cam.capture_still(out, timeout_s=1)
        assert Image.open(out).size == (64, 48)
        request.release.assert_called_once()
        assert [p.name for p in out.parent.iterdir()] == ["frame.jpg"]  # no .part left

    def test_timed_out_capture_releases_request_when_it_arrives(self, fake_picamera2, tmp_path):
        cam = _started_camera()
        job = _FakeJob()
        captured = {}

        def capture_request(wait, flush, signal_function):
            captured["signal"] = signal_function
            return job

        fake_picamera2.capture_request.side_effect = capture_request
        assert not cam.capture_still(tmp_path / "f.jpg", timeout_s=0.05)

        late = MagicMock()
        job._future.set_result(late)
        captured["signal"](job)  # picamera2's event loop signals completion
        late.release.assert_called_once()

    def test_request_finishing_during_timeout_handling_is_released(self, fake_picamera2, tmp_path):
        """Completion lands after get_result timed out but before 'abandoned' is set."""
        cam = _started_camera()
        late = MagicMock()

        class RacingJob(_FakeJob):
            def get_result(self, timeout=None):
                if timeout == 0:
                    return late
                captured["signal"](self)  # completes in the gap
                raise FutureTimeoutError()

        captured = {}

        def capture_request(wait, flush, signal_function):
            captured["signal"] = signal_function
            return RacingJob()

        fake_picamera2.capture_request.side_effect = capture_request
        assert not cam.capture_still(tmp_path / "f.jpg", timeout_s=0.05)
        late.release.assert_called_once()
