"""Illegal-ball guard on live camera frames, in its own thread.

The plan guard otherwise looks at debug-view trips (one every ~3.4 s on the station) once per poll (check_every_s,
3 s in v4), so a held ball could be up to ~6 s old when judged, and pick-to-bin takes a few seconds. The station
add-on (station_addons/sitecustomize.py) serves the controller's newest frame at GET /frame/<camera>.jpg, live at
30 fps whether or not a run is going (checked read-only on the NUC, 7 Nov: 4 ms per frame). This thread runs the
local detector's guard on each wrist camera at `rate_hz`; `confirm_frames` yes answers in a row on new frames of the
same camera POST /stop at once, without waiting for the runner's next poll. The runner then ends the match as for
any guard trip.

Reads only GET /frame; the one thing it sends is /stop, and only while armed (a stage the plan guard covers).

Safety rules (from the Codex review of the first version):
- `command_lock` is shared with the runner: the runner holds it while it sends /start, /prompt or /home and checks
  `tripped` first; the guard holds it while it sets `tripped` and sends /stop. So no motion command can follow a
  trip unseen, and a trip right after a motion command still stops it.
- Arming has a generation: a judgment that started before a disarm (or a re-arm for another stage) is dropped.
- Only a new frame counts as a look: the same bytes judged again neither add a yes nor refresh `last_answer_t`, so a
  frozen camera shows up as blindness (plan guard max_blind_s), not as "all clear".
- Yes answers are counted per camera, so motion in one wrist view can't confirm a frozen yes in the other.
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
    """GET /frame/<role>.jpg from the station API for each role. A role that fails this tick is just missing from
    the result; it is tried again next tick (a camera that recovers is used again)."""

    def __init__(self, base_url: str = "http://127.0.0.1:8091", roles: tuple[str, ...] = ("wrist_l", "wrist_r"),
                 *, timeout_s: float = 1.0):
        self.base_url = base_url.rstrip("/")
        self.roles = list(roles)
        self.timeout_s = timeout_s
        self.last_error: str | None = None

    def grab(self) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for role in self.roles:
            try:
                with urllib.request.urlopen(f"{self.base_url}/frame/{role}.jpg", timeout=self.timeout_s) as resp:
                    out[role] = resp.read()
            except urllib.error.HTTPError as exc:
                self.last_error = f"{role}: HTTP {exc.code}"
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                self.last_error = f"{role}: {exc}"
        return out

    def probe(self, tries: int = 3) -> list[str]:
        """Roles that returned a frame in at least one of `tries` grabs (startup check before relying on them)."""
        seen: set[str] = set()
        for _ in range(tries):
            seen |= set(self.grab())
            if len(seen) == len(self.roles):
                break
        return [r for r in self.roles if r in seen]


class LiveGuard:
    """Runs `judge({role: jpeg})` per camera at `rate_hz` while armed. `judge` returns (True/False/None, why) like
    TableDetector.guard. `confirm_frames` yes answers in a row on new frames of one camera call `stop()` once and
    set `tripped` to the reason. A no on new frames of that camera resets its count; None leaves it alone."""

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
        self.command_lock = threading.RLock()   # shared with the runner's motion commands (see module doc)
        self._lock = threading.Lock()           # guards the fields below
        self._armed: str | None = None          # the covered stage's name while armed
        self._gen = 0                           # bumped on every arm-from-disarmed and every disarm
        self._count: dict[str, int] = {}        # per camera: yes answers in a row on new frames
        self._sig: dict[str, str] = {}          # per camera: signature of the last frame judged
        self.tripped: str | None = None
        self.last_answer_t: float | None = None   # clock() of the last yes/no answer on a new frame
        self.ticks = 0
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None

    # -- control -------------------------------------------------------------------------------
    def arm(self, stage: str) -> None:
        with self._lock:
            if self._armed is None:
                self._gen += 1
                self._count, self._sig = {}, {}
                self.last_answer_t = self._clock()   # max_blind_s counts from arming, as the poll guard does
                self._log("live_guard_armed", stage=stage)
            self._armed = stage

    def disarm(self) -> None:
        with self.command_lock:          # waits for a stop being sent; after this no stop is dispatched
            with self._lock:
                if self._armed is not None:
                    self._log("live_guard_disarmed", stage=self._armed, ticks=self.ticks)
                self._armed = None
                self._gen += 1

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
        """One tick: grab, judge each camera, count, maybe stop. The thread calls this; tests call it directly."""
        with self._lock:
            stage, gen = self._armed, self._gen
        if stage is None or self.tripped is not None:
            return
        frames = self.source()
        self.ticks += 1
        for role in sorted(frames):
            jpeg = frames[role]
            sig = hashlib.sha1(jpeg).hexdigest()
            with self._lock:
                if gen != self._gen:
                    return                  # disarmed or re-armed while looking: these answers are stale
                if sig == self._sig.get(role):
                    continue                # the same frame again is not a new look
            answer, why = self.judge({role: jpeg})
            with self._lock:
                if gen != self._gen:
                    return
                self._sig[role] = sig
                if answer is None:
                    continue
                self.last_answer_t = self._clock()
                if answer is False:
                    self._count[role] = 0
                    continue
                self._count[role] = count = self._count.get(role, 0) + 1
            self._log("guard_yes", stage=stage, count=count, reason=why, camera=role, source="live")
            if count >= self.confirm_frames:
                self._trip(stage, gen, why)
                return

    def _trip(self, stage: str, gen: int, why: str) -> None:
        with self.command_lock:
            with self._lock:
                if gen != self._gen or self.tripped is not None:
                    return
                self.tripped = why
            try:
                self._stop()
                self._log("guard_tripped", stage=stage, reason=why, source="live", stop="sent")
            except Exception as exc:  # noqa: BLE001 - the runner's own stop follows on its next check
                self._log("guard_tripped", stage=stage, reason=why, source="live", stop=f"failed: {exc}")

    def _loop(self) -> None:
        while not self._closed.is_set():
            t = self._clock()
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the guard
                self._log("live_guard_error", error=f"{type(exc).__name__}: {exc}")
            self._closed.wait(max(0.0, self.period - (self._clock() - t)))
