"""Darsh's operator decisions (plans/operator_notes.yaml) reach the plan, the monitor and the Dream prompt."""
from pathlib import Path

from harness.operator_notes import apply_raw, attempts_for, entry_for, load
from harness.plan import load_plan
from harness.promptgen import Context, compose

ROOT = Path(__file__).resolve().parents[1]
NOTES = load(ROOT / "plans/operator_notes.yaml")


def test_retry_rule_two_tries_three_for_high_points_pass_bars_untouched():
    plan = load_plan(ROOT / "plans/hill_climb_v4.yaml")
    by = {s.name: s for s in plan.stages}
    assert by["unplug_cable"].max_attempts == 99 and by["tower_pass"].max_attempts == 99  # pass bars keep trying
    assert by["unplug_more"].max_attempts == 2 and by["ball_1"].max_attempts == 2 and by["higher_1"].max_attempts == 2
    assert attempts_for(NOTES, entry_for(NOTES, "plug_1")) == 3       # +20 per plug: worth a third try


def test_white_ball_push_stage_is_inserted_once_before_the_balls_and_guarded():
    plan = load_plan(ROOT / "plans/hybrid_v4.yaml")
    names = [s.name for s in plan.stages]
    assert names.index("ball_push_white") == names.index("ball_1") - 1
    assert names.count("ball_push_white") == 1
    push = plan.stages[names.index("ball_push_white")]
    assert push.skip_unless and push.on_fail == "skip" and push.max_attempts == 1
    assert plan.guard.covers("ball_push_white")


def test_last_cube_hole_up_and_technique_and_error_detection_attached():
    plan = load_plan(ROOT / "plans/hill_climb_v4.yaml")
    assert plan.tower_target[2] == "blue cube, hole facing up" and plan.tower_target[0].endswith("sideways")
    stack3 = next(s for s in plan.stages if s.name == "stack_3")
    assert "hole faces straight up" in stack3.technique and "overhangs" in stack3.watch_for


def test_notes_can_be_switched_off_for_the_control_arm():
    plain = load_plan(ROOT / "plans/hill_climb_v4.yaml", operator_notes=False, prompt_technique=False)
    assert "ball_push_white" not in [s.name for s in plain.stages]
    assert all(s.technique is None and s.watch_for is None for s in plain.stages)
    assert not plain.prompt_technique


def test_prompt_technique_goes_after_the_safety_prefix_and_before_the_action():
    ctx = Context(prefix="No power.", technique="Grip  at full\nstrength.")
    assert compose("No power. unplug the cable", 1, ctx) == "No power. Grip at full strength. unplug the cable"
    assert compose("No power. unplug the cable", 1, Context(prefix="No power.")) == "No power. unplug the cable"
    assert compose("No power. unplug the cable", 3, ctx).startswith("No power. Grip at full strength.")


def test_monitor_sees_technique_and_failure_signs():
    from harness.verifiers import MonitorVerifier
    plan = load_plan(ROOT / "plans/hill_climb_v4.yaml")
    mon = MonitorVerifier(plan, transport=lambda payload: None)
    text = mon._stage_text(next(s for s in plan.stages if s.name == "unplug_more"), 3.0)
    assert "Operator's technique for this stage" in text and "Error detection:" in text
    assert "cord instead of the plug" in text


def test_apply_raw_does_not_mutate_and_respects_plan_overrides():
    raw = {"stages": [{"name": "ball_1", "prompt": "p", "on_fail": "skip", "technique": "mine"}],
           "tower_target": ["red cube, hole facing sideways"]}
    out = apply_raw(raw, NOTES)
    assert raw["stages"][0]["technique"] == "mine" and "watch_for" not in raw["stages"][0]
    assert out["stages"][-1]["technique"] == "mine" and out["stages"][-1]["watch_for"]
    assert out["tower_target"] == ["red cube, hole facing up"]
