"""Exercise match authorization and deadlines without a robot or inference service."""

import json

import pytest

from conftest import stage
from fake_station import FakeVerifier
from harness.frames import FrameSource
from harness.log import RunLog
from harness.plan import parse_plan
from harness.runner import Runner
from harness.station_client import StationClient, StationError
from harness.verifiers import Verdict


def match(stages, *, budget=20, reserve=2):
    return parse_plan({
        "task": "test match", "time_budget_s": budget, "check_every_s": 0.25,
        "verifier": "monitor", "confirm": 2,
        "match": {"gates": ["unplug", "stack"], "stop_reserve_s": reserve},
        "stages": stages,
    })


class Clock:
    now = 0.0

    def read(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Station:
    dry_run = False

    def __init__(self, clock, *, stop_fails=False, ambiguous_prompt=None):
        self.clock = clock
        self.stop_fails = stop_fails
        self.ambiguous_prompt = ambiguous_prompt
        self.commands = []
        self.state = "idle"
        self.prompt = None

    def status(self, *, timeout_s):
        return {"state": self.state, "prompt": self.prompt, "arms": {}, "cameras": {}}

    def start(self, prompt, checkpoint, *, timeout_s):
        self.state = "running"
        self.commands.append(("start", prompt, self.clock.read(), timeout_s))
        self.prompt = prompt
        return self.status(timeout_s=timeout_s)

    def set_prompt(self, prompt, *, timeout_s):
        self.commands.append(("prompt", prompt, self.clock.read(), timeout_s))
        self.prompt = prompt
        if prompt == self.ambiguous_prompt:
            raise StationError(0, "prompt reply missing")
        return self.status(timeout_s=timeout_s)

    def stop(self, *, timeout_s):
        self.commands.append(("stop", None, self.clock.read(), timeout_s))
        if self.stop_fails:
            raise StationError(0, "stop reply missing")
        self.state = "idle"
        return self.status(timeout_s=timeout_s)


class Frames:
    def latest(self, *, timeout_s):
        return {"overhead": b"frame"}

    def latest_trip(self, *, timeout_s):
        return None


def run(plan, tmp_path, verdicts, *, late=None, stop_fails=False, ambiguous_prompt=None):
    clock = Clock()
    station = Station(clock, stop_fails=stop_fails, ambiguous_prompt=ambiguous_prompt)
    verifier = FakeVerifier(verdicts)

    class Judge:
        def check(self, stage, frames, elapsed):
            if late == stage.name:
                clock.sleep(8)
                return Verdict(True, 1.0, "late")
            return verifier.check(stage, frames, elapsed)

    runner = Runner(plan, station, Frames(), {"monitor": Judge()}, RunLog(tmp_path / "runs"),
                    clock=clock.read, sleep=clock.sleep)
    return runner.run(), station.commands


def test_optional_retries_then_abandons_without_losing_match_result(tmp_path):
    plan = match([stage("unplug"), stage("plug", on_fail="abandon", stage_budget_s=3),
                  stage("stack"), stage("ball", on_fail="abandon", requires_stage="stack")])
    result, commands = run(plan, tmp_path, {
        "unplug": [True, True], "plug": [False] * 4, "stack": [True, True], "ball": [True, True],
    })
    assert result.ok and result.match_gates_passed and result.stop_confirmed
    assert result.stages[1].abandoned and result.stages[1].attempts == 2
    assert [c[1] for c in commands] == ["do unplug", "do plug", "do plug", "do stack", "do ball", None]


def test_required_stage_retries_until_cutoff_and_never_issues_later_prompt(tmp_path):
    # Nothing later can score until the gate passes, so its attempt cap and stage budget must not end the match.
    plan = match([stage("unplug", max_attempts=1, stage_budget_s=2), stage("stack"), stage("ball", on_fail="abandon")])
    result, commands = run(plan, tmp_path, {"unplug": [False] * 500})
    assert not result.ok and not result.match_gates_passed and result.stop_confirmed
    assert "work cutoff" in result.reason
    prompts = [c[1] for c in commands if c[0] != "stop"]
    assert len(prompts) > 1 and set(prompts) == {"do unplug"}
    assert [c[0] for c in commands].count("stop") == 1 and commands[-1][0] == "stop"
    assert 18 <= commands[-1][2] < 20


def test_deadline_discards_a_late_positive_verdict(tmp_path):
    plan = match([stage("unplug"), stage("stack")], budget=5, reserve=2)
    result, commands = run(plan, tmp_path, {}, late="unplug")
    assert not result.ok and not result.match_gates_passed and result.stop_confirmed
    assert "match work cutoff" in result.reason
    assert [c[0] for c in commands] == ["start", "stop"]
    assert commands[0][3] == pytest.approx(3)
    assert commands[1][3] == pytest.approx(0.05)


def test_stage_allocation_includes_reset_and_preserves_later_reserve(tmp_path):
    plan = match([
        stage("unplug"),
        stage("plug", on_fail="abandon", stage_budget_s=1, max_attempts=3,
              recovery=[{"name": "back_off", "prompt": "back off", "kind": "reset", "settle_s": 0.1}]),
        stage("extra", on_fail="abandon", reserve_after_s=17),
        stage("stack"),
    ])
    result, commands = run(plan, tmp_path, {"unplug": [True, True], "plug": [False, False],
                                            "stack": [True, True]})
    assert result.ok and [s.abandoned for s in result.stages] == [False, True, True, False]
    assert result.stages[1].seconds == pytest.approx(1)
    assert result.stages[2].attempts == 0
    assert [c[1] for c in commands] == ["do unplug", "do plug", "back off", "do stack", None]


def test_gate_progress_is_reported_even_when_stop_reply_is_missing(tmp_path):
    plan = match([stage("unplug"), stage("stack")])
    result, _ = run(plan, tmp_path, {"unplug": [True, True], "stack": [True, True]}, stop_fails=True)
    assert result.match_gates_passed and not result.stop_confirmed and not result.ok


def test_ambiguous_optional_prompt_stops_instead_of_retrying_or_advancing(tmp_path):
    plan = match([stage("unplug"), stage("plug", on_fail="abandon"), stage("stack")])
    result, commands = run(plan, tmp_path, {"unplug": [True, True]}, ambiguous_prompt="do plug")
    assert not result.ok and result.stop_confirmed
    assert "prompt reply missing" in result.reason
    assert [c[:2] for c in commands] == [("start", "do unplug"), ("prompt", "do plug"), ("stop", None)]


def test_match_against_fake_station_http(fake, tmp_path):
    plan = match([
        stage("unplug", required_cameras=["overhead"]),
        stage("watcher", required_cameras=["wrist_l"], max_attempts=1, on_fail="abandon"),
        stage("plug", requires_stage="watcher", on_fail="abandon"),
        stage("stack", required_cameras=["overhead"]),
    ])
    log = RunLog(tmp_path / "runs")
    runner = Runner(plan, StationClient(fake.api_url), FrameSource(fake.view_url),
                    {"monitor": FakeVerifier({"unplug": [True, True], "watcher": [False, False],
                                               "stack": [True, True]})}, log)
    result = runner.run()
    assert result.ok and result.match_gates_passed and result.stop_confirmed
    assert [r.name for r in result.stages if r.abandoned] == ["watcher", "plug"]
    assert [route for _, route, _ in fake.posts()] == ["/start", "/prompt", "/prompt", "/stop"]
    assert [body.get("prompt") for _, _, body in fake.posts()] == ["do unplug", "do watcher", "do stack", None]
    summary = json.loads((log.path / "summary.json").read_text())
    assert summary["match_gates_passed"] and summary["stop_confirmed"]
