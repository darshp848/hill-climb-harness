"""Opt-in tuning of Dream's station agent for the Metal arms: bus tick rate, joint torque cap, IK weights, tick log.

Loaded by this folder's sitecustomize.py (PYTHONPATH=station_addons). Dream's installed files are never edited, and
with none of the environment variables below set NOTHING changes: every patch is off by default. Each patch checks the
dreamlabs version it was written against (1.0.37 / dreamlabs-client 1.1.3) and skips itself, with a line in the
station log, if the code it wraps is not there.

Environment (read once, when `python -m robots.station` starts; `dreamcli robot up` passes its env down):

- PAH_METAL_TICK_HZ=60
    The Metal bus thread's rate (Dream hard-codes 30 in robots.metal.ARM). This is the rate the joint state is read
    and published at AND the rate new MIT setpoints reach the motors (Dream's command thread already interpolates
    at 250 Hz, but at 30 Hz only every 8th of those reaches the arm). Everything Dream derives from the tick rate
    (ramp, speed caps, park speed) is in physical units and rescales itself. Two per-command constants in
    robots.damiao are rescaled here so they keep their time meaning: the velocity feed-forward filter weight
    (FF_ALPHA, same 0.8 s time constant) and the startup-sync step (SYNC_STEP_DEG, same 30 deg/s).
    The loop sleeps only what is left of the period, so an overrun tick just runs late (no backlog); the tick log
    says how often that happens. Policy rows (control_hz) are untouched.

- PAH_TORQUE_CAP_NM=6,10,6,4,2,2        (one per joint, base to wrist; or a single number for all)
    A cap on the torque each joint pushes with ON TOP of its gravity feed-forward, after each Dream action.
    Damiao's MIT mode computes tau = kp*(q_goal - q) + kd*(dq_goal - dq) + tau_ff on the motor, so the host cannot
    clamp tau directly; this moves the position goal toward the measured angle so that kp*|q_goal - q| <= cap. The
    gravity feed-forward is never cut (a cap below gravity would drop the arm). On the station (run 3, 8 Nov) the
    P term peaked around 1.5 N.m in free motion, so these caps leave free motion alone; in contact (a blocked
    wrist, a block pressed into the table) a joint presses with at most the cap instead of kp times the whole
    error, which is what tripped "wrist_yaw blocked for 6 s". Off by default.

- PAH_FF_TAU_S=0.15
    The time constant of Dream's velocity feed-forward filter. Dream's FF_ALPHA 0.04 per 30 Hz command is a 0.8 s
    time constant, so the feed-forward lags any motion shorter than a second and the PD (kp 100) carries the arm
    behind its target. Unset keeps Dream's 0.8 s (rescaled to the tick rate when PAH_METAL_TICK_HZ is set).

- PAH_GRAVITY_SCALE=1.0
    Multiplies Dream's gravity feed-forward (already MakerMods' SDK factors 1.0-1.2 on top of the URDF model).

- PAH_IK_TRAJ=20,100,200,5
    The eef head's trajectory-solve weights (velocity, acceleration, jerk, history x). Dream's default is 5/50/200/5,
    tuned on a YAM; Dream's own note in dream_ik/specs.py says 20/100/200/5 hit the speed cap less often than the tick
    solve on a Metal arm, with less jerk and fewer direction flips. Only affects action_space: eef with ik_solve: traj.

- PAH_EEF_OFFSET_M=r:0,0,-0.02;l:0,0,-0.02
    Intercepts Dream's end-effector answer before the IK: each arm's tool positions (`action.<side>_eef_xyz`, every
    row of every chunk, in that arm's base frame, metres) are shifted by this offset, then Dream's own IK solves them.
    For a systematic aim error, such as the fine-tune aiming high because 73 of its 170 episodes were recorded with
    gravity feed-forward off. eef head only.

- PAH_EEF_Z_MIN_M=r:0.01;l:0.01
    A floor on each arm's tool height (base frame), applied after the offset: no target below it reaches the IK.

- PAH_EEF_HOOK=package.module:function
    Your own override: called as function(side, xyz[rows,3], rot6[rows,6], grip[rows,1], observation) for each arm of
    each chunk, after the offset and floor, and returns (xyz, rot6, grip) of the same shapes. An exception in it is
    logged once per kind and that chunk runs unmodified.

- PAH_EEF_LOG=/home/pah/.dreamlabs/eef-log.jsonl
    One line per chunk: time, per arm the tool targets as Dream sent them, as passed to the IK, and the solved
    joints. The first record of the IK's targets on the station (hand error can then be scored offline).

- PAH_PULL_FORCE_N=r:20                 (newtons at the tool point, per arm; at most MAX_PULL_FORCE_N)
  PAH_PULL_DIR=r:0,-1,0                 (that arm's base frame, the frame of Dream's eef answers; or r:tool)
    Pull force while the gripper holds something. Dream's policy steers positions only, so a grasped plug is pulled
    only as hard as the joints' position error makes it (the station logs show ~0.6 N.m: no pull at all). With this
    on, once an arm's gripper has held for PULL_DELAY_S the arm adds J^T F to its MIT feed-forward (on top of
    gravity), F = force x direction at the tool point, ramped in over PULL_RAMP_S. `tool` pulls back along the
    gripper's approach axis (minus the flange x axis), frozen when the pull starts. It stops, until the gripper lets
    go and grips again, when the tool point has moved PAH_PULL_TRAVEL_M along the pull (the plug came out, or nothing
    resisted), after PAH_PULL_MAX_S, or at once when the gripper stops holding. Each joint's added torque is capped
    by PAH_PULL_TAU_CAP_NM. Events go to the station log (`pah_tuning: pull ...`).
- PAH_PULL_MAX_S=3   PAH_PULL_TRAVEL_M=0.012   PAH_PULL_TAU_CAP_NM=8,8,8,3,1.5,1.5   PAH_PULL_TCP_M=0.136,0,0.002
    The pull's limits, and the tool point in the flange frame (fitted to Dream's eef answers vs its IK's joints on
    the station, 8 Nov: median 5 mm).

- PAH_TICK_LOG=/home/pah/.dreamlabs/tick-log.csv
    One row per bus tick per arm: wall time, arm, tick duration, period, measured joints (deg), measured torque per
    motor (N.m, from the MIT replies), and drops. Every 10 s a summary line goes to the station log
    (`pah_tuning: tick ...`). Off by default; turning it on alone changes no motion.
"""

from __future__ import annotations

import csv
import dataclasses
import importlib.abc
import importlib.machinery
import logging
import math
import os
import sys
import threading
import time
from typing import Any

_log = logging.getLogger("pah_tuning")

TESTED_DREAMLABS = "1.0.37"
JOINTS = 6
# Never run the Metal bus faster than this: past it the gripper's late replies (3 ms flushes, up to 3 per query) eat
# the whole period. MakerMods' own SDK streams at 120 Hz.
MAX_TICK_HZ = 120.0
MIN_TICK_HZ = 10.0
SUMMARY_EVERY_S = 10.0
MAX_PULL_FORCE_N = 40.0
PULL_DELAY_S = 0.3
PULL_RAMP_S = 0.5
PULL_TCP_M = (0.136, 0.0, 0.002)
PULL_TAU_CAP_NM = (8.0, 8.0, 8.0, 3.0, 1.5, 1.5)


def _say(message: str) -> None:
    """To the station log (stdout of robots.station) and the logger; never raises."""
    try:
        print(f"pah_tuning: {message}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


def _floats(name: str, count: int | None = None) -> list[float] | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    values = [float(x) for x in raw.replace(";", ",").split(",") if x.strip()]
    if count is not None and len(values) == 1:
        values = values * count
    if count is not None and len(values) != count:
        raise ValueError(f"{name} needs 1 or {count} numbers, got {raw!r}")
    if not all(math.isfinite(v) for v in values):
        raise ValueError(f"{name} has a non-finite number: {raw!r}")
    return values


SIDES = {"r": "right", "l": "left", "right": "right", "left": "left"}


def _per_side(name: str, width: int) -> dict[str, list[float]] | None:
    """`r:0,0,-0.02;l:0,0,-0.01` (or one value list for both arms) -> {"right": [...], "left": [...]}."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    out: dict[str, list[float]] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            side, values = part.split(":", 1)
            sides = [SIDES[side.strip().lower()]]
        else:
            sides, values = ["right", "left"], part
        nums = [float(x) for x in values.split(",") if x.strip()]
        if len(nums) != width or not all(math.isfinite(x) for x in nums):
            raise ValueError(f"{name} needs {width} numbers per arm, got {raw!r}")
        for side in sides:
            out[side] = nums
    return out


def _pull_dirs() -> dict[str, list[float] | str] | None:
    """`r:0,-1,0;l:tool` -> {"right": [0, -1, 0] (unit), "left": "tool"}."""
    raw = os.environ.get("PAH_PULL_DIR", "").strip()
    if not raw:
        return None
    out: dict[str, list[float] | str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        side, sep, value = part.partition(":")
        sides = [SIDES[side.strip().lower()]] if sep else ["right", "left"]
        value = value if sep else part
        if value.strip().lower() == "tool":
            vec: list[float] | str = "tool"
        else:
            nums = [float(x) for x in value.split(",") if x.strip()]
            norm = math.sqrt(sum(x * x for x in nums)) if len(nums) == 3 else 0.0
            if len(nums) != 3 or not math.isfinite(norm) or norm < 1e-6:
                raise ValueError(f"PAH_PULL_DIR needs `tool` or 3 numbers (not all 0) per arm, got {raw!r}")
            vec = [x / norm for x in nums]
        for s in sides:
            out[s] = vec
    return out


@dataclasses.dataclass
class Settings:
    tick_hz: float | None = None
    torque_cap_nm: list[float] | None = None
    gravity_scale: float | None = None
    ik_traj: list[float] | None = None
    tick_log: str | None = None
    ff_tau_s: float | None = None
    eef_offset_m: dict[str, list[float]] | None = None
    eef_z_min_m: dict[str, float] | None = None
    eef_hook: str | None = None
    eef_log: str | None = None
    pull_force_n: dict[str, float] | None = None
    pull_dir: dict[str, list[float] | str] | None = None
    pull_max_s: float | None = None
    pull_travel_m: float | None = None
    pull_tau_cap_nm: list[float] | None = None
    pull_tcp_m: list[float] | None = None

    @classmethod
    def from_env(cls) -> Settings:
        tick = _floats("PAH_METAL_TICK_HZ")
        grav = _floats("PAH_GRAVITY_SCALE")
        s = cls(
            tick_hz=None if tick is None else tick[0],
            torque_cap_nm=_floats("PAH_TORQUE_CAP_NM", JOINTS),
            gravity_scale=None if grav is None else grav[0],
            ik_traj=_floats("PAH_IK_TRAJ", 4),
            tick_log=os.environ.get("PAH_TICK_LOG", "").strip() or None,
            ff_tau_s=None if _floats("PAH_FF_TAU_S") is None else _floats("PAH_FF_TAU_S")[0],
        )
        s.eef_offset_m = _per_side("PAH_EEF_OFFSET_M", 3)
        z_min = _per_side("PAH_EEF_Z_MIN_M", 1)
        s.eef_z_min_m = None if z_min is None else {k: v[0] for k, v in z_min.items()}
        s.eef_hook = os.environ.get("PAH_EEF_HOOK", "").strip() or None
        s.eef_log = os.environ.get("PAH_EEF_LOG", "").strip() or None
        force = _per_side("PAH_PULL_FORCE_N", 1)
        s.pull_force_n = None if force is None else {k: v[0] for k, v in force.items()}
        s.pull_dir = _pull_dirs()
        if (s.pull_force_n is None) != (s.pull_dir is None):
            raise ValueError("PAH_PULL_FORCE_N and PAH_PULL_DIR go together")
        if s.pull_force_n is not None:
            for side, f in s.pull_force_n.items():
                if not 0.0 < f <= MAX_PULL_FORCE_N:
                    raise ValueError(f"PAH_PULL_FORCE_N must be 0..{MAX_PULL_FORCE_N:g} N, got {f}")
                if side not in s.pull_dir:
                    raise ValueError(f"PAH_PULL_DIR has no direction for the {side} arm")
            pmax, ptravel = _floats("PAH_PULL_MAX_S"), _floats("PAH_PULL_TRAVEL_M")
            s.pull_max_s = 3.0 if pmax is None else pmax[0]
            s.pull_travel_m = 0.012 if ptravel is None else ptravel[0]
            s.pull_tau_cap_nm = _floats("PAH_PULL_TAU_CAP_NM", JOINTS) or list(PULL_TAU_CAP_NM)
            s.pull_tcp_m = _floats("PAH_PULL_TCP_M", 3) or list(PULL_TCP_M)
            if not 0.2 <= s.pull_max_s <= 10.0:
                raise ValueError("PAH_PULL_MAX_S must be 0.2..10 s")
            if not 0.003 <= s.pull_travel_m <= 0.05:
                raise ValueError("PAH_PULL_TRAVEL_M must be 0.003..0.05 m")
            if any(not 0.0 < c <= 15.0 for c in s.pull_tau_cap_nm):
                raise ValueError("PAH_PULL_TAU_CAP_NM must be 0..15 N.m per joint")
            if any(abs(x) > 0.3 for x in s.pull_tcp_m):
                raise ValueError("PAH_PULL_TCP_M is within 0.3 m of the flange")
        if s.eef_offset_m and any(abs(x) > 0.05 for v in s.eef_offset_m.values() for x in v):
            raise ValueError("PAH_EEF_OFFSET_M is capped at 5 cm per axis")
        if s.ff_tau_s is not None and not 0.02 <= s.ff_tau_s <= 2.0:
            raise ValueError(f"PAH_FF_TAU_S must be 0.02..2 s, got {s.ff_tau_s}")
        if s.tick_hz is not None and not MIN_TICK_HZ <= s.tick_hz <= MAX_TICK_HZ:
            raise ValueError(f"PAH_METAL_TICK_HZ must be {MIN_TICK_HZ:g}..{MAX_TICK_HZ:g}, got {s.tick_hz}")
        if s.torque_cap_nm is not None and any(c <= 0 for c in s.torque_cap_nm):
            raise ValueError("PAH_TORQUE_CAP_NM must be positive")
        if s.gravity_scale is not None and not 0.0 <= s.gravity_scale <= 1.5:
            raise ValueError("PAH_GRAVITY_SCALE must be 0..1.5")
        if s.ik_traj is not None and any(w < 0 for w in s.ik_traj):
            raise ValueError("PAH_IK_TRAJ weights must be >= 0")
        return s

    def active(self) -> bool:
        return any(v is not None for v in dataclasses.asdict(self).values())


# ---------------------------------------------------------------------------------------------------------- patches


def patch_metal(metal: Any, settings: Settings) -> None:
    """robots.metal.ARM at the new tick rate (before robots.bodies reads it)."""
    if settings.tick_hz is None:
        return
    old = metal.ARM.tick_hz
    metal.ARM = dataclasses.replace(metal.ARM, tick_hz=float(settings.tick_hz))
    _say(f"Metal bus tick {old:g} Hz -> {settings.tick_hz:g} Hz")


def ff_alpha_at(hz: float, alpha_30: float, base_hz: float = 30.0) -> float:
    """The filter weight that keeps alpha_30's time constant at `hz` ticks per second."""
    return 1.0 - (1.0 - alpha_30) ** (base_hz / hz)


def cap_goal_deg(goal_deg: float, q_deg: float, kp: float, cap_nm: float) -> float:
    """The goal moved toward q so that kp * |goal - q| <= cap, N.m above gravity (degrees in, degrees out)."""
    max_err_deg = math.degrees(cap_nm / kp) if kp > 0 else math.inf
    return q_deg + max(-max_err_deg, min(max_err_deg, goal_deg - q_deg))


def _axis_rotation(axis: Any, angle: float) -> Any:
    """Rodrigues, as robots.metal._rotation."""
    import numpy as np

    k = np.array([[0.0, -axis[2], axis[1]], [axis[2], 0.0, -axis[0]], [-axis[1], axis[0], 0.0]])
    return np.eye(3) + math.sin(angle) * k + (1.0 - math.cos(angle)) * k @ k


def _rpy(roll: float, pitch: float, yaw: float) -> Any:
    """A URDF origin's rotation (yaw about z, pitch about y, roll about x, fixed axes), as robots.metal._rpy."""
    import numpy as np

    x, y, z = np.eye(3)
    return _axis_rotation(z, yaw) @ _axis_rotation(y, pitch) @ _axis_rotation(x, roll)


def arm_kinematics(links: Any, q_deg: Any, tcp_m: Any) -> tuple[Any, Any, list[Any], list[Any]]:
    """The tool point (base frame, m), the flange rotation, and each joint's axis and pivot, from Dream's link table
    (robots.metal.LINKS: xyz, rpy, axis, mass, com per joint, base to wrist) at joints q (degrees)."""
    import numpy as np

    rotation, origin = np.eye(3), np.zeros(3)
    axes, pivots = [], []
    for (xyz, rpy, axis, _mass, _com), angle in zip(links, q_deg, strict=True):
        origin = origin + rotation @ np.asarray(xyz, dtype=np.float64)
        rotation = rotation @ _rpy(*rpy)
        axes.append(rotation @ np.asarray(axis, dtype=np.float64))
        pivots.append(origin)
        rotation = rotation @ _axis_rotation(np.asarray(axis, dtype=np.float64), math.radians(angle))
    return origin + rotation @ np.asarray(tcp_m, dtype=np.float64), rotation, axes, pivots


def wrench_torques(axes: list[Any], pivots: list[Any], point: Any, force: Any) -> list[float]:
    """J^T F for revolute joints: each joint's torque for the hand to push with `force` (N, base frame) at `point`."""
    import numpy as np

    return [float(a @ np.cross(point - p, force)) for a, p in zip(axes, pivots)]


class PullController:
    """One arm's pull: the extra joint torques to add to the feed-forward this tick (see PAH_PULL_FORCE_N).

    idle -> (gripper holding) wait -> (held PULL_DELAY_S) pull -> (travel, time) spent -> (gripper lets go) idle.
    Any tick without holding drops straight to idle with no added torque.
    """

    def __init__(self, side: str, settings: Settings, links: Any, say: Any = None) -> None:
        self.side = side
        self.force_n = settings.pull_force_n[side]
        self.direction = settings.pull_dir[side]
        self.max_s = settings.pull_max_s
        self.travel_m = settings.pull_travel_m
        self.tau_cap = list(settings.pull_tau_cap_nm)
        self.tcp = list(settings.pull_tcp_m)
        self.links = links
        self.say = say or (lambda message: _say(message))  # looked up per call (benches capture the log)
        self.state = "idle"
        self.t_hold = self.t0 = 0.0
        self.p0: Any = None
        self.d: Any = None
        self.peak_travel = 0.0
        self.last_force_n = 0.0
        self.last_tau: list[float] = [0.0] * len(links)
        self.pulls = 0

    def _end(self, why: str, now: float) -> None:
        self.say(
            f"pull {self.side}: end ({why}) after {now - self.t0:.2f} s, moved {1e3 * self.peak_travel:.1f} mm along "
            f"the pull"
        )

    def torques(self, q_deg: Any, holding: bool, now: float) -> list[float]:
        import numpy as np

        zero = [0.0] * len(self.links)
        self.last_force_n, self.last_tau = 0.0, zero
        if not holding:
            if self.state == "pull":
                self._end("gripper let go", now)
            self.state = "idle"
            return zero
        if self.state == "idle":
            self.state, self.t_hold = "wait", now
        if self.state == "wait" and now - self.t_hold >= PULL_DELAY_S:
            p, rot, _, _ = arm_kinematics(self.links, q_deg, self.tcp)
            self.d = -rot[:, 0] if self.direction == "tool" else np.asarray(self.direction, dtype=np.float64)
            self.state, self.t0, self.p0, self.peak_travel = "pull", now, p, 0.0
            self.pulls += 1
            self.say(
                f"pull {self.side}: start {self.force_n:g} N along {np.round(self.d, 3).tolist()} at tool point "
                f"{np.round(p, 3).tolist()} m"
            )
        if self.state != "pull":
            return zero
        p, _, axes, pivots = arm_kinematics(self.links, q_deg, self.tcp)
        travel = float((p - self.p0) @ self.d)
        self.peak_travel = max(self.peak_travel, travel)
        if travel >= self.travel_m:
            self._end("moved: out, or nothing held it", now)
            self.state = "spent"
            return zero
        if now - self.t0 >= self.max_s:
            self._end("time limit", now)
            self.state = "spent"
            return zero
        force = self.force_n * min(1.0, (now - self.t0) / PULL_RAMP_S)
        tau = wrench_torques(axes, pivots, p, force * self.d)
        tau = [max(-c, min(c, t)) for t, c in zip(tau, self.tau_cap)]
        self.last_force_n, self.last_tau = force, tau
        return tau


class _Stats:
    """Per-arm tick durations, summarised to the station log every SUMMARY_EVERY_S."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.durations: dict[str, list[float]] = {}
        self.capped: dict[str, int] = {}
        self.last = time.monotonic()

    def add(self, arm: str, dt: float, period: float, capped: int) -> None:
        with self.lock:
            self.durations.setdefault(arm, []).append(dt)
            self.capped[arm] = self.capped.get(arm, 0) + capped
            now = time.monotonic()
            if now - self.last < SUMMARY_EVERY_S:
                return
            span, self.last = now - self.last, now
            parts = []
            for name, ds in sorted(self.durations.items()):
                ds.sort()
                n = len(ds)
                p50, p95, top = ds[n // 2], ds[min(n - 1, int(0.95 * n))], ds[-1]
                late = sum(d > period for d in ds)
                parts.append(
                    f"{name}: {n / span:.1f} Hz, tick p50 {1e3 * p50:.1f} ms p95 {1e3 * p95:.1f} ms max "
                    f"{1e3 * top:.1f} ms, {late} over the {1e3 * period:.1f} ms period, "
                    f"{self.capped.get(name, 0)} torque-capped joint-ticks"
                )
            self.durations, self.capped = {}, {}
        _say("tick " + "; ".join(parts))


def patch_damiao(damiao: Any, settings: Settings) -> None:
    """Rescale the per-command constants; torque cap and gravity scale in DamiaoChain.send_action."""
    hz = settings.tick_hz if settings.tick_hz is not None else 30.0
    if settings.tick_hz is not None or settings.ff_tau_s is not None:
        old_alpha, old_step = damiao.FF_ALPHA, damiao.SYNC_STEP_DEG
        if settings.ff_tau_s is not None:
            damiao.FF_ALPHA = 1.0 - math.exp(-1.0 / (hz * settings.ff_tau_s))
        else:
            damiao.FF_ALPHA = ff_alpha_at(hz, old_alpha)
        damiao.SYNC_STEP_DEG = old_step * 30.0 / hz
        _say(
            f"damiao FF_ALPHA {old_alpha:g} -> {damiao.FF_ALPHA:.4f}, SYNC_STEP_DEG {old_step:g} -> "
            f"{damiao.SYNC_STEP_DEG:.3f} at {hz:g} Hz"
        )
    _patch_torque(damiao, settings)
    if settings.pull_force_n:
        patch_pull(damiao.DamiaoChain, settings)  # last: it wraps the (scaled) gravity, and is not scaled itself


def _patch_torque(damiao: Any, settings: Settings) -> None:
    cap, scale = settings.torque_cap_nm, settings.gravity_scale
    chain_cls = damiao.DamiaoChain
    if cap is None and scale is None:
        return
    if not hasattr(chain_cls, "send_action"):
        _say("DamiaoChain.send_action not found: torque patch skipped")
        return
    original = chain_cls.send_action

    def send_action(self: Any, goal_deg: dict[str, float]) -> dict[str, float]:
        reading = getattr(self, "_reading", None)
        if reading is not None and cap is not None and getattr(self, "_synced", True):
            q = reading.q_deg
            goal_deg = dict(goal_deg)
            hits = 0
            for i, joint in enumerate(self._joints):
                kp = self._gains[joint][0]
                want = float(goal_deg[joint])
                got = cap_goal_deg(want, q[i], kp, cap[i])
                if got != want:
                    hits += 1
                goal_deg[joint] = got
            self._pah_capped = getattr(self, "_pah_capped", 0) + hits
        return original(self, goal_deg)

    chain_cls.send_action = send_action
    if scale is not None:
        original_init = chain_cls.__init__

        def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
            original_init(self, *args, **kwargs)
            raw = self._gravity
            self._gravity = lambda q: tuple(scale * g for g in raw(q))

        chain_cls.__init__ = __init__
    _say(
        f"torque: cap {cap if cap is not None else 'off'} N.m, gravity scale "
        f"{scale if scale is not None else 'unchanged'}"
    )


def patch_pull(chain_cls: Any, settings: Settings) -> None:
    """Each arm's gravity feed-forward plus its PullController's torques (the MIT frame's tau_ff, every tick)."""
    original_init = chain_cls.__init__
    if not all(hasattr(chain_cls, a) for a in ("__init__", "send_action")):
        _say("DamiaoChain has changed: pull patch skipped")
        return

    def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        side = getattr(self, "name", None)
        if side not in settings.pull_force_n or not hasattr(self, "_gravity"):
            return
        from robots import metal

        if len(metal.LINKS) != JOINTS:
            _say(f"pull {side}: robots.metal.LINKS has {len(metal.LINKS)} joints, not {JOINTS}: pull off")
            return
        pull = PullController(side, settings, metal.LINKS)
        raw = self._gravity
        said: set[str] = set()

        def gravity_and_pull(q_deg: Any) -> tuple[float, ...]:
            base = tuple(raw(q_deg))
            try:
                controller = getattr(getattr(self, "gripper", None), "controller", None)
                holding = bool(getattr(controller, "holding", False)) and not getattr(self.gripper, "error", "")
                extra = pull.torques(tuple(q_deg)[:JOINTS], holding, time.monotonic())
            except Exception as exc:  # noqa: BLE001 - never stop the arm over the pull; gravity alone
                kind = type(exc).__name__
                if kind not in said:
                    said.add(kind)
                    _say(f"pull {side}: failed, gravity only: {kind}: {exc}")
                pull.state = "spent"
                return base
            return tuple(b + e for b, e in zip(base, extra, strict=True))

        self._gravity = gravity_and_pull
        self._pah_pull = pull

    chain_cls.__init__ = __init__
    _say(
        f"pull: force {settings.pull_force_n} N, direction {settings.pull_dir}, max {settings.pull_max_s:g} s, travel "
        f"stop {1e3 * settings.pull_travel_m:g} mm, joint caps {settings.pull_tau_cap_nm} N.m, tool point "
        f"{settings.pull_tcp_m} m"
    )


def patch_motor_arm(motor_arm: Any, settings: Settings) -> None:
    """Time every bus tick; optional CSV of the measured joints and torques."""
    if settings.tick_hz is None and settings.tick_log is None and settings.torque_cap_nm is None:
        return
    stats = _Stats()
    writer_lock = threading.Lock()
    log_file = None
    writer = None
    if settings.tick_log:
        log_file = open(settings.tick_log, "a", newline="", buffering=1)  # noqa: SIM115 - lives with the process
        writer = csv.writer(log_file)
        if log_file.tell() == 0:
            writer.writerow(
                ["t", "arm", "tick_ms", "period_ms"]
                + [f"q{i}_deg" for i in range(7)]
                + [f"tau{i}_nm" for i in range(7)]
                + ["drops"]
            )
    original = motor_arm.MotorArm._tick

    def _tick(self: Any) -> None:
        t0 = time.perf_counter()
        try:
            original(self)
        finally:
            dt = time.perf_counter() - t0
            chain = self.chain
            capped = getattr(chain, "_pah_capped", 0)
            if capped:
                chain._pah_capped = 0
            stats.add(self.name, dt, self.period, capped)
            if writer is not None:
                reading = getattr(chain, "_reading", None)
                if reading is not None:
                    drops = chain.drops() if callable(getattr(chain, "drops", None)) else ""
                    row = [f"{time.time():.4f}", self.name, f"{1e3 * dt:.2f}", f"{1e3 * self.period:.2f}"]
                    row += [f"{x:.3f}" for x in reading.q_deg] + [f"{x:.3f}" for x in reading.torque_nm]
                    row.append(drops)
                    with writer_lock:
                        writer.writerow(row)

    motor_arm.MotorArm._tick = _tick
    _say(f"tick timing on{', CSV ' + settings.tick_log if settings.tick_log else ''}")


def patch_ik_specs(specs: Any, settings: Settings) -> None:
    """New default trajectory-solve weights for every ServeSpec built after this (the dream client builds its own)."""
    if settings.ik_traj is None:
        return
    vel, acc, jerk, hist = settings.ik_traj
    fields = specs.ServeSpec.model_fields
    for name, value in (
        ("traj_vel_weight", vel),
        ("traj_acc_weight", acc),
        ("traj_jerk_weight", jerk),
        ("traj_history_weight", hist),
    ):
        if name not in fields:
            _say(f"dream_ik ServeSpec has no {name}: IK patch skipped")
            return
    for name, value in (
        ("traj_vel_weight", vel),
        ("traj_acc_weight", acc),
        ("traj_jerk_weight", jerk),
        ("traj_history_weight", hist),
    ):
        fields[name].default = float(value)
    specs.ServeSpec.model_rebuild(force=True)
    _say(f"IK traj weights vel {vel:g} acc {acc:g} jerk {jerk:g} history x{hist:g}")


def eef_transform(settings: Settings, hook: Any, side: str, xyz: Any, rot6: Any, grip: Any, observation: Any):
    """Offset, floor, then the user hook, on one arm's chunk. Arrays in, arrays out (copies)."""
    import numpy as np

    xyz = np.array(xyz, dtype=np.float64, copy=True)
    rot6 = np.array(rot6, dtype=np.float64, copy=True)
    grip = np.array(grip, dtype=np.float64, copy=True)
    if settings.eef_offset_m and side in settings.eef_offset_m:
        xyz = xyz + np.asarray(settings.eef_offset_m[side])
    if settings.eef_z_min_m and side in settings.eef_z_min_m:
        xyz[:, 2] = np.maximum(xyz[:, 2], settings.eef_z_min_m[side])
    if hook is not None:
        new = hook(side, xyz, rot6, grip, observation)
        nx, nr, ng = (np.asarray(a, dtype=np.float64) for a in new)
        if nx.shape != xyz.shape or nr.shape != rot6.shape or ng.reshape(grip.shape).shape != grip.shape:
            raise ValueError(f"PAH_EEF_HOOK returned shapes {nx.shape} {nr.shape} {ng.shape}")
        if not (np.isfinite(nx).all() and np.isfinite(nr).all() and np.isfinite(ng).all()):
            raise ValueError("PAH_EEF_HOOK returned a non-finite value")
        xyz, rot6, grip = nx, nr, np.clip(ng.reshape(grip.shape), 0.0, 1.0)
    return xyz, rot6, grip


def patch_client_ik(client_ik: Any, settings: Settings) -> None:
    """Wrap ClientIK.solve: transform the eef answer before Dream's IK, log targets and solved joints."""
    if not (settings.eef_offset_m or settings.eef_z_min_m or settings.eef_hook or settings.eef_log):
        return
    import importlib
    import json

    import numpy as np

    hook = None
    if settings.eef_hook:
        module, _, func = settings.eef_hook.partition(":")
        hook = getattr(importlib.import_module(module), func)
    original = client_ik.ClientIK.solve
    said: set[str] = set()
    log = open(settings.eef_log, "a", buffering=1) if settings.eef_log else None  # noqa: SIM115

    def solve(self: Any, action: dict, observation: dict, *args: Any, **kwargs: Any) -> dict:
        per_key = action.get("per_key") if isinstance(action, dict) else None
        if self.head != "eef" or not isinstance(per_key, dict):
            return original(self, action, observation, *args, **kwargs)
        record: dict[str, Any] = {"t": time.time()}
        try:
            new_per_key = dict(per_key)
            for side in self._solver.sides:
                keys = (f"action.{side}_eef_xyz", f"action.{side}_eef_rot6", f"action.{side}_grip")
                if not all(k in per_key for k in keys):
                    continue
                xyz, rot6, grip = (np.asarray(per_key[k], dtype=np.float64) for k in keys)
                grip2 = grip[:, None] if grip.ndim == 1 else grip
                nx, nr, ng = eef_transform(settings, hook, side, xyz, rot6, grip2, observation)
                new_per_key[keys[0]], new_per_key[keys[1]] = nx, nr
                new_per_key[keys[2]] = ng[:, 0] if grip.ndim == 1 else ng
                if log is not None:
                    record[side] = {"dream_xyz": xyz.round(4).tolist(), "ik_xyz": nx.round(4).tolist(),
                                    "rot6": nr.round(4).tolist(), "grip": ng.round(3).ravel().tolist()}
            action = {**action, "per_key": new_per_key}
        except Exception as exc:  # noqa: BLE001 - this chunk runs as Dream sent it
            kind = type(exc).__name__
            if kind not in said:
                said.add(kind)
                _say(f"eef override failed, chunk unmodified: {kind}: {exc}")
        out = original(self, action, observation, *args, **kwargs)
        if log is not None:
            for side in self._solver.sides:
                key = client_ik.SIDES[side][0]
                if key in out and side in record:
                    record[side]["q"] = np.asarray(out[key]).round(4).tolist()
            log.write(json.dumps(record) + "\n")
        return out

    client_ik.ClientIK.solve = solve
    _say(
        f"eef override: offset {settings.eef_offset_m}, z floor {settings.eef_z_min_m}, hook {settings.eef_hook}, "
        f"log {settings.eef_log}"
    )


PATCHES = {
    "robots.metal": patch_metal,
    "robots.damiao": patch_damiao,
    "robots.motor_arm": patch_motor_arm,
    "dream_ik.specs": patch_ik_specs,
    "dream.client_ik": patch_client_ik,
}


# ------------------------------------------------------------------------------------------------------ import hook


class _AfterImport(importlib.abc.MetaPathFinder):
    """Runs a patch right after a module in PATCHES finishes executing, before anything else reads it."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.busy: set[str] = set()

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:
        if fullname not in PATCHES or fullname in self.busy:
            return None
        self.busy.add(fullname)
        try:
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        finally:
            self.busy.discard(fullname)
        if spec is None or spec.loader is None:
            return spec
        loader, patch, settings = spec.loader, PATCHES[fullname], self.settings

        class _Loader(importlib.abc.Loader):
            def create_module(self, spec: Any) -> Any:
                return loader.create_module(spec)

            def exec_module(self, module: Any) -> None:
                loader.exec_module(module)
                try:
                    patch(module, settings)
                except Exception as exc:  # noqa: BLE001 - never stop Dream's station over a patch
                    _say(f"{fullname}: patch failed, running Dream's own code: {type(exc).__name__}: {exc}")

        spec.loader = _Loader()
        return spec


def _dreamlabs_version() -> str | None:
    try:
        from importlib import metadata

        return metadata.version("dreamlabs")
    except Exception:  # noqa: BLE001
        return None


def install() -> Settings | None:
    """Reads the environment and installs the import hook; returns the settings, or None when nothing is on."""
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        _say(f"bad setting, NOTHING patched: {exc}")
        return None
    if not settings.active():
        return None
    version = _dreamlabs_version()
    if version != TESTED_DREAMLABS:
        _say(f"dreamlabs {version} is not the tested {TESTED_DREAMLABS}: NOTHING patched")
        return None
    already = [name for name in PATCHES if name in sys.modules]
    if already:
        _say(f"{already} imported before the hook: NOTHING patched")
        return None
    sys.meta_path.insert(0, _AfterImport(settings))
    _say(f"on: {settings}")
    return settings
