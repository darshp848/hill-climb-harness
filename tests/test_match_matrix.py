"""Decision matrix using the shipped v4 plan and a deterministic 300-second match clock."""

import json
from dataclasses import dataclass

import pytest

from fake_station import TINY_JPEG
from harness.frames import CameraTrip
from harness.log import RunLog
from harness.plan import load_plan
from harness.runner import Runner
from harness.verifiers import MonitorVerdict, Verdict
from test_match_five_minutes import FULL_PLAN, SlowProgress
from test_match_supervisor import Clock, Station


@dataclass(frozen=True)
class Case:
    name: str
    gates_pass: bool
    ok: bool
    stop_confirmed: bool
    reached: str
    blocked: tuple[str, ...] = ()


CASES = (
    Case("nominal", True, True, True, "ball_8"),
    Case("occupied_station", False, False, False, "", ("unplug_cable",)),
    Case("unplug_exhaustion", False, False, True, "unplug_cable", ("stack_two", "ball_1")),
    Case("wrong_prompt", False, False, True, "clear_of_stack", ("ball_1",)),
    Case("replayed_trip", False, False, True, "clear_of_stack", ("ball_1",)),
    Case("late_gate_verdict", False, False, True, "clear_of_stack", ("ball_1",)),
    Case("missing_wrist", True, True, True, "ball_1", ("plug_loose_cable",)),
    Case("verified_plug_reset", True, True, True, "ball_1"),
    Case("unknown_plug_reset", True, True, True, "ball_1"),
    Case("unknown_required_reset", False, False, True, "unplug_cable", ("stack_two", "ball_1")),
    Case("ball_failure", True, True, True, "ball_1", ("ball_2",)),
    Case("arm_fault", False, False, True, "raise_stack", ("clear_of_stack", "ball_1")),
    Case("prompt_ack_missing", False, False, True, "watcher_into_place", ("plug_loose_cable", "stack_two")),
    Case("work_cutoff", True, True, True, "ball_8"),
    Case("stop_ack_missing", True, False, False, "ball_8"),
    Case("stop_ack_after_deadline", True, False, True, "ball_8"),
)


class MatchFixture(Station):
    """Script station replies and debug trips on the same monotonic clock as the runner."""

    def __init__(self, clock, plan, case):
        self.prompts = {stage.name: stage.prompt for stage in plan.stages}
        self.case = case
        super().__init__(clock, stop_fails=case == "stop_ack_missing",
                         ambiguous_prompt=self.prompts["watcher_into_place"] if case == "prompt_ack_missing" else None)
        if case == "occupied_station":
            self.state = "running"
        self.frozen_trip = None

    def status(self, *, timeout_s):
        status = super().status(timeout_s=timeout_s)
        if self.case == "arm_fault" and self.prompt == self.prompts["raise_stack"]:
            status["arms"] = {"left": True, "right": False}
        return status

    def latest_n(self, *, timeout_s):
        return round(self.clock.read() * 1000) if self.prompt else None

    def latest(self, *, timeout_s):
        frames = {"overhead": TINY_JPEG, "wrist_l": TINY_JPEG, "wrist_r": TINY_JPEG}
        if self.case == "missing_wrist" and self.prompt == self.prompts["watcher_into_place"]:
            del frames["wrist_l"]
        return frames

    def latest_trip(self, *, timeout_s):
        if self.prompt is None:
            return None
        trip = self.latest_n(timeout_s=timeout_s)
        prompt = self.prompt
        if prompt == self.prompts["clear_of_stack"]:
            if self.case == "wrong_prompt":
                prompt = self.prompts["raise_stack"]
            elif self.case == "replayed_trip":
                if self.frozen_trip is None:
                    self.frozen_trip = trip
                trip = self.frozen_trip
        return CameraTrip(trip, prompt, self.latest(timeout_s=timeout_s))

    def stop(self, *, timeout_s):
        reply = super().stop(timeout_s=timeout_s)
        if self.case == "stop_ack_after_deadline":
            self.clock.sleep(6)
        return reply


class Judge:
    def __init__(self, fixture):
        self.fixture = fixture
        self.slow = SlowProgress(fixture.clock.read)

    def check(self, stage, frames, elapsed):
        case = self.fixture.case
        if case in ("work_cutoff", "stop_ack_after_deadline"):
            return self.slow.check(stage, frames, elapsed)
        if case == "unplug_exhaustion" and stage.name == "unplug_cable":
            return Verdict(None)
        if case == "late_gate_verdict" and stage.name == "clear_of_stack":
            self.fixture.clock.sleep(31)
        if case == "ball_failure" and stage.name == "ball_1":
            return Verdict(False, 1, "ball outcome did not meet the success question")

        plug_reset = case in ("verified_plug_reset", "unknown_plug_reset") and stage.name == "plug_loose_cable"
        required_reset = case == "unknown_required_reset" and stage.name == "unplug_cable"
        if plug_reset or required_reset:
            resets = {r.prompt for r in stage.menu() if r.kind == "reset"}
            if self.fixture.prompt in resets:
                return MonitorVerdict(None, scene_ok=True if case == "verified_plug_reset" else None)
            issued = sum(command[1] == stage.prompt for command in self.fixture.commands)
            if issued == 1:
                return MonitorVerdict(None, 1, "stalled", status="stalled", progress=0.1, recovery=1)
        return Verdict(True, 1, "verified by fixture")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_full_v4_decision_matrix(case, tmp_path):
    plan = load_plan(FULL_PLAN)
    assert plan.time_budget_s == 300 and plan.match.stop_reserve_s == 5
    clock = Clock()
    fixture = MatchFixture(clock, plan, case.name)
    log = RunLog(tmp_path)
    result = Runner(plan, fixture, fixture, {"monitor": Judge(fixture)}, log,
                    clock=clock.read, sleep=clock.sleep).run()
    events = [json.loads(line) for line in (log.path / "events.jsonl").read_text().splitlines()]
    starts = [e["stage"] for e in events if e["event"] == "stage_start"]
    results = {s.name: s for s in result.stages}

    assert result.ok is case.ok, result
    assert result.match_gates_passed is case.gates_pass, result
    assert result.stop_confirmed is (None if case.name == "occupied_station" else case.stop_confirmed)
    assert all(stage not in starts for stage in case.blocked)
    if not case.reached:
        assert not starts and not fixture.commands
        return

    assert case.reached in starts
    assert all(t < 295 for route, _, t, _ in fixture.commands if route != "stop")
    assert [c[0] for c in fixture.commands].count("stop") == 1 and fixture.commands[-1][0] == "stop"
    assert fixture.commands[-1][2] <= 295
    if "ball_1" in starts:
        gate_end = next(i for i, event in enumerate(events)
                        if event["event"] == "attempt_end" and event["stage"] == "clear_of_stack" and event["ok"])
        ball_start = next(i for i, event in enumerate(events)
                          if event["event"] == "stage_start" and event["stage"] == "ball_1")
        assert gate_end < ball_start

    if case.name == "work_cutoff":
        assert fixture.commands[-1][2] == pytest.approx(295)
        assert results["ball_7"].ok and results["ball_8"].abandoned
    elif case.name == "stop_ack_after_deadline":
        assert result.seconds > 300 and "deadline" in result.reason
    elif case.name == "replayed_trip":
        assert any(e["event"] == "camera_evidence_wait" and "new camera trip" in e["reason"] for e in events)
    elif case.name == "wrong_prompt":
        assert any(e["event"] == "camera_evidence_wait" and "different prompt" in e["reason"] for e in events)
    elif case.name == "verified_plug_reset":
        assert results["plug_loose_cable"].ok and results["plug_loose_cable"].attempts == 2
        assert any(e["event"] == "scene_check" and e["scene_ok"] for e in events)
    elif case.name in ("unknown_plug_reset", "unknown_required_reset"):
        stage_name = "plug_loose_cable" if case.name == "unknown_plug_reset" else "unplug_cable"
        assert sum(c[1] == fixture.prompts[stage_name] for c in fixture.commands) == 1
        if case.name == "unknown_plug_reset":
            assert results[stage_name].abandoned
