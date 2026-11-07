"""Reads the newest camera frames from the station's debug view (`--view-port 8093`)."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


_ROLE_NAMES = {
    "scene": "overhead", "top": "overhead", "overhead": "overhead",
    "left": "wrist_l", "wrist_l": "wrist_l",
    "right": "wrist_r", "wrist_r": "wrist_r",
}


def camera_matches(role: str, wanted: str) -> bool:
    """Match YAM station and simulator names, or a custom camera role substring."""
    actual = role.removeprefix("video.").removeprefix("image_window.")
    expected = wanted.removeprefix("video.").removeprefix("image_window.")
    canonical = _ROLE_NAMES.get(expected)
    return wanted in role or (canonical is not None and _ROLE_NAMES.get(actual) == canonical)


@dataclass(frozen=True)
class CameraTrip:
    n: int
    prompt: str | None
    frames: dict[str, bytes]


class FrameSource:
    def __init__(self, base_url: str = "http://127.0.0.1:8093", *, timeout_s: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.last_error: str | None = None
        self._cache: CameraTrip | None = None

    def _get_json(self, route: str) -> Any:
        with urllib.request.urlopen(self.base_url + route, timeout=self.timeout_s) as resp:
            return json.loads(resp.read())

    def latest_n(self) -> int | None:
        """Number of the newest round trip, or None."""
        try:
            trips = self._get_json("/trips")
            self.last_error = None
            if isinstance(trips, list) and trips:
                return int(trips[-1]["n"])
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
        return None

    def latest(self) -> dict[str, bytes]:
        """role -> JPEG bytes: the newest frame of each camera in the newest round trip. {} if none."""
        trip = self.latest_trip(fallback=True)
        return dict(trip.frames) if trip is not None else {}

    def latest_trip(self, *, fallback: bool = False) -> CameraTrip | None:
        """Read frames and prompt from the same trip. Evidence checks use only the newest trip."""
        try:
            trips = self._get_json("/trips")
            if not isinstance(trips, list) or not trips:
                self.last_error = None
                return None
            for entry in reversed(trips[-3:] if fallback else trips[-1:]):
                n = int(entry["n"])
                if self._cache is not None and n == self._cache.n:
                    self.last_error = None
                    return self._cache
                try:
                    trip = self._get_json(f"/trip/{n}")
                except urllib.error.HTTPError as exc:
                    if exc.code == 404:
                        continue
                    raise
                out = _decode_frames(trip.get("frames") or {})
                if out:
                    obs = trip.get("observation") or {}
                    prompt = obs.get("prompt") if isinstance(obs, dict) else None
                    self._cache = CameraTrip(n, prompt if isinstance(prompt, str) else None, out)
                    self.last_error = None
                    return self._cache
            self.last_error = None
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
        return None


def _decode_frames(frames: dict[str, Any]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for key, images in frames.items():
        if not isinstance(images, list) or not images or not isinstance(images[-1], str):
            continue
        role = key.removeprefix("video.")
        try:
            jpeg = base64.b64decode(images[-1], validate=True)
        except ValueError:
            continue
        if jpeg.startswith(b"\xff\xd8"):
            out[role] = jpeg
    return out
