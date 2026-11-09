"""Training data for the learned joint fault detector (harness.joint_fault_net).

Base motion is real: every joints.jsonl the sim matches recorded (Dream driving both YAM arms, 20 Hz). Each clip is
6 s of one joint, shifted to sit inside a limit set (the YAM's or the Metal arm's), with sensor noise at the CAN
reply's resolution. Then one of these is injected, and labels come from the resulting signals, not from any rule:

  normal       nothing injected
  near_miss    a fast move toward a limit that stops 1-12 deg short (hard negative: no fault)
  limit_run    a move that runs past the limit (fault from the sample the joint is past it)
  blocked      the joint stops (or crawls) while its command keeps going; torque climbs (fault from the contact)
  oscillation  a growing 1.5-6 Hz oscillation (fault once its amplitude is over 2 deg)
  overload     torque climbs to the joint's torque limit while the joint sags (fault at >= 90% of the limit)
  overspeed    a burst over half the motor's top speed (fault while over it)

`now[t]` is the fault state at t; `soon[t]` is any fault in [t, t + HORIZON]. Clips are split by match file, so a test
clip's motion was never seen in training.
"""

from __future__ import annotations

import glob
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from harness.joint_fault_net import DT, HORIZON, WINDOW, features
from harness.joint_observer import METAL_LIMITS_DEG, YAM_LIMITS_DEG

JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_yaw", "wrist_roll")
YAM_TAU_MAX = 10.0
METAL_TAU_MAX = {"shoulder_pan": 27.0, "shoulder_lift": 27.0, "elbow_flex": 27.0, "wrist_flex": 10.0,
                 "wrist_yaw": 10.0, "wrist_roll": 10.0}
VMAX_DEG_S = 573.0
CLIP = 120                       # 6 s at 20 Hz
KINDS = ("normal", "near_miss", "limit_run", "blocked", "oscillation", "overload", "overspeed")
KIND_P = (0.30, 0.16, 0.14, 0.12, 0.10, 0.09, 0.09)
FIELDS = 5                       # q, q_cmd, dq, tau, tau_cmd


def load_series(paths: list[str]) -> list[np.ndarray]:
    """Per file, arm and joint: (T, 5) arrays on a 20 Hz grid, one per stretch of increasing sim time."""
    out = []
    for path in paths:
        rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
        for arm in ("left", "right"):
            for name in JOINTS:
                t_list, v_list = [], []

                def flush():
                    if len(t_list) * DT >= CLIP * DT + 1.0:
                        t = np.array(t_list)
                        v = np.array(v_list, dtype=np.float64)
                        grid = np.arange(t[0], t[-1], DT)
                        if len(grid) >= CLIP:
                            out.append(np.stack([np.interp(grid, t, v[:, k]) for k in range(FIELDS)], axis=1))
                    t_list.clear()
                    v_list.clear()

                for r in rows:
                    j = (r.get("arms") or {}).get(arm, {}).get(name)
                    if j is None or any(x is None for x in j):
                        continue
                    t = r["t"]
                    if t_list and t < t_list[-1] - 0.5:
                        flush()
                    if t_list and t <= t_list[-1]:
                        continue
                    t_list.append(t)
                    v_list.append(j[:FIELDS])
                flush()
    return out


def match_files(root: str) -> list[str]:
    return sorted(glob.glob(str(Path(root) / "**" / "joints.jsonl"), recursive=True))


def _minjerk(n: int) -> np.ndarray:
    s = np.linspace(0.0, 1.0, n)
    return 10 * s ** 3 - 15 * s ** 4 + 6 * s ** 5


@dataclass
class Clip:
    raw: np.ndarray        # (CLIP, 5)
    now: np.ndarray        # (CLIP,) bool
    kind: str
    lo: float
    hi: float
    tau_max: float
    onset: int             # first fault sample, -1 when none


def make_clip(series: list[np.ndarray], rng: np.random.Generator, kind: str | None = None) -> Clip:
    base = series[rng.integers(len(series))]
    start = rng.integers(0, len(base) - CLIP + 1)
    q, qc, dq, tau, tauc = (base[start:start + CLIP, k].copy() for k in range(FIELDS))
    name = JOINTS[rng.integers(len(JOINTS))]
    if rng.random() < 0.5:
        (lo, hi), tmax = YAM_LIMITS_DEG[name], YAM_TAU_MAX
    else:
        (lo, hi), tmax = METAL_LIMITS_DEG[name], METAL_TAU_MAX[name]
    tau, tauc = tau * tmax / YAM_TAU_MAX, tauc * tmax / YAM_TAU_MAX
    kind = kind or KINDS[rng.choice(len(KINDS), p=KIND_P)]
    t = np.arange(CLIP)
    s = int(rng.integers(WINDOW + 8, CLIP - 30))         # injection sample
    # Place the clip in the range: random spot, or near a limit for the limit kinds.
    span_lo, span_hi = q.min(), q.max()
    side = 1 if rng.random() < 0.5 else -1
    bound = hi if side > 0 else lo
    if kind in ("limit_run", "near_miss"):
        d0 = rng.uniform(5.0, 60.0)
        shift = (bound - side * d0) - q[s]
    else:
        room_lo, room_hi = lo + 5 - span_lo, hi - 5 - span_hi
        shift = rng.uniform(room_lo, room_hi) if room_hi > room_lo else (lo + hi) / 2 - q.mean()
    q += shift
    qc += shift
    mask = np.zeros(CLIP, dtype=bool)
    after = t >= s
    n_after = CLIP - s
    if kind == "limit_run":
        v = rng.uniform(15.0, 250.0)
        a = rng.uniform(0.0, 300.0)
        tt = (t[after] - s) * DT
        overshoot = rng.uniform(3.0, 25.0)
        dist = abs(bound - q[s]) + overshoot
        move = np.minimum(v * tt + 0.5 * a * tt ** 2, dist)
        vel = np.where(v * tt + 0.5 * a * tt ** 2 < dist, v + a * tt, 0.0)
        q[after] += side * move
        qc[after] += side * np.minimum(move + v * 0.1, dist)
        dq[after] += side * vel
    elif kind == "near_miss":
        stop = rng.uniform(1.0, 12.0)
        dist = abs(bound - q[s]) - stop
        n = int(rng.integers(8, 40))
        prof = np.concatenate([_minjerk(n), np.ones(max(0, n_after - n))])[:n_after]
        q[after] += side * dist * prof
        qc[after] += side * dist * np.concatenate([_minjerk(max(2, n - 2)), np.ones(n_after)])[:n_after]
        dq[after] += side * dist * np.gradient(prof) / DT
    elif kind == "blocked":
        crawl = rng.uniform(0.0, 0.25) if rng.random() < 0.4 else 0.0
        if np.ptp(qc[s:s + 20]) < 10.0:
            v = rng.uniform(15.0, 80.0) * (1 if rng.random() < 0.5 else -1)
            qc[after] += v * (t[after] - s) * DT
        hold = q[s] + crawl * (qc[after] - qc[s])
        q[after] = hold
        dq[after] = np.gradient(hold) / DT
        push = np.clip(0.5 * tmax / 10.0 * (qc[after] - q[after]), -tmax, tmax)
        tauc[after] = tauc[s] + push
        # The motor delivers what is commanded, or (an outside push or a current limit) measures less or more.
        tau[after] = tau[s] + push * (1.0 if rng.random() < 0.5 else rng.uniform(0.5, 1.4))
        mask[after] = True
    elif kind == "oscillation":
        f = rng.uniform(1.5, 6.0)
        amp_max = rng.uniform(3.0, 15.0)
        ramp = rng.uniform(0.5, 2.0)
        tt = (t[after] - s) * DT
        amp = amp_max * np.minimum(1.0, tt / ramp)
        x = amp * np.sin(2 * np.pi * f * tt)
        q[after] += x
        dq[after] += amp * 2 * np.pi * f * np.cos(2 * np.pi * f * tt)
        tau[after] -= 0.3 * tmax / 10.0 * x
        tauc[after] -= 0.3 * tmax / 10.0 * x
        mask[after] = amp >= 2.0
    elif kind == "overload":
        target = rng.uniform(0.95, 1.3) * tmax * (1 if rng.random() < 0.5 else -1)
        n = int(rng.integers(5, 30))
        ramp = np.minimum(1.0, (t[after] - s) / n)
        load = np.clip(tau[s] + (target - tau[s]) * ramp, -tmax, tmax)
        tau[after] = load
        tauc[after] = load
        sag = rng.uniform(1.0, 6.0) * ramp * -np.sign(target)
        q[after] += sag
        dq[after] += np.gradient(sag) / DT
    elif kind == "overspeed":
        peak = rng.uniform(0.55, 1.0) * VMAX_DEG_S
        disp = rng.uniform(30.0, 90.0)
        n = max(3, int(round(1.875 * disp / peak / DT)))
        room_up, room_dn = hi - q[s:].max(), q[s:].min() - lo
        sign = 1 if room_up > room_dn else -1
        prof = np.concatenate([_minjerk(n), np.ones(max(0, n_after - n))])[:n_after]
        q[after] += sign * disp * prof
        qc[after] += sign * disp * prof
        dq[after] += sign * disp * np.gradient(prof) / DT
    # CAN-resolution noise (Damiao MIT reply: q 16 bit over +-12.5 rad, dq 12 bit over +-30 rad/s, tau 12 bit)
    q += rng.normal(0, 0.02, CLIP)
    dq += rng.normal(0, 0.8, CLIP)
    tau += rng.normal(0, 0.003 * tmax, CLIP)
    # In the sim the measured torque is the commanded one; on a motor it is a current reading with its own error.
    tau += rng.normal(0, rng.uniform(0.0, 0.03) * tmax, CLIP)
    # Labels from the signals.
    now = mask | (q > hi) | (q < lo) | (np.abs(dq) > 0.5 * VMAX_DEG_S) | (np.abs(tau) >= 0.9 * tmax)
    idx = np.flatnonzero(now)
    return Clip(np.stack([q, qc, dq, tau, tauc], axis=1), now, kind, lo, hi, tmax, int(idx[0]) if len(idx) else -1)


def soon_labels(now: np.ndarray) -> np.ndarray:
    out = now.copy()
    for k in range(1, HORIZON + 1):
        out[:-k] |= now[k:]
    return out


def clip_features(c: Clip) -> np.ndarray:
    return features(c.raw[:, 0], c.raw[:, 1], c.raw[:, 2], c.raw[:, 3], c.raw[:, 4], c.lo, c.hi, c.tau_max)


def build(series: list[np.ndarray], n: int, seed: int) -> tuple[list[Clip], np.ndarray, np.ndarray, np.ndarray]:
    """n clips; returns them with X (n, CLIP, 8), now (n, CLIP) and soon (n, CLIP)."""
    rng = np.random.default_rng(seed)
    clips = [make_clip(series, rng) for _ in range(n)]
    X = np.stack([clip_features(c) for c in clips])
    now = np.stack([c.now for c in clips])
    return clips, X, now, np.stack([soon_labels(c.now) for c in clips])
