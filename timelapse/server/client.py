"""
HTTP client for a PiLapse server (https://github.com/mietechnologies/PiLapse_Server).

Users authenticate with a JWT from login (used only during
`timelapse server connect`); the device authenticates with its own
long-lived device token for everything else.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests

API_PREFIX = "/api/v1"

# Must match the server's intent pattern.
_INTENT_INVALID = re.compile(r"[^A-Za-z0-9_-]")


def intent_for_profile(profile: str) -> str:
    """Map a profile name to a server intent (letters, digits, '-', '_')."""
    intent = _INTENT_INVALID.sub("-", profile).lstrip("-_")[:64]
    return intent or "default"


class ServerError(Exception):
    """A request failed. `status` is the HTTP status, or None if unreachable."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def is_auth_error(self) -> bool:
        return self.status in (401, 403)

    @property
    def is_transient(self) -> bool:
        """Worth retrying later: unreachable, rate limited, or a server error."""
        return self.status is None or self.status == 429 or self.status >= 500


class ServerClient:
    def __init__(
        self,
        url: str,
        *,
        device_token: Optional[str] = None,
        user_token: Optional[str] = None,
        timeout_s: float = 30,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._device_token = device_token
        self._user_token = user_token
        self._timeout = timeout_s
        self._session = session or requests.Session()

    # ── plumbing ───────────────────────────────────────────────────────────

    def _headers(self, auth: Optional[str]) -> dict[str, str]:
        if auth == "device" and self._device_token:
            return {"Authorization": f"Device {self._device_token}"}
        if auth == "user" and self._user_token:
            return {"Authorization": f"Bearer {self._user_token}"}
        return {}

    def _request(self, method: str, path: str, auth: Optional[str] = None, **kw) -> dict:
        try:
            resp = self._session.request(
                method,
                f"{self.url}{API_PREFIX}{path}",
                headers=self._headers(auth),
                timeout=self._timeout,
                **kw,
            )
        except requests.RequestException as exc:
            raise ServerError(f"Could not reach {self.url}: {exc}") from exc

        try:
            body: dict[str, Any] = resp.json()
        except ValueError:
            body = {}

        if not resp.ok:
            message = body.get("message") or f"HTTP {resp.status_code}"
            raise ServerError(message, resp.status_code)
        return body

    # ── endpoints ──────────────────────────────────────────────────────────

    def info(self) -> dict:
        """Server name/version/setupRequired. Raises if this isn't a PiLapse server."""
        try:
            data = self._request("GET", "/info").get("data", {})
        except ServerError as exc:
            if exc.status == 404:
                raise ServerError(
                    f"{self.url} doesn't look like a PiLapse server.", exc.status
                ) from exc
            raise
        if data.get("name") != "pilapse-server":
            raise ServerError(f"{self.url} doesn't look like a PiLapse server.")
        return data

    def signup(self, name: str, email: str, password: str) -> str:
        body = self._request(
            "POST",
            "/user/signup",
            json={
                "name": name,
                "email": email,
                "password": password,
                "passwordConfirm": password,
            },
        )
        self._user_token = body["token"]
        return self._user_token

    def login(self, email: str, password: str) -> str:
        body = self._request(
            "POST", "/user/login", json={"email": email, "password": password}
        )
        self._user_token = body["token"]
        return self._user_token

    def add_pi(self, name: str, model: str = "") -> tuple[dict, str]:
        """Register this Pi. Returns (pi, device_token)."""
        payload: dict[str, Any] = {"name": name}
        if model:
            payload["model"] = model
        body = self._request("POST", "/pi/add", auth="user", json=payload)
        token = body["deviceToken"]
        self._device_token = token
        return body["data"]["pi"], token

    def get_pi(self, pi_id: str) -> dict:
        return self._request("GET", f"/pi/{pi_id}", auth="device")["data"]["pi"]

    def upload_photo(
        self,
        pi_id: str,
        path: Path,
        *,
        intent: str,
        taken_at: Optional[datetime] = None,
    ) -> dict:
        fields = {"intent": intent}
        if taken_at is not None:
            fields["date"] = taken_at.isoformat()
        with Path(path).open("rb") as f:
            body = self._request(
                "POST",
                f"/pi/{pi_id}/photos",
                auth="device",
                data=fields,
                files={"image": (Path(path).name, f, "image/jpeg")},
            )
        return body["data"]["photo"]
