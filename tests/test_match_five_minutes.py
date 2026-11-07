"""Opt-in five-minute run of the full plan against the local station HTTP API."""

import json
import os
import time
from pathlib import Path

import pytest

from fake_station import FakeStation
from harness.frames import FrameSource
from harness.log import RunLog
from harness.plan import load_plan
from harness.runner import Runner
from harness.station_client import StationClient
from harness.verifiers import Verdict

FULL_PLAN = Path(__file__).resolve().parents[1] / "plans" / "hill_climb_full.yaml"


class SlowProgress:
    """Script visual decisions so the real match approaches its work cutoff."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = {}
        self.thresholds = {
            "unplug_cable": 60,
            "put_cable_down": 20.5,
            "watcher_into_place": float("inf"),
            "release_before_stack": 8.5,
            "stack_two": 44.5,
            "raise_stack": 44.5,
            "clear_of_stack": 14.5,
        }

    def check(self, stage, frames, elapsed):
        now = self.clock()
        started = self.started.setdefault(stage.name, now - elapsed)
        verified = now - started >= self.thresholds.get(stage.name, 5.5)
        return Verdict(True if verified else None, 1.0 if verified else 0.0, "scripted camera verdict")


@pytest.mark.skipif(os.getenv("HILL_CLIMB_WALL_CLOCK") != "1", reason="opt in to the five-minute HTTP run")
def test_full_match_wall_clock(tmp_path):
    plan = load_plan(FULL_PLAN)
    log = RunLog(os.getenv("HILL_CLIMB_VALIDATION_LOGS") or tmp_path)
    print(f"Five-minute HTTP run logs: {log.path}", flush=True)

    with FakeStation(tick_s=0.25) as fake:
        client = StationClient(fake.api_url)
        runner = Runner(plan, client, FrameSource(fake.view_url), {"monitor": SlowProgress()}, log)
        result = runner.run()

        events = [json.loads(line) for line in (log.path / "events.jsonl").read_text().splitlines()]
        match_start = next(event["t"] for event in events if event["event"] == "match_clock_start")
        stop_requested = next(event["t"] for event in events if event["event"] == "stop") - match_start
        stop_reply = next(event["t"] for event in events if event["event"] == "stop_reply") - match_start
        prompts = [event for event in events if event["event"] == "prompt_change"]
        assert all(event["t"] - match_start < 295 for event in prompts)
        # Stop follows the last abandonment; with under one check left before the 295 s cutoff that is early.
        abandoned_last = next(e["t"] for e in reversed(events) if e["event"] == "stage_abandoned") - match_start
        assert 290 <= abandoned_last <= stop_requested < 297, result
        assert stop_requested <= stop_reply < 300
        assert result.ok and result.match_gates_passed and result.stop_confirmed
        results = {stage.name: stage for stage in result.stages}
        assert results["unplug_cable"].attempts == 2
        assert results["watcher_into_place"].abandoned and results["plug_loose_cable"].attempts == 0
        assert results["ball_1"].ok and results["ball_8"].abandoned
        assert "insufficient time" in results["ball_8"].reason
        posts = fake.posts()

        # Observe the remaining stop reserve through the end of the match.
        time.sleep(max(0, 300 - (time.time() - match_start)))
        assert client.status()["state"] == "idle" and fake.posts() == posts
        assert [route for _, route, _ in posts].count("/stop") == 1 and posts[-1][1] == "/stop"
        print(f"Stop requested at {stop_requested:.3f}s, acknowledged at {stop_reply:.3f}s", flush=True)
