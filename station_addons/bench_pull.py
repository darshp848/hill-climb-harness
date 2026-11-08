"""Does PAH_PULL_* get a seated plug out? Dream's Metal driver (fake_damiao + metal_plant) holds the right arm at a
real grasp pose from the station (8 Nov, run starting 18:55:39 UTC, t = 60 s: the policy was holding still, joint
error ~0.4 deg, the plug never moved) and closes the gripper on an object. A plug is a stiff spring at the tool point
that pops out once the force along its pull-out axis reaches --withdraw newtons.

    PAH_PULL_FORCE_N=r:20 PAH_PULL_DIR=r:0,-1,0 python station_addons/bench_pull.py --withdraw 15 --axis 0,-1,0

Prints one JSON line: whether and when the plug came out, the peak plug force, the pull events, and how far the tool
point ended from where it was held. With no PAH_PULL_* set it is the baseline (Dream's own driver).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pah_tuning  # noqa: E402

EVENTS: list[str] = []
_say = pah_tuning._say


def _record(message: str) -> None:
    EVENTS.append(message)
    _say(message)


pah_tuning._say = _record
pah_tuning.install()

import numpy as np  # noqa: E402
from robots import config, metal, motor_arm  # noqa: E402

import fake_damiao  # noqa: E402
import metal_plant  # noqa: E402

GRASP_POSE_DEG = (10.74, -118.9, 74.48, -27.8, -3.89, 7.32)  # right arm, station 8 Nov 18:56:39 UTC
COMMAND_HZ = 250.0
OPEN, CLOSED = 0.9, 0.0  # loop gripper units
OBJECT_DEG = 9.3  # jaws stop here: the 8 mm the station read on the cord (0.082 x 113.8 deg)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--withdraw", type=float, default=15.0, help="plug pull-out force, N (0: no plug)")
    ap.add_argument("--axis", default="0,-1,0", help="plug pull-out axis, right arm base frame")
    ap.add_argument("--stiffness", type=float, default=5000.0, help="plug spring, N/m")
    ap.add_argument("--seconds", type=float, default=6.0, help="after the gripper closes")
    ap.add_argument("--release-at", type=float, default=None, help="open the gripper this long after closing, s")
    ap.add_argument("--hold-torque", type=float, default=2.0)
    a = ap.parse_args()
    axis = np.array([float(x) for x in a.axis.split(",")])
    axis /= np.linalg.norm(axis)
    tcp = pah_tuning.PULL_TCP_M

    q0 = np.radians(GRASP_POSE_DEG)
    plant = metal_plant.MetalPlant(q0.copy(), metal_plant.PlantParams().scaled(4.0, 9.0, 3.0))  # the fitted plant
    plug = {"anchor": None, "out_at": None, "peak_n": 0.0, "force": np.zeros(3)}

    def external(p: metal_plant.MetalPlant) -> np.ndarray:
        if plug["anchor"] is None or plug["out_at"] is not None:
            plug["force"] = np.zeros(3)
            return np.zeros(6)
        x = p.tool_point(tcp)
        f = -a.stiffness * (x - plug["anchor"])  # the socket's pull on the plug (and so on the arm)
        along = float(-(f @ axis))  # how hard the arm pulls it out
        plug["peak_n"] = max(plug["peak_n"], along)
        plug["force"] = f
        if a.withdraw > 0 and along >= a.withdraw:
            plug["out_at"] = time.perf_counter()
            return np.zeros(6)
        return p.point_jacobian(x).T @ f

    plant.external = external
    bus = fake_damiao.FakeMetalBus(tuple(GRASP_POSE_DEG) + (100.0,), gripper_late=0.5, plant=plant,
                                   gripper_block_deg=OBJECT_DEG)
    cfg = config.ArmConfig(can_channel="fake", gripper_open_deg=113.8, gripper_closed_deg=0.0,
                           gripper_hold_torque_nm=a.hold_torque)
    follower = motor_arm.open_arm(metal.ARM, cfg, "right", opener=lambda channel: bus)
    driver = follower._driver
    chain = getattr(driver, "chain", None) or getattr(follower, "chain", None)
    stop = threading.Event()
    grip = {"cmd": OPEN}

    def feeder() -> None:
        while not stop.is_set():
            driver.write(q0, grip["cmd"])
            time.sleep(1.0 / COMMAND_HZ)

    threading.Thread(target=feeder, daemon=True).start()
    time.sleep(2.0)  # startup sync, settle at the pose with the jaws open
    x_hold = plant.tool_point(tcp).copy()
    plug["anchor"] = x_hold.copy() if a.withdraw > 0 else None
    grip["cmd"] = CLOSED
    t_close = time.perf_counter()
    trace = []
    while time.perf_counter() - t_close < a.seconds:
        now = time.perf_counter() - t_close
        if a.release_at is not None and now >= a.release_at:
            grip["cmd"] = OPEN
        pull = getattr(chain, "_pah_pull", None) if chain is not None else None
        trace.append((now, float(-(plug["force"] @ axis)), float((plant.tool_point(tcp) - x_hold) @ axis),
                      pull.last_force_n if pull else 0.0, pull.state if pull else "off"))
        time.sleep(0.01)
    stop.set()
    x_end = plant.tool_point(tcp)
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            driver.stop()
        except Exception:  # noqa: BLE001
            pass
    tr = np.array([(t, f, d, pf) for t, f, d, pf, _ in trace])
    out = {
        "settings": {k: os.environ.get(k) for k in sorted(os.environ) if k.startswith("PAH_PULL")},
        "withdraw_n": a.withdraw, "axis": axis.round(3).tolist(), "hold_torque_nm": a.hold_torque,
        "plug_out": plug["out_at"] is not None,
        "plug_out_after_close_s": None if plug["out_at"] is None else round(plug["out_at"] - t_close, 2),
        "peak_plug_force_n": round(plug["peak_n"], 1),
        "max_tool_move_along_axis_mm": round(1e3 * float(tr[:, 2].max()), 1),
        "end_tool_offset_mm": round(1e3 * float(np.linalg.norm(x_end - x_hold)), 1),
        "max_commanded_pull_n": round(float(tr[:, 3].max()), 1),
        "states_seen": sorted({s for *_, s in trace}),
        "events": [e for e in EVENTS if e.startswith("pull ")],
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
