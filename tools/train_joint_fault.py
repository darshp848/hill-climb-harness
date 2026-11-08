"""Architecture search and training for the learned joint fault detector (harness.joint_fault_net).

    python tools/train_joint_fault.py --traces ../laptop_runs            # search, then retrain the best, export
    python tools/train_joint_fault.py --traces ../laptop_runs --quick    # small run to check the pipeline

Data: tools/joint_fault_data.py (real sim motion + injected faults), split by match file into train / val / test.
Every candidate (MLP, dilated causal TCN, GRU, transformer encoder; several widths and depths) trains the same number
of steps on the same windows. Its alarm threshold is picked on VAL so false alarms stay within --fa-budget per joint-
hour; the search ranks candidates on VAL by mean "timely recall" over the fault kinds (alarm no later than 0.25 s
after the fault starts), ties to fewer weights. The top candidates are retrained with more seeds and steps; the winner
is scored once on TEST next to the rule observer (harness.joint_observer) on the same clips, and exported to
harness/models/joint_fault_net.json with `beats_baseline` set from that comparison. Needs torch (training only).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import torch
from torch import nn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.joint_fault_net import DT, FEATURES, HORIZON, N_FEATURES, WINDOW, FaultModel  # noqa: E402
from harness.joint_observer import JointObserver, ObserverConfig  # noqa: E402
from tools import joint_fault_data as D  # noqa: E402

FAULT_KINDS = ("limit_run", "blocked", "oscillation", "overload", "overspeed")
PERSIST = 2


# ------------------------------------------------------------------------------------------------------------ models

class MLP(nn.Module):
    def __init__(self, hidden):
        super().__init__()
        dims = [WINDOW * N_FEATURES] + list(hidden)
        for i in range(len(hidden)):
            setattr(self, f"l{i}", nn.Linear(dims[i], dims[i + 1]))
        self.n = len(hidden)
        self.head = nn.Linear(dims[-1], 2)

    def forward(self, x):
        h = x.flatten(1)
        for i in range(self.n):
            h = torch.relu(getattr(self, f"l{i}")(h))
        return self.head(h)


class TCN(nn.Module):
    def __init__(self, channels, dilations, kernel=3):
        super().__init__()
        self.dilations, self.kernel = list(dilations), kernel
        cin = N_FEATURES
        for i, d in enumerate(dilations):
            setattr(self, f"c{i}", nn.Conv1d(cin, channels, kernel, dilation=d))
            if cin != channels:
                setattr(self, f"r{i}", nn.Conv1d(cin, channels, 1))
            cin = channels
        self.head = nn.Linear(2 * channels, 2)

    def forward(self, x):
        h = x.transpose(1, 2)
        for i, d in enumerate(self.dilations):
            y = torch.relu(getattr(self, f"c{i}")(nn.functional.pad(h, ((self.kernel - 1) * d, 0))))
            res = getattr(self, f"r{i}")(h) if hasattr(self, f"r{i}") else h
            h = y + res
        return self.head(torch.cat([h[:, :, -1], h.mean(2)], 1))


class GRU(nn.Module):
    def __init__(self, hidden, layers):
        super().__init__()
        self.gru = nn.GRU(N_FEATURES, hidden, layers, batch_first=True)
        self.head = nn.Linear(hidden, 2)

    def forward(self, x):
        out, _ = self.gru(x)
        return self.head(out[:, -1])


class Encoder(nn.Module):
    def __init__(self, d_model, heads, layers, ff):
        super().__init__()
        self.inp = nn.Linear(N_FEATURES, d_model)
        self.pos = nn.Parameter(torch.zeros(WINDOW, d_model))
        layer = nn.TransformerEncoderLayer(d_model, heads, ff, dropout=0.0, batch_first=True, norm_first=True)
        self.enc = nn.TransformerEncoder(layer, layers, enable_nested_tensor=False)
        self.norm = nn.LayerNorm(d_model)
        self.head = nn.Linear(d_model, 2)

    def forward(self, x):
        h = self.enc(self.inp(x) + self.pos)
        return self.head(self.norm(h)[:, -1])


def build_model(family, cfg):
    if family == "mlp":
        return MLP(cfg["hidden"])
    if family == "tcn":
        return TCN(cfg["channels"], cfg["dilations"], cfg["kernel"])
    if family == "gru":
        return GRU(cfg["hidden"], cfg["layers"])
    return Encoder(cfg["d_model"], cfg["heads"], cfg["layers"], cfg["ff"])


CANDIDATES = [
    ("mlp", {"hidden": []}),
    ("mlp", {"hidden": [32]}),
    ("mlp", {"hidden": [64, 32]}),
    ("mlp", {"hidden": [128, 64]}),
    ("tcn", {"channels": 8, "dilations": [1, 2, 4, 8], "kernel": 3}),
    ("tcn", {"channels": 16, "dilations": [1, 2, 4], "kernel": 3}),
    ("tcn", {"channels": 16, "dilations": [1, 2, 4, 8], "kernel": 3}),
    ("tcn", {"channels": 32, "dilations": [1, 2, 4, 8], "kernel": 3}),
    ("gru", {"hidden": 8, "layers": 1}),
    ("gru", {"hidden": 16, "layers": 1}),
    ("gru", {"hidden": 32, "layers": 1}),
    ("gru", {"hidden": 32, "layers": 2}),
    ("transformer", {"d_model": 16, "heads": 2, "layers": 1, "ff": 32}),
    ("transformer", {"d_model": 32, "heads": 4, "layers": 1, "ff": 64}),
    ("transformer", {"d_model": 32, "heads": 4, "layers": 2, "ff": 64}),
]


def n_params(model):
    return sum(p.numel() for p in model.parameters())


def name_of(family, cfg):
    if family == "mlp":
        return "mlp[" + "-".join(map(str, cfg["hidden"])) + "]" if cfg["hidden"] else "logistic"
    if family == "tcn":
        return f"tcn c{cfg['channels']} L{len(cfg['dilations'])}"
    if family == "gru":
        return f"gru h{cfg['hidden']} L{cfg['layers']}"
    return f"enc d{cfg['d_model']} L{cfg['layers']} h{cfg['heads']}"


# -------------------------------------------------------------------------------------------------- data and training

class Split:
    def __init__(self, clips, X, now, soon):
        self.clips, self.now, self.soon = clips, now, soon
        self.X = torch.from_numpy(X)
        self.windows = self.X.unfold(1, WINDOW, 1).permute(0, 1, 3, 2)   # (N, CLIP-W+1, W, F), a view
        self.y = torch.from_numpy(np.stack([now, soon], -1)[:, WINDOW - 1:].astype(np.float32))

    def batch(self, gen, size):
        c = torch.randint(0, self.windows.shape[0], (size,), generator=gen)
        e = torch.randint(0, self.windows.shape[1], (size,), generator=gen)
        return self.windows[c, e], self.y[c, e]


def train(family, cfg, data, norm, steps, seed, lr=3e-3, batch=512):
    torch.manual_seed(seed)
    model = build_model(family, cfg)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=lr, total_steps=steps, pct_start=0.15)
    pos = data.y.mean((0, 1)).clamp(1e-3, 1 - 1e-3)
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=((1 - pos) / pos).sqrt())
    mean, std = (torch.from_numpy(a) for a in norm)
    gen = torch.Generator().manual_seed(seed)
    model.train()
    for _ in range(steps):
        x, y = data.batch(gen, batch)
        loss = loss_fn(model((x - mean) / std), y)
        opt.zero_grad()
        loss.backward()
        nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        sched.step()
    model.eval()
    return model


@torch.no_grad()
def predict(model, data, norm):
    mean, std = (torch.from_numpy(a) for a in norm)
    N, E = data.windows.shape[:2]
    out = []
    flat = data.windows.reshape(N * E, WINDOW, N_FEATURES)
    for i in range(0, flat.shape[0], 8192):
        out.append(torch.sigmoid(model((flat[i:i + 8192] - mean) / std)))
    p = torch.cat(out).reshape(N, E, 2).numpy()
    full = np.zeros((N, D.CLIP, 2), dtype=np.float32)
    full[:, WINDOW - 1:] = p
    return full


# --------------------------------------------------------------------------------------------------------- metrics

def alarms(flags: np.ndarray, persist: int) -> np.ndarray:
    run = np.zeros_like(flags, dtype=np.int32)
    for t in range(flags.shape[1]):
        run[:, t] = np.where(flags[:, t], (run[:, t - 1] if t else 0) + 1, 0)
    return run >= persist


def evaluate(alarm: np.ndarray, data: Split) -> dict:
    """Event metrics for (N, CLIP) alarms on a split."""
    N = alarm.shape[0]
    hours = N * (D.CLIP - WINDOW) * DT / 3600.0
    rising = alarm & ~np.concatenate([np.zeros((N, 1), bool), alarm[:, :-1]], 1)
    clear = ~data.soon.copy()
    for k in range(1, 11):                       # an alarm up to 0.5 s before the soon window is still not false
        clear[:, :-k] &= ~data.soon[:, k:]
    fa = int((rising & clear)[:, WINDOW:].sum())
    per_kind = {}
    for kind in FAULT_KINDS:
        lat = []
        for i, c in enumerate(data.clips):
            if c.kind != kind or c.onset < WINDOW:
                continue
            lo = c.onset - HORIZON
            hits = np.flatnonzero(alarm[i, lo: c.onset + 40])
            lat.append((hits[0] + lo - c.onset) * DT if len(hits) else math.inf)
        lat = np.array(lat)
        found = np.isfinite(lat)
        per_kind[kind] = {"n": int(len(lat)), "recall": float(found.mean()) if len(lat) else None,
                          "timely": float((lat <= 0.25 + 1e-9).mean()) if len(lat) else None,
                          "early": float((lat < 0).mean()) if len(lat) else None,
                          "latency_med_s": float(np.median(lat[found])) if found.any() else None}
    timely = [v["timely"] for v in per_kind.values() if v["timely"] is not None]
    return {"fa_per_joint_hour": fa / hours, "timely_mean": float(np.mean(timely)), "kinds": per_kind}


def auroc(score, label):
    order = np.argsort(score)
    ranks = np.empty(len(score))
    ranks[order] = np.arange(1, len(score) + 1)
    pos = label.sum()
    neg = len(label) - pos
    return float((ranks[label].sum() - pos * (pos + 1) / 2) / max(pos * neg, 1))


def pick_threshold(p_soon, data, budget):
    best = None
    for thr in np.unique(np.concatenate([np.linspace(0.05, 0.95, 19), 1.0 - np.logspace(-5, -1.3, 40)])):
        m = evaluate(alarms(p_soon >= thr, PERSIST), data)
        if m["fa_per_joint_hour"] <= budget and (best is None or m["timely_mean"] > best[1]["timely_mean"]):
            best = (float(thr), m)
    return best or (0.995, evaluate(alarms(p_soon >= 0.995, PERSIST), data))


def score_model(model, data, norm, budget, thr=None):
    p = predict(model, data, norm)
    if thr is None:
        thr, m = pick_threshold(p[..., 1], data, budget)
    else:
        m = evaluate(alarms(p[..., 1] >= thr, PERSIST), data)
    sl = slice(WINDOW - 1, None)
    m["auroc_now"] = auroc(p[:, sl, 0].ravel(), data.now[:, sl].ravel())
    m["auroc_soon"] = auroc(p[:, sl, 1].ravel(), data.soon[:, sl].ravel())
    return thr, m, p


RULE_KINDS = ("limit_breach", "limit_approach", "overspeed", "torque_saturation", "tracking_divergence",
              "oscillation")


def rule_alarms(data: Split) -> dict[str, np.ndarray]:
    """The rule observer's alarms on each clip: 'any' and one array per alert kind."""
    out = {k: np.zeros((len(data.clips), D.CLIP), dtype=bool) for k in ("any",) + RULE_KINDS}
    for i, c in enumerate(data.clips):
        obs = JointObserver(ObserverConfig(limits_deg={"j": (c.lo, c.hi)}, vmax_deg_s={"j": D.VMAX_DEG_S}))
        for t in range(D.CLIP):
            q, qc, dq, tau, tauc = c.raw[t]
            for a in obs.observe(t * DT, "a", {"j": {"q_deg": q, "q_cmd_deg": qc, "dq_deg_s": dq, "tau_nm": tau,
                                                       "tau_cmd_nm": tauc, "tau_max_nm": c.tau_max}}):
                out["any"][i, t] = True
                if a["kind"] in out:
                    out[a["kind"]][i, t] = True
    return out


def numpy_step_us(spec, joints=12, reps=200):
    m = FaultModel(spec)
    x = np.random.default_rng(0).normal(size=(joints, WINDOW, N_FEATURES)).astype(np.float32)
    m.probs(x)
    t0 = time.perf_counter()
    for _ in range(reps):
        m.probs(x)
    return (time.perf_counter() - t0) / reps * 1e6


def export(model, family, cfg, norm, thr, extra):
    return {"family": family, "config": cfg, "window": WINDOW, "rate_hz": 1 / DT, "features": list(FEATURES),
            "horizon": HORIZON, "feature_mean": norm[0].tolist(), "feature_std": norm[1].tolist(),
            "threshold": {"now": 0.5, "soon": thr}, "persist": PERSIST,
            "weights": {k: v.detach().double().numpy().tolist() for k, v in model.state_dict().items()}, **extra}


# ------------------------------------------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", default=str(ROOT.parent / "laptop_runs"))
    ap.add_argument("--steps", type=int, default=3000)
    ap.add_argument("--final-steps", type=int, default=8000)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--fa-budget", type=float, default=2.0, help="false alarms per joint-hour on val")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "harness" / "models"))
    args = ap.parse_args()
    torch.set_num_threads(max(1, torch.get_num_threads()))
    n_train, n_val, n_test = (2000, 800, 800) if args.quick else (16000, 4000, 4000)
    if args.quick:
        args.steps, args.final_steps, args.top, args.seeds = 300, 600, 2, 1
    files = D.match_files(args.traces)
    if len(files) < 3:
        sys.exit(f"need at least 3 joints.jsonl under {args.traces}, found {len(files)}")
    n_test_files = max(1, len(files) // 6)
    split_files = {"train": files[: -2 * n_test_files], "val": files[-2 * n_test_files: -n_test_files],
                   "test": files[-n_test_files:]}
    data = {}
    for (name, paths), n, seed in zip(split_files.items(), (n_train, n_val, n_test), (1, 2, 3)):
        data[name] = Split(*D.build(D.load_series(paths), n, seed))
        print(f"{name}: {len(paths)} files, {n} clips", flush=True)
    flatX = data["train"].X.reshape(-1, N_FEATURES).numpy()
    norm = (flatX.mean(0).astype(np.float32), np.maximum(flatX.std(0), 0.05).astype(np.float32))

    results = []
    print(f"{'model':22}{'weights':>8}{'s':>6}{'thr':>6}{'FA/jh':>7}{'timely':>8}{'AUC now':>8}{'AUC soon':>9}",
          flush=True)
    for family, cfg in CANDIDATES:
        t0 = time.time()
        model = train(family, cfg, data["train"], norm, args.steps, seed=0)
        thr, m, _ = score_model(model, data["val"], norm, args.fa_budget)
        r = {"name": name_of(family, cfg), "family": family, "config": cfg, "weights": n_params(model),
             "train_s": round(time.time() - t0, 1), "val": m, "thr": thr}
        results.append(r)
        print(f"{r['name']:22}{r['weights']:>8}{r['train_s']:>6.0f}{thr:>6.2f}{m['fa_per_joint_hour']:>7.1f}"
              f"{m['timely_mean']:>8.3f}{m['auroc_now']:>8.3f}{m['auroc_soon']:>9.3f}", flush=True)

    ranked = sorted(results, key=lambda r: (-round(r["val"]["timely_mean"], 2), r["weights"]))
    finals = []
    for r in ranked[: args.top]:
        runs = []
        for seed in range(args.seeds):
            model = train(r["family"], r["config"], data["train"], norm, args.final_steps, seed=100 + seed)
            thr, m, _ = score_model(model, data["val"], norm, args.fa_budget)
            runs.append((m["timely_mean"], thr, model, m))
        vals = [x[0] for x in runs]
        best = max(runs, key=lambda x: x[0])
        finals.append({"r": r, "mean": float(np.mean(vals)), "sd": float(np.std(vals)), "best": best})
        print(f"final {r['name']:22} val timely {np.mean(vals):.3f} +- {np.std(vals):.3f} over {len(vals)} seeds",
              flush=True)
    win = max(finals, key=lambda f: (round(f["mean"], 2), -f["r"]["weights"]))
    _, thr, model, val_m = win["best"]
    _, test_m, _ = score_model(model, data["test"], norm, args.fa_budget, thr=thr)
    rules = rule_alarms(data["test"])
    rule_m = evaluate(rules["any"], data["test"])
    rule_m["fa_by_kind"] = {k: round(evaluate(rules[k], data["test"])["fa_per_joint_hour"], 1) for k in RULE_KINDS}
    print("rule false alarms per joint-hour by kind:", rule_m["fa_by_kind"], flush=True)
    beats = test_m["timely_mean"] > rule_m["timely_mean"] and \
        test_m["fa_per_joint_hour"] <= max(rule_m["fa_per_joint_hour"], args.fa_budget * 1.5)
    spec = export(model, win["r"]["family"], win["r"]["config"], norm, thr, {
        "name": win["r"]["name"], "n_weights": win["r"]["weights"], "beats_baseline": bool(beats),
        "trained_on": {k: [Path(p).parent.name for p in v] for k, v in split_files.items()},
        "test": test_m, "rule_test": rule_m, "val": val_m,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    spec["numpy_us_per_step_12_joints"] = round(numpy_step_us(spec), 1)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "joint_fault_net.json").write_text(json.dumps(spec), encoding="utf-8")
    (out / "joint_fault_search.json").write_text(json.dumps({
        "candidates": results, "finals": [{"name": f["r"]["name"], "weights": f["r"]["weights"],
                                           "val_timely_mean": f["mean"], "val_timely_sd": f["sd"]}
                                          for f in finals],
        "winner": win["r"]["name"], "test": test_m, "rule_test": rule_m, "beats_baseline": bool(beats),
        "numpy_us_per_step_12_joints": spec["numpy_us_per_step_12_joints"]}, indent=1), encoding="utf-8")
    print(f"\nwinner {win['r']['name']} ({win['r']['weights']} weights), threshold {thr:.2f}")
    for label, m in (("learned", test_m), ("rules", rule_m)):
        print(f"TEST {label:8} FA/joint-h {m['fa_per_joint_hour']:.1f}  timely {m['timely_mean']:.3f}  " +
              "  ".join(f"{k} {v['timely']:.2f}/{v['early']:.2f}/{v['latency_med_s']}" for k, v in m["kinds"].items()
                        if v["timely"] is not None))
    print("beats_baseline", beats, " numpy us/step (12 joints)", spec["numpy_us_per_step_12_joints"])


if __name__ == "__main__":
    main()
