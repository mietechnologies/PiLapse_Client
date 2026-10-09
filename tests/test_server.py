"""
Tests for the server connection: client, uploader queue, secrets, config.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from timelapse.config.models import GlobalConfig, ServerConfig
from timelapse.server.client import ServerClient, ServerError, intent_for_profile
from timelapse.server.uploader import AUTH_RETRY_S, MIN_BACKOFF_S, PhotoUploader
from timelapse.utils.secrets import get_secret, write_secret

TAKEN = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


# ─── client ──────────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self.ok = status < 400
        self._body = body

    def json(self) -> dict:
        return self._body


class FakeSession:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return self.responses.pop(0)


class TestIntent:
    @pytest.mark.parametrize(
        "profile,intent",
        [
            ("default", "default"),
            ("printer-5s", "printer-5s"),
            ("my garden", "my-garden"),
            ("_hidden", "hidden"),
            ("x" * 80, "x" * 64),
            ("...", "default"),
        ],
    )
    def test_maps_profile_names(self, profile, intent):
        assert intent_for_profile(profile) == intent


class TestServerClient:
    def test_info_accepts_pilapse_server(self):
        s = FakeSession(FakeResponse(200, {"data": {"name": "pilapse-server", "setupRequired": True}}))
        info = ServerClient("http://srv:3000/", session=s).info()
        assert info["setupRequired"] is True
        assert s.calls[0]["url"] == "http://srv:3000/api/v1/info"

    def test_info_rejects_other_servers(self):
        s = FakeSession(FakeResponse(404, {}))
        with pytest.raises(ServerError, match="doesn't look like a PiLapse server"):
            ServerClient("http://srv", session=s).info()

    def test_login_then_add_pi_uses_bearer_then_device_token(self):
        s = FakeSession(
            FakeResponse(200, {"token": "jwt"}),
            FakeResponse(201, {"deviceToken": "dev", "data": {"pi": {"_id": "p1", "name": "Pi"}}}),
            FakeResponse(200, {"data": {"pi": {"_id": "p1"}}}),
        )
        c = ServerClient("http://srv", session=s)
        c.login("a@b.c", "pw")
        pi, token = c.add_pi("Pi", model="Raspberry Pi Zero 2 W")
        c.get_pi("p1")

        assert token == "dev" and pi["_id"] == "p1"
        assert s.calls[1]["headers"] == {"Authorization": "Bearer jwt"}
        assert s.calls[1]["json"] == {"name": "Pi", "model": "Raspberry Pi Zero 2 W"}
        assert s.calls[2]["headers"] == {"Authorization": "Device dev"}

    def test_upload_sends_intent_and_date(self, tmp_path):
        photo = tmp_path / "frame.jpg"
        photo.write_bytes(b"jpeg")
        s = FakeSession(FakeResponse(201, {"data": {"photo": {"_id": "ph"}}}))
        ServerClient("http://srv", device_token="dev", session=s).upload_photo(
            "p1", photo, intent="printer-5s", taken_at=TAKEN
        )
        call = s.calls[0]
        assert call["url"] == "http://srv/api/v1/pi/p1/photos"
        assert call["data"] == {"intent": "printer-5s", "date": TAKEN.isoformat()}
        assert call["files"]["image"][0] == "frame.jpg"

    def test_error_classification(self):
        s = FakeSession(FakeResponse(401, {"message": "Invalid or revoked device token."}))
        with pytest.raises(ServerError) as exc:
            ServerClient("http://srv", device_token="x", session=s).get_pi("p1")
        assert exc.value.is_auth_error and not exc.value.is_transient
        assert "revoked" in str(exc.value)
        assert ServerError("down").is_transient
        assert ServerError("busy", 503).is_transient
        assert not ServerError("bad", 400).is_transient


# ─── uploader ────────────────────────────────────────────────────────────────


class FakeClient:
    url = "http://srv"

    def __init__(self, *errors) -> None:
        self.errors = list(errors)
        self.uploads: list[tuple[Path, str]] = []

    def upload_photo(self, pi_id, path, *, intent, taken_at):
        if self.errors:
            err = self.errors.pop(0)
            if err:
                raise err
        self.uploads.append((Path(path), intent))


@pytest.fixture
def photo(tmp_path):
    p = tmp_path / "photos" / "frame_1.jpg"
    p.parent.mkdir()
    p.write_bytes(b"jpeg")
    return p


def make_uploader(tmp_path, client):
    return PhotoUploader(client, "p1", queue_dir=tmp_path / "queue")


class TestUploader:
    def test_uploads_in_order_and_clears_queue(self, tmp_path, photo):
        second = photo.with_name("frame_2.jpg")
        second.write_bytes(b"jpeg")
        client = FakeClient()
        up = make_uploader(tmp_path, client)
        up.enqueue(photo, intent="a", taken_at=TAKEN)
        up.enqueue(second, intent="b", taken_at=TAKEN)

        assert up.drain() is None
        assert client.uploads == [(photo, "a"), (second, "b")]
        assert up.pending() == []

    def test_keeps_queue_and_backs_off_when_unreachable(self, tmp_path, photo):
        client = FakeClient(ServerError("down"), ServerError("down"), None)
        up = make_uploader(tmp_path, client)
        up.enqueue(photo, intent="a", taken_at=TAKEN)

        assert up.drain() == MIN_BACKOFF_S
        assert up.drain() == MIN_BACKOFF_S * 2
        assert len(up.pending()) == 1
        assert up.drain() is None
        assert up.pending() == []

    def test_queue_survives_restart(self, tmp_path, photo):
        make_uploader(tmp_path, FakeClient()).enqueue(photo, intent="a", taken_at=TAKEN)
        client = FakeClient()
        assert make_uploader(tmp_path, client).drain() is None
        assert client.uploads == [(photo, "a")]

    def test_pauses_on_rejected_token(self, tmp_path, photo):
        up = make_uploader(tmp_path, FakeClient(ServerError("revoked", 401)))
        up.enqueue(photo, intent="a", taken_at=TAKEN)
        assert up.drain() == AUTH_RETRY_S
        assert len(up.pending()) == 1

    def test_drops_photos_the_server_refuses(self, tmp_path, photo):
        client = FakeClient(ServerError("bad intent", 400))
        up = make_uploader(tmp_path, client)
        up.enqueue(photo, intent="a", taken_at=TAKEN)
        assert up.drain() is None
        assert up.pending() == []

    def test_finds_photos_moved_to_archive(self, tmp_path, photo):
        archive = tmp_path / "archive"
        client = FakeClient()
        up = make_uploader(tmp_path, client)
        up.enqueue(photo, intent="a", taken_at=TAKEN, archive_dir=archive)

        moved = archive / "timelapse_20261008" / photo.name
        moved.parent.mkdir(parents=True)
        photo.rename(moved)

        assert up.drain() is None
        assert client.uploads == [(moved, "a")]

    def test_skips_deleted_photos(self, tmp_path, photo):
        client = FakeClient()
        up = make_uploader(tmp_path, client)
        up.enqueue(photo, intent="a", taken_at=TAKEN)
        photo.unlink()
        assert up.drain() is None
        assert client.uploads == []
        assert up.pending() == []


# ─── secrets & config ────────────────────────────────────────────────────────


class TestWriteSecret:
    def test_creates_0600_file_and_updates_in_place(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TIMELAPSE_SERVER_TOKEN", raising=False)
        path = tmp_path / "secrets" / "server.env"
        path.parent.mkdir()
        path.write_text("OTHER=keep\n")
        path.chmod(0o600)

        write_secret(path, "TIMELAPSE_SERVER_TOKEN", "one")
        write_secret(path, "TIMELAPSE_SERVER_TOKEN", "two")

        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert path.read_text() == "OTHER=keep\nTIMELAPSE_SERVER_TOKEN=two\n"
        assert get_secret("TIMELAPSE_SERVER_TOKEN", str(path)) == "two"

        write_secret(path, "TIMELAPSE_SERVER_TOKEN", None)
        assert path.read_text() == "OTHER=keep\n"


class TestServerConfig:
    def test_normalizes_url(self):
        assert ServerConfig(url=" http://srv:3000/ ", pi_id="p").url == "http://srv:3000"

    def test_rejects_non_http_url(self):
        with pytest.raises(ValidationError):
            ServerConfig(url="srv:3000", pi_id="p")

    def test_global_config_round_trip(self, tmp_path, monkeypatch):
        from timelapse.config import loader

        monkeypatch.setattr(loader, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(loader, "PROFILES_DIR", tmp_path / "profiles")
        monkeypatch.setattr(loader, "GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")

        loader.save_global_config(GlobalConfig())
        assert "server" not in (tmp_path / "config.yaml").read_text()

        loader.save_global_config(
            GlobalConfig(server=ServerConfig(url="http://srv", pi_id="p1", pi_name="Garden"))
        )
        assert loader.load_global_config().server.pi_name == "Garden"
