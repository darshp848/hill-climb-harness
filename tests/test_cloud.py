"""Cloud planning is offline and must never invoke gcloud in a dry-run."""
import argparse
import io
import json
import time
from pathlib import Path

import pytest

from cloud import collect, control
from cloud.judge import DelayedOracle
from cloud.run import prepare_plan
from harness.plan import load_plan
from harness.verifiers import MonitorVerdict
from experiments.sim_match import OracleVerifier
from harness.plan import Stage

ROOT = Path(__file__).resolve().parents[1]


def test_matrix_pilot_balanced_and_full_unique():
    full = control.tasks(ROOT / "cloud/matrix.yaml")
    pilot = control.tasks(ROOT / "cloud/matrix.yaml", True)
    assert len(full) == 1000 and len(pilot) == 10
    assert len({(x["name"], x["seed"]) for x in full}) == 1000
    assert {x["name"] for x in pilot} == {"hill_climb_v4", "hybrid_v4"}


def job_args(judge="oracle"):
    return argparse.Namespace(project="hill-climb-sims", region="us-central1", bucket="example",
                              judge=judge, image="example/image:tag", job="pilot-v1", parallelism=8,
                              matches_per_task=1, machine_type="e2-standard-2",
                              judge_models="google/gemini-3.1-flash-lite", judge_mode="race", judge_hedge=1,
                              dream_hedge=1, private=False)


def test_batch_limits_and_runtime_secret_references():
    entries = control.tasks(ROOT / "cloud/matrix.yaml", True)
    job = control.job_config(job_args(), entries)
    group = job["taskGroups"][0]
    assert (group["taskCount"], group["parallelism"], group["taskCountPerNode"]) == (10, 8, 1)
    assert job["allocationPolicy"]["instances"][0]["policy"]["provisioningModel"] == "SPOT"
    assert set(group["taskSpec"]["environment"]["secretVariables"]) == {"DREAM_API_KEY"}
    api = control.job_config(job_args("api"), entries)
    assert "ANTHROPIC_API_KEY" in api["taskGroups"][0]["taskSpec"]["environment"]["secretVariables"]
    assert api["labels"] != job["labels"]
    grouped = job_args()
    grouped.matches_per_task = 3
    assert control.job_config(grouped, entries)["taskGroups"][0]["taskCount"] == 4


def test_upload_failure_is_never_retried():
    job = control.job_config(job_args(), control.tasks(ROOT / "cloud/matrix.yaml", True))
    spec = job["taskGroups"][0]["taskSpec"]
    assert spec["lifecyclePolicies"] == [{"action": "FAIL_TASK", "actionCondition": {"exitCodes": [75]}}]


def test_preflight_failure_runs_no_match_and_pins_gcloud_python(monkeypatch, tmp_path):
    from cloud import entrypoint
    seen = []

    def fake_run(cmd, **kw):
        seen.append((cmd, kw.get("env", {}).get("CLOUDSDK_PYTHON")))
        return argparse.Namespace(returncode=1, stderr=b"denied")

    monkeypatch.setattr(entrypoint.subprocess, "run", fake_run)
    monkeypatch.setattr(entrypoint.subprocess, "Popen", lambda *a, **k: pytest.fail("match started"))
    monkeypatch.setenv("OUTPUT_PREFIX", "gs://example/runs/x")
    monkeypatch.setenv("DREAM_API_KEY", "k")
    monkeypatch.setenv("CLOUDSDK_PYTHON", "/usr/bin/python3")
    monkeypatch.delenv("MATRIX_SHA256", raising=False)
    monkeypatch.setattr("sys.argv", ["entrypoint", "--matrix", str(ROOT / "cloud/matrix.yaml"), "--limit", "1",
                                     "--out", str(tmp_path)])
    with pytest.raises(SystemExit) as stop:
        entrypoint.main()
    assert stop.value.code == entrypoint.UPLOAD_FAILED
    assert seen and seen[0][0][:3] == ["gcloud", "storage", "cp"]
    import sys
    assert seen[0][1] == sys.executable


def test_openrouter_judge_and_private_network():
    entries = control.tasks(ROOT / "cloud/matrix.yaml", True)
    a = job_args("openrouter")
    a.judge_models, a.judge_mode, a.judge_hedge, a.private = "m1,m2", "alternate", 2, True
    job = control.job_config(a, entries)
    env = job["taskGroups"][0]["taskSpec"]["environment"]
    assert "OPENROUTER_API_KEY" in env["secretVariables"]
    cmd = job["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"]
    assert cmd[cmd.index("--judge-models") + 1] == "m1,m2" and cmd[cmd.index("--judge-mode") + 1] == "alternate"
    assert job["allocationPolicy"]["network"]["networkInterfaces"][0]["noExternalIpAddress"] is True
    assert job["labels"] != control.job_config(job_args("openrouter"), entries)["labels"]


def test_dry_run_never_calls_gcloud_or_reads_keys(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(control.subprocess, "run", lambda *a, **k: pytest.fail("gcloud invoked"))
    monkeypatch.setattr(control.Path, "home", lambda: pytest.fail("credentials read"))
    for operation in ("setup", "build", "submit"):
        monkeypatch.setattr("sys.argv", ["control", operation, "--dry-run", "--pilot",
                                         "--output", str(tmp_path / "job.json")])
        control.main()
    assert "DRY RUN:" in capsys.readouterr().out


def test_sim_plan_retains_match_safeguards_and_correct_colours():
    for path, skip in [("hill_climb_v4", []), ("hybrid_v4", ["cable_extra"])]:
        plan = prepare_plan(ROOT / "plans" / (path + ".yaml"), skip)
        assert plan.guard and plan.confirm == 2 and plan.claude_user["parallel"] == 2
        assert all(s.verifier == "monitor" for s in plan.stages)
        assert "white ball" not in plan.guard.question
        if path == "hybrid_v4":
            assert all(not s.name.startswith("cable_extra") for s in plan.stages)
            assert "white golf ball" in next(s.prompt for s in plan.stages if s.name == "ball_1")
        assert all("white or black" not in s.success for s in plan.stages)


def test_oracle_uses_monitor_parallel_scheduler_and_drops_stale(monkeypatch):
    from PIL import Image
    from cloud import judge
    plan = load_plan(ROOT / "plans/hill_climb_v4.yaml")
    plan.claude_user["parallel"] = 2
    stage, next_stage = plan.stages[:2]
    mon = DelayedOracle(plan, "offline", 4, latency=(0.08, 0.08))
    monkeypatch.setattr(judge, "get", lambda *a: {"referee": {}})
    monkeypatch.setattr(mon.oracle, "check", lambda *a, **k: MonitorVerdict(True, 1, "oracle", guard=True))
    jpeg = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(jpeg, "JPEG")
    frames = {"scene": jpeg.getvalue()}
    t0 = time.monotonic()
    assert mon.check(stage, frames, 1).done is None
    mon.check(stage, frames, 2)
    assert time.monotonic() - t0 < 0.08
    assert mon._inflight == 2
    mon.notify_prompt_change("new prompt")
    assert mon.wait_idle(2)
    assert mon._result is None and mon.take_guard_alert()
    assert mon.calls == 2


def test_collector_units_shapes_milestones_and_retry_dedup(tmp_path):
    example = {"scenario": "hill_climb", "seed": 1, "label": "hybrid_v4", "policy": "dream", "judge": "oracle",
               "referee": {"points": 10, "loses": False, "dq": None, "eight_in_bin": 0, "unplugged": 1,
                           "seated": 0, "tower_in": 4, "golf_in_bin": 0, "stage1_at": 20, "stage2_at": 80,
                           "first_touch": {"plugged_0": 18}},
               "harness": {"seconds": 295, "reason": "deadline", "ok": False},
               "stages": [{"name": "cable_unplug", "ok": True, "attempts": 2}], "match_wall_s": 300,
               "metrics": {}, "monitor_calls": 30, "versions": {}, "effective_plan_sha256": "abc"}
    for attempt in ("a", "b"):
        folder = tmp_path / attempt
        folder.mkdir()
        (folder / "match.json").write_text(json.dumps(example))
        (folder / "complete.json").write_text(json.dumps({"run_id": "pilot", "attempt": attempt}))
    rows = collect.collect(tmp_path)
    assert len(rows) == 1
    assert rows[0]["tower"] == pytest.approx(0.1016)
    assert rows[0]["stages"] == [("cable_unplug", True, 2)]
    assert rows[0]["stage1_at"] == 20 and rows[0]["dq"] == []


def oracle_snapshot(out=0, balls=0):
    return {"referee": {"unplugged": out, "golf_in_bin": balls, "eight_in_bin": 0,
                         "tower_in": 2.36, "seated": 0},
            "bodies": {"hub": [0, 0, 0.76], "cube_0": [0.2, 0, 0.79]},
            "tcp": {"left": [0, 0, 1], "right": [0, 0, 1]},
            "joints": {side: [0, 0, 0, 0, 0, 0, 1] for side in ("left", "right")}}


def test_oracle_hybrid_cables_and_relative_balls_use_stage_start_counts():
    oracle = OracleVerifier("offline")
    cable = Stage("cable_unplug_more", "unplug", "one more cable out?")
    oracle.start[cable.name] = oracle_snapshot(out=1)["referee"]
    assert oracle.check(cable, {}, 0, snapshot=oracle_snapshot(out=1)).done is None
    assert oracle.check(cable, {}, 1, snapshot=oracle_snapshot(out=2)).done is True
    ball = Stage("ball_5", "bin", "one more ball than at stage start?")
    oracle.start[ball.name] = oracle_snapshot(balls=2)["referee"]
    assert oracle.check(ball, {}, 0, snapshot=oracle_snapshot(balls=2)).done is None
    assert oracle.check(ball, {}, 1, snapshot=oracle_snapshot(balls=3)).done is True


def test_oracle_precondition_does_not_claim_plug_completed():
    from dataclasses import replace
    oracle = OracleVerifier("offline")
    stage = Stage("plug_1", "plug", "new cable seated?", skip_unless="are both grippers empty?")
    snap = oracle_snapshot()
    assert oracle.check(replace(stage, success=stage.skip_unless), {}, 0, snapshot=snap).done is True
    assert oracle.check(stage, {}, 0, snapshot=snap).done is None
    unknown = Stage("unrecognized", "move", "done?")
    assert oracle.check(unknown, {}, 0, snapshot=snap).confidence == 0
