"""harness.decide: with default weights the equations make the same calls as the runner's rules."""

import random



from harness.decide import (Action, AttemptState, DoneWeights, FailWeights, Look, StageModel, action_value,
                            attempt_fails, choose, look_failing, plan_timeouts, run_looks)

CONFIRM, WINDOW = 2, 15.0


def runner_done(looks):
    """The done branch of Runner._watch, transcribed (frames always present, no immediate verdicts)."""
    consec_done, last_done_t = 0, None
    for lk in looks:
        failing = lk.done is False or lk.failing
        if lk.done is True and lk.same_frames:
            continue
        if lk.done is True:
            consec_done += 1
            recent = last_done_t is not None and lk.t - last_done_t <= WINDOW
            last_done_t = lk.t
            if consec_done >= CONFIRM or recent:
                return lk.t
        elif failing and last_done_t is not None and lk.t - last_done_t <= WINDOW:
            pass
        elif failing:
            consec_done = 0
        elif lk.done is not None or lk.monitor:
            consec_done = 0
    return None


def random_looks(rng):
    t, out, have_done = 0.0, [], False
    for _ in range(rng.randint(1, 14)):
        t += rng.choice([1.0, 3.0, 3.0, 6.0, 12.0, 20.0])
        done = rng.choice([True, True, False, None])
        out.append(Look(t, done, failing=rng.random() < 0.2, same_frames=have_done and done is True and rng.random() < 0.3,
                        monitor=rng.random() < 0.8))
        have_done = have_done or done is True
    return out


def test_default_done_weights_match_the_runner_on_random_sequences():
    rng = random.Random(7)
    for _ in range(5000):
        looks = random_looks(rng)
        assert run_looks(looks) == runner_done(looks), looks


def test_done_cases_by_hand():
    assert run_looks([Look(3, True), Look(6, True)]) == 6                     # confirm 2
    assert run_looks([Look(3, True), Look(6, False), Look(9, True)]) == 9      # second done within the window
    assert run_looks([Look(3, True), Look(25, False), Look(28, True)]) is None  # outside it, the no resets
    assert run_looks([Look(3, True), Look(6, True, same_frames=True)]) is None  # same frames: one look, not two


def test_fast_sensor_evidence_can_pass_a_stage_sooner_when_weighted():
    looks = [Look(3, True, sensor_release=1.0)]
    assert run_looks(looks) is None
    assert run_looks(looks, DoneWeights(w_sensor_release=1.0)) == 3


def runner_fails(a: AttemptState) -> bool:
    """The fail rules of Runner._watch (stall-on-progress only checked past the settle gate, as here)."""
    if a.consec_fail >= CONFIRM and (not a.monitor or a.elapsed >= 0.5 * a.timeout_s):
        return True
    if a.stall_s is not None and a.consec_stalled >= CONFIRM:
        return True
    if a.stall_s is not None and a.since_rise_s is not None and a.since_rise_s >= a.stall_s:
        return True
    return a.elapsed >= a.timeout_s


def test_default_fail_weights_match_the_runner():
    rng = random.Random(3)
    for _ in range(20000):
        timeout = rng.choice([10.0, 20.0, 40.0, 60.0])
        stall = rng.choice([None, 20.0, 35.0])
        a = AttemptState(elapsed=rng.uniform(0, 70), timeout_s=timeout, consec_fail=rng.randint(0, 3),
                         consec_stalled=rng.randint(0, 3), since_rise_s=rng.choice([None, rng.uniform(0, 40)]),
                         stall_s=stall, monitor=rng.random() < 0.8)
        assert attempt_fails(a) == runner_fails(a), a


def test_a_regression_makes_a_failing_look_not_an_instant_fail():
    assert look_failing(None, False, progress=0.3, max_progress=0.6, done_seen=False)
    assert not look_failing(None, False, progress=0.4, max_progress=0.6, done_seen=False)
    assert not look_failing(None, False, progress=0.3, max_progress=0.6, done_seen=True)


def test_sum_combination_lets_weak_signals_add_up():
    a = AttemptState(elapsed=30, timeout_s=60, consec_fail=1, since_rise_s=10, stall_s=20)
    assert not attempt_fails(a)                                  # every rule is half way
    assert attempt_fails(a, FailWeights(combine="sum"))           # together they pass 1


STAGES = [
    StageModel("unplug", value=10, p_max=0.9, tau_c=25, t_min=5, gate=True),
    StageModel("extra_cable", value=5, p_max=0.5, tau_c=15, t_min=4),
    StageModel("tower", value=20, p_max=0.7, tau_c=40, t_min=8, gate=True),
    StageModel("ball", value=5, p_max=0.6, tau_c=12, t_min=4),
]


def test_value_of_time_never_falls_as_time_grows():
    plan = plan_timeouts(STAGES, 300)
    for i in range(len(STAGES)):
        assert all(plan.W[i][L] <= plan.W[i][L + 1] + 1e-9 for L in range(300))
    assert plan.value(0, 300) > plan.value(0, 60) > 0


def test_optional_stage_gets_less_time_when_the_clock_is_short():
    plan = plan_timeouts(STAGES, 300)
    assert plan.timeout(1, 60) <= plan.timeout(1, 250)
    assert plan.timeout(1, 20) == 0 or plan.timeout(1, 20) < plan.timeout(1, 250)   # often skipped when short


def test_gate_stage_is_never_skipped_while_it_can_still_pay():
    plan = plan_timeouts(STAGES, 300)
    assert plan.timeout(0, 120) > 0 and plan.timeout(2, 100) > 0


def test_action_choice_weighs_points_time_and_risk():
    plan = plan_timeouts(STAGES, 300)
    keep = Action("keep_going", p=0.5, cost_s=10)
    reset = Action("reset_then_retry", p=0.7, cost_s=25)
    risky = Action("reach_across_tower", p=0.8, cost_s=8, risk=0.3)
    assert choose(plan, 2, 200, [keep, reset, risky]).name != "reach_across_tower"
    assert action_value(plan, 2, 5, reset) <= action_value(plan, 2, 5, keep)   # 5 s left: a 25 s reset can't pay


def test_action_too_long_for_the_clock_is_worth_nothing():
    plan = plan_timeouts(STAGES, 60)
    assert action_value(plan, 3, 3, Action("x", p=1.0, cost_s=10)) == 0.0
