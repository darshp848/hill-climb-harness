"""Replay a station trace's commanded joints through Dream's Metal driver on a MuJoCo Metal arm (fake_damiao +
metal_plant), with the PAH_* tuning from the environment, and score tracking against the same commands.

    PAH_FF_TAU_S=0.15 python station_addons/bench_replay.py --trace station_addons/data/run3_2026-11-08.csv \
        --arm r --seconds 60 [--armature 1.0 --damping 1.0 --friction 1.0] [--json out.json]

The trace is Dream's last-run.csv (one row per 20 Hz policy tick: commanded_* the joints the loop submitted,
measured_* what the arm read). The feeder plays the rows as Dream's command thread does (linear between rows,
written at 250 Hz). Scores: raw RMS of measured - commanded per joint, the lag that minimises it, and the RMS at that
lag. The same scores of the REAL arm on the same window come out as `real_*`, so the plant can be fitted to the
station first (sweep --armature/--damping/--friction with Dream's settings) and the tuning judged after.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pah_tuning  # noqa: E402

pah_tuning.install()

import numpy as np  # noqa: E402
from robots import config, metal, motor_arm  # noqa: E402

import fake_damiao  # noqa: E402
import metal_plant  # noqa: E402

COMMAND_HZ = 250.0
LAGS_S = np.arange(0.0, 0.401, 0.01)


def load_trace(path: str, arm: str):
    with open(path) as f:
        rows = list(csv.DictReader(f))
    t = np.array([float(r["t_s"]) for r in rows])
    cmd = np.array([[float(r[f"commanded_{arm}{j}"]) for j in range(6)] for r in rows])
    meas = np.array([[float(r[f"measured_{arm}{j}"]) for j in range(6)] for r in rows])
    ramp = np.array([float(r["ramp_alpha"]) for r in rows])
    grip = np.array([float(r[f"cmd_gripper_{arm}"]) for r in rows])
    return t - t[0], cmd, meas, ramp, grip


def busiest_window(t, cmd, ramp, seconds):
    """Start index of the `seconds` window, after the ramp, with the most commanded joint travel."""
    ok = np.where(ramp >= 1.0)[0]
    first = int(ok[0]) if len(ok) else 0
    travel = np.concatenate([[0.0], np.abs(np.diff(cmd, axis=0)).sum(axis=1)])
    best, best_i = -1.0, first
    for i in range(first, len(t)):
        j = np.searchsorted(t, t[i] + seconds)
        if j >= len(t):
            break
        if travel[i:j].sum() > best:
            best, best_i = travel[i:j].sum(), i
    return best_i


def scores(t_meas, q_meas, t_cmd, q_cmd):
    """Per joint: raw RMS (rad), best lag (s), RMS at that lag."""
    out = {"rms_rad": [], "lag_s": [], "rms_at_lag_rad": []}
    for j in range(6):
        ref = np.interp(t_meas, t_cmd, q_cmd[:, j])
        out["rms_rad"].append(float(np.sqrt(np.mean((q_meas[:, j] - ref) ** 2))))
        best = None
        for lag in LAGS_S:
            r = np.interp(t_meas - lag, t_cmd, q_cmd[:, j])
            e = float(np.sqrt(np.mean((q_meas[:, j] - r) ** 2)))
            if best is None or e < best[1]:
                best = (float(lag), e)
        out["lag_s"].append(best[0])
        out["rms_at_lag_rad"].append(best[1])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True)
    ap.add_argument("--arm", default="r", choices=["l", "r"])
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--start", type=int, default=None, help="trace row to start at (default: busiest window)")
    ap.add_argument("--armature", type=float, default=1.0)
    ap.add_argument("--damping", type=float, default=1.0)
    ap.add_argument("--friction", type=float, default=1.0)
    ap.add_argument("--gripper-late", type=float, default=0.5)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    t, cmd, meas, ramp, grip = load_trace(a.trace, a.arm)
    i0 = a.start if a.start is not None else busiest_window(t, cmd, ramp, a.seconds)
    i1 = int(np.searchsorted(t, t[i0] + a.seconds))
    tw, cw, mw, gw = t[i0:i1] - t[i0], cmd[i0:i1], meas[i0:i1], grip[i0:i1]

    params = metal_plant.PlantParams().scaled(a.armature, a.damping, a.friction)
    plant = metal_plant.MetalPlant(mw[0].copy(), params)
    q0_deg = tuple(np.degrees(mw[0])) + (30.0,)
    bus = fake_damiao.FakeMetalBus(q0_deg, gripper_late=a.gripper_late, plant=plant)
    cfg = config.ArmConfig(can_channel="fake", gripper_open_deg=100.0, gripper_closed_deg=0.0,
                           gripper_hold_torque_nm=1.5)
    durations: list[float] = []
    original = motor_arm.MotorArm._tick

    def timed(self):
        t_0 = time.perf_counter()
        original(self)
        durations.append(time.perf_counter() - t_0)

    motor_arm.MotorArm._tick = timed
    follower = motor_arm.open_arm(metal.ARM, cfg, "right", opener=lambda channel: bus)
    driver = follower._driver
    stop = threading.Event()
    # hold the first command until the startup sync settles, then play the window in real time
    for _ in range(int(1.5 * COMMAND_HZ)):
        driver.write(cw[0], float(gw[0]))
        time.sleep(1.0 / COMMAND_HZ)
    bus.log.clear()
    durations.clear()
    t_play0 = time.perf_counter()

    def feeder() -> None:
        while not stop.is_set():
            now = time.perf_counter() - t_play0
            if now >= tw[-1]:
                break
            q = np.array([np.interp(now, tw, cw[:, j]) for j in range(6)])
            driver.write(q, float(np.interp(now, tw, gw)))
            time.sleep(1.0 / COMMAND_HZ)

    th = threading.Thread(target=feeder, daemon=True)
    th.start()
    th.join(timeout=tw[-1] + 5.0)
    stop.set()
    t_play1 = time.perf_counter()
    taus = []
    t_sim = np.array([x[0] - t_play0 for x in bus.log])
    q_sim = np.array([x[1][:6] for x in bus.log])
    keep = (t_sim > 0.5) & (t_sim < tw[-1] - 0.5)
    try:
        driver.stop()
    except Exception:  # noqa: BLE001
        pass
    sim = scores(t_sim[keep], q_sim[keep], tw, cw)
    real_keep = (tw > 0.5) & (tw < tw[-1] - 0.5)
    real = scores(tw[real_keep], mw[real_keep], tw, cw)
    def hand_err(t_m, q_m):
        ref = np.stack([np.interp(t_m, tw, cw[:, j]) for j in range(6)], axis=1)
        e = np.array([plant.hand_xyz(qm) - plant.hand_xyz(qr) for qm, qr in zip(q_m[::5], ref[::5])])
        return {"hand_mean_m": [float(x) for x in e.mean(axis=0)],
                "hand_rms_m": float(np.sqrt((e**2).sum(axis=1).mean()))}

    sim |= hand_err(t_sim[keep], q_sim[keep])
    real |= hand_err(tw[real_keep], mw[real_keep])
    d = np.asarray(durations)
    out = {
        "settings": {k: os.environ.get(k) for k in sorted(os.environ) if k.startswith("PAH_")},
        "plant": {"armature": a.armature, "damping": a.damping, "friction": a.friction},
        "arm": a.arm, "start_row": i0, "seconds": float(tw[-1]),
        "tick_hz_set": metal.ARM.tick_hz,
        "tick_hz_achieved": float(len(d) / (t_play1 - t_play0)),
        "tick_ms_p50": float(1e3 * np.percentile(d, 50)), "tick_ms_p95": float(1e3 * np.percentile(d, 95)),
        "sim": sim, "real": real,
        "sim_rms_mean_rad": float(np.mean(sim["rms_rad"])), "real_rms_mean_rad": float(np.mean(real["rms_rad"])),
        "sim_lag_mean_s": float(np.mean(sim["lag_s"])), "real_lag_mean_s": float(np.mean(real["lag_s"])),
    }
    del taus
    print(json.dumps(out))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
