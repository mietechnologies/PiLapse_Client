"""Tests for the video generator's control flow (ffmpeg is mocked)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from timelapse.config.models import ProfileConfig
from timelapse.scheduler.state import ProfileState
from timelapse.video import generator as gen_mod
from timelapse.video.generator import VideoGenerator


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("timelapse.scheduler.state._STATE_DIR", tmp_path / "state")
    cfg = ProfileConfig(
        photos_dir=str(tmp_path / "photos"),
        videos_dir=str(tmp_path / "videos"),
        archive_dir=str(tmp_path / "archive"),
    )
    cfg.photos_path.mkdir()
    for i in range(3):
        (cfg.photos_path / f"frame_20260101T00000{i}Z.jpg").write_bytes(b"x")
    state = ProfileState("vid")
    return cfg, state


def _no_thread(target, **_kw):
    class T:
        def start(self):
            target()

    return T()


class TestVideoGenerator:
    def test_failure_suppresses_automatic_retries(self, setup):
        cfg, state = setup
        gen = VideoGenerator(cfg, state)
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch.object(gen, "_run_ffmpeg", return_value=False) as run, \
             patch.object(gen_mod.threading, "Thread", side_effect=_no_thread):
            gen.generate_async()
            gen.generate_async()
            gen.generate_async()
        assert run.call_count == 1

    def test_retries_after_cooldown(self, setup, monkeypatch):
        cfg, state = setup
        gen = VideoGenerator(cfg, state)
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch.object(gen, "_run_ffmpeg", return_value=False) as run, \
             patch.object(gen_mod.threading, "Thread", side_effect=_no_thread):
            gen.generate_async()
            gen._last_failure -= gen_mod.RETRY_COOLDOWN_S + 1
            gen.generate_async()
        assert run.call_count == 2

    def test_photos_taken_during_encode_carry_over(self, setup):
        cfg, state = setup
        for _ in range(3):
            state.record_capture()
        gen = VideoGenerator(cfg, state)

        def fake_ffmpeg(_list, _out):
            # A capture lands while ffmpeg runs
            (cfg.photos_path / "frame_20260101T000009Z.jpg").write_bytes(b"x")
            return True

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch.object(gen, "_run_ffmpeg", side_effect=fake_ffmpeg):
            assert gen.generate() is not None

        assert state.get_current_batch() == 1
        assert [f.name for f in cfg.photos_path.iterdir()] == ["frame_20260101T000009Z.jpg"]

    @pytest.mark.parametrize(
        "codec, expected",
        [("libx264", ["-crf", "23"]), ("h264_v4l2m2m", ["-b:v", "8M"])],
    )
    def test_rate_control_matches_codec(self, setup, codec, expected):
        cfg, state = setup
        cfg.video.video_codec = codec
        assert VideoGenerator(cfg, state)._rate_control_args() == expected
