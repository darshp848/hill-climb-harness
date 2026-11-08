"""Illegal-ball guard on live camera frames, in its own thread.

The plan guard otherwise looks at debug-view trips (one every ~3.4 s on the station) once per poll (check_every_s,
3 s in v4), so a held ball could be up to ~6 s old when judged, and pick-to-bin takes a few seconds. The station
add-on (station_addons/sitecustomize.py) serves the controller's newest frame at GET /frame/<camera>.jpg, live at
30 fps whether or not a run is going (checked read-only on the NUC, 7 Nov: 4 ms per frame). This thread runs the
local detector's guard on those frames at `rate_hz`, and after `confirm_frames` yes answers on distinct frames in a
row it POSTs /stop itself, without waiting for the runner's next poll. The runner then ends the match as for any
guard trip.

Reads only GET /frame; the one thing it sends is /stop, and only while armed (a stage the plan guard covers).
"""

from __future__ import annotations

import hashlib
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any


class LiveFrames:
    """GET /frame/<role>.jpg from the station API for each role. Roles that 404 (no add-on, no such camera) are
    dropped after `max_misses` tries in a row so a missing route costs nothing per tick."""

    def __init__(self, base_url: str = "http://127.0.0.1:8091", roles: tuple[str, ...] = ("wrist_l", "wrist_r"),
                 *, timeout_s: float = 1.0, max_misses: int = 3):
        self.base_url = base_url.rstrip("/")
        self.roles = list(roles)
        self.timeout_s = timeout_s
        self.max_misses = max_misses
        self._misses: dict[str, int] = {}
        self.last_error: str | None = None

    def grab(self) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for role in list(self.roles):
            try:
                with urllib.request.urlopen(f"{self.base_url}/frame/{role}.jpg", timeout=self.timeout_s) as resp:
                    out[role] = resp.read()
                self._misses[role] = 0
            except urllib.error.HTTPError as exc:
                self.last_error = f"{role}: HTTP {exc.code}"
                if exc.code == 404:
                    self._misses[role] = self._misses.get(role, 0) + 1
                    if self._misses[role] >= self.max_misses:
                        self.roles.remove(role)
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                self.last_error = f"{role}: {exc}"
        return out


class LiveGuard:
    """Runs `judge(source())` at `rate_hz` while armed. `judge` returns (True/False/None, why) like
    TableDetector.guard. `confirm_frames` yes answers in a row, each on frames not seen before, call `stop()` once
    and set `tripped` to the reason. A no answer resets the count; None (no readable wrist view) leaves it alone."""

    def __init__(self, source: Callable[[], dict[str, bytes]],
                 judge: Callable[[dict[str, bytes]], tuple[bool | None, str]],
                 stop: Callable[[], Any], *, rate_hz: float = 10.0, confirm_frames: int = 3,
                 log: Callable[..., None] | None = None, clock: Callable[[], float] = time.monotonic):
        if confirm_frames < 1 or rate_hz <= 0:
            raise ValueError("confirm_frames must be >= 1 and rate_hz > 0")
        self.source, self.judge, self._stop = source, judge, stop
        self.period = 1.0 / rate_hz
        self.confirm_frames = confirm_frames
        self._log = log or (lambda *a, **k: None)
        self._clock = clock
        self._lock = threading.Lock()
        self._armed: str | None = None   # the covered stage's name while armed
        self._count = 0
        self._last_sig: str | None = None
        self.tripped: str | None = None
        self.last_answer_t: float | None = None   # clock() of the last yes/no answer
        self.ticks = 0
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None

    # -- control -------------------------------------------------------------------------------
    def arm(self, stage: str) -> None:
        with self._lock:
            if self._armed is None:
                self._count, self._last_sig = 0, None
                self.last_answer_t = self._clock()   # max_blind_s counts from arming, as the poll guard does
                self._log("live_guard_armed", stage=stage)
            self._armed = stage

    def disarm(self) -> None:
        with self._lock:
            if self._armed is not None:
                self._log("live_guard_disarmed", stage=self._armed, ticks=self.ticks)
            self._armed = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="live-guard", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._closed.set()
        self.disarm()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # -- one look ------------------------------------------------------------------------------
    def step(self) -> None:
        """One tick: grab, judge, count, maybe stop. The thread calls this; tests call it directly."""
        with self._lock:
            stage = self._armed
        if stage is None or self.tripped is not None:
            return
        frames = self.source()
        if not frames:
            return
        answer, why = self.judge(frames)
        self.ticks += 1
        if answer is None:
            return
        sig = hashlib.sha1(b"".join(frames[k] for k in sorted(frames))).hexdigest()
        with self._lock:
            if self._armed is None:   # disarmed while judging: the answer is about a stage that has ended
                return
            self.last_answer_t = self._clock()
            if answer is False:
                self._count, self._last_sig = 0, sig
                return
            if sig == self._last_sig:
                return              # the same frozen frames judged again are not a second look
            self._count += 1
            self._last_sig = sig
            count = self._count
        self._log("guard_yes", stage=stage, count=count, reason=why, source="live")
        if count >= self.confirm_frames:
            self.tripped = why
            try:
                self._stop()
                self._log("guard_tripped", stage=stage, reason=why, source="live", stop="sent")
            except Exception as exc:  # noqa: BLE001 - the runner's own stop follows on its next poll
                self._log("guard_tripped", stage=stage, reason=why, source="live", stop=f"failed: {exc}")

    def _loop(self) -> None:
        while not self._closed.is_set():
            t = self._clock()
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the guard
                self._log("live_guard_error", error=f"{type(exc).__name__}: {exc}")
            self._closed.wait(max(0.0, self.period - (self._clock() - t)))
