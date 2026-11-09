"""The runner's decision tree as weighted equations, so its constants can be tuned (HARNESS_DECISION_MODEL section 5).

Three pieces, all pure functions of numbers (no I/O, no clock):

1. Done evidence, a running score over looks:  l_t = keep * l_{t-1} + sum_i w_i * phi_i(t),  done when l_t >= theta.
   With DEFAULT weights it is exactly the runner's rule: `confirm` monitor "done" looks in a row pass the stage, a
   "not done" look resets the count, a repeated look at the same frames counts for nothing, and a second "done"
   within done_window_s passes even with a "failing" flicker in between.
2. Fail score:  F = combine_k(w_k * g_k), each g_k a ratio that reaches 1 when today's hard rule fires
   (consecutive failing looks / confirm, consecutive "stalled" looks / confirm, time since progress rose / stall_s,
   elapsed / timeout_s). A look is failing on a no, a "failing" status, or progress falling REGRESSION_DROP below
   the attempt's best (look_failing). The attempt fails when F >= theta_fail. combine="max" is today's tree (any one rule);
   combine="sum" lets weak signals add up, which is what tuning can explore.
3. Value of the time left:  W_i(L) = max_T  F_i(T) (V_i + W_{i+1}(L - t_succ)) + (1 - F_i(T)) * [skip ? W_{i+1}(L - T) : 0]
   over a stage list, by dynamic programming on a 1 s grid. It gives each stage the timeout that maximises expected
   points for the time left (`plan_timeouts`) and the value of an action (`action_value`):
   Q(a) = P_a * V_s + W(L - c_a) - risk_weight * R_a.

Nothing here moves a robot or changes the runner yet: the runner keeps its own rules. The tests check that the
default weights reproduce them; tuning and shadow logging come next.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Iterable, Sequence

# Today's runner constants (harness/runner.py), the starting point for tuning.
CONFIRM = 2
DONE_WINDOW_S = 15.0
REGRESSION_DROP = 0.3
FAIL_MIN_FRACTION = 0.5


# --------------------------------------------------------------------------------------------- 1. done evidence
@dataclass(frozen=True)
class DoneWeights:
    w_monitor: float = 1.0          # a monitor "done" look
    w_sensor_release: float = 0.0   # gripper went from closed on an object to open near the target (/arms); new, off
    w_goal_box: float = 0.0         # detector sees the object at its goal (in the bin, on the tower); new, off
    w_plug_count: float = 0.0       # cable-end boxes: one fewer plug in the strip; new, off
    keep: float = 1.0               # share of the score carried to the next look when nothing argues against it
    reset_on_no: bool = True        # a "not done" / "failing" look sets the score to 0 (outside the done window)
    theta: float = float(CONFIRM)   # done when the score reaches this
    window_s: float = DONE_WINDOW_S  # a second "done" within this passes, flickers in between notwithstanding


@dataclass(frozen=True)
class Look:
    """One check. done: the verifier's answer (True / False / None = no answer). failing: monitor status failing."""
    t: float
    done: bool | None
    failing: bool = False
    same_frames: bool = False       # the same camera images as the previous done look
    monitor: bool = True            # from the monitor: a look with no done answer still resets the count
    sensor_release: float = 0.0     # 0..1 features from the fast sensors
    goal_box: float = 0.0
    plug_count: float = 0.0


@dataclass
class DoneState:
    score: float = 0.0
    last_done_t: float | None = None


def done_step(state: DoneState, look: Look, w: DoneWeights = DoneWeights()) -> tuple[DoneState, bool]:
    """Folds one look into the done score. Returns (new state, stage passes now)."""
    fast = w.w_sensor_release * look.sensor_release + w.w_goal_box * look.goal_box + w.w_plug_count * look.plug_count
    if look.done is True and look.same_frames:
        return DoneState(state.score * w.keep + fast, state.last_done_t), state.score * w.keep + fast >= w.theta
    if look.done is True:
        recent = state.last_done_t is not None and look.t - state.last_done_t <= w.window_s
        score = state.score * w.keep + w.w_monitor + fast
        return DoneState(score, look.t), score >= w.theta or recent
    # The runner: a no / failing look right after a done (within the window) changes nothing; otherwise it resets the
    # count. A monitor look with no done answer resets it even inside the window (the window still passes the next done).
    flicker = look.done is False or look.failing
    blank = look.done is None and look.monitor
    in_window = state.last_done_t is not None and look.t - state.last_done_t <= w.window_s
    if flicker and in_window:
        score = state.score
    elif (flicker or blank) and w.reset_on_no:
        score = fast
    elif flicker or blank:
        score = state.score * w.keep + fast
    else:
        score = state.score * w.keep + fast
    return DoneState(score, state.last_done_t), score >= w.theta


def run_looks(looks: Iterable[Look], w: DoneWeights = DoneWeights()) -> float | None:
    """Time of the look that passes the stage, or None."""
    st = DoneState()
    for lk in looks:
        st, ok = done_step(st, lk, w)
        if ok:
            return lk.t
    return None


# ----------------------------------------------------------------------------------------------- 2. fail score
@dataclass(frozen=True)
class FailWeights:
    w_fail: float = 1.0          # consecutive failing looks / confirm (monitor: only after min_fraction of the timeout)
    w_stalled: float = 1.0       # consecutive "stalled" monitor looks / confirm (stages with a stall window)
    w_stall: float = 1.0         # seconds since progress last rose / stall_s
    w_time: float = 1.0          # elapsed / timeout_s
    w_on_target: float = 0.0     # subtracted while a gripper holds the target (today: a one-off 20 s extension)
    w_regress: float = 1.0       # in look_failing: (max progress - progress) / regress_drop >= 1 makes a failing look
    confirm: int = CONFIRM
    regress_drop: float = REGRESSION_DROP
    min_fraction: float = FAIL_MIN_FRACTION
    combine: str = "max"         # "max" = today's tree (any rule fires); "sum" = weak signals add up
    theta: float = 1.0


def look_failing(done: bool | None, status_failing: bool, progress: float | None, max_progress: float | None,
                 done_seen: bool, w: FailWeights = FailWeights()) -> bool:
    """Is one look a failing look? The verifier said no, the monitor said failing, or progress fell regress_drop
    below the attempt's best (not counted once a done has been seen, as in the runner)."""
    regress = 0.0
    if progress is not None and max_progress is not None and not done_seen:
        regress = w.w_regress * max(0.0, max_progress - progress) / w.regress_drop
    return done is False or status_failing or regress >= 1.0 - 1e-9


@dataclass(frozen=True)
class AttemptState:
    elapsed: float
    timeout_s: float
    consec_fail: int = 0                 # failing looks in a row (look_failing)
    consec_stalled: int = 0              # "stalled" monitor looks in a row
    since_rise_s: float | None = None    # seconds since progress last rose by STALL_RISE (None: no progress yet)
    stall_s: float | None = None         # the stage's stall window (None: no stall rules)
    monitor: bool = True                 # failing looks come from the monitor (then min_fraction applies)
    on_target: bool = False


def fail_terms(a: AttemptState, w: FailWeights = FailWeights()) -> dict[str, float]:
    """Each term reaches its weight when its hard rule would fire on its own."""
    gate_ok = not a.monitor or a.elapsed >= w.min_fraction * a.timeout_s
    terms = {"fail": w.w_fail * (a.consec_fail / w.confirm) if gate_ok else 0.0,
             "time": w.w_time * a.elapsed / a.timeout_s}
    if a.stall_s is not None:
        terms["stalled"] = w.w_stalled * a.consec_stalled / w.confirm
        if a.since_rise_s is not None:
            terms["stall"] = w.w_stall * a.since_rise_s / a.stall_s
    return terms


def fail_score(a: AttemptState, w: FailWeights = FailWeights()) -> float:
    terms = list(fail_terms(a, w).values())
    s = max(terms) if w.combine == "max" else sum(terms)
    return s - (w.w_on_target if a.on_target else 0.0)


def attempt_fails(a: AttemptState, w: FailWeights = FailWeights()) -> bool:
    return fail_score(a, w) >= w.theta - 1e-9


# -------------------------------------------------------------------------------- 3. value of the time left
@dataclass(frozen=True)
class StageModel:
    """One stage for planning. Success by time tau: F(tau) = p_max * (1 - exp(-max(0, tau - t_min) / tau_c)).
    `gate`: a pass bar; if it fails, nothing after it scores (it is retried until the clock instead of skipped).
    Fit p_max, t_min, tau_c per stage from run logs or the stress sim."""
    name: str
    value: float
    p_max: float
    tau_c: float
    t_min: float = 0.0
    gate: bool = False

    def F(self, tau: float) -> float:
        if tau <= self.t_min:
            return 0.0
        return self.p_max * (1.0 - math.exp(-(tau - self.t_min) / self.tau_c))

    def mean_success_time(self, T: float, step: float = 1.0) -> float:
        """Expected time spent given success by T (integral of tau dF over [0, T] / F(T))."""
        FT = self.F(T)
        if FT <= 0:
            return T
        n = max(1, int(T / step))
        acc, prev = 0.0, 0.0
        for k in range(1, n + 1):
            tau = k * T / n
            f = self.F(tau)
            acc += (tau - T / n / 2) * (f - prev)
            prev = f
        return acc / FT


@dataclass
class Plan:
    """W[i][L]: expected points from stage i on with L whole seconds left; best_T[i][L]: that stage's timeout."""
    stages: Sequence[StageModel]
    horizon_s: int
    W: list[list[float]] = field(default_factory=list)
    best_T: list[list[int]] = field(default_factory=list)

    def value(self, i: int, left_s: float) -> float:
        if i >= len(self.stages):
            return 0.0
        return self.W[i][max(0, min(self.horizon_s, int(left_s)))]

    def timeout(self, i: int, left_s: float) -> int:
        return self.best_T[i][max(0, min(self.horizon_s, int(left_s)))]


def plan_timeouts(stages: Sequence[StageModel], horizon_s: int, step_s: int = 1) -> Plan:
    """Backward DP over the stage list. For a gate stage a failure leaves 0 (nothing later scores)."""
    n = len(stages)
    W = [[0.0] * (horizon_s + 1) for _ in range(n + 1)]
    best = [[0] * (horizon_s + 1) for _ in range(n)]
    for i in range(n - 1, -1, -1):
        st = stages[i]
        succ_t = [st.mean_success_time(T) for T in range(horizon_s + 1)]
        for L in range(horizon_s + 1):
            top, top_T = W[i + 1][L], 0           # T = 0: skip the stage
            for T in range(step_s, L + 1, step_s):
                p = st.F(T)
                after_ok = W[i + 1][max(0, L - int(round(succ_t[T])))]
                after_fail = 0.0 if st.gate else W[i + 1][L - T]
                v = p * (st.value + after_ok) + (1 - p) * after_fail
                if v > top + 1e-12:
                    top, top_T = v, T
            W[i][L], best[i][L] = top, top_T
    return Plan(stages, horizon_s, W, best)


@dataclass(frozen=True)
class Action:
    """A choice at a decision point: p = chance it succeeds the stage, cost_s = seconds it uses, risk = 0..1 chance of
    something costly (a DQ, a knocked tower, an illegal ball)."""
    name: str
    p: float
    cost_s: float
    risk: float = 0.0


def action_value(plan: Plan, i: int, left_s: float, a: Action, risk_weight: float = 50.0) -> float:
    """Q(a) = p (V_i + W_{i+1}(L - c)) + (1 - p) W_i'(L - c) - risk_weight * risk, where after a failed action the
    same stage can still be tried with the time left (W_i), or nothing later scores for a gate that ran out."""
    st = plan.stages[i]
    rest = left_s - a.cost_s
    if rest < 0:
        return -risk_weight * a.risk
    ok = a.p * (st.value + plan.value(i + 1, rest))
    again = (1 - a.p) * plan.value(i, rest)
    return ok + again - risk_weight * a.risk


def choose(plan: Plan, i: int, left_s: float, actions: Sequence[Action], risk_weight: float = 50.0) -> Action:
    return max(actions, key=lambda a: action_value(plan, i, left_s, a, risk_weight))


def with_weights(obj, **kw):
    """Copy of a weights dataclass with some fields changed (for hill climbing)."""
    return replace(obj, **kw)


# ------------------------------------------------------------------------------------------ 4. shadow mode
def weights_from(raw: dict | None) -> tuple[DoneWeights, FailWeights]:
    """plan `decide: {done: {...}, fail: {...}}` -> weights. Unknown keys are an error (a typo must not pass)."""
    raw = raw or {}
    extra = set(raw) - {"done", "fail"}
    if extra:
        raise ValueError(f"plan.decide: unknown key(s): {', '.join(sorted(extra))}")
    out = []
    for key, cls in (("done", DoneWeights), ("fail", FailWeights)):
        part = raw.get(key) or {}
        if not isinstance(part, dict):
            raise ValueError(f"plan.decide.{key}: must be a mapping")
        names = set(cls.__dataclass_fields__)
        bad = set(part) - names
        if bad:
            raise ValueError(f"plan.decide.{key}: unknown weight(s): {', '.join(sorted(bad))}")
        if key == "fail" and part.get("combine", "max") not in ("max", "sum"):
            raise ValueError("plan.decide.fail.combine: max or sum")
        out.append(cls(**part))
    return out[0], out[1]


class Shadow:
    """Runs the equations next to the runner on every look and reports what they would decide. It never acts.

    The runner calls start() at each attempt and look() once per verdict, with the inputs it already computed
    (`failing` includes the regression rule). Counts follow the runner's: a failing look right after a done (within
    the window) is not counted, a done resets the failing count."""

    def __init__(self, done: DoneWeights = DoneWeights(), fail: FailWeights = FailWeights(), *,
                 fixed: set[str] = frozenset()):
        self.base_done, self.base_fail = done, fail
        self.done_w, self.fail_w = done, fail
        self._fixed = set(fixed)        # "done.theta", "fail.confirm": set by the plan, not taken from the stage
        self._st = DoneState()
        self._consec_fail = 0
        self._timeout = 1.0
        self._stall: float | None = None

    @classmethod
    def from_plan(cls, raw: dict | None) -> "Shadow":
        done, fail = weights_from(raw)
        raw = raw or {}
        fixed = {f"{k}.{n}" for k in ("done", "fail") for n in (raw.get(k) or {})}
        return cls(done, fail, fixed=fixed)

    def start(self, timeout_s: float, stall_s: float | None, confirm: int = CONFIRM) -> None:
        """Per attempt. The stage's confirm sets the done threshold and the failing-look count unless the plan fixed them."""
        self.done_w = self.base_done if "done.theta" in self._fixed else replace(self.base_done, theta=float(confirm))
        self.fail_w = self.base_fail if "fail.confirm" in self._fixed else replace(self.base_fail, confirm=int(confirm))
        self._st, self._consec_fail = DoneState(), 0
        self._timeout, self._stall = timeout_s, stall_s

    def look(self, elapsed: float, done: bool | None, failing: bool, *, same_frames: bool, monitor: bool,
             consec_stalled: int, since_rise_s: float | None, sensor_release: float = 0.0, goal_box: float = 0.0,
             plug_count: float = 0.0) -> dict:
        lk = Look(elapsed, done, failing=failing and done is not False, same_frames=same_frames, monitor=monitor,
                  sensor_release=sensor_release, goal_box=goal_box, plug_count=plug_count)
        in_window = self._st.last_done_t is not None and elapsed - self._st.last_done_t <= self.done_w.window_s
        self._st, passes = done_step(self._st, lk, self.done_w)
        if done is True and not same_frames:
            self._consec_fail = 0
        elif (failing or done is False) and not in_window:
            self._consec_fail += 1
        elif not (failing or done is False) and (done is not None or monitor):
            self._consec_fail = 0
        a = AttemptState(elapsed=elapsed, timeout_s=self._timeout, consec_fail=self._consec_fail,
                         consec_stalled=consec_stalled, since_rise_s=since_rise_s, stall_s=self._stall, monitor=monitor)
        terms = fail_terms(a, self.fail_w)
        score = fail_score(a, self.fail_w)
        return {"done_score": round(self._st.score, 3), "would_pass": bool(passes),
                "fail_score": round(score, 3), "would_fail": bool(not passes and score >= self.fail_w.theta - 1e-9),
                "terms": {k: round(v, 3) for k, v in terms.items()}}
