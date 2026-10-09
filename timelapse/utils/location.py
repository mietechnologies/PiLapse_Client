"""
Finding the device location for sunrise/sunset and weather.

Used only by `timelapse setup`; at run time the stored lat/lon is used and
nothing here touches the network.

  - detect_from_ip(): city-level guess from the public IP (ipinfo.io, no key).
    Usually within 10-50 km, which moves sunrise by a minute or two.
    A VPN or Tailscale exit node can put it in the wrong city, so the
    wizard always asks the user to confirm.
  - search(query): city / place-name lookup (Open-Meteo geocoding, no key).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

IP_LOOKUP_URL = "https://ipinfo.io/json"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
TIMEOUT_S = 10


@dataclass(frozen=True)
class Place:
    latitude: float
    longitude: float
    name: str


def detect_from_ip() -> Optional[Place]:
    try:
        import requests

        resp = requests.get(IP_LOOKUP_URL, timeout=TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
        lat_s, lon_s = data["loc"].split(",")
        name = ", ".join(p for p in (data.get("city"), data.get("region"), data.get("country")) if p)
        return Place(float(lat_s), float(lon_s), name or "Unknown")
    except Exception as exc:
        logger.warning("IP location lookup failed: %s", exc)
        return None


def search(query: str, count: int = 5) -> list[Place]:
    try:
        import requests

        resp = requests.get(
            GEOCODE_URL,
            params={"name": query, "count": count, "language": "en", "format": "json"},
            timeout=TIMEOUT_S,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except Exception as exc:
        logger.warning("Place search failed: %s", exc)
        return []
    places = []
    for r in results:
        name = ", ".join(p for p in (r.get("name"), r.get("admin1"), r.get("country")) if p)
        places.append(Place(float(r["latitude"]), float(r["longitude"]), name))
    return places
