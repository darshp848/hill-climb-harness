"""Sim check of the end-effector override: Dream's real client IK (dream.client_ik.ClientIK, dreamlabs-client 1.1.3)
solves chunks built from a station trace, with and without the PAH_EEF_* override, and the solved joints' tool
positions are compared.

    PAH_EEF_OFFSET_M=0,0,-0.02 python station_addons/bench_eef_override.py --trace station_addons/data/run3_2026-11-08.csv

Each chunk is 48 rows of the trace's commanded joints, turned into tool poses with Dream's own forward kinematics,
i.e. exactly the eef answer that would have produced those joints. With no override the IK should give the same
hand path back (the IK's own residual); with an offset the solved hand path should move by the offset.
Prints per arm: mean tool shift (cm, x y z), RMS hand error vs the intended path (cm), and solve time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pah_tuning  # noqa: E402

settings = pah_tuning.install()

import numpy as np  # noqa: E402
from dream import client_ik  # noqa: E402
from dream.models import Model  # noqa: E402
from dream.smoothing import SeamSmoother  # noqa: E402

import bench_replay  # noqa: E402

ROWS = 48


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True)
    ap.add_argument("--chunks", type=int, default=8)
    ap.add_argument("--solve", default="tick", choices=["tick", "traj"])
    a = ap.parse_args()

    model = Model(id="dream-1-ft:sim-test", owner="mine", base_model="dreamlabs/dream-1-beta", embodiment="emb_sim",
                  servable=True, created_at=None)
    ik = client_ik.ClientIK({"arm": "metal", "head": "eef", "solve": a.solve}, 20.0, None, model)
    if a.solve == "traj":
        ik.traj_ready.wait(120)
    sides = ik._solver.sides
    data = {s[0]: bench_replay.load_trace(a.trace, s[0]) for s in sides}
    t, _, _, ramp, _ = data["r"]
    starts = np.linspace(np.where(ramp >= 1.0)[0][0], len(t) - ROWS - 1, a.chunks).astype(int)
    shifts = {s: [] for s in sides}
    errs = {s: [] for s in sides}
    solve_ms = []
    for i0 in starts:
        per_key, obs = {}, {}
        intended = {}
        for side in sides:
            _, cmd, meas, _, grip = data[side[0]]
            q = cmd[i0:i0 + ROWS]
            poses = np.stack([ik._solver.fk(qi[None], side)[0] if not ik._one_arm else ik._solver.fk(qi[None])[0]
                              for qi in q])
            intended[side] = poses[:, :3, 3]
            per_key[f"action.{side}_eef_xyz"] = poses[:, :3, 3]
            per_key[f"action.{side}_eef_rot6"] = np.concatenate([poses[:, :3, 0], poses[:, :3, 1]], axis=1)
            per_key[f"action.{side}_grip"] = grip[i0:i0 + ROWS]
            obs[client_ik.SIDES[side][1]] = cmd[i0]
            obs[f"state.{side}_grip"] = float(grip[i0])
        t0 = time.perf_counter()
        out = ik.solve({"per_key": per_key}, obs, SeamSmoother(20.0), 0.0, None)
        solve_ms.append(1e3 * (time.perf_counter() - t0))
        for side in sides:
            q = np.asarray(out[client_ik.SIDES[side][0]])[:, :6]
            got = np.stack([ik._solver.fk(qi[None], side)[0][:3, 3] if not ik._one_arm
                            else ik._solver.fk(qi[None])[0][:3, 3] for qi in q])
            want = intended[side] + (np.asarray(settings.eef_offset_m[side]) if settings and settings.eef_offset_m
                                     and side in settings.eef_offset_m else 0.0)
            shifts[side].append((got - intended[side])[8:].mean(axis=0))
            errs[side].append(np.sqrt(((got - want)[8:] ** 2).sum(axis=1).mean()))
    out = {"settings": {k: v for k, v in os.environ.items() if k.startswith("PAH_")}, "solve": a.solve,
           "solve_ms_median": float(np.median(solve_ms))}
    for side in sides:
        out[side] = {"mean_shift_cm": [round(100 * float(x), 2) for x in np.mean(shifts[side], axis=0)],
                     "hand_err_vs_target_cm": round(100 * float(np.mean(errs[side])), 2)}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
