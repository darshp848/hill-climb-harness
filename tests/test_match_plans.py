"""The Hill Climb match plans, --var placeholders and done_message."""

from pathlib import Path

import pytest

from harness.cli import main
from harness.plan import load_plan, substitute

PLANS = Path(__file__).resolve().parent.parent / "plans"


def test_substitute_fills_and_leaves_flow_maps():
    assert substitute("a {color} ball, {name: x}", {"color": "orange"}) == "a orange ball, {name: x}"


def test_substitute_missing_var_is_an_error():
    with pytest.raises(ValueError, match="color"):
        substitute("put a {color} ball", {})


@pytest.mark.parametrize("name", ["hill_climb_sprint.yaml", "hill_climb_full.yaml"])
def test_match_plans_load_and_follow_the_rules(name):
    plan = load_plan(PLANS / name, {"color": "orange"})
    assert plan.verifier == "monitor"            # no human input after "go"
    assert plan.confirm >= 2                     # a false "done" is a DQ
    assert plan.time_budget_s <= 300
    assert plan.done_message
    optional = ("ball_", "watcher_into_place", "plug_loose_cable", "unplug_more")   # after Stage 1 passed: skipping is safe
    gated = [s for s in plan.stages if not s.name.startswith(optional)]
    assert [s.name for s in gated][:2] == ["unplug_cable", "put_cable_down"]
    assert all(s.on_fail != "skip" for s in gated)   # Stages 1-2 are never skipped
    assert all(s.verifier == "monitor" for s in plan.stages)
    assert "{color}" not in " ".join(s.prompt + s.success for s in plan.stages)


def test_full_plan_needs_the_color():
    with pytest.raises(ValueError, match="color"):
        load_plan(PLANS / "hill_climb_full.yaml")


def test_done_message_printed_on_success(tmp_path, capsys):
    plan = tmp_path / "p.yaml"
    plan.write_text(
        'task: t\ncheck_every_s: 0.2\ndone_message: "CALL STOP NOW"\nverifier: time\nstages:\n'
        '  - {name: a, prompt: "do {thing}", success: "done?", timeout_s: 1, min_run_s: 0.1}\n')
    rc = main([str(plan), "--mock", "--var", "thing=it", "--runs-dir", str(tmp_path / "runs")])
    out = capsys.readouterr().out
    assert rc == 0 and "CALL STOP NOW" in out


def test_bad_var_rejected(tmp_path):
    assert main([str(PLANS / "hill_climb_full.yaml"), "--mock", "--var", "color"]) == 2
