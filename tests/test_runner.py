"""
Tests for the single-process device runner (timelapse/runner.py).
"""

from __future__ import annotations

import time

import pytest

from timelapse.config.models import CaptureConfig, ProfileConfig
from timelapse.runner import DeviceRunner, camera_settings, stream_profile


def profile(tmp_path=None, name="p", *, preview=False, rtmp=False, width=1920, interval="1h"):
    cfg = ProfileConfig.model_validate(
        {
            "capture": {"width": width, "height": 1080},
            "schedule": {"rules": [{"every": interval}]},
            "streaming": {"preview": {"enabled": preview}, "rtmp": {"enabled": rtmp}},
        }
    )
    if tmp_path is not None:
        cfg = ProfileConfig.model_validate(
            {
                **cfg.model_dump(),
                "photos_dir": str(tmp_path / name / "photos"),
                "videos_dir": str(tmp_path / name / "videos"),
                "archive_dir": str(tmp_path / name / "archive"),
            }
        )
    return cfg


class TestStreamProfile:
    def test_prefers_default(self):
        profiles = {"a": profile(preview=True), "default": profile(preview=True)}
        assert stream_profile(profiles) == "default"

    def test_first_streaming_profile_by_name(self):
        profiles = {"default": profile(), "b": profile(rtmp=True), "a": profile(preview=True)}
        assert stream_profile(profiles) == "a"

    def test_none_when_nothing_streams(self):
        assert stream_profile({"a": profile()}) is None


def test_camera_settings_satisfy_every_profile():
    profiles = {"a": profile(width=1280), "b": profile(width=4056)}
    profiles["a"].capture.jpeg_quality = 95
    s = camera_settings(profiles)
    assert s["still_width"] == 4056
    assert s["jpeg_quality"] == 95


class FakeUploader:
    url = "http://srv"

    def __init__(self) -> None:
        self.queued: list[str] = []

    def start(self): ...
    def stop(self): ...

    def enqueue(self, path, *, intent, taken_at, archive_dir=None):
        self.queued.append(intent)


@pytest.fixture
def two_intents(tmp_path, monkeypatch):
    monkeypatch.setattr("timelapse.scheduler.state._STATE_DIR", tmp_path / "state")
    profiles = {
        "printer-5s": profile(tmp_path, "printer-5s", interval="5s"),
        "printer-30s": profile(tmp_path, "printer-30s", interval="30s"),
    }
    profiles["printer-30s"].upload_to_server = False
    return profiles


def photos(tmp_path, name):
    return sorted((tmp_path / name / "photos").glob("*.jpg"))


def test_one_photo_is_filed_for_every_due_intent(tmp_path, two_intents):
    created = []
    import timelapse.capture.camera as camera_mod

    real_create = camera_mod.create_camera
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        camera_mod, "create_camera", lambda **kw: created.append(real_create(**kw)) or created[-1]
    )

    uploader = FakeUploader()
    device = DeviceRunner(two_intents, None, uploader=uploader, preview=False)
    device.start()
    device.scheduler.stop()  # drive on_due by hand
    try:
        intents = device.scheduler._intents
        device.on_due(intents, None)
    finally:
        device.stop()
        monkey.undo()

    assert len(created) == 1
    fast, slow = photos(tmp_path, "printer-5s"), photos(tmp_path, "printer-30s")
    assert len(fast) == len(slow) == 1
    assert fast[0].name == slow[0].name
    assert fast[0].stat().st_ino == slow[0].stat().st_ino  # hard link, no extra space
    assert uploader.queued == ["printer-5s"]
    for r in device.runners:
        assert r.state.get_photo_count() == 1


def test_scheduler_drives_both_intents(tmp_path, monkeypatch):
    monkeypatch.setattr("timelapse.scheduler.state._STATE_DIR", tmp_path / "state")
    profiles = {
        "fast": profile(tmp_path, "fast", interval="2s"),
        "slow": profile(tmp_path, "slow", interval="4s"),
    }
    device = DeviceRunner(profiles, None, preview=False, clock_sync_timeout_s=0)
    device.start()
    try:
        deadline = time.time() + 12
        while time.time() < deadline and not (
            photos(tmp_path, "fast") and photos(tmp_path, "slow")
        ):
            time.sleep(0.2)
    finally:
        device.stop()

    assert photos(tmp_path, "fast")
    assert photos(tmp_path, "slow")
