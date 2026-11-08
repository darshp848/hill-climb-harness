"""final-harness: the pieces added when main, start-pose-live-guard and hybrid-harness were put together."""

from __future__ import annotations

import json

import pytest
from conftest import stage

from harness import runner as runner_mod
from harness.log import RunLog
from harness.plan import load_plan
from harness.runner import Runner


class _Station:
    dry_run = False

    def __init__(self) -> None:
        self.posts: list[str] = []

    def reset(self) -> dict:
        self.posts.append("/reset")
        return {"state": "resetting"}

    def home(self) -> dict:
        self.posts.append("/home")
        return {"state": "homing"}

    def status(self) -> dict:
        return {"state": "idle"}

    def wait_for_state(self, states, timeout_s, poll_s=0.5) -> dict:
        return {"state": "idle"}


class _Guard:
    def __init__(self, armed: bool) -> None:
        self.armed, self.tripped = armed, None


def _runner(write_plan, tmp_path, guard=None) -> tuple[Runner, _Station]:
    plan = load_plan(write_plan([stage("a")]))
    st = _Station()
    r = Runner(plan, st, None, {}, RunLog(tmp_path / "run"), sleep=lambda s: None)
    r.live_guard = guard
    r._t0 = r._clock()
    return r, st


def _events(r: Runner, kind: str) -> list[dict]:
    lines = (r.log.path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e.get("event") == kind or e.get("kind") == kind]


def test_reset_is_skipped_while_the_live_guard_is_armed(write_plan, tmp_path):
    r, st = _runner(write_plan, tmp_path, _Guard(armed=True))
    r._reset_and_wait("auto reset 1 after: SVD did not converge")
    assert st.posts == []                       # /reset moves both arms and can't be cancelled: not while armed
    assert _events(r, "reset_skipped")


def test_reset_goes_through_the_motion_checks(write_plan, tmp_path):
    r, st = _runner(write_plan, tmp_path, _Guard(armed=False))
    r._stop_event.set()                         # operator stop: nothing that moves an arm may follow
    with pytest.raises(runner_mod._Stop):
        r._reset_and_wait("reset_first")
    assert st.posts == []


def test_reset_is_sent_when_the_guard_is_disarmed(write_plan, tmp_path):
    r, st = _runner(write_plan, tmp_path, _Guard(armed=False))
    r._reset_and_wait("reset_first")
    assert st.posts == ["/reset"]
