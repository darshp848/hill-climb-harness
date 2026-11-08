"""Offline benchmark coverage. Every HTTP entry point is blocked or replaced with a fake."""

from __future__ import annotations

import argparse
import base64
import copy
import io
import json
import threading
import urllib.error
import urllib.request

import pytest

from harness.plan import load_plan
from harness.verifiers import openrouter_request_body, openrouter_transport, parse_monitor_reply
from tools import bench_judges as bench

KEY = "test-only-credential-must-never-appear-in-reports"
JPEG = b"\xff\xd8offline-frame\xff\xd9"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network is forbidden in these tests")
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)


@pytest.fixture
def plan():
    return load_plan(bench.ROOT / "plans/hill_climb_v4.yaml")


def response(done=False, guard=False, cost=.01, text=None):
    answer = text if text is not None else '<json>' + json.dumps({
        "stage_done": done, "guard": guard, "confidence": .95, "reason": "offline"}) + '</json>'
    return {"choices": [{"message": {"content": answer}}],
            "usage": {"cost": cost, "prompt_tokens": 100, "completion_tokens": 20}}


def fake_http(monkeypatch, fn):
    def send(request, timeout):
        assert request.full_url == "https://openrouter.ai/api/v1/chat/completions"
        assert request.get_header("Authorization") == f"Bearer {KEY}"
        assert KEY not in request.data.decode()
        return io.BytesIO(json.dumps(fn(json.loads(request.data))).encode())
    monkeypatch.setattr(urllib.request, "urlopen", send)


def test_translation_preserves_system_text_images_and_wire_fields():
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(JPEG).decode()}}
    payload = {"system": [{"type": "text", "text": "station rules"}], "max_tokens": 4000,
               "output_config": {"effort": "low"}, "_frames_sig": "private bookkeeping",
               "messages": [{"role": "user", "content": [{"type": "text", "text": "overhead"}, image,
                                                           {"type": "text", "text": "question"}]},
                            {"role": "assistant", "content": "prior text"}]}
    body = openrouter_request_body("vision/model", payload)
    assert bench.validate_translation(payload, body) == 1
    assert body["messages"][0] == {"role": "system", "content": "station rules"}
    broken = copy.deepcopy(body)
    broken["messages"][1]["content"].reverse()
    with pytest.raises(ValueError, match="translation mismatch"):
        bench.validate_translation(payload, broken)


def test_raw_stage_answer_survives_monitor_confidence_gate():
    verdict = parse_monitor_reply('<json>{"stage_done": true, "confidence": 0.2}</json>', 0)
    assert verdict.stage_done is True and verdict.done is None
    negative = parse_monitor_reply('<json>{"stage_done": false, "confidence": 0.9}</json>', 0)
    assert negative.stage_done is False and negative.done is None
    assert parse_monitor_reply("broken", 0).stage_done is None


def test_race_collects_loser_cost_after_winning_latency(plan, monkeypatch):
    release_loser = threading.Event()
    slow_finished = threading.Event()
    def respond(body):
        if body["model"] == "slow/model":
            assert release_loser.wait(5)
            slow_finished.set()
            return response(cost=.07)
        return response(cost=.01)
    fake_http(monkeypatch, respond)
    original = bench.Accounting.drain
    def drain(self, expected, timeout_s):
        assert not slow_finished.is_set()  # check_now returned while the loser was still waiting
        release_loser.set()
        return original(self, expected, timeout_s)
    monkeypatch.setattr(bench.Accounting, "drain", drain)
    row = bench.run_trial(plan, bench.Judge("openrouter", ("fast/model", "slow/model")),
                          ("photo", "ball_1", False, False), {"overhead": JPEG}, 1,
                          dry_run=False, timeout_s=5)
    assert slow_finished.is_set() and row["drained"]
    assert row["stage_correct"] and row["guard_correct"]
    assert row["cost_usd"] == pytest.approx(.08)
    assert row["attempts_finished"] == 2
    summary = bench.summaries([row])[0]
    assert summary["cost_per_call_usd"] == pytest.approx(.04)
    assert summary["cost_per_trial_usd"] == pytest.approx(.08)


def test_real_monitor_hedge_recovers_from_bad_json_and_counts_every_copy(plan, monkeypatch):
    lock = threading.Lock()
    counter = 0
    def respond(body):
        nonlocal counter
        with lock:
            counter += 1
            n = counter
        return response(cost=.02, text="bad JSON" if n == 1 else None)
    fake_http(monkeypatch, respond)
    row = bench.run_trial(plan, bench.Judge("openrouter", ("vision/model",), 2),
                          ("photo", "ball_1", False, False), {"overhead": JPEG}, 1,
                          dry_run=False, timeout_s=5)
    assert row["stage_correct"] and not row["parse_failure"]
    assert counter == row["attempts_finished"] == 2
    assert row["cost_usd"] == pytest.approx(.04)
    assert plan.claude_user.get("hedge", 1) == 1


@pytest.mark.parametrize("text,parse_failure,transport_failure", [
    ("not JSON", True, False), ("", False, True),
])
def test_paid_empty_or_malformed_response_retains_cost(plan, monkeypatch, text, parse_failure, transport_failure):
    fake_http(monkeypatch, lambda body: response(cost=.03, text=text))
    row = bench.run_trial(plan, bench.Judge("openrouter", ("vision/model",)),
                          ("photo", "ball_1", False, False), {"overhead": JPEG}, 1,
                          dry_run=False, timeout_s=5)
    assert not row["stage_correct"] and not row["guard_correct"]
    assert row["parse_failure"] == parse_failure
    assert row["transport_failure"] == transport_failure
    assert row["cost_usd"] == pytest.approx(.03)


def test_http_failure_callback_contains_no_exception_message(monkeypatch):
    events = []
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("https://openrouter.ai", 401, KEY, {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", fail)
    send = openrouter_transport(["vision/model"], on_attempt=events.append)
    with pytest.raises(urllib.error.HTTPError):
        send({"messages": [], "max_tokens": 100})
    assert len(events) == 1 and events[0]["error_type"] == "HTTPError"
    assert KEY not in json.dumps(events)


@pytest.mark.parametrize("cost", [None, -1, True, "0.01", float("inf"), float("nan")])
def test_unknown_cost_is_not_zero(cost):
    assert bench.numeric_cost({"cost": cost}) is None


def test_summary_uses_labelled_guard_denominator_and_interpolated_latency(plan, monkeypatch):
    fake_http(monkeypatch, lambda body: response(done=False, guard=False, cost=.01))
    judge = bench.Judge("openrouter", ("vision/model",))
    a = bench.run_trial(plan, judge, ("photo", "release_cable", True, None), {"overhead": JPEG}, 1,
                        dry_run=False, timeout_s=5)
    b = bench.run_trial(plan, judge, ("photo", "ball_1", False, True), {"overhead": JPEG}, 1,
                        dry_run=False, timeout_s=5)
    a["latency_s"], b["latency_s"] = 1.0, 3.0
    summary = bench.summaries([a, b])[0]
    assert summary["stage_done_accuracy"] == .5
    assert summary["guard_trials"] == 1 and summary["guard_accuracy"] == 0
    assert summary["latency_p50_s"] == 2 and summary["latency_p90_s"] == pytest.approx(2.8)
    assert summary["latency_max_s"] == 3


def test_dry_run_covers_all_configs_cases_and_hedges_without_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(bench, "sim_frames", lambda held: {"overhead": JPEG, "wrist_l": JPEG, "wrist_r": JPEG})
    assert bench.main(["--dry-run", "--hedge", "1", "--hedge", "2", "--out", str(tmp_path)]) == 0
    report = json.loads((tmp_path / "results.json").read_text())
    assert len(report["trials"]) == 9 * 6 * 2
    assert all(r["dry_run_valid"] for r in report["trials"])
    assert all(r["stage_correct"] is None and r["cost_usd"] is None for r in report["trials"])
    assert all(r["stage_done_accuracy"] is None and r["latency_p50_s"] is None for r in report["summary"])
    assert (tmp_path / "summary.csv").read_text().startswith("judge,trials,")
    assert KEY not in capsys.readouterr().out + (tmp_path / "results.json").read_text()


@pytest.mark.parametrize("cost,limit,stop", [(.02, "0.01", "max_cost_usd"), (None, "1", "incomplete_cost_accounting")])
def test_cli_budget_and_missing_usage_stop_before_next_trial(tmp_path, monkeypatch, capsys, cost, limit, stop):
    fake_http(monkeypatch, lambda body: response(done=True, cost=cost))
    assert bench.main(["--judge", "openrouter:vision/model", "--no-sim", "--max-cost-usd", limit,
                       "--out", str(tmp_path)]) == 2
    report = json.loads((tmp_path / "results.json").read_text())
    assert len(report["trials"]) == 1 and report["stopped"] == stop
    assert KEY not in (tmp_path / "results.json").read_text() + capsys.readouterr().out


def test_zero_budget_does_not_build_transport_or_render(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("zero budget must not reach renderer or transport")
    monkeypatch.setattr(bench, "transport_for", fail)
    monkeypatch.setattr(bench, "sim_frames", fail)
    assert bench.main(["--max-cost-usd", "0", "--out", str(tmp_path)]) == 2
    assert json.loads((tmp_path / "results.json").read_text())["trials"] == []
    assert (tmp_path / "summary.csv").read_text().startswith("judge,trials,")


@pytest.mark.parametrize("spec", ["garbage", "openrouter:", "api:a,b", "subscription:a,b", "openrouter:a,", "openrouter:a b"])
def test_reject_bad_configs(spec):
    with pytest.raises(argparse.ArgumentTypeError):
        bench.judge_spec(spec)


def test_incomplete_drain_keeps_cost_unknown(plan, monkeypatch):
    fake_http(monkeypatch, lambda body: response())
    monkeypatch.setattr(bench.Accounting, "drain", lambda *args: ([], False))
    row = bench.run_trial(plan, bench.Judge("openrouter", ("vision/model",)),
                          ("photo", "ball_1", False, False), {"overhead": JPEG}, 1,
                          dry_run=False, timeout_s=5)
    assert not row["cost_complete"] and row["cost_usd"] is None


def test_invalid_translation_fails_dry_run_without_scoring(plan, monkeypatch):
    monkeypatch.setattr(bench, "openrouter_request_body", lambda *args: {"messages": [], "max_tokens": 4000})
    row = bench.run_trial(plan, bench.Judge("openrouter", ("vision/model",)),
                          ("photo", "ball_1", False, False), {"overhead": JPEG}, 1,
                          dry_run=True, timeout_s=1)
    assert not row["dry_run_valid"] and row["stage_correct"] is None


def test_race_inside_hedge_counts_all_four_attempts_and_positive_guard(plan, monkeypatch):
    fake_http(monkeypatch, lambda body: response(guard=True, cost=.02))
    row = bench.run_trial(plan, bench.Judge("openrouter", ("first/model", "second/model"), 2),
                          ("sim_eight_held", "ball_1", False, True), {"overhead": JPEG}, 1,
                          dry_run=False, timeout_s=5)
    assert row["stage_correct"] and row["guard_correct"]
    assert row["attempts_expected"] == row["attempts_finished"] == 4
    assert row["cost_usd"] == pytest.approx(.08)


def test_malformed_response_object_still_finishes_accounting(monkeypatch):
    fake_http(monkeypatch, lambda body: [])
    events = []
    send = openrouter_transport(["vision/model"], on_attempt=events.append)
    with pytest.raises(ValueError, match="must be an object"):
        send({"messages": [], "max_tokens": 100})
    assert len(events) == 1 and events[0]["error_type"] == "ValueError"


def test_malformed_usage_stops_without_callback_failure(plan, monkeypatch):
    def respond(body):
        reply = response()
        reply["usage"] = "invalid provider metadata"
        return reply
    fake_http(monkeypatch, respond)
    row = bench.run_trial(plan, bench.Judge("openrouter", ("vision/model",)),
                          ("photo", "ball_1", False, False), {"overhead": JPEG}, 1,
                          dry_run=False, timeout_s=5)
    assert row["stage_correct"] and row["drained"]
    assert not row["cost_complete"] and row["cost_usd"] is None


@pytest.mark.parametrize("backend", ["api", "subscription"])
def test_native_backend_is_measured_but_dollar_cost_stays_unknown(plan, monkeypatch, backend):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    def make(*args):
        def send(payload):
            assert payload["model"] == "claude-haiku-5-5"
            return {"content": [{"type": "text", "text": '<json>{"stage_done": false, '
                                 '"guard": false, "confidence": 1}</json>'}],
                    "usage": {"input_tokens": 100, "output_tokens": 20}}
        return send
    monkeypatch.setattr(bench, "anthropic_transport", make)
    monkeypatch.setattr(bench, "claude_cli_transport", make)
    row = bench.run_trial(plan, bench.Judge(backend, ("claude-haiku-5-5",)),
                          ("photo", "ball_1", False, False), {"overhead": JPEG}, 1,
                          dry_run=False, timeout_s=5)
    assert row["stage_correct"] and row["guard_correct"]
    assert row["attempts_finished"] == 1 and row["latency_s"] >= 0
    assert not row["cost_complete"] and row["cost_usd"] is None


def test_invalid_plan_guard_fails_before_network(tmp_path, plan, monkeypatch):
    plan.guard = None
    monkeypatch.setattr(bench, "load_plan", lambda path: plan)
    with pytest.raises(SystemExit) as exc:
        bench.main(["--dry-run", "--no-sim", "--out", str(tmp_path)])
    assert exc.value.code == 2
