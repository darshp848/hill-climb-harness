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
    Case("unknown_required_reset", True, True, True, "ball_1"),
    Case("ball_failure", True, True, True, "ball_2"),
    Case("eight_ball_in_bin", True, True, True, "ball_2"),
    Case("cable_never_released", True, True, True, "ball_1"),
    Case("gateway_fault", True, True, True, "ball_1"),
    Case("motor_fault", False, False, True, "stack_two", ("raise_stack", "ball_1")),
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
        self.faulted = False

    def status(self, *, timeout_s):
        if self.case in ("gateway_fault", "motor_fault") and self.prompt == self.prompts["stack_two"]                 and self.state == "running" and not self.faulted:
            self.faulted = True
            self.state = "error"   # the station's run died mid-stage
        status = super().status(timeout_s=timeout_s)
        if self.state == "error":
            status["last_error"] = (
                {"message": "DreamAPI refused the inference request: DA-6000 GATEWAY_FAULT (connection_timed_out)",
                 "type": "DreamServerError"} if self.case == "gateway_fault" else
                {"message": "right arm motor 4 over-current", "type": "ArmFault"})
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
        self.late_sent = False

    def check(self, stage, frames, elapsed):
        case = self.fixture.case
        if case in ("work_cutoff", "stop_ack_after_deadline"):
            return self.slow.check(stage, frames, elapsed)
        if case == "unplug_exhaustion" and stage.name == "unplug_cable":
            return Verdict(None)
        if case == "late_gate_verdict" and stage.name == "clear_of_stack":
            if not self.late_sent:   # one positive verdict that arrives after the attempt timed out
                self.late_sent = True
                self.fixture.clock.sleep(31)
                return Verdict(True, 1, "late")
            return Verdict(None)
        if case == "eight_ball_in_bin" and stage.name == "ball_1":
            remove = stage.menu()[0].prompt
            if self.fixture.prompt == remove:
                return MonitorVerdict(None, 1, "8-ball out of the bin", scene_ok=True)
            if not any(command[1] == remove for command in self.fixture.commands):
                return MonitorVerdict(False, 1, "a black 8-ball is in our bin", status="failing", recovery=0)
        if case == "cable_never_released" and stage.name in ("put_cable_down", "release_before_stack"):
            return Verdict(None)   # the relay-table sim: Dream-1 keeps holding the unplugged plug
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
    elif case.name == "unknown_plug_reset":
        assert sum(c[1] == fixture.prompts["plug_loose_cable"] for c in fixture.commands) == 1
        assert results["plug_loose_cable"].abandoned
    elif case.name == "unknown_required_reset":
        # An unverified reset on a required gate falls back to a plain retry instead of ending the match.
        unplug = [i for i, c in enumerate(fixture.commands) if c[1] == fixture.prompts["unplug_cable"]]
        reset = next(i for i, c in enumerate(fixture.commands) if c[1] == "open both grippers and move the arms up")
        assert len(unplug) == 2 and unplug[0] < reset < unplug[1] and results["unplug_cable"].ok
    elif case.name == "eight_ball_in_bin":
        ball = [i for i, c in enumerate(fixture.commands) if c[1] == fixture.prompts["ball_1"]]
        remove = next(i for i, c in enumerate(fixture.commands) if "8-ball out of our bin" in (c[1] or ""))
        assert ball[0] < remove < ball[1] and results["ball_1"].ok and results["ball_1"].attempts == 2
    elif case.name == "cable_never_released":
        assert results["put_cable_down"].abandoned and results["release_before_stack"].abandoned
        assert results["stack_two"].ok and results["clear_of_stack"].ok
        assert results["put_cable_down"].seconds <= 20.5 and results["release_before_stack"].seconds <= 15.5
    elif case.name == "gateway_fault":
        restarts = [e for e in events if e["event"] == "station_restart"]
        assert len(restarts) == 1 and "DA-6000" in restarts[0]["error"]
        assert [c[0] for c in fixture.commands].count("start") == 2 and results["stack_two"].ok
    elif case.name == "motor_fault":
        assert not any(e["event"] == "station_restart" for e in events) and "over-current" in result.reason
    elif case.name == "ball_failure":
        assert results["ball_1"].abandoned and results["ball_2"].ok
    elif case.name == "late_gate_verdict":
        # The late "done" is dropped before it is even logged; the attempt times out and the gate never passes.
        assert not any(e["event"] == "verdict" and e["stage"] == "clear_of_stack" and e["done"] for e in events)
        assert any(e["event"] == "attempt_end" and e["stage"] == "clear_of_stack" and "timeout" in e["reason"]
                   for e in events)
        assert "work cutoff" in result.reason
