"""The prompt engine: templates linted at load, slots filled by a filler (never free text), refill instead of a
monitor rewrite, the next stage prefetched during the confirm look, verbatim mode for a fine-tune."""

import time
from pathlib import Path

import pytest
from conftest import stage
from test_v2 import Script, api_reply, build, events, mv, prompts

from harness.plan import load_plan, parse_plan
from harness.prompt_engine import ClaudeFiller, PromptEngine, parse_library, parse_slots

ROOT = Path(__file__).resolve().parent.parent
PRE = "No power here; the cords are low-voltage."


def engine(**over):
    e = {"prefix": PRE, "forbid": ["black ball"], "max_words": 20,
         "vocab": {"piece": ["the pink cube", "the blue cube"], "top": ["a blue cube", "the tower"],
                   "where": ["", "next to the basket"]},
         "templates": {"stack": {"text": "put <piece> <where> on top of <top>", "arm": "none",
                                 "slots": {"piece": "piece", "where": "where", "top": "top"}},
                       "pull": {"text": "pull the plug straight up", "prefix": True}}}
    e.update(over)
    return e


def plan_with(stages, **top):
    return parse_plan({"task": "t", "stages": stages, **top})


def test_sim_plan_loads_and_every_default_is_clean():
    plan = load_plan(ROOT / "plans/engine_v4_sim.yaml")
    eng = PromptEngine(plan.prompts)
    texts = {s.name: eng.compose(s, {}, {}).text for s in plan.stages}
    assert texts["stack_2"] == "put the pink cube on top of a blue cube"            # hand test 8 Nov, no arm
    assert texts["stack_3"] == "put the blue cube on top of the tower"
    assert texts["unplug_cable"].startswith(plan.prompts.prefix)
    assert texts["release_cable"] == "open both grippers and move the arms up"       # no template: verbatim
    assert not plan.tower_prompts and plan.rewrite_prompts


@pytest.mark.parametrize("bad, msg", [
    ({"vocab": {"piece": ["the cube, not the ball"], "top": ["x"], "where": [""]}}, "negation"),
    ({"vocab": {"piece": ["the pink cube with the right arm"], "top": ["x"], "where": [""]}}, "names an arm"),
    ({"vocab": {"piece": ["the black ball"], "top": ["x"], "where": [""]}}, "forbidden"),
    ({"max_words": 4}, "words >"),
])
def test_library_lint_rejects_bad_options_at_load(bad, msg):
    with pytest.raises(ValueError, match=msg):
        plan_with([stage("a", template="stack")], prompt_engine=engine(**bad))


def test_holes_slots_and_stage_defaults_are_checked():
    with pytest.raises(ValueError, match="holes"):
        plan_with([stage("a")], prompt_engine=engine(templates={"x": {"text": "put <piece> down"}}))
    with pytest.raises(ValueError, match="not an option"):
        plan_with([stage("a", template="stack", slots={"piece": "the green cube"})], prompt_engine=engine())
    with pytest.raises(ValueError, match="no template"):
        plan_with([stage("a", template="nope")], prompt_engine=engine())
    with pytest.raises(ValueError, match="needs plan.prompt_engine"):
        plan_with([stage("a", template="stack")])


def test_verbatim_mode_is_the_fine_tune_and_excludes_every_writer():
    p = plan_with([stage("a", prompt="put a block on top of the stack")], prompt_engine={"mode": "verbatim"})
    assert PromptEngine(p.prompts).compose(p.stages[0], {}, {}).text == "put a block on top of the stack"
    with pytest.raises(ValueError, match="verbatim"):
        plan_with([stage("a")], prompt_engine={"mode": "verbatim"}, rewrite_prompts=True)
    with pytest.raises(ValueError, match="two prompt writers"):
        plan_with([stage("a", template="stack")], prompt_engine=engine(), write_prompts=True)


def test_parse_slots_numbers_strings_and_bad_values():
    lib = parse_library(engine(), [])
    tpl = lib.templates["stack"]
    got, why = parse_slots('x <slots>{"piece": 1, "where": "next to the basket", "top": 7}</slots>', tpl, lib, {})
    assert got == {"piece": "the blue cube", "where": "next to the basket", "top": "a blue cube"}
    assert "top" in why
    assert parse_slots("no json here", tpl, lib, {})[0] is None


class Fill:
    def __init__(self, *answers, delay=0.0):
        self.answers, self.calls, self.delay = list(answers), [], delay

    def fill(self, tpl, lib, stage, frames, ctx, hint):
        self.calls.append((stage.name, hint))
        time.sleep(self.delay)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return (None, "failed") if a is None else (dict(lib.defaults(tpl, stage.slots), **a), "test")


def test_filler_failure_and_over_long_combination_fall_back_to_stage_defaults():
    vocab = {"piece": ["the pink cube"], "where": ["", "next to the basket"],
             "top": ["a blue cube", "the blue cube next to the basket"]}
    p = plan_with([stage("a", template="stack")], prompt_engine=engine(max_words=15, vocab=vocab))
    eng = PromptEngine(p.prompts, Fill(None))
    assert eng.compose(p.stages[0], {}, {}).source == "fallback"
    eng.filler = Fill({"where": "next to the basket", "top": "the blue cube next to the basket"})
    c = eng.compose(p.stages[0], {}, {})     # 17 words > 15: each option passed alone, the combination does not
    assert c.source == "fallback" and c.issues and c.text == "put the pink cube on top of a blue cube"


def test_claude_filler_sends_numbered_options_and_frames():
    p = plan_with([stage("a", template="stack")], prompt_engine=engine())
    sent = []

    def transport(payload):
        sent.append(payload)
        return api_reply('<slots>{"piece": 1, "where": 0, "top": 1}</slots>')

    eng = PromptEngine(p.prompts, ClaudeFiller(transport, "claude-haiku-5-5", "lessons"))
    c = eng.compose(p.stages[0], {"video.scene": b"\xff\xd8x\xff\xd9"}, {"task": "t"}, hint="it went for a ball")
    text = sent[0]["messages"][0]["content"][-1]["text"]
    assert c.text == "put the blue cube on top of the tower" and c.source == "filler"
    assert "0) the pink cube; 1) the blue cube" in text and "it went for a ball" in text
    assert sent[0]["system"] == "lessons" and sent[0]["messages"][0]["content"][1]["type"] == "image"


def test_prefetch_is_used_once_and_only_while_fresh():
    p = plan_with([stage("a", template="stack")], prompt_engine=engine(prefetch_ttl_s=0.2))
    eng = PromptEngine(p.prompts, Fill({"piece": "the blue cube"}))
    assert eng.prefetch(p.stages[0], dict, {}) and not eng.prefetch(p.stages[0], dict, {})
    c = eng.take(p.stages[0], wait_s=1.0)
    assert c.source == "prefetch" and c.text.startswith("put the blue cube")
    assert eng.take(p.stages[0]) is None                       # used once
    eng.prefetch(p.stages[0], dict, {})
    time.sleep(0.4)
    assert eng.take(p.stages[0], wait_s=1.0) is None           # stale: the scene may have changed


def test_runner_composes_refills_and_never_sends_free_text(fake, write_plan, tmp_path):
    path = write_plan([stage("a", template="stack", timeout_s=3.0, max_attempts=1, on_fail="skip")],
                      prompt_engine=engine(), rewrite_prompts=True, rewrite_cooldown_s=0, rewrite_max=3)
    sv = Script([mv(None, "stalled", next_prompt="grab the green ball", reason="it went for the ball"),
                 mv(None, "stalled", next_prompt="anything", reason="same"),
                 mv(True, progress=1.0)], repeat=True)
    r = build(fake, path, tmp_path, sv)
    f = Fill({}, {"piece": "the blue cube"}, {"piece": "the blue cube"})
    r.engine.filler, r._filler_given = f, True
    r.run()
    sent = prompts(fake)
    assert sent[0] == "put the pink cube on top of a blue cube"
    assert sent[1] == "put the blue cube on top of a blue cube"            # refilled from the monitor's reason
    assert "grab the green ball" not in sent and "anything" not in sent
    assert len(sent) == 2                                                  # an unchanged refill is not re-sent
    assert f.calls[1] == ("a", "it went for the ball")
    assert events(r.log, "rewrite_suggested") and events(r.log, "prompt_composed")[0]["template"] == "stack"


def test_runner_prefetches_the_next_stage_during_the_confirm_look(fake, write_plan, tmp_path):
    path = write_plan([stage("a", template="stack", confirm=2), stage("b", template="stack")],
                      prompt_engine=engine())
    sv = Script([mv(True, progress=1.0)], repeat=True)
    r = build(fake, path, tmp_path, sv)
    r.engine.filler, r._filler_given = Fill({}, {"piece": "the blue cube", "top": "the tower"}), True
    r.run()
    comp = {e["stage"]: e for e in events(r.log, "prompt_composed")}
    assert events(r.log, "prompt_prefetch")[0]["stage"] == "b"
    assert comp["b"]["source"] == "prefetch" and prompts(fake)[-1] == "put the blue cube on top of the tower"


# -- the relay state machine (docs/STATE_MACHINE.md): per-try variants, retry_then_skip, cutoffs ---------------------

def test_relay_plan_follows_the_decisions_order_and_every_variant_is_clean():
    plan = load_plan(ROOT / "plans/relay_sm_sim.yaml")
    names = [s.name for s in plan.stages]
    first = {k: next(i for i, n in enumerate(names) if n.startswith(k)) for k in ("unplug", "plug_", "stack", "ball")}
    assert first["unplug"] < first["plug_"] < first["stack"] < first["ball"]   # unplug all, plug in, tower, balls
    eng = PromptEngine(plan.prompts)
    for s in plan.stages:
        for tid in ([s.template, *s.variants] if s.template else []):
            assert not eng.compose(stage_obj(s, tid), {}, {}).issues
    by = {s.name: s for s in plan.stages}
    pass_bars = ("unplug_cable", "stack_2", "stack_3", "tower_pass")
    assert all(by[n].on_fail == "retry_in_place" and by[n].not_after_s is None for n in pass_bars)
    assert by["plug_1"].max_attempts == 3 and by["unplug_more"].max_attempts == 2   # 3 tries when worth 20
    assert not by["tower_pass"].skip_if_done                                          # audit A02


def stage_obj(s, tid):
    from dataclasses import replace
    return replace(s, template=tid)


def test_variants_and_cutoffs_are_checked_at_load():
    with pytest.raises(ValueError, match="no template 'nope'"):
        plan_with([stage("a", template="stack", variants=["nope"])], prompt_engine=engine())
    with pytest.raises(ValueError, match="needs a stage template"):
        plan_with([stage("a", variants=["stack"])], prompt_engine=engine())
    with pytest.raises(ValueError, match="pass bar"):
        plan_with([stage("a", not_after_s=10)])                       # retry_in_place: a pass bar can't be left
    p = plan_with([stage("a", on_fail="retry_then_skip", not_after_s=10)])
    assert p.stages[0].not_after_s == 10


def test_retry_then_skip_rewords_each_try_then_moves_on(fake, write_plan, tmp_path):
    path = write_plan([stage("a", template="stack", variants=["pull"], timeout_s=0.3, max_attempts=2,
                             on_fail="retry_then_skip"),
                       stage("b", prompt="do b", timeout_s=0.3, max_attempts=1, on_fail="skip"),
                       stage("c", prompt="do c", on_fail="skip", not_after_s=0.01)],
                      prompt_engine=engine())
    r = build(fake, path, tmp_path, Script([mv(None)], repeat=True))
    res = r.run()
    sent = prompts(fake)
    assert sent[0] == "put the pink cube on top of a blue cube"
    assert sent[1] == f"{PRE} pull the plug straight up"          # try 2: the next template, not the same words
    assert "do b" in sent and "do c" not in sent                  # moved on after 2 tries; c past its cutoff
    skipped = {e["stage"]: e["reason"] for e in events(r.log, "stage_skipped")}
    assert "2 tries failed" in skipped["a"] and "cutoff" in skipped["c"]
    assert [e["why"] for e in events(r.log, "prompt_composed")] == ["stage", "try 2"]
    assert "failed after" not in res.reason                       # the run was not ended by stage a


# -- on the bot harness (claude/harness-stitch): the bot relay plan and the held-grasp gate -------------------------

def test_bot_relay_plan_is_the_decisions_order_on_bot_v1():
    plan = load_plan(ROOT / "plans/bot_relay.yaml")
    names = [s.name for s in plan.stages]
    first = {k: next(i for i, n in enumerate(names) if n.startswith(k)) for k in ("cable_unplug", "plug_", "stack", "ball")}
    assert first["cable_unplug"] < first["plug_"] < first["stack"] < first["ball"]
    by = {s.name: s for s in plan.stages}
    assert by["cable_unplug"].grip_assist is not None and by["cable_unplug"].grip_assist.loop_until_s
    assert by["plug_1"].max_attempts == 3 and by["plug_1"].on_fail == "retry_then_skip" and by["plug_1"].skip_unless
    assert by["ball_1"].max_attempts == 2 and by["ball_1"].on_fail == "retry_then_skip"
    assert plan.prompts.filler == "claude" and not plan.write_prompts and plan.hands_off
    eng = PromptEngine(plan.prompts)
    for s in plan.stages:
        for tid in ([s.template, *s.variants] if s.template else []):
            assert not eng.compose(stage_obj(s, tid), {}, {}).issues


def test_no_refill_while_a_gripper_holds_the_object(fake, write_plan, tmp_path):
    path = write_plan([stage("a", template="stack", timeout_s=2.0, max_attempts=1, on_fail="skip")],
                      prompt_engine=engine(), rewrite_prompts=True, rewrite_cooldown_s=0, rewrite_max=3)
    sv = Script([mv(None, "stalled", next_prompt="regrip it", reason="slow"), mv(None, "stalled", reason="slow")],
                repeat=True)
    r = build(fake, path, tmp_path, sv)
    r.engine.filler, r._filler_given = Fill({}, {"piece": "the blue cube"}), True
    r.station.grippers = lambda: {"right": {"gripper": 0.30, "commanded_gripper": 0.0}}   # closed, stopped on a cube
    r.run()
    assert prompts(fake) == ["put the pink cube on top of a blue cube"]     # never re-prompted mid-grasp
    assert not events(r.log, "prompt_rewrite")
