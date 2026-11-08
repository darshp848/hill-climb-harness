"""The matrix state-space model must make the runner's decisions (decide.py is the reference for the done rule)."""

from __future__ import annotations

import random
import time

import numpy as np

from harness import decide
from harness.hedge import first_of
from harness.statespace import Observer, StateSpace, Weights


def _looks(rng: random.Random, n: int = 40):
    t, out = 0.0, []
    for _ in range(n):
        t += rng.choice([1.0, 2.5, 3.0, 5.0, 9.0])
        r = rng.random()
        done = True if r < 0.35 else (False if r < 0.55 else None)
        failing = done is not True and rng.random() < 0.3
        same = done is True and rng.random() < 0.15
        out.append(decide.Look(t, done, failing=failing, same_frames=same, monitor=True))
    return out


def _ss_pass_time(looks, w: Weights) -> float | None:
    ss = StateSpace(w)
    ss.start(timeout_s=1e9, stall_s=None)
    prev = 0.0
    for lk in looks:
        u = ss.u(dt=lk.t - prev, v_new=1, v_done=lk.done is True, v_same=lk.same_frames,
                 v_failing=(lk.done is False) or lk.failing)
        prev = lk.t
        if ss.step(u)["pass"][0]:
            return lk.t
    return None


def test_matches_reference_done_rule_on_random_runs():
    rng = random.Random(7)
    for _ in range(3000):
        looks = _looks(rng)
        ref = decide.run_looks(looks)
        got = _ss_pass_time(looks, Weights(min_gap_s=0.0))
        assert got == ref, (looks, ref, got)


def test_min_gap_merges_close_dones():
    w = Weights(min_gap_s=3.0)
    looks = [decide.Look(10.0, True), decide.Look(11.0, True), decide.Look(14.5, True)]
    assert _ss_pass_time(looks, w) == 14.5          # 11.0 is too close to 10.0 to count
    assert _ss_pass_time(looks, Weights(min_gap_s=0.0)) == 11.0


def test_vectorised_over_weight_sets():
    ws = [Weights(confirm=c, min_gap_s=0.0, window_s=0.0) for c in (1, 2, 3)]
    ss = StateSpace(ws)
    ss.start(1e9, None)
    passes = []
    for k in range(3):
        d = ss.step(ss.u(dt=5, v_new=1, v_done=1))
        passes.append(d["pass"].copy())
    assert passes[0].tolist() == [True, False, False]
    assert passes[1].tolist() == [True, True, False]
    assert passes[2].tolist() == [True, True, True]


def test_guards_and_fail_rule():
    ss = StateSpace(Weights())
    ss.start(timeout_s=40, stall_s=None)
    for _ in range(2):
        d = ss.step(ss.u(dt=3, l_guard_yes=1))
    assert not d["guard"][0]
    d = ss.step(ss.u(dt=0.1, l_guard_yes=1))
    assert d["guard"][0]                             # 3 live frames in a row
    ss.start(timeout_s=40, stall_s=None)
    for _ in range(2):
        d = ss.step(ss.u(dt=5, v_new=1, v_failing=1))
    assert not d["fail"][0]                          # only 10 s of a 40 s stage: too early
    d = ss.step(ss.u(dt=15, v_new=1, v_failing=1))
    assert d["fail"][0]


def test_observer_blends_fast_and_slow():
    ob = Observer(Weights(l_fast=0.25, l_slow=0.8))
    ob.reset()
    for _ in range(4):
        ob.fast({"at_goal": 1.0})
    fast_only = ob.z[2]
    ob.slow({"at_goal": 0.0})                        # Claude says no: it wins most of the way
    assert fast_only > 0.6 and ob.z[2] < 0.3


def test_tick_is_fast():
    ss = StateSpace([Weights() for _ in range(1000)])
    ss.start(60, 20)
    u = ss.u(dt=0.05, v_new=1, v_done=1)
    t = time.perf_counter()
    for _ in range(200):
        ss.step(u)
    per_tick_ms = (time.perf_counter() - t) / 200 * 1000
    assert per_tick_ms < 5.0                         # 1,000 weight sets per tick, well inside a 50 ms (20 Hz) budget


def test_first_of_takes_the_fastest_good_answer():
    delays = iter([0.3, 0.05, 0.2])

    def call():
        d = next(delays)
        time.sleep(d)
        return d
    res = first_of(call, 3)
    assert res.value == 0.05 and res.latency_s < 0.2
    bad = first_of(lambda: (_ for _ in ()).throw(RuntimeError("x")), 2, timeout_s=1)
    assert res.winner is not None and bad.winner is None and bad.errors
    assert np.isfinite(res.latency_s)
