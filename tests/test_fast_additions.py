"""Hedged monitor calls, OpenRouter request translation, layered prompts, plan keys."""

from __future__ import annotations

import json
import time

from conftest import stage

from harness.plan import load_plan
from harness.promptgen import Context, compose
from harness.verifiers import MonitorVerifier, to_openai_messages


def test_openai_translation_keeps_images_and_system():
    payload = {"system": "be a monitor", "max_tokens": 50, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "is it done?"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}}]}]}
    msgs = to_openai_messages(payload)
    assert msgs[0] == {"role": "system", "content": "be a monitor"}
    parts = msgs[1]["content"]
    assert parts[0] == {"type": "text", "text": "is it done?"}
    assert parts[1]["image_url"]["url"] == "data:image/jpeg;base64,QUJD"


def test_layered_prompt_levels():
    pre = "The strip has no power."
    p = f"{pre} Pull the cable out."
    ctx = Context(prefix=pre, holding={"right": "a cable", "left": "nothing"}, tower=["blue cube"], attempt=2,
                  progress=0.4, last_failure="the gripper slipped off the plug")
    assert compose(p, 0, ctx) == "Pull the cable out."
    assert compose(p, 1, ctx) == p
    l5 = compose(p, 5, ctx)
    assert l5.startswith(pre) and l5.endswith("Pull the cable out.")
    for bit in ("right gripper holds a cable", "blue cube", "try 2", "40%", "slipped", "8-ball"):
        assert bit in l5
    assert "left gripper" not in l5


def test_plan_reads_new_keys(write_plan, tmp_path):
    path = write_plan([stage("a")])
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw + "\nconfirm_gap_s: 3\nprompt_level: 4\nfast: {rate_hz: 25, mode: live}\n", encoding="utf-8")
    plan = load_plan(path)
    assert plan.confirm_gap_s == 3.0 and plan.prompt_level == 4 and plan.fast["rate_hz"] == 25


def test_hedged_monitor_takes_the_first_good_reply(write_plan):
    path = write_plan([stage("a")])
    path.write_text(path.read_text(encoding="utf-8") + "\nclaude: {hedge: 3}\n", encoding="utf-8")
    plan = load_plan(path)
    delays = iter([0.4, 0.05, 0.3])
    reply = "<json>" + json.dumps({"stage_done": True, "progress": 0.9, "status": "ok", "reason": "fast one"}) + "</json>"

    def transport(payload):
        time.sleep(next(delays))
        return {"content": [{"type": "text", "text": reply}]}
    mv = MonitorVerifier(plan, transport=transport)
    t = time.monotonic()
    v = mv._call({"_frames_sig": "x"}, 0)
    assert v.done is True and time.monotonic() - t < 0.3 and mv.hedge_wins == [1]
