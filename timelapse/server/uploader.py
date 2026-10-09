"""
Background photo uploader.

Every captured photo is first written to an on-disk queue (one small JSON
file per photo), then a worker thread uploads the queue oldest-first. If the
server is unreachable the worker backs off and retries, so photos taken while
offline are sent once the server is back, including after a restart.
Capture never waits on the network.

If a queued photo has since been moved into the archive by video generation,
it is uploaded from there. If it was deleted (after_video: delete, or the disk
guard), the entry is dropped with a warning.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from timelapse.server.client import ServerClient, ServerError

logger = logging.getLogger(__name__)

QUEUE_DIR = Path.home() / ".local" / "share" / "timelapse" / "upload-queue"

MIN_BACKOFF_S = 5.0
MAX_BACKOFF_S = 300.0
AUTH_RETRY_S = 600.0


class PhotoUploader:
    def __init__(
        self,
        client: ServerClient,
        pi_id: str,
        queue_dir: Path = QUEUE_DIR,
    ) -> None:
        self._client = client
        self._pi_id = pi_id
        self._queue_dir = Path(queue_dir)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._backoff = MIN_BACKOFF_S
        self.last_upload_at: Optional[datetime] = None
        self.last_error: Optional[str] = None

    # ── queue ──────────────────────────────────────────────────────────────

    def enqueue(
        self,
        path: Path,
        *,
        intent: str,
        taken_at: datetime,
        archive_dir: Optional[Path] = None,
    ) -> None:
        """Queue a photo for upload. Safe to call from any thread."""
        self._queue_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "path": str(path),
            "intent": intent,
            "taken_at": taken_at.isoformat(),
            "archive_dir": str(archive_dir) if archive_dir else None,
        }
        name = f"{time.time_ns():020d}-{secrets.token_hex(3)}.json"
        tmp = self._queue_dir / f".{name}.tmp"
        tmp.write_text(json.dumps(entry))
        os.replace(tmp, self._queue_dir / name)
        self._wake.set()

    def pending(self) -> list[Path]:
        if not self._queue_dir.exists():
            return []
        return sorted(self._queue_dir.glob("[0-9]*.json"))

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="uploader", daemon=True)
        self._thread.start()
        logger.info(
            "Uploading photos to %s (%d queued).", self._client.url, len(self.pending())
        )

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            # Clear before draining so an enqueue during the drain isn't missed.
            self._wake.clear()
            delay = self.drain()
            if delay is None:
                self._wake.wait()  # queue empty: sleep until enqueue/stop
            else:
                self._stop.wait(delay)

    # ── work ───────────────────────────────────────────────────────────────

    def drain(self) -> Optional[float]:
        """
        Upload queued photos until the queue is empty or a request fails.
        Returns None when the queue is empty, otherwise seconds to wait before
        retrying.
        """
        for entry_path in self.pending():
            if self._stop.is_set():
                return 0
            try:
                self._upload_entry(entry_path)
            except ServerError as exc:
                self.last_error = str(exc)
                if exc.is_auth_error:
                    logger.error(
                        "Server rejected this Pi's device token (%s). Uploads are "
                        "paused; run `timelapse server connect` to reconnect.",
                        exc,
                    )
                    return AUTH_RETRY_S
                if exc.is_transient:
                    delay = self._backoff
                    self._backoff = min(self._backoff * 2, MAX_BACKOFF_S)
                    logger.warning(
                        "Upload failed (%s); %d queued, retrying in %ds.",
                        exc,
                        len(self.pending()),
                        delay,
                    )
                    return delay
                # Anything else (bad request, file too large) won't succeed later.
                logger.error("Server refused %s (%s); dropping it.", entry_path.name, exc)
                entry_path.unlink(missing_ok=True)
                continue
            self._backoff = MIN_BACKOFF_S
        return None

    def _upload_entry(self, entry_path: Path) -> None:
        try:
            entry = json.loads(entry_path.read_text())
        except (OSError, ValueError) as exc:
            logger.error("Unreadable upload queue entry %s (%s); dropping it.", entry_path, exc)
            entry_path.unlink(missing_ok=True)
            return

        photo = self._locate(entry)
        if photo is None:
            logger.warning(
                "Queued photo %s no longer exists (deleted before upload); skipping.",
                entry["path"],
            )
            entry_path.unlink(missing_ok=True)
            return

        self._client.upload_photo(
            self._pi_id,
            photo,
            intent=entry["intent"],
            taken_at=datetime.fromisoformat(entry["taken_at"]),
        )
        entry_path.unlink(missing_ok=True)
        self.last_upload_at = datetime.now().astimezone()
        self.last_error = None
        logger.debug("Uploaded %s", photo.name)

    @staticmethod
    def _locate(entry: dict) -> Optional[Path]:
        path = Path(entry["path"])
        if path.exists():
            return path
        archive = entry.get("archive_dir")
        if archive:
            # Video generation moves photos to <archive_dir>/<video-stem>/<name>.
            matches = sorted(Path(archive).glob(f"*/{path.name}"))
            if matches:
                return matches[-1]
        return None


def uploader_from_config(server_cfg) -> Optional[PhotoUploader]:
    """Build an uploader from GlobalConfig.server, or None if it can't run."""
    from timelapse.utils.secrets import get_secret

    if server_cfg is None:
        return None
    token = get_secret(server_cfg.token_env, server_cfg.secrets_file)
    if not token:
        logger.error(
            "Connected to %s but no device token was found ($%s or %s). "
            "Photos will not be uploaded; run `timelapse server connect`.",
            server_cfg.url,
            server_cfg.token_env,
            server_cfg.secrets_file,
        )
        return None
    client = ServerClient(server_cfg.url, device_token=token, timeout_s=server_cfg.timeout_s)
    return PhotoUploader(client, server_cfg.pi_id)
