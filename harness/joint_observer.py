"""Joint look-ahead observer: flags a failure mode BEFORE the arm reaches an illegal joint state.

Fed every joint reading (harness.can_live on the NUC: each motor reply, ~30 Hz per joint under Dream's 30 Hz Metal
loop; the sim: every control step), per joint it keeps an alpha-beta-gamma filter on position, velocity and
acceleration (blending in the motor's own velocity), projects `horizon_s` ahead and checks the projection and the
present against the limits:

  limit_breach        position outside its soft limit now
  limit_approach      projected position past the limit within the horizon, while moving toward it (eta given)
  overspeed           |velocity| over speed_frac of the motor's max speed
  torque_saturation   |torque| over sat_frac of the motor's max for sat_s (pressing, stalled, lifting too much)
  tracking_divergence command and measurement drifting apart for div_s while the command moves (blocked/collision)
  oscillation         velocity sign flips >= osc_flips within osc_window_s at speed (unstable / buzzing)
  fault               the motor's status nibble is a fault (robots.damiao FAULT_STATUSES)
  overheat            a temperature at or over hot_c (the Metal gripper latches off at 55 C)

Pure Python (stdlib) so it runs on the NUC and the match MacBook. The learned detector (harness.joint_fault_net, a
small TCN picked by an architecture search; docs/JOINT_FAULT_NET.md) runs next to it in the harness.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

# Soft limits, degrees, from robots/metal.py JOINTS (dreamlabs 1.0.37); the gripper in motor degrees.
METAL_LIMITS_DEG = {"shoulder_pan": (-160.0, 160.0), "shoulder_lift": (-180.0, 0.0), "elbow_flex": (0.0, 180.0),
                    "wrist_flex": (-123.0, 81.0), "wrist_yaw": (-85.0, 85.0), "wrist_roll": (-145.0, 145.0),
                    "gripper": (0.0, 137.5)}
# The sim's YAM arms (i2rt yam.xml joint ranges), in the Metal joint names sim_station reports them under.
YAM_LIMITS_DEG = {"shoulder_pan": (-150.0, 175.0), "shoulder_lift": (0.0, 209.1), "elbow_flex": (0.0, 210.0),
                  "wrist_flex": (-90.0, 90.0), "wrist_yaw": (-90.0, 90.0), "wrist_roll": (-120.0, 120.0)}
# Max speed per motor model, deg/s (robots/damiao.py MOTOR_LIMITS VMAX, rad/s).
METAL_VMAX_DEG_S = {"shoulder_pan": 573.0, "shoulder_lift": 573.0, "elbow_flex": 573.0, "wrist_flex": 573.0,
                    "wrist_yaw": 1719.0, "wrist_roll": 1719.0, "gripper": 1719.0}
FAULT_STATUSES = set(range(8, 15))


@dataclass
class ObserverConfig:
    horizon_s: float = 0.25
    margin_deg: float = 3.0
    speed_frac: float = 0.5
    sat_frac: float = 0.85
    sat_s: float = 0.3
    div_deg: float = 8.0
    div_s: float = 0.3
    osc_flips: int = 4
    osc_window_s: float = 0.6
    osc_min_speed: float = 20.0
    hot_c: float = 50.0
    alpha: float = 0.6
    beta: float = 0.3
    gamma: float = 0.05
    limits_deg: dict = field(default_factory=lambda: dict(METAL_LIMITS_DEG))
    vmax_deg_s: dict = field(default_factory=lambda: dict(METAL_VMAX_DEG_S))


class _JointFilter:
    def __init__(self):
        self.t = None
        self.q = self.v = self.a = 0.0
        self.sat_since = None
        self.div_since = None
        self.flips: deque = deque(maxlen=32)
        self.last_sign = 0

    def update(self, t: float, q: float, dq: float | None, cfg: ObserverConfig) -> None:
        if self.t is None or t <= self.t:
            self.t, self.q, self.v, self.a = t, q, (dq or 0.0), 0.0
            return
        dt = t - self.t
        qp = self.q + self.v * dt + 0.5 * self.a * dt * dt
        vp = self.v + self.a * dt
        r = q - qp
        self.q = qp + cfg.alpha * r
        self.v = vp + cfg.beta * r / dt
        self.a = self.a + 2 * cfg.gamma * r / (dt * dt)
        if dq is not None:                       # the motor measures its own velocity: trust it half-way
            self.v = 0.5 * self.v + 0.5 * dq
        self.t = t


class JointObserver:
    """observe(t, arm, joints) per reading; returns the failure modes active now (list of dicts)."""

    def __init__(self, config: ObserverConfig | None = None):
        self.cfg = config or ObserverConfig()
        self.filters: dict[tuple[str, str], _JointFilter] = {}

    def predict(self, f: _JointFilter, horizon: float) -> float:
        """Kinematic baseline: constant acceleration, its share capped so a noisy estimate can't dominate."""
        acc = max(-abs(f.v) / max(horizon, 1e-3), min(abs(f.v) / max(horizon, 1e-3), f.a))
        return f.q + f.v * horizon + 0.5 * acc * horizon * horizon

    def observe(self, t: float, arm: str, joints: dict) -> list[dict]:
        cfg, alerts = self.cfg, []
        for name, j in joints.items():
            q = j.get("q_deg")
            if q is None:
                continue
            f = self.filters.setdefault((arm, name), _JointFilter())
            f.update(t, q, j.get("dq_deg_s"), cfg)

            def alert(kind, **extra):
                alerts.append({"kind": kind, "arm": arm, "joint": name, **extra})

            lo, hi = cfg.limits_deg.get(name, (-math.inf, math.inf))
            if q < lo - cfg.margin_deg or q > hi + cfg.margin_deg:
                alert("limit_breach", q_deg=round(q, 1), limit=(lo, hi))
            else:
                qh = self.predict(f, cfg.horizon_s)
                for bound, sign in ((hi, 1), (lo, -1)):
                    toward = f.v * sign > 0
                    if toward and (qh - bound) * sign > -cfg.margin_deg:
                        eta = max(0.0, (bound - q) / f.v) if abs(f.v) > 1e-6 else None
                        alert("limit_approach", q_deg=round(q, 1), bound=bound,
                              eta_s=None if eta is None else round(eta, 2), q_pred_deg=round(qh, 1))
            vmax = cfg.vmax_deg_s.get(name)
            if vmax and abs(f.v) > cfg.speed_frac * vmax:
                alert("overspeed", v_deg_s=round(f.v, 1))
            tau, tmax = j.get("tau_nm"), j.get("tau_max_nm")
            if tau is not None and tmax:
                if abs(tau) >= cfg.sat_frac * tmax:
                    f.sat_since = f.sat_since if f.sat_since is not None else t
                    if t - f.sat_since >= cfg.sat_s:
                        alert("torque_saturation", tau_nm=round(tau, 2), tau_max_nm=tmax)
                else:
                    f.sat_since = None
            qc = j.get("q_cmd_deg")
            if qc is not None and name != "gripper":   # the gripper holds short of its command on an object
                if abs(qc - q) >= cfg.div_deg:
                    f.div_since = f.div_since if f.div_since is not None else t
                    if t - f.div_since >= cfg.div_s:
                        alert("tracking_divergence", q_deg=round(q, 1), q_cmd_deg=round(qc, 1))
                else:
                    f.div_since = None
            sign = 0 if abs(f.v) < cfg.osc_min_speed else (1 if f.v > 0 else -1)
            if sign and f.last_sign and sign != f.last_sign:
                f.flips.append(t)
            if sign:
                f.last_sign = sign
            if sum(1 for x in f.flips if t - x <= cfg.osc_window_s) >= cfg.osc_flips:
                alert("oscillation", flips=cfg.osc_flips)
            if j.get("status") in FAULT_STATUSES:
                alert("fault", status=j.get("status"))
            hot = max(j.get("temp_rotor_c") or 0, j.get("temp_mos_c") or 0)
            if hot >= cfg.hot_c:
                alert("overheat", temp_c=hot)
        return alerts
