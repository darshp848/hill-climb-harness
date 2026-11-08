"""Joint fault detector on REAL Metal-arm motion: Dream's rollout traces from the NUC, no torque.

    rsync -a --include='*/' --include='last-run.csv' --exclude='*' pah@nuck-5:rollouts/ ../rollouts/
    python tools/joint_fault_real.py --rollouts ../rollouts              # full run (~1 h with torch on a laptop)
    python tools/joint_fault_real.py --rollouts ../rollouts --quick      # pipeline check, a few minutes

Dream's station writes `last-run.csv` after every run (see harness/force.py): per 20 Hz tick the measured and
commanded joint angles of both arms in radians. There is no torque in it, so this is the torque-free variant of
tools/train_joint_fault.py:

1. Base motion is the rollouts themselves, split by run into train / val / test (no run is in two splits).
2. Train and val clips get the same injected faults as the sim pipeline (tools/joint_fault_data.py), except overload,
   which needs torque. The three torque features are zeroed everywhere, so the models learn without them.
3. Every candidate in train_joint_fault.CANDIDATES, plus EXTRA_CANDIDATES below, trains the same number of steps.
   Its alarm threshold is picked on val (injected real-motion clips) for --fa-budget false alarms per joint-hour.
4. Then the part this tool exists for: each model and the rule observer (harness.joint_observer, torque rules off)
   run over the TEST runs untouched, nothing injected. Every alarm there is either a real event or a false alarm.
   The report lists alarms per joint-hour and each alarm's run, arm, joint and time, so a person can check them
   against the run's video or log.

Output: a table on stdout and `--out` (JSON) with every number and every alarm on the untouched test runs.
Caveat: the untouched runs are policy rollouts, not a clean "nothing went wrong" set, so read the alarm list before
calling them false alarms.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.joint_fault_net import DT, N_FEATURES, WINDOW, features  # noqa: E402
from harness.joint_observer import METAL_LIMITS_DEG, METAL_VMAX_DEG_S, JointObserver, ObserverConfig  # noqa: E402
from tools import joint_fault_data as D  # noqa: E402
from tools import train_joint_fault as T  # noqa: E402

JOINTS = D.JOINTS                     # Dream's trace columns l0..l5 / r0..r5 are these, in this order
TORQUE = slice(5, 8)                  # tau, tau_cmd, tau_diff
GAP_S = 0.25                          # a tick gap longer than this (waiting on the model) splits a series
FAULT_KINDS = tuple(k for k in T.FAULT_KINDS if k != "overload")

# Add designs to compare here, as (family, config) in train_joint_fault.build_model's terms. Families: mlp, tcn, gru,
# transformer (the encoder). Example: ("transformer", {"d_model": 64, "heads": 4, "layers": 2, "ff": 128}).
EXTRA_CANDIDATES: list[tuple[str, dict]] = []


# ------------------------------------------------------------------------------------------------------------ data

def load_rollout(path: str) -> list[dict]:
    """One run: per (arm, joint) and per stretch without gaps, {arm, joint, t, q, qc} in seconds and degrees."""
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if len(rows) < WINDOW * 2:
        return []
    t = np.array([float(r["t_s"]) for r in rows])
    cuts = np.flatnonzero(np.diff(t) > GAP_S) + 1
    out = []
    for arm, a in (("left", "l"), ("right", "r")):
        for i, name in enumerate(JOINTS):
            q = np.degrees([float(r[f"measured_{a}{i}"]) for r in rows])
            qc = np.degrees([float(r[f"commanded_{a}{i}"]) for r in rows])
            for seg in np.split(np.arange(len(t)), cuts):
                if len(seg) < D.CLIP:
                    continue
                ts = t[seg]
                grid = np.arange(ts[0], ts[-1], DT)
                out.append({"arm": arm, "joint": name, "t": grid,
                            "q": np.interp(grid, ts, q[seg]), "qc": np.interp(grid, ts, qc[seg])})
    return out


def as_base(series: list[dict]) -> list[np.ndarray]:
    """The (T, 5) arrays joint_fault_data.make_clip injects into: q, q_cmd, dq (finite difference), tau 0, tau_cmd 0."""
    out = []
    for s in series:
        z = np.zeros_like(s["q"])
        out.append(np.stack([s["q"], s["qc"], np.gradient(s["q"]) / DT, z, z], axis=1))
    return out


def build_split(series: list[dict], n: int, seed: int) -> T.Split:
    probs = np.array([0.0 if k == "overload" else p for k, p in zip(D.KINDS, D.KIND_P)])
    old = D.KIND_P
    D.KIND_P = tuple(probs / probs.sum())             # make_clip reads it at call time
    try:
        clips, X, now, soon = D.build(as_base(series), n, seed)
    finally:
        D.KIND_P = old
    X[..., TORQUE] = 0.0
    return T.Split(clips, X, now, soon)


def series_features(s: dict) -> np.ndarray:
    lo, hi = METAL_LIMITS_DEG[s["joint"]]
    z = np.zeros_like(s["q"])
    X = features(s["q"], s["qc"], np.gradient(s["q"]) / DT, z, z, lo, hi, 1.0)
    X[:, TORQUE] = 0.0
    return X


# ------------------------------------------------------------------------------------------- untouched test runs

@torch.no_grad()
def series_probs(model, X: np.ndarray, norm) -> np.ndarray:
    """p_soon per sample of one untouched series (the first WINDOW-1 samples padded with the first reading)."""
    mean, std = (torch.from_numpy(a) for a in norm)
    pad = np.concatenate([np.repeat(X[:1], WINDOW - 1, 0), X]).astype(np.float32)
    w = torch.from_numpy(pad).unfold(0, WINDOW, 1).permute(0, 2, 1)
    return torch.sigmoid(model((w - mean) / std))[:, 1].numpy()


def rising(alarm: np.ndarray) -> np.ndarray:
    return np.flatnonzero(alarm & ~np.concatenate([[False], alarm[:-1]]))


def rule_alarms_series(s: dict) -> dict[str, np.ndarray]:
    obs = JointObserver(ObserverConfig(limits_deg=dict(METAL_LIMITS_DEG), vmax_deg_s=dict(METAL_VMAX_DEG_S)))
    dq = np.gradient(s["q"]) / DT
    out = {k: np.zeros(len(s["t"]), bool) for k in ("any",) + T.RULE_KINDS}
    for i, t in enumerate(s["t"]):
        for a in obs.observe(float(t), s["arm"], {s["joint"]: {"q_deg": float(s["q"][i]), "q_cmd_deg": float(s["qc"][i]),
                                                              "dq_deg_s": float(dq[i])}}):
            out["any"][i] = True
            if a["kind"] in out:
                out[a["kind"]][i] = True
    return out


def untouched_report(test: list[tuple[str, dict]], alarm_of) -> dict:
    """alarm_of(series) -> {label: bool array}; returns alarms per joint-hour and the alarm list per label."""
    hours = sum(len(s["t"]) * DT for _, s in test) / 3600.0
    events: dict[str, list] = {}
    for run, s in test:
        for label, alarm in alarm_of(s).items():
            for i in rising(alarm):
                events.setdefault(label, []).append({"run": run, "arm": s["arm"], "joint": s["joint"],
                                                     "t_s": round(float(s["t"][i] - s["t"][0]), 2),
                                                     "q_deg": round(float(s["q"][i]), 1)})
    return {"joint_hours": round(hours, 2),
            "per_joint_hour": {k: round(len(v) / hours, 2) for k, v in events.items()}, "alarms": events}


# ------------------------------------------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rollouts", required=True, help="folder with <run>/last-run.csv copied off the NUC")
    ap.add_argument("--steps", type=int, default=3000)
    ap.add_argument("--fa-budget", type=float, default=2.0, help="false alarms per joint-hour on val")
    ap.add_argument("--only", default="", help="comma-separated candidate names to run (default: all)")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default="joint_fault_real.json")
    args = ap.parse_args()
    n_train, n_val = (2000, 800) if args.quick else (16000, 4000)
    if args.quick:
        args.steps = 300

    runs = []
    for path in sorted(glob.glob(str(Path(args.rollouts) / "**" / "last-run.csv"), recursive=True)):
        series = load_rollout(path)
        if series:
            runs.append((Path(path).parent.name, series))
    if len(runs) < 6:
        sys.exit(f"need at least 6 runs with data under {args.rollouts}, found {len(runs)}")
    rng = np.random.default_rng(0)
    order = rng.permutation(len(runs))
    n_hold = max(2, len(runs) // 6)
    split = {"test": [runs[i] for i in order[:n_hold]], "val": [runs[i] for i in order[n_hold:2 * n_hold]],
             "train": [runs[i] for i in order[2 * n_hold:]]}
    for k, v in split.items():
        secs = sum(len(s["t"]) * DT for _, ss in v for s in ss)
        print(f"{k}: {len(v)} runs, {secs / 3600:.2f} joint-hours", flush=True)
    flat = {k: [s for _, ss in v for s in ss] for k, v in split.items()}
    train = build_split(flat["train"], n_train, 1)
    val = build_split(flat["val"], n_val, 2)
    fx = train.X.reshape(-1, N_FEATURES).numpy()
    norm = (fx.mean(0).astype(np.float32), np.maximum(fx.std(0), 0.05).astype(np.float32))
    test = [(run, s) for run, ss in split["test"] for s in ss]
    test_X = [series_features(s) for _, s in test]

    T.FAULT_KINDS = FAULT_KINDS                       # evaluate() averages timely recall over these
    candidates = T.CANDIDATES + EXTRA_CANDIDATES
    if args.only:
        keep = {x.strip() for x in args.only.split(",")}
        candidates = [c for c in candidates if T.name_of(*c) in keep]
    results = []
    print(f"{'model':22}{'weights':>8}{'s':>6}{'thr':>7}{'val FA/jh':>10}{'val timely':>11}{'REAL FA/jh':>11}",
          flush=True)
    for family, cfg in candidates:
        t0 = time.time()
        model = T.train(family, cfg, train, norm, args.steps, seed=0)
        thr, m, _ = T.score_model(model, val, norm, args.fa_budget)
        probs = {id(s): p for (_, s), p in zip(test, (series_probs(model, X, norm) for X in test_X))}
        real = untouched_report(test, lambda s: {"learned": T.alarms((probs[id(s)] >= thr)[None], T.PERSIST)[0]})
        r = {"name": T.name_of(family, cfg), "family": family, "config": cfg, "weights": T.n_params(model),
             "train_s": round(time.time() - t0, 1), "thr": thr, "val": m,
             "real_fa_per_joint_hour": real["per_joint_hour"].get("learned", 0.0),
             "real_alarms": real["alarms"].get("learned", [])}
        results.append(r)
        print(f"{r['name']:22}{r['weights']:>8}{r['train_s']:>6.0f}{thr:>7.3f}{m['fa_per_joint_hour']:>10.1f}"
              f"{m['timely_mean']:>11.3f}{r['real_fa_per_joint_hour']:>11.2f}", flush=True)

    rules = untouched_report(test, rule_alarms_series)
    print(f"\nrule observer on the untouched test runs ({rules['joint_hours']} joint-hours), alarms per joint-hour:")
    for k, v in sorted(rules["per_joint_hour"].items(), key=lambda kv: -kv[1]):
        print(f"  {k:22}{v:8.2f}")
    Path(args.out).write_text(json.dumps({
        "split": {k: [run for run, _ in v] for k, v in split.items()}, "steps": args.steps,
        "fa_budget": args.fa_budget, "fault_kinds": list(FAULT_KINDS), "candidates": results,
        "rules_untouched": rules, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=1),
        encoding="utf-8")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
