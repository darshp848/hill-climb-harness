"""The fast loop: perception + observer + state-space decisions at `rate_hz` (default 20), in its own thread.

Each tick:
  1. grab the newest live frames (GET /frame/<cam>.jpg, ~4 ms on the station) at a small width,
  2. run the local classifiers on frames it has not judged before (colour detector guard on the wrists),
  3. read the gripper sensors (GET /arms) every few ticks,
  4. update the observer (fast measurements), and fold in any Claude verdict the runner handed over since the last
     tick (slow measurements),
  5. step the state-space model and publish the decision.

mode "shadow" (default): logs what it would decide; the runner keeps its own rules.
mode "live": a guard decision sends /stop at once (like the live guard), and pass / fail / stall are picked up by
the runner on its next look (check_every_s can be 0.25 s), so a stage ends within one runner look of the evidence.
The runner's own rules still run: whichever fires first ends the attempt.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Callable

from .statespace import Observer, StateSpace, Weights


class FastLoop:
    def __init__(self, *, weights: Weights | None = None, rate_hz: float = 20.0, mode: str = "shadow",
                 frames: Callable[[], dict[str, bytes]] | None = None,
                 guard_judge: Callable[[dict[str, bytes]], tuple[bool | None, str]] | None = None,
                 grippers: Callable[[], dict[str, Any]] | None = None,
                 stop: Callable[[], Any] | None = None, log: Callable[..., None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        if mode not in ("shadow", "live"):
            raise ValueError("fast.mode: shadow or live")
        self.w = weights or Weights()
        self.ss = StateSpace(self.w)
        self.obs = Observer(self.w)
        self.period = 1.0 / rate_hz
        self.mode = mode
        self._frames, self._judge, self._grippers, self._stop = frames, guard_judge, grippers, stop
        self._log = log or (lambda *a, **k: None)
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: list[dict[str, float]] = []      # verdict inputs handed over by the runner
        self._sig: dict[str, str] = {}
        self._stage: str | None = None
        self._guarded = False
        self._last_t: float | None = None
        self.decision: dict[str, bool] = {}
        self.tripped: str | None = None
        self.ticks = 0
        self.tick_ms: list[float] = []
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None

    # -- runner hooks -------------------------------------------------------------------------------------
    def attempt_start(self, stage: str, timeout_s: float, stall_s: float | None, *, guarded: bool,
                      monitor: bool = True) -> None:
        with self._lock:
            self._stage, self._guarded = stage, guarded
            self.ss.start(timeout_s, stall_s, monitor=monitor)
            self.obs.reset()
            self._pending.clear()
            self._sig.clear()
            self._last_t = self._clock()
            self.decision, self.tripped = {}, None

    def attempt_end(self) -> None:
        with self._lock:
            self._stage = None

    def verdict(self, *, done: bool | None, failing: bool, same_frames: bool, stalled: bool,
                progress: float | None, guard: bool | None, holding: bool | None = None) -> None:
        """A real monitor verdict landed (slow measurement). Applied on the next tick."""
        with self._lock:
            self._pending.append(dict(done=done, failing=failing, same=same_frames, stalled=stalled,
                                      progress=progress, guard=guard, holding=holding))

    # -- one tick -----------------------------------------------------------------------------------------
    def step(self) -> dict[str, bool]:
        t0 = self._clock()
        with self._lock:
            if self._stage is None:
                return {}
            pending, self._pending = self._pending, []
            stage, guarded = self._stage, self._guarded
            dt = t0 - (self._last_t or t0)
            self._last_t = t0
        l_yes = l_no = 0.0
        if guarded and self._frames is not None and self._judge is not None:
            for role, jpeg in sorted(self._frames().items()):
                sig = hashlib.sha1(jpeg).hexdigest()
                if self._sig.get(role) == sig:
                    continue                     # a frame judged before is not a new look
                self._sig[role] = sig
                ans, _why = self._judge({role: jpeg})
                if ans is True:
                    l_yes = 1.0
                elif ans is False and not l_yes:
                    l_no = 1.0
        fast_meas: dict[str, float | None] = {"illegal_grip": 1.0 if l_yes else (0.0 if l_no else None)}
        if self._grippers is not None and self.ticks % 4 == 0:
            g = self._grippers() or {}
            closed = [a.get("opening") for a in g.values() if isinstance(a, dict) and a.get("opening") is not None]
            if closed:
                fast_meas["holding"] = 1.0 if min(closed) < 0.15 else 0.0
        with self._lock:
            self.obs.fast(fast_meas)
            u = self.ss.u(dt=dt, l_guard_yes=l_yes, l_guard_no=l_no)
            d = self.ss.step(u, self.obs.z)
            for v in pending:
                rise = self.ss.note_progress(v["progress"]) if v["progress"] is not None else False
                self.obs.slow({"progress": v["progress"], "illegal_grip": None if v["guard"] is None else
                               float(v["guard"]), "holding": None if v["holding"] is None else float(v["holding"])})
                u = self.ss.u(dt=0.0, v_new=1, v_done=v["done"] is True, v_same=v["same"], v_failing=v["failing"],
                              v_stalled=v["stalled"], v_rise=rise, v_guard_yes=v["guard"] is True,
                              v_guard_no=v["guard"] is False)
                d2 = self.ss.step(u, self.obs.z)
                d = {k: d[k] | d2[k] for k in d}
            dec = {k: bool(v[0]) for k, v in d.items()}
            if any(dec.values()) and dec != self.decision:
                self._log("fast_decision", stage=stage, mode=self.mode, **dec,
                          z={k: round(float(x), 2) for k, x in zip(("progress", "holding", "at_goal", "illegal",
                                                                     "cable_out"), self.obs.z)})
            self.decision = {k: self.decision.get(k, False) or v for k, v in dec.items()}
            trip = self.mode == "live" and dec["guard"] and self.tripped is None
            if trip:
                self.tripped = "fast loop guard"
        if trip and self._stop is not None:
            try:
                self._stop()
                self._log("guard_tripped", stage=stage, source="fast", stop="sent")
            except Exception as exc:  # noqa: BLE001
                self._log("guard_tripped", stage=stage, source="fast", stop=f"failed: {exc}")
        self.ticks += 1
        self.tick_ms.append(1000 * (self._clock() - t0))
        if len(self.tick_ms) > 2000:
            del self.tick_ms[:1000]
        return dec

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="fast-loop", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._closed.is_set():
            t = self._clock()
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the loop
                self._log("fast_loop_error", error=f"{type(exc).__name__}: {exc}")
            self._closed.wait(max(0.0, self.period - (self._clock() - t)))
