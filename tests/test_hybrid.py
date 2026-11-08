"""The hybrid pieces: monitor-written prompts, /reset first and after Dream's IK error, gripper sensors over the
images, tower prompts with <top>, the operator talking in plain words, and plans/hybrid_v4.yaml."""

import json
from pathlib import Path

from conftest import stage
from test_v2 import Script, Transport, build, events, mv, prompts, reply_text, routes

from harness.log import RunLog
from harness.operator import Operator, load_lessons, parse_reply
from harness.plan import load_plan
from harness.runner import AUTO_RESET_MAX, Runner
from harness.verifiers import MonitorVerifier, parse_monitor_reply

ROOT = Path(__file__).resolve().parent.parent


def test_hybrid_plan_loads_and_cable_section_is_optional():
    plan = load_plan(ROOT / "plans/hybrid_v4.yaml")
    names = [s.name for s in plan.stages]
    assert plan.reset_first and plan.rewrite_prompts and plan.system_prompt and plan.time_budget_s == 295
    assert names[0] == "cable_unplug" and "cable_extra_4_down" in names and "stack_2" in names and names[-1].startswith("ball_")
    assert all(s.prompt.startswith(plan.prompt_prefix) for s in plan.stages if s.name.startswith("cable_"))
    assert not any(n.startswith("cable_") for n in names[names.index("arms_clear"):])
    assert all(s.stall_s and s.stall_s >= 30 for s in plan.stages if s.name.startswith(("stack", "tower")))
    assert "{" not in plan.system_prompt and "orange ball" in plan.stages[-1].prompt


def test_next_prompt_parsed_and_asked_for(write_plan):
    v = parse_monitor_reply(reply_text(next_prompt="  pick up the pink cube\n and place it gently "), 0)
    assert v.next_prompt == "pick up the pink cube and place it gently"
    plan = load_plan(write_plan([stage("a", verifier="monitor")], rewrite_prompts=True, system_prompt="lessons"))
    t = Transport(reply_text())
    m = MonitorVerifier(plan, transport=t)
    m.check_now(plan.stages[0], {"video.scene": b"\xff\xd8x\xff\xd9"}, 1.0)
    assert t.payloads[0]["system"] == "lessons"
    assert "next_prompt" in t.payloads[0]["messages"][0]["content"][-1]["text"]


def test_monitor_rewrites_the_prompt_with_prefix_cooldown_and_cap(fake, write_plan, tmp_path, monkeypatch):
    monkeypatch.setattr("harness.runner.REWRITE_COOLDOWN_S", 0.0)
    pre = "No power here."
    path = write_plan([stage("a", prompt=f"{pre} pull the plug", timeout_s=3.0, max_attempts=1, on_fail="skip")],
                      rewrite_prompts=True, prompt_prefix=pre)
    sv = Script([mv(None, "ok", next_prompt="ignored: status ok"), mv(None, "stalled", next_prompt="grip the rightmost plug"),
                 mv(None, "stalled", next_prompt="grip the second plug"),
                 mv(None, "failing", next_prompt="grip the third plug"), mv(None, "stalled", next_prompt="grip a fourth plug"),
                 mv(True, progress=1.0)], repeat=True)
    r = build(fake, path, tmp_path, sv)
    r.run()
    sent = prompts(fake)
    rewrites = events(r.log, "prompt_rewrite")
    assert len(rewrites) == 3                                   # REWRITE_MAX per attempt
    assert sent[1] == f"{pre} grip the rightmost plug"          # the prefix is put back on
    assert all(e["previous"] for e in rewrites)


def test_reset_first_then_auto_reset_after_svd(fake, write_plan, tmp_path):
    fake.reset_s = fake.home_s = 0.05
    path = write_plan([stage("a", timeout_s=6.0)], reset_first=True)
    hits = {"n": 0}

    def svd(st, frames, elapsed):
        hits["n"] += 1
        if hits["n"] in (2, 6, 10):
            with fake._lock:
                fake.last_error = "LinAlgError: SVD did not converge"
                fake.state = "error"
        return mv(True, progress=1.0) if hits["n"] > 14 else mv(None)

    r = build(fake, path, tmp_path, Script([svd]))
    r.run()
    rs = routes(fake)
    assert rs[0] == "/reset" and rs[1] == "/start"              # grippers open before the first stage
    assert len(events(r.log, "auto_reset")) == AUTO_RESET_MAX   # a third SVD restarts without a reset
    assert rs.count("/reset") == 1 + AUTO_RESET_MAX


def test_gripper_note_reads_this_stations_sensors(fake, write_plan, tmp_path):
    r = build(fake, write_plan([stage("a")]), tmp_path, Script([mv(None)]))
    r.station.grippers = lambda: {"right": {"gripper": 0.03, "commanded_gripper": None},
                                  "left": {"gripper": 0.11, "commanded_gripper": 0.0}}
    note = r._grip_note()
    assert "right gripper shut on nothing" in note and "left gripper nearly shut (0.11): a thin plug or nothing" in note


def test_done_seen_during_recovery_passes_after_a_second_look(fake, write_plan, tmp_path):
    rec = [{"name": "lift", "kind": "reset", "prompt": "open and lift", "settle_s": 0.05}]
    path = write_plan([stage("a", timeout_s=0.3, max_attempts=3, recovery=rec)])
    sv = Script([mv(None, "ok")])
    r = build(fake, path, tmp_path, sv)
    r._reset_scene = lambda st, rc, v: (setattr(r, "_done_in_recovery", "plug out"), (True, "scene ok"))[1]
    sv.check_now = lambda st, frames, e: mv(True, progress=1.0)
    res = r.run()
    assert res.stages[0].ok and res.stages[0].reason == "done during recovery: plug out"
    assert events(r.log, "recovery_done_check")[0]["passed"]


def test_tower_prompt_names_the_piece_below(fake, write_plan, tmp_path):
    path = write_plan([stage("stack_2")], tower_target=["blue cube", "pink cube", "yellow stick"],
                      tower_prompts={"cube": "put the <piece> on the <top>", "stick": "stand the <piece> on the <top>"})
    r = build(fake, path, tmp_path, Script([mv(None)]))
    r._tower = ["blue cube", "pink cube"]
    assert r._stage_prompt(r.plan.stages[0], None) == "stand the yellow stick on the pink cube"


def test_operator_reply_parsing():
    text = 'Skipping it.\n<json>{"reply": "ok, skipping stack_3", "skip_stages": ["stack_3", "nope"], ' \
           '"end_current_stage": false, "prompt_now": null, "note": "lower blocks slowly"}</json>'
    reply, acts = parse_reply(text, ["stack_2", "stack_3"])
    assert reply == "ok, skipping stack_3"
    assert acts == {"skip_stages": ["stack_3"], "note": "lower blocks slowly"}
    assert parse_reply("just words", ["a"]) == ("just words", {})


def test_operator_skips_ends_and_notes(fake, write_plan, tmp_path):
    path = write_plan([stage("a", timeout_s=5.0), stage("b"), stage("c", timeout_s=0.5)])
    log = RunLog(tmp_path / "runs")
    plan = load_plan(path)
    lessons = tmp_path / "lessons.md"
    t = Transport('<json>{"reply": "ok", "end_current_stage": true, "skip_stages": ["b"], '
                  '"note": "carry blocks high"}</json>')
    op = Operator(plan, log, transport=t, lessons_path=lessons, print_fn=lambda s: None)
    log.listeners.append(op.record)
    notes = []

    class V(Script):
        operator_notes = notes

    sv = V([lambda st, f, e: (op.submit("skip the next step and stop this one") if st.name == "a" and len(sv.calls) == 2
                              else None) or mv(None)])
    r = build(fake, path, tmp_path, sv, log=log)
    r.operator = op
    res = r.run()
    by = {s.name: s for s in res.stages}
    assert by["a"].reason == "operator ended the stage" and by["b"].reason == "skipped by operator"
    assert not any(c[0] == "b" for c in sv.calls) and any(c[0] == "c" for c in sv.calls)
    assert notes == ["carry blocks high"]
    assert "carry blocks high" in load_lessons(lessons)
    kinds = [e["event"] for e in events(log)]
    assert "operator_input" in kinds and "operator_reply" in kinds and "operator_action" in kinds
    assert "skip the next step" in t.payloads[0]["messages"][0]["content"][-1]["text"]


def test_trace_report_shows_rewrites_and_operator(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    import trace_report
    run = tmp_path / "20261107T000000Z"
    run.mkdir()
    evs = [{"t": 0, "event": "run_start", "task": "t"},
           {"t": 1, "event": "prompt_rewrite", "stage": "a", "prompt": "new one", "previous": "old one", "reason": "r"},
           {"t": 2, "event": "operator_input", "text": "why?"},
           {"t": 3, "event": "operator_reply", "reply": "because", "actions": {}}]
    (run / "events.jsonl").write_text("\n".join(json.dumps(e) for e in evs))
    trace_report.render(run)
    page = (run / "trace.html").read_text()
    assert "monitor rewrote prompt" in page and "class='was'" in page and "operator said" in page
