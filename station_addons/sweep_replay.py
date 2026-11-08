"""Run bench_replay.py over a grid (plant fit, then tuning settings) with N runs in parallel; one JSON line per run.

    python station_addons/sweep_replay.py --stage fit  --jobs 12 --out results/fit.jsonl
    python station_addons/sweep_replay.py --stage tune --jobs 12 --out results/tune.jsonl --plants results/fit.jsonl
    python station_addons/sweep_replay.py --report results/fit.jsonl results/tune.jsonl

Every run is real time (Dream's driver threads run on the wall clock), so --jobs should stay well under the core count
(each run uses about one core); tick timings degrade when the machine is oversubscribed.

fit:  Dream's settings (no PAH_*), plant armature x damping x friction, both arms, two 40 s windows; the plants whose
      simulated tracking (raw RMS and lag) is closest to the REAL arm on the same commands are kept.
tune: the 3 best plants x the settings in SETTINGS x both arms x two windows.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
TRACE = os.path.join(HERE, "data", "run3_2026-11-08.csv")

PLANT_GRID = {"armature": [1.0, 2.0, 4.0, 8.0], "damping": [1.0, 3.0, 9.0], "friction": [1.0, 3.0]}
WINDOWS = [None, 1900]  # busiest window, and a later one
SETTINGS = {
    "dream": {},
    "ff0.3": {"PAH_FF_TAU_S": "0.3"},
    "ff0.15": {"PAH_FF_TAU_S": "0.15"},
    "tick60": {"PAH_METAL_TICK_HZ": "60"},
    "tick60_ff0.15": {"PAH_METAL_TICK_HZ": "60", "PAH_FF_TAU_S": "0.15"},
    "tick100_ff0.15": {"PAH_METAL_TICK_HZ": "100", "PAH_FF_TAU_S": "0.15"},
    "grav0.5": {"PAH_GRAVITY_SCALE": "0.5"},
    "grav0": {"PAH_GRAVITY_SCALE": "0"},
    "grav0.5_ff0.15": {"PAH_GRAVITY_SCALE": "0.5", "PAH_FF_TAU_S": "0.15"},
    "cap": {"PAH_TORQUE_CAP_NM": "6,10,6,4,2,2"},
}


def run(job: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PAH_")} | job["env"]
    argv = [sys.executable, os.path.join(HERE, "bench_replay.py"), "--trace", TRACE, "--arm", job["arm"],
            "--seconds", str(job["seconds"]), "--armature", str(job["armature"]), "--damping", str(job["damping"]),
            "--friction", str(job["friction"])]
    if job["start"] is not None:
        argv += ["--start", str(job["start"])]
    p = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=job["seconds"] + 120)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
    if p.returncode or not lines:
        return job | {"error": p.stderr[-800:]}
    return job | {"result": json.loads(lines[-1])}


def fit_score(res: dict) -> float:
    s, r = res["sim"], res["real"]
    return sum(abs(a - b) / max(b, 1e-3) for a, b in zip(s["rms_rad"], r["rms_rad"])) / 6 + sum(
        abs(a - b) for a, b in zip(s["lag_s"], r["lag_s"])
    ) / 6 / 0.1


def best_plants(path: str, n: int = 3) -> list[tuple[float, float, float]]:
    agg: dict[tuple, list[float]] = {}
    for line in open(path):
        d = json.loads(line)
        if "result" in d:
            agg.setdefault((d["armature"], d["damping"], d["friction"]), []).append(fit_score(d["result"]))
    ranked = sorted(agg, key=lambda k: sum(agg[k]) / len(agg[k]))
    return ranked[:n]


def report(paths: list[str]) -> None:
    for path in paths:
        rows = [json.loads(ln) for ln in open(path)]
        print(f"== {path}: {len(rows)} runs, {sum('error' in r for r in rows)} errors")
        if rows and rows[0].get("stage") == "fit":
            for k in best_plants(path, 5):
                print("  plant", k)
            continue
        agg: dict[str, list[dict]] = {}
        for r in rows:
            if "result" in r:
                agg.setdefault(r["name"], []).append(r["result"])
        print(f"  {'setting':18} {'rms mrad':>9} {'lag ms':>7} {'hand cm':>8} {'hand z cm':>9} {'tick Hz':>8}")
        for name, rs in agg.items():
            n = len(rs)
            rms = 1e3 * sum(x["sim_rms_mean_rad"] for x in rs) / n
            lag = 1e3 * sum(x["sim_lag_mean_s"] for x in rs) / n
            hand = 100 * sum(x["sim"]["hand_rms_m"] for x in rs) / n
            hz = 100 * sum(x["sim"]["hand_mean_m"][2] for x in rs) / n
            tick = sum(x["tick_hz_achieved"] for x in rs) / n
            print(f"  {name:18} {rms:9.1f} {lag:7.0f} {hand:8.2f} {hz:9.2f} {tick:8.1f}")
        real = [x["real"] for rs in agg.values() for x in rs]
        if real:
            print(f"  {'REAL station':18} {1e3 * sum(sum(x['rms_rad']) / 6 for x in real) / len(real):9.1f} "
                  f"{1e3 * sum(sum(x['lag_s']) / 6 for x in real) / len(real):7.0f} "
                  f"{100 * sum(x['hand_rms_m'] for x in real) / len(real):8.2f} "
                  f"{100 * sum(x['hand_mean_m'][2] for x in real) / len(real):9.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["fit", "tune"])
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    ap.add_argument("--seconds", type=float, default=40.0)
    ap.add_argument("--out")
    ap.add_argument("--plants", help="fit results to take the best plants from (tune)")
    ap.add_argument("--report", nargs="*")
    a = ap.parse_args()
    if a.report:
        report(a.report)
        return
    jobs = []
    if a.stage == "fit":
        for arm_, dmp, fr, arm, start in itertools.product(
            PLANT_GRID["armature"], PLANT_GRID["damping"], PLANT_GRID["friction"], ["r", "l"], WINDOWS
        ):
            jobs.append({"stage": "fit", "name": "dream", "env": {}, "armature": arm_, "damping": dmp,
                         "friction": fr, "arm": arm, "start": start, "seconds": a.seconds})
    else:
        plants = best_plants(a.plants) if a.plants else [(1.0, 1.0, 1.0)]
        for (arm_, dmp, fr), (name, env), arm, start in itertools.product(
            plants, SETTINGS.items(), ["r", "l"], WINDOWS
        ):
            jobs.append({"stage": "tune", "name": name, "env": env, "armature": arm_, "damping": dmp,
                         "friction": fr, "arm": arm, "start": start, "seconds": a.seconds})
    print(f"{len(jobs)} runs, {a.jobs} at a time, ~{len(jobs) / a.jobs * (a.seconds + 8) / 60:.0f} min", flush=True)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f, ThreadPoolExecutor(a.jobs) as pool:
        for i, res in enumerate(pool.map(run, jobs)):
            f.write(json.dumps(res) + "\n")
            f.flush()
            if "error" in res:
                print(f"[{i + 1}/{len(jobs)}] ERROR {res['error'][-200:]}", flush=True)
            elif (i + 1) % 10 == 0:
                print(f"[{i + 1}/{len(jobs)}]", flush=True)


if __name__ == "__main__":
    main()
