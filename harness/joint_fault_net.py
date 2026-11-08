"""Learned joint fault detector: 8 numbers per joint reading in, 2 fault flags out.

Per joint, at the harness's telemetry rate (20 Hz, harness.joint_telemetry), the last WINDOW readings become 8
features each (features()): where the joint is in its range, how close it is to the nearer limit, how fast it moves
toward that limit, how far it lags its command, how fast the command moves toward the limit, measured torque, commanded
torque and their difference (both as a share of the joint's torque limit). A small network trained on Dream's sim
matches with injected faults (tools/train_joint_fault.py) turns that window into two probabilities:

  now    the joint is in a fault state: past a limit, blocked, oscillating, at its torque limit or over speed
  soon   it is now, or will be within HORIZON_S (0.25 s): the look-ahead flag

Inference is numpy only (the harness on the MacBook); the NUC keeps the rule observer (harness.joint_observer), which
is stdlib. The weights file names the architecture the search picked (mlp, tcn, gru or transformer) and the
thresholds chosen on validation traces; JointFaultNet runs it on live readings.
"""

from __future__ import annotations

import json
import math
from collections import deque
from pathlib import Path

import numpy as np

FEATURES = ("pos_frac", "margin", "v_toward", "track_err", "cmd_v_toward", "tau", "tau_cmd", "tau_diff")
N_FEATURES = len(FEATURES)
RATE_HZ = 20.0
DT = 1.0 / RATE_HZ
WINDOW = 16                      # 0.8 s of history
HORIZON_S = 0.25
HORIZON = int(round(HORIZON_S * RATE_HZ))
MARGIN_SCALE_DEG = 30.0
SPEED_SCALE_DEG_S = 200.0
TRACK_SCALE_DEG = 10.0
DEFAULT_MODEL = Path(__file__).with_name("models") / "joint_fault_net.json"


def features(q, q_cmd, dq, tau, tau_cmd, lo, hi, tau_max, dt=DT, q_cmd_prev=None):
    """(T, 8) float32 features from per-sample arrays (degrees, deg/s, N.m). `q_cmd_prev` is the command one sample
    before the first (None: the command's first speed is 0)."""
    q, q_cmd, dq, tau, tau_cmd = (np.asarray(a, dtype=np.float64) for a in (q, q_cmd, dq, tau, tau_cmd))
    span = max(hi - lo, 1e-6)
    to_hi = hi - q
    to_lo = q - lo
    toward = np.where(to_hi < to_lo, 1.0, -1.0)            # +1: the nearer limit is the upper one
    margin = np.minimum(to_hi, to_lo)
    prev = np.concatenate([[q_cmd[0] if q_cmd_prev is None else q_cmd_prev], q_cmd[:-1]])
    dt = np.broadcast_to(np.asarray(dt, dtype=np.float64), q.shape)
    cmd_v = (q_cmd - prev) / np.clip(dt, 0.02, 0.2)
    tmax = max(float(tau_max or 0.0), 1e-6)
    out = np.stack([
        np.clip(2.0 * (q - lo) / span - 1.0, -2.0, 2.0),
        np.clip(margin / MARGIN_SCALE_DEG, -1.0, 3.0),
        np.clip(dq * toward / SPEED_SCALE_DEG_S, -5.0, 5.0),
        np.clip((q_cmd - q) / TRACK_SCALE_DEG, -5.0, 5.0),
        np.clip(cmd_v * toward / SPEED_SCALE_DEG_S, -5.0, 5.0),
        np.clip(tau / tmax, -2.0, 2.0),
        np.clip(tau_cmd / tmax, -2.0, 2.0),
        np.clip((tau - tau_cmd) / tmax, -2.0, 2.0),
    ], axis=-1)
    return out.astype(np.float32)


# ---------------------------------------------------------------- numpy forward passes (match tools/train_joint_fault.py)

def _relu(x):
    return np.maximum(x, 0.0)


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _layer_norm(x, w, b, eps=1e-5):
    m = x.mean(-1, keepdims=True)
    v = ((x - m) ** 2).mean(-1, keepdims=True)
    return (x - m) / np.sqrt(v + eps) * w + b


def _forward_mlp(p, cfg, x):                         # x (B, W, F)
    h = x.reshape(x.shape[0], -1)
    for i in range(len(cfg["hidden"])):
        h = _relu(h @ p[f"l{i}.weight"].T + p[f"l{i}.bias"])
    return h @ p["head.weight"].T + p["head.bias"]


def _forward_tcn(p, cfg, x):
    h = x.transpose(0, 2, 1)                         # (B, C, W)
    k = cfg["kernel"]
    for i, d in enumerate(cfg["dilations"]):
        w, b = p[f"c{i}.weight"], p[f"c{i}.bias"]  # (Cout, Cin, k)
        pad = (k - 1) * d
        hp = np.pad(h, ((0, 0), (0, 0), (pad, 0)))
        T = h.shape[2]
        y = np.zeros((h.shape[0], w.shape[0], T), dtype=np.float64) + b[None, :, None]
        for j in range(k):
            seg = hp[:, :, j * d: j * d + T]           # (B, Cin, T)
            y += np.einsum("oc,bct->bot", w[:, :, j], seg)
        y = _relu(y)
        if f"r{i}.weight" in p:                       # 1x1 projection on the residual when channels change
            res = np.einsum("oc,bct->bot", p[f"r{i}.weight"][:, :, 0], h) + p[f"r{i}.bias"][None, :, None]
        else:
            res = h
        h = y + res
    last = h[:, :, -1]
    mean = h.mean(axis=2)
    return np.concatenate([last, mean], axis=1) @ p["head.weight"].T + p["head.bias"]


def _forward_gru(p, cfg, x):
    B, T, _ = x.shape
    inp = x
    for layer in range(cfg["layers"]):
        wi, wh = p[f"gru.weight_ih_l{layer}"], p[f"gru.weight_hh_l{layer}"]
        bi, bh = p[f"gru.bias_ih_l{layer}"], p[f"gru.bias_hh_l{layer}"]
        H = wh.shape[1]
        h = np.zeros((B, H))
        outs = []
        for t in range(T):
            gi = inp[:, t] @ wi.T + bi
            gh = h @ wh.T + bh
            r = _sigmoid(gi[:, :H] + gh[:, :H])
            z = _sigmoid(gi[:, H:2 * H] + gh[:, H:2 * H])
            n = np.tanh(gi[:, 2 * H:] + r * gh[:, 2 * H:])
            h = (1 - z) * n + z * h
            outs.append(h)
        inp = np.stack(outs, axis=1)
    return inp[:, -1] @ p["head.weight"].T + p["head.bias"]


def _forward_transformer(p, cfg, x):
    d, heads = cfg["d_model"], cfg["heads"]
    h = x @ p["inp.weight"].T + p["inp.bias"] + p["pos"][None, : x.shape[1]]
    B, T, _ = h.shape
    hd = d // heads
    for i in range(cfg["layers"]):
        pre = f"enc.layers.{i}."
        a = _layer_norm(h, p[pre + "norm1.weight"], p[pre + "norm1.bias"])
        qkv = a @ p[pre + "self_attn.in_proj_weight"].T + p[pre + "self_attn.in_proj_bias"]
        q, k, v = np.split(qkv, 3, axis=-1)
        q = q.reshape(B, T, heads, hd).transpose(0, 2, 1, 3)
        k = k.reshape(B, T, heads, hd).transpose(0, 2, 1, 3)
        v = v.reshape(B, T, heads, hd).transpose(0, 2, 1, 3)
        s = q @ k.transpose(0, 1, 3, 2) / math.sqrt(hd)
        s = np.exp(s - s.max(-1, keepdims=True))
        s = s / s.sum(-1, keepdims=True)
        o = (s @ v).transpose(0, 2, 1, 3).reshape(B, T, d)
        h = h + o @ p[pre + "self_attn.out_proj.weight"].T + p[pre + "self_attn.out_proj.bias"]
        a = _layer_norm(h, p[pre + "norm2.weight"], p[pre + "norm2.bias"])
        f = _relu(a @ p[pre + "linear1.weight"].T + p[pre + "linear1.bias"])
        h = h + f @ p[pre + "linear2.weight"].T + p[pre + "linear2.bias"]
    h = _layer_norm(h, p["norm.weight"], p["norm.bias"])
    return h[:, -1] @ p["head.weight"].T + p["head.bias"]


FORWARD = {"mlp": _forward_mlp, "tcn": _forward_tcn, "gru": _forward_gru, "transformer": _forward_transformer}


class FaultModel:
    """A trained detector from its JSON file: probs(windows) -> (B, 2) probabilities [now, soon]."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.family = spec["family"]
        self.cfg = spec["config"]
        self.params = {k: np.asarray(v, dtype=np.float64) for k, v in spec["weights"].items()}
        self.mean = np.asarray(spec["feature_mean"], dtype=np.float64)
        self.std = np.asarray(spec["feature_std"], dtype=np.float64)
        self.threshold = dict(spec.get("threshold") or {"now": 0.5, "soon": 0.5})
        self.persist = int(spec.get("persist", 2))
        if spec.get("window", WINDOW) != WINDOW or tuple(spec.get("features", FEATURES)) != FEATURES:
            raise ValueError("model was trained on another window or feature set")

    @classmethod
    def load(cls, path: str | Path = DEFAULT_MODEL) -> "FaultModel":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def logits(self, windows: np.ndarray) -> np.ndarray:
        x = (np.asarray(windows, dtype=np.float64) - self.mean) / self.std
        return FORWARD[self.family](self.params, self.cfg, x)

    def probs(self, windows: np.ndarray) -> np.ndarray:
        return _sigmoid(self.logits(windows))


class JointFaultNet:
    """Runs a FaultModel on live readings, one window per (arm, joint).

    step(t, arm, joints) takes the same per-joint dicts as JointObserver.observe (q_deg, q_cmd_deg, dq_deg_s, tau_nm,
    tau_cmd_nm, tau_max_nm) and returns alerts `learned_fault` (now) and `learned_fault_soon` for joints whose
    probability stayed over its threshold for `persist` readings in a row. A repeated timestamp is not a new reading.
    """

    def __init__(self, model: FaultModel, limits_deg: dict, skip: tuple[str, ...] = ("gripper",)):
        self.model = model
        self.limits = limits_deg
        self.skip = set(skip)
        self._feat: dict[tuple, deque] = {}
        self._last: dict[tuple, tuple] = {}
        self._streak: dict[tuple, int] = {}
        self.last_probs: dict[tuple, tuple[float, float]] = {}

    def step(self, t: float, arm: str, joints: dict) -> list[dict]:
        ready, keys = [], []
        for name, j in joints.items():
            if name in self.skip or name not in self.limits or j.get("q_deg") is None:
                continue
            key = (arm, name)
            prev = self._last.get(key)
            if prev is not None and t <= prev[0]:
                continue
            lo, hi = self.limits[name]
            dt = DT if prev is None else t - prev[0]
            row = features([j["q_deg"]], [j.get("q_cmd_deg", j["q_deg"])], [j.get("dq_deg_s") or 0.0],
                           [j.get("tau_nm") or 0.0], [j.get("tau_cmd_nm") or 0.0], lo, hi,
                           j.get("tau_max_nm") or 10.0, dt=dt, q_cmd_prev=None if prev is None else prev[1])[0]
            self._last[key] = (t, j.get("q_cmd_deg", j["q_deg"]))
            buf = self._feat.setdefault(key, deque(maxlen=WINDOW))
            if not buf:
                buf.extend([row] * (WINDOW - 1))     # pad a fresh joint with its first reading
            buf.append(row)
            ready.append(np.stack(buf))
            keys.append(key)
        alerts = []
        if not ready:
            return alerts
        p = self.model.probs(np.stack(ready))
        for key, (p_now, p_soon) in zip(keys, p):
            self.last_probs[key] = (float(p_now), float(p_soon))
            for kind, prob, head in (("learned_fault", p_now, "now"), ("learned_fault_soon", p_soon, "soon")):
                sk = (kind,) + key
                self._streak[sk] = self._streak.get(sk, 0) + 1 if prob >= self.model.threshold[head] else 0
                if self._streak[sk] >= self.model.persist:
                    alerts.append({"kind": kind, "arm": key[0], "joint": key[1], "p": round(float(prob), 3)})
        return alerts
