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


@dataclasses.dataclass
class Settings:
    tick_hz: float | None = None
    torque_cap_nm: list[float] | None = None
    gravity_scale: float | None = None
    ik_traj: list[float] | None = None
    tick_log: str | None = None
    ff_tau_s: float | None = None

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
    cap, scale = settings.torque_cap_nm, settings.gravity_scale
    if cap is None and scale is None:
        return
    chain_cls = damiao.DamiaoChain
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


PATCHES = {
    "robots.metal": patch_metal,
    "robots.damiao": patch_damiao,
    "robots.motor_arm": patch_motor_arm,
    "dream_ik.specs": patch_ik_specs,
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
