"""Joint telemetry in the harness: position, commanded position, torque and commanded torque of every joint, live.

The Dream API returns none of this. On the station it comes from harness.can_live on the NUC (receive-only CAN
listener, GET /joints); in the sim from sim_station's GET /joints (MuJoCo), in the same shape. A thread polls at
`rate_hz` and turns the numbers into signals the rest of the harness uses:

- `holding[arm]`: the gripper pushes against something (|torque| >= hold_frac of its limit) and is not fully shut:
  it is holding an object. Measured, so more reliable than an image (into the monitor's gripper note);
- `strain`: a joint at >= strain_frac of its torque limit (lifting, pulling a seated plug, pressing on the tower);
- `blocked`: a joint lagging its command by >= blocked_deg while barely moving (pushing into something);
- `contact`: measured torque differs from commanded by >= contact_frac of the limit (an outside force).
A signal counts only after `persist` polls in a row, so one noisy reading never raises it. `note()` words the
current signals for the monitor; `alerts()` returns new ones for the run log and error detection.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Thresholds:
    hold_frac: float = 0.25
    strain_frac: float = 0.6
    blocked_deg: float = 8.0
    blocked_speed_deg_s: float = 3.0
    contact_frac: float = 0.3
    persist: int = 3


@dataclass
class JointTelemetry:
    url: str                                   # station or sim base URL; GET {url}/joints
    rate_hz: float = 20.0
    thresholds: Thresholds = field(default_factory=Thresholds)
    log: Callable[..., Any] | None = None      # RunLog.event, for joint_alert events
    fetch: Callable[[], dict] | None = None    # tests: replace the HTTP read
    record_to: str | None = None               # append every reading as one JSON line (q, q_cmd, dq, tau, tau_cmd)

    def __post_init__(self):
        self.latest: dict = {}
        self.ok = False
        self._streak: dict[tuple, int] = {}
        self._active: set[tuple] = set()
        self._new: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        from .joint_observer import JointObserver  # noqa: PLC0415
        self.observer = JointObserver()
        self._observed: dict[tuple, dict] = {}

    def _get(self) -> dict:
        if self.fetch:
            return self.fetch()
        with urllib.request.urlopen(self.url.rstrip("/") + "/joints", timeout=1.0) as reply:
            return json.loads(reply.read())

    def poll_once(self) -> None:
        try:
            snap = self._get()
        except Exception:
            self.ok = False
            return
        self.ok = bool(snap.get("arms"))
        if self.record_to and self.ok:     # joint traces: training data for a learned look-ahead observer
            row = {"t": snap.get("t"), "arms": {
                arm: {name: [j.get(k) for k in ("q_deg", "q_cmd_deg", "dq_deg_s", "tau_nm", "tau_cmd_nm")]
                      for name, j in joints.items()} for arm, joints in snap["arms"].items()}}
            try:
                with open(self.record_to, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            except OSError:
                pass
        raised = self._signals(snap)
        # Look-ahead observer (harness.joint_observer): the source's own (can_live on the NUC, at the CAN reply
        # rate) if it sends one, else run here on each reading.
        observed = snap.get("observer_alerts")
        if observed is None:
            t = snap.get("t") or time.time()
            observed = [a for arm, joints in (snap.get("arms") or {}).items()
                        for a in self.observer.observe(t, arm, joints)]
        for a in observed:
            raised.add((a["kind"], a["arm"], a["joint"]))
            self._observed[(a["kind"], a["arm"], a["joint"])] = a
        with self._lock:
            self.latest = snap
            for key in raised - self._active:
                self._new.append(self._words(key))
                if self.log:
                    self.log("joint_alert", kind=key[0], arm=key[1], joint=key[2], text=self._words(key))
            self._active = raised

    def _signals(self, snap: dict) -> set[tuple]:
        th, seen = self.thresholds, set()
        for arm, joints in (snap.get("arms") or {}).items():
            for name, j in joints.items():
                tau, tmax = j.get("tau_nm"), j.get("tau_max_nm")
                if tau is None or not tmax:
                    continue
                frac = abs(tau) / tmax
                if name == "gripper":
                    shut = j.get("q_cmd_deg") is not None and j.get("q_deg") is not None and \
                        abs(j["q_deg"] - j["q_cmd_deg"]) < 1e-3
                    if frac >= th.hold_frac and not shut:
                        seen.add(("holding", arm, name))
                    continue
                if frac >= th.strain_frac:
                    seen.add(("strain", arm, name))
                q, qc, dq = j.get("q_deg"), j.get("q_cmd_deg"), j.get("dq_deg_s")
                if q is not None and qc is not None and dq is not None and \
                        abs(qc - q) >= th.blocked_deg and abs(dq) <= th.blocked_speed_deg_s:
                    seen.add(("blocked", arm, name))
                tc = j.get("tau_cmd_nm")
                if tc is not None and abs(tau - tc) / tmax >= th.contact_frac:
                    seen.add(("contact", arm, name))
        out = set()
        for key in seen | set(self._streak):
            self._streak[key] = self._streak.get(key, 0) + 1 if key in seen else 0
            if self._streak[key] >= th.persist:
                out.add(key)
        self._streak = {k: v for k, v in self._streak.items() if v}
        return out

    def _words(self, key: tuple) -> str:
        kind, arm, joint = key
        if key in self._observed:
            a = self._observed[key]
            return {"limit_breach": f"{arm} {joint} is past its joint limit ({a.get('q_deg')} deg)",
                    "limit_approach": f"{arm} {joint} will reach its joint limit in "
                                      f"{a.get('eta_s') if a.get('eta_s') is not None else '?'} s",
                    "overspeed": f"{arm} {joint} is moving too fast ({a.get('v_deg_s')} deg/s)",
                    "torque_saturation": f"{arm} {joint} is at its torque limit ({a.get('tau_nm')} N.m)",
                    "tracking_divergence": f"{arm} {joint} cannot follow its command (pushing into something)",
                    "oscillation": f"{arm} {joint} is oscillating",
                    "fault": f"{arm} {joint} motor reports a fault (status {a.get('status')})",
                    "overheat": f"{arm} {joint} motor is hot ({a.get('temp_c')} C)"}.get(kind, f"{arm} {joint}: {kind}")
        j = (self.latest.get("arms") or {}).get(arm, {}).get(joint, {})
        tau, tmax = j.get("tau_nm"), j.get("tau_max_nm")
        load = f" ({abs(tau):.1f} of {tmax:.0f} N.m)" if tau is not None and tmax else ""
        return {"holding": f"{arm} gripper is squeezing an object{load}",
                "strain": f"{arm} {joint} is straining{load}",
                "blocked": f"{arm} {joint} is blocked: it lags its command and is not moving",
                "contact": f"{arm} {joint} feels an outside force{load}"}[kind]

    def holding(self) -> dict[str, bool]:
        with self._lock:
            arms = (self.latest.get("arms") or {}).keys()
            return {a: ("holding", a, "gripper") in self._active for a in arms}

    def note(self) -> str:
        """The current signals in words, for the monitor; '' when the feed is down or nothing stands out."""
        with self._lock:
            if not self.ok:
                return ""
            parts = [self._words(k) for k in sorted(self._active)]
            for arm in sorted((self.latest.get("arms") or {})):
                if ("holding", arm, "gripper") not in self._active:
                    parts.append(f"{arm} gripper is not squeezing anything (joint torque)")
            return "; ".join(parts)

    def alerts(self) -> list[str]:
        with self._lock:
            new, self._new = self._new, []
            return new

    def start(self) -> "JointTelemetry":
        def loop():
            period = 1.0 / self.rate_hz
            while not self._stop.is_set():
                t = time.monotonic()
                self.poll_once()
                self._stop.wait(max(0.0, period - (time.monotonic() - t)))
        self._thread = threading.Thread(target=loop, name="joint-telemetry", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
