"""The stage decision logic as a matrix state-space model with an observer, fast enough to run at 20+ Hz.

Two layers, both plain numpy and vectorised over K parameter sets at once (so a hill-climb can score thousands of
weight settings on one logged run in one pass):

1. OBSERVER (state estimation). The task state z = [progress, holding_target, object_at_goal, illegal_in_grip,
   cable_out] is estimated from two kinds of measurement:
     - fast, noisy local features every tick (colour detector, cable-end classifier, gripper sensors), and
     - slow, more reliable Claude verdicts whenever one lands (every ~5 s, 6.5-13 s old).
   z_hat <- F z_hat + L_fast (y_fast - H_fast z_hat)        every tick
   z_hat <- z_hat + L_slow (y_slow - H_slow z_hat)          when a verdict arrives
   (a fixed-gain Luenberger / steady-state Kalman form; the gains L are tunable weights).

2. DECISION STATE. x = [done evidence, failing evidence, stalled evidence, guard evidence (monitor), guard evidence
   (local), best progress, seconds since progress rose, elapsed, seconds since last counted done].
     x <- (I - diag(r(u))) A x + B u            r(u) in {0,1}^n: which states an input resets
   and decisions are threshold tests on y = C x. With the default weights this reproduces the runner's rules
   (confirm-in-a-row, done window, min gap, fail-after-half-timeout, stall, guard counts); the tests check that.

Why matrices: the update is a gated linear recurrence, i.e. one recurrent cell
   h' = (1 - g(u)) * (A h) + B u,   g(u) = clip(R u, 0, 1)
so the same weights run as numpy on the CPU (microseconds a tick) or can be exported as an RNN cell (`export`) and
learned from logged sim runs once there is data to fit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------------------------------------------- names
X = ("done", "fail", "stalled", "guard_m", "guard_l", "pmax", "since_rise", "t", "since_done")
U = ("dt", "v_new", "v_done", "v_same", "v_failing", "v_stalled", "v_rise", "v_guard_yes", "v_guard_no",
     "l_guard_yes", "l_guard_no", "obs_release")
Z = ("progress", "holding", "at_goal", "illegal_grip", "cable_out")
XI = {n: i for i, n in enumerate(X)}
UI = {n: i for i, n in enumerate(U)}
ZI = {n: i for i, n in enumerate(Z)}
BIG = 1e6


@dataclass
class Weights:
    """Everything a hill-climb may tune. Defaults = today's runner (runner.py constants and plan v4)."""
    confirm: float = 2.0            # done looks needed
    window_s: float = 15.0          # a second done within this passes
    min_gap_s: float = 3.0          # dones closer than this are one look (plan confirm_gap_s)
    fail_confirm: float = 2.0
    fail_min_fraction: float = 0.5  # monitor failing only counts after this share of the timeout
    stall_confirm: float = 2.0
    guard_confirm: float = 1.0      # plan guard confirm (monitor)
    live_guard_frames: float = 3.0  # live guard frames in a row
    w_goal: float = 0.0             # observer: object at goal adds to done evidence per second (fast path, off)
    w_release: float = 0.0          # observer: gripper released at the goal adds this once
    goal_conf: float = 0.9          # observer at_goal estimate above this counts as evidence
    # observer gains
    l_fast: float = 0.25            # how much one local measurement moves the estimate
    l_slow: float = 0.8             # how much one Claude verdict moves it
    decay: float = 0.98             # per-tick pull of the estimate toward "unknown" (0.5) without measurements


# ------------------------------------------------------------------------------------------------ observer
@dataclass
class Observer:
    """Fixed-gain observer over z (Z). Measurements are dicts name -> value in 0..1 (None/missing = not measured)."""
    w: Weights = field(default_factory=Weights)
    z: np.ndarray = field(default_factory=lambda: np.full(len(Z), 0.5))

    def reset(self) -> None:
        self.z = np.full(len(Z), 0.5)
        self.z[ZI["progress"]] = 0.0

    def predict(self) -> None:
        # F: progress holds, beliefs relax toward 0.5 (unknown) between measurements
        prog = self.z[ZI["progress"]]
        self.z = 0.5 + self.w.decay * (self.z - 0.5)
        self.z[ZI["progress"]] = prog

    def _update(self, meas: dict[str, float | None], gain: float) -> None:
        for k, v in meas.items():
            if v is None or k not in ZI:
                continue
            i = ZI[k]
            self.z[i] += gain * (float(v) - self.z[i])
        np.clip(self.z, 0.0, 1.0, out=self.z)

    def fast(self, meas: dict[str, float | None]) -> None:
        self.predict()
        self._update(meas, self.w.l_fast)

    def slow(self, meas: dict[str, float | None]) -> None:
        self._update(meas, self.w.l_slow)


# ---------------------------------------------------------------------------------------- decision model
class StateSpace:
    """x <- (I - diag(r)) A x + B u, decisions from y = C x. Vectorised over K weight sets (x has shape (K, n))."""

    def __init__(self, weights: list[Weights] | Weights | None = None):
        ws = weights if isinstance(weights, list) else [weights or Weights()]
        self.ws = ws
        self.K, n, m = len(ws), len(X), len(U)
        A = np.eye(n)
        B = np.zeros((m, n))            # u @ B  -> increments
        R = np.zeros((m, n))            # u @ R  -> reset mask (clipped to 0..1)
        B[UI["dt"], XI["since_rise"]] = 1.0
        B[UI["dt"], XI["t"]] = 1.0
        B[UI["dt"], XI["since_done"]] = 1.0
        B[UI["v_guard_yes"], XI["guard_m"]] = 1.0
        B[UI["l_guard_yes"], XI["guard_l"]] = 1.0
        R[UI["v_rise"], XI["since_rise"]] = 1.0
        R[UI["v_guard_no"], XI["guard_m"]] = 1.0
        R[UI["l_guard_no"], XI["guard_l"]] = 1.0
        self.A, self.B, self.R = A, B, R
        self.x = np.zeros((self.K, n))
        self.x[:, XI["since_done"]] = BIG
        self.x[:, XI["since_rise"]] = 0.0
        self.timeout_s, self.stall_s, self.monitor = 60.0, None, True
        self.passed = np.zeros(self.K, bool)
        self._stalled = 0.0
        vec = lambda f: np.array([getattr(w, f) for w in ws], float)  # noqa: E731
        self._conf, self._window, self._gap = vec("confirm"), vec("window_s"), vec("min_gap_s")
        self._w_goal, self._w_release, self._goal_conf = vec("w_goal"), vec("w_release"), vec("goal_conf")
        # decide()'s thresholds, built once: a per-tick list comprehension over K weight sets is the slow part of an
        # offline sweep with thousands of them
        self._fail_frac, self._fail_conf = vec("fail_min_fraction"), vec("fail_confirm")
        self._stall_conf, self._guard_conf, self._live_frames = vec("stall_confirm"), vec("guard_confirm"),             vec("live_guard_frames")

    def start(self, timeout_s: float, stall_s: float | None, *, monitor: bool = True) -> None:
        self.x[:] = 0.0
        self.x[:, XI["since_done"]] = BIG
        self.timeout_s, self.stall_s, self.monitor = timeout_s, stall_s, monitor
        self.passed[:] = False
        self._stalled = 0.0

    def u(self, **kw: float) -> np.ndarray:
        v = np.zeros(len(U))
        for k, val in kw.items():
            v[UI[k]] = float(val)
        return v

    def step(self, u: np.ndarray, observer_z: np.ndarray | None = None) -> dict[str, np.ndarray]:
        """One tick. u: input vector (U); v_* inputs are 1 only on the tick a real monitor verdict lands
        (v_new), l_* on a tick the local detector judged a new frame. Returns decision arrays of shape (K,)."""
        x = self.x
        # linear part: integrators (time, since_rise, since_done) and the guard counters with their resets
        reset = np.clip(u @ self.R, 0.0, 1.0)
        x[:] = (1.0 - reset) * (x @ self.A.T) + u @ self.B
        pass_now = np.zeros(self.K, bool)
        if u[UI["v_new"]]:                       # the runner's per-verdict branches, vectorised over K
            x[:, XI["stalled"]] = x[:, XI["stalled"]] * 0 + (self._stalled + 1 if u[UI["v_stalled"]] else 0)
            self._stalled = x[0, XI["stalled"]]
            since = x[:, XI["since_done"]]
            in_window = since <= self._window
            if u[UI["v_same"]]:
                pass                             # the same frames judged twice are one look
            elif u[UI["v_done"]]:
                ok = since >= self._gap          # closer than min_gap_s: one look, not two
                recent = ok & in_window
                x[ok, XI["fail"]] = 0.0
                x[ok, XI["done"]] += 1.0
                x[ok, XI["since_done"]] = 0.0
                pass_now = ok & (recent | (x[:, XI["done"]] >= self._conf))
            elif u[UI["v_failing"]]:
                out = ~in_window                 # one flicker after a done does not undo it
                x[out, XI["done"]] = 0.0
                x[out, XI["fail"]] += 1.0
            else:
                x[:, XI["done"]] = 0.0
                x[:, XI["fail"]] = 0.0
        if observer_z is not None:               # observer fast path (weights 0 by default)
            at_goal = observer_z[ZI["at_goal"]] >= self._goal_conf
            x[:, XI["done"]] += self._w_goal * at_goal * u[UI["dt"]] + self._w_release * u[UI["obs_release"]]
            pass_now |= x[:, XI["done"]] >= self._conf
        self.passed |= pass_now
        return self.decide(pass_now)

    def decide(self, pass_now: np.ndarray | None = None) -> dict[str, np.ndarray]:
        x = self.x
        t = x[:, XI["t"]]
        no_done = x[:, XI["since_done"]] >= BIG / 2
        fmin = self._fail_frac * self.timeout_s
        fail = (x[:, XI["fail"]] >= self._fail_conf) & ((t >= fmin) | (not self.monitor))
        stall = np.zeros(self.K, bool)
        if self.stall_s is not None:
            stall = no_done & ((x[:, XI["stalled"]] >= self._stall_conf)
                               | ((x[:, XI["since_rise"]] >= self.stall_s) & (x[:, XI["pmax"]] < 0.95)
                                  & (x[:, XI["pmax"]] > 0)))
        guard = (x[:, XI["guard_m"]] >= self._guard_conf) | (x[:, XI["guard_l"]] >= self._live_frames)
        timeout = t >= self.timeout_s
        return {"pass": pass_now if pass_now is not None else np.zeros(self.K, bool), "fail": fail, "stall": stall,
                "guard": guard, "timeout": timeout}

    def note_progress(self, p: float, rise: float = 0.05) -> bool:
        """Feature stage: returns v_rise (progress rose by `rise` over the best so far) and keeps pmax."""
        rose = p >= self.x[0, XI["pmax"]] + rise
        if p > self.x[0, XI["pmax"]]:
            self.x[:, XI["pmax"]] = p
        return bool(rose)

    # -- as a neural network ------------------------------------------------------------------------------
    def export(self, path: str) -> None:
        """Saves the recurrent cell: h' = (1 - clip(u R, 0, 1)) * (h A^T) + u B, plus the thresholds per weight
        set. Loadable with numpy, or as the weights of a gated RNN cell in any framework."""
        thr = np.array([[w.confirm, w.window_s, w.min_gap_s, w.fail_confirm, w.fail_min_fraction, w.stall_confirm,
                         w.guard_confirm, w.live_guard_frames, w.w_goal, w.w_release, w.goal_conf] for w in self.ws])
        np.savez(path, A=self.A, B=self.B, R=self.R, thresholds=thr, x_names=np.array(X), u_names=np.array(U))
