"""Bench Dream's Metal driver on the fake bus: tick time and tracking at the tick rate PAH_METAL_TICK_HZ asks for.

    PAH_METAL_TICK_HZ=60 python station_addons/bench_metal_tick.py [--seconds 6] [--json out.json]

Runs Dream's own robots.motor_arm / robots.damiao / robots.metal (dreamlabs 1.0.37) on station_addons/fake_damiao.py,
with this folder's pah_tuning patches installed exactly as sitecustomize.py installs them on the station. A feeder
thread plays what Dream's command thread does: policy rows at 20 Hz (control_hz), interpolated and written at 250 Hz.
The reference is a 0.4 Hz sweep of the shoulder, elbow and wrist (the smooth reaching a chunk carries). Reports the
tick duration distribution, the achieved tick rate, and the tracking error of the measured joints against the
continuous reference.
"""

from __future__ import annotations

import argparse
import json
import math
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

Q0 = (0.0, -40.0, 60.0, -20.0, 0.0, 0.0, 30.0)
AMP = np.radians([15.0, 20.0, 25.0, 20.0, 15.0, 15.0])
FREQ_HZ = 0.4
CONTROL_HZ = 20.0
COMMAND_HZ = 250.0


def reference(t: float) -> np.ndarray:
    return np.radians(Q0[:6]) + AMP * math.sin(2 * math.pi * FREQ_HZ * t)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--json", default=None)
    ap.add_argument("--gripper-late", type=float, default=0.5)
    a = ap.parse_args()

    bus = fake_damiao.FakeMetalBus(Q0, gripper_late=a.gripper_late)
    cfg = config.ArmConfig(can_channel="fake", gripper_open_deg=100.0, gripper_closed_deg=0.0,
                           gripper_hold_torque_nm=1.5)
    durations: list[float] = []
    original = motor_arm.MotorArm._tick

    def timed(self):  # outermost timer, after pah_tuning's own
        t0 = time.perf_counter()
        original(self)
        durations.append(time.perf_counter() - t0)

    motor_arm.MotorArm._tick = timed
    follower = motor_arm.open_arm(metal.ARM, cfg, "right", opener=lambda channel: bus)
    driver = follower.driver if hasattr(follower, "driver") else follower._driver  # the MotorArm
    # warm up onto the reference start (the startup sync ramps at 30 deg/s)
    t_start = time.perf_counter()
    stop = threading.Event()

    def feeder() -> None:
        row_t, row_prev, row_next = None, None, None
        while not stop.is_set():
            now = time.perf_counter() - t_start
            # a policy row every 1/CONTROL_HZ, linearly interpolated between rows like the command thread
            k = math.floor(now * CONTROL_HZ)
            t0, t1 = k / CONTROL_HZ, (k + 1) / CONTROL_HZ
            q0, q1 = reference(t0), reference(t1)
            q = q0 + (q1 - q0) * (now - t0) * CONTROL_HZ
            driver.write(q, 0.3)
            time.sleep(1.0 / COMMAND_HZ)

    thread = threading.Thread(target=feeder, daemon=True)
    thread.start()
    time.sleep(2.0)  # sync settles
    durations.clear()
    bus.log.clear()
    t_meas0 = time.perf_counter()
    errors = []
    while time.perf_counter() - t_meas0 < a.seconds:
        time.sleep(0.002)
    t_meas1 = time.perf_counter()
    stop.set()
    for t, q, _q_cmd in bus.log:
        ref = reference(t - t_start)
        errors.append(np.degrees(np.asarray(q[:6]) - ref))
    follower_close = getattr(follower, "close", None)
    try:
        driver.stop()
    except Exception:  # noqa: BLE001
        pass
    err = np.asarray(errors)
    d = np.sort(np.asarray(durations))
    out = {
        "tick_hz_set": metal.ARM.tick_hz,
        "tick_hz_achieved": len(d) / (t_meas1 - t_meas0),
        "tick_ms_p50": 1e3 * float(np.percentile(d, 50)),
        "tick_ms_p95": 1e3 * float(np.percentile(d, 95)),
        "tick_ms_max": 1e3 * float(d[-1]),
        "over_period": int((d > 1.0 / metal.ARM.tick_hz).sum()),
        "ticks": int(len(d)),
        "track_rms_deg": [round(float(x), 3) for x in np.sqrt((err**2).mean(axis=0))],
        "track_max_deg": [round(float(x), 3) for x in np.abs(err).max(axis=0)],
        "gripper_late": a.gripper_late,
    }
    print(json.dumps(out))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(out, f, indent=1)
    del follower_close


if __name__ == "__main__":
    main()
