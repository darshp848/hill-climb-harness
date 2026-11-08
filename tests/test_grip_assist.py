"""Stage.grip_assist (Dream grips, the harness fixes the grip and pulls; ported from tools/try_prompt.py), per-stage
writer switch and retry prompt, the --monitor-model flag, and plans/bot_v1.yaml. Fake station only: no robot."""

import os
import time
from pathlib import Path

import pytest

from conftest import stage
from test_hybrid import Writer
from test_v2 import Script, build, events, mv, prompts, routes

from harness.cli import DEFAULT_OPENROUTER_MONITOR, _set_backend
from harness.plan import load_plan
from harness.station_client import StationClient

ROOT = Path(__file__).resolve().parent.parent
PRE = "No power here."
PUT = "Put the held cable down to the right and open the gripper."


def _assist(**kw):
    base = {"regrip": 2, "regrip_dz": -0.015, "pull_up_m": [0.06, 0.04], "pull_s": 0.01, "seat_dz": 0.01, "repull": 2,
            "put_down_prompt": PUT, "put_down_s": 0.3, "poll_s": 0.01}
    base.update(kw)
    return base


def _cable_plan(write_plan, assist, **kw):
    st = stage("cable_unplug", prompt=f"{PRE} grip the plug and pull it up", timeout_s=kw.pop("timeout_s", 3.0),
               max_attempts=1, on_fail="skip", grip_assist=assist, **kw)
    return write_plan([st], prompt_prefix=PRE)


def _after_put_down(fake):
    """Verifier: done once the put-down prompt went out (the assist finished), nothing before."""
    return Script([lambda st, fr, e: mv(True, progress=1.0) if any(PUT in p for p in prompts(fake)) else mv(None)])


def _moves(fake):
    return [b for _, r, b in fake.posts() if r == "/move"]


def test_regrip_then_pull_then_put_down(fake, write_plan, tmp_path):
    fake.grip_readings = [0.9, 0.9, 0.9, 0.03]   # open on the way in, then shut on nothing (the last repeats)

    def on_move(b):
        if b["gripper"] == 0.0 and b["dxyz"] == [0, 0, 0]:
            fake.grip_readings[:] = [0.08]       # after the regrip's close: holding a plug

    fake.on_move = on_move
    r = build(fake, _cable_plan(write_plan, _assist()), tmp_path, _after_put_down(fake))
    res = r.run()
    assert res.stages[0].ok, res.stages[0].reason
    rs = routes(fake)
    first_move = rs.index("/move")
    assert rs[first_move - 1] == "/stop"         # /move only after Dream is stopped
    moves = [(m["dxyz"][2], m["gripper"], m["duration_s"]) for m in _moves(fake)]
    assert moves == [(0.0, 0.6, 0.8), (-0.015, 0.6, 1.0), (0.0, 0.0, 1.0),   # regrip: open, lower 1.5 cm, close
                     (0.0, 0.45, 0.6), (-0.01, 0.45, 1.0),                   # seat: open a little, lower 1 cm
                     (0.0, 0.0, 0.6),                                        # squeeze in place
                     (0.06, 0.0, 0.01), (0.04, 0.0, 0.01)]                   # pull straight up, grip shut
    starts = [b["prompt"] for _, rt, b in fake.posts() if rt == "/start"]
    assert starts == [f"{PRE} grip the plug and pull it up", f"{PRE} {PUT}"]  # the put-down gets the safety sentence
    assert rs.index("/start", first_move) > len(rs) - 1 - rs[::-1].index("/move")   # Dream back only after the moves
    assert rs[-1] == "/stop"
    assert events(r.log, "grip_regrip")[0]["held"] is True
    assert events(r.log, "grip_pull")[0]["outcome"] == "pulled"
    assert not events(r.log, "grip_assist_error")   # the fake 409s a /move during a run: none was refused


def test_slip_on_the_way_up_goes_back_down_and_regrips(fake, write_plan, tmp_path):
    fake.grip_readings = [0.9, 0.9, 0.9, 0.1, 0.1]   # Dream holds a plug: straight to the pull
    lifts = {"n": 0}

    def on_move(b):
        dz, g = b["dxyz"][2], b["gripper"]
        if dz > 0:
            lifts["n"] += 1
            if lifts["n"] == 2:
                fake.grip_readings[:] = [0.03]       # the plug slipped out on the first pull
        elif dz == 0 and g == 0.0 and lifts["n"] == 2:
            fake.grip_readings[:] = [0.1]            # the re-grip at the bottom holds

    fake.on_move = on_move
    r = build(fake, _cable_plan(write_plan, _assist(seat_dz=0.0)), tmp_path, _after_put_down(fake))
    assert r.run().stages[0].ok
    assert [e["repull"] for e in events(r.log, "grip_slipped")] == [1]
    pull = events(r.log, "grip_pull")[0]
    assert pull["outcome"] == "pulled" and pull["repulls"] == 1
    moves = [(round(m["dxyz"][2], 3), m["gripper"]) for m in _moves(fake)]
    assert moves == [(0.0, 0.0), (0.06, 0.0), (0.04, 0.0),          # squeeze, pull
                     (-0.1, 0.45), (-0.01, 0.45), (0.0, 0.0),       # back down, a little lower, close
                     (0.06, 0.0), (0.04, 0.0)]                      # pull again
    assert not events(r.log, "grip_regrip")


@pytest.mark.parametrize("hold,reading,pulls", [([0.045, 0.20], 0.1, True),    # a plug reads 0.05-0.15
                                                ([0.25, 0.60], 0.1, False),    # cube thresholds: 0.1 is no cube
                                                ([0.25, 0.60], 0.4, True),     # a held cube reads 0.33-0.51
                                                ([0.045, 0.20], 0.27, False)]) # too open to pull a plug
def test_hold_thresholds_are_per_stage(fake, write_plan, tmp_path, hold, reading, pulls):
    fake.grip_readings = [0.9, 0.9, 0.9, reading]
    r = build(fake, _cable_plan(write_plan, _assist(hold=hold, regrip=0), timeout_s=0.6), tmp_path, _after_put_down(fake))
    r.run()
    assert bool(_moves(fake)) is pulls
    assert bool(events(r.log, "grip_pull")) is pulls


def test_no_help_unless_the_gripper_was_open_first(fake, write_plan, tmp_path):
    fake.grip_readings = [0.03]                  # shut from the start: proves nothing
    r = build(fake, _cable_plan(write_plan, _assist(), timeout_s=0.5), tmp_path, Script([mv(None)]))
    r.run()
    assert not _moves(fake) and not events(r.log, "grip_assist")


def test_empty_regrips_restart_dream_at_most_regrip_times(fake, write_plan, tmp_path):
    fake.grip_readings = [0.9, 0.9, 0.9, 0.03]

    def on_move(b):
        if b["gripper"] == 0.0:                  # closed on nothing again; Dream will open it on the next approach
            fake.grip_readings[:] = [0.03, 0.03, 0.9, 0.9, 0.9, 0.03]

    fake.on_move = on_move
    r = build(fake, _cable_plan(write_plan, _assist(regrip=2), timeout_s=1.5), tmp_path, Script([mv(None)]))
    r.run()
    regrips = events(r.log, "grip_regrip")
    assert [e["held"] for e in regrips] == [False, False]
    assert [e["regrips_left"] for e in regrips] == [1, 0]
    starts = [b["prompt"] for _, rt, b in fake.posts() if rt == "/start"]
    assert starts == [f"{PRE} grip the plug and pull it up"] * 3   # the same approach prompt each time


def test_a_refused_move_hands_back_to_dream(fake, write_plan, tmp_path):
    fake.grip_readings = [0.9, 0.9, 0.9, 0.1]
    fake._do_move = lambda body: (_ for _ in ()).throw(ValueError("dxyz too long"))   # station answers 400
    r = build(fake, _cable_plan(write_plan, _assist(), timeout_s=0.8), tmp_path, Script([mv(None)]))
    res = r.run()
    assert "dxyz too long" in events(r.log, "grip_assist_error")[0]["error"]
    assert len(events(r.log, "grip_assist")) == 1   # no more help this attempt
    assert "station error" not in res.reason


def test_move_contract_in_the_client(fake):
    client = StationClient(fake.api_url)
    with pytest.raises(ValueError):
        client.move("right", [0.0, 0.0, 0.13], 0.0, 1.0)
    dry = StationClient(fake.api_url, dry_run=True)
    dry.move("right", [0.0, 0.0, 0.05], 0.0, 1.0)
    assert not fake.posts()


def test_grip_assist_validation(write_plan):
    with pytest.raises(ValueError, match="pull_up_m"):
        load_plan(_cable_plan(write_plan, _assist(pull_up_m=[0.15])))
    with pytest.raises(ValueError, match="hold"):
        load_plan(_cable_plan(write_plan, _assist(hold=[0.3, 0.1])))
    with pytest.raises(ValueError, match="unknown"):
        load_plan(_cable_plan(write_plan, _assist(pull=1)))
    assert load_plan(_cable_plan(write_plan, True)).stages[0].grip_assist.hold == (0.045, 0.20)


def test_writer_off_per_stage_and_retry_prompt(fake, write_plan, tmp_path):
    path = write_plan([stage("ball_1", prompt="pick up a ball", max_attempts=1, on_fail="skip"),
                       stage("stack_2", prompt="put a cube on top of the other cube", write_prompt=False,
                             retry_prompt="stack the blocks and sticks as high as possible", timeout_s=0.3,
                             max_attempts=2)],
                      write_prompts=True, rewrite_prompts=True, rewrite_cooldown_s=0)
    w = Writer([lambda st, fr, e: mv(True, progress=1.0) if st.name == "ball_1"
                else mv(None, "stalled", next_prompt="with the right arm, pick up the pink cube")],
               ("With the right arm, pick up the orange golf ball and drop it into the white basket.", "written", 1.0))
    r = build(fake, path, tmp_path, w)
    r.run()
    assert prompts(fake) == ["With the right arm, pick up the orange golf ball and drop it into the white basket.",
                             "put a cube on top of the other cube", "stack the blocks and sticks as high as possible"]
    assert w.asked == ["ball_1"]                     # the writer only for the ball stage
    assert not events(r.log, "prompt_rewrite")       # no mid-stage rewrite on a write_prompt false stage


def test_monitor_model_flag(monkeypatch):
    for k in ("HARNESS_OPENROUTER_MODELS", "HARNESS_CLAUDE_BACKEND"):
        monkeypatch.setenv(k, "x")      # registers the variable so monkeypatch restores it after the test
        monkeypatch.delenv(k)
    _set_backend(None, "openrouter")
    assert os.environ["HARNESS_OPENROUTER_MODELS"] == DEFAULT_OPENROUTER_MONITOR == "google/gemini-3.1-flash-lite"
    monkeypatch.delenv("HARNESS_OPENROUTER_MODELS")
    monkeypatch.setenv("HARNESS_CLAUDE_BACKEND", "api")
    _set_backend("qwen/some-vl", None)
    assert os.environ["HARNESS_CLAUDE_BACKEND"] == "openrouter" and os.environ["HARNESS_OPENROUTER_MODELS"] == "qwen/some-vl"
    _set_backend("qwen/some-vl", "subscription")     # an explicit backend wins
    assert os.environ["HARNESS_CLAUDE_BACKEND"] == "subscription"


def test_bot_v1_plan():
    plan = load_plan(ROOT / "plans/bot_v1.yaml")
    names = [s.name for s in plan.stages]
    first_stack, first_ball = names.index("tower"), names.index("ball_1")
    assert all(n.startswith(("cable_", "arms_")) for n in names[:first_stack])
    assert all(n.startswith(("stack", "tower")) for n in names[first_stack:first_ball])
    assert all(n.startswith("ball_") for n in names[first_ball:])
    assert plan.checkpoint == "dreamlabs/dream-1-beta" and plan.time_budget_s == 295 and plan.rewrite_max == 1
    assert plan.claude_user["confirm_model"] == plan.claude_user["writer_model"] == "claude-sonnet-5-5"
    unplugs = [s for s in plan.stages if s.name.startswith("cable_unplug")]
    assert len(unplugs) == 1 and all(s.grip_assist and s.confirm_check and not s.write_prompt for s in unplugs)
    assert unplugs[0].grip_assist.loop_until_s == 120
    ga = unplugs[0].grip_assist
    assert (ga.regrip, ga.pull_up_m, ga.pull_s, ga.repull, ga.hold) == (2, [0.10], 4.0, 0, (0.062, 0.20))
    assert ga.put_down_prompt and unplugs[0].prompt.endswith(ga.put_down_prompt)   # one prompt: unplug + place
    assert "white basket" in ga.put_down_prompt
    assert unplugs[0].prompt.startswith(plan.prompt_prefix) and "half open" in unplugs[0].prompt and "port on top of" in unplugs[0].prompt and "toss" not in unplugs[0].prompt
    stacks = plan.stages[first_stack:first_ball]
    assert [x.name for x in stacks] == ["tower"]
    assert stacks[0].prompt == "stack one blue cube on top of the other blue cube, then put the pink cube on top of them" and stacks[0].resume_on_no
    assert stacks[0].confirm == 2 and stacks[0].confirm_check and not stacks[0].write_prompt
    assert not plan.tower_prompts
    balls = plan.stages[first_ball:]
    assert all(s.write_prompt and "nearest" in s.prompt and "white basket" in s.prompt and s.writer_notes
               and "left arm" in s.writer_notes and "SKIP" in s.writer_notes and s.confirm_check for s in balls)
    assert plan.stop_on_pass and plan.claude_user["hedge"] == 2
    assert all(s.confirm == 2 and s.confirm_check for s in stacks)
    assert all(plan.guard.covers(s.name) for s in balls) and not plan.guard.covers("cable_unplug")


# -- 8 Nov evening additions: lock a pass, nearest-arm ball writer, no homing between stages, hedged calls ----------
class Confirming(Script):
    """Script verifier that also answers the confirm_check, in order."""

    def __init__(self, items, answers):
        super().__init__(items)
        self.answers = list(answers)

    def confirm(self, stage, frames):
        from harness.verifiers import Verdict
        return Verdict(self.answers.pop(0) if len(self.answers) > 1 else self.answers[0], 1.0, "scripted confirm")


def test_stop_on_pass_locks_the_success_and_resumes_on_a_confirm_no(fake, write_plan, tmp_path):
    path = write_plan([stage("stack_2", prompt="put a cube on top of the other cube", timeout_s=3.0, confirm=1,
                             confirm_check="a cube on a cube?")], stop_on_pass=True)
    v = Confirming([mv(True, progress=1.0)], [False, True])
    r = build(fake, path, tmp_path, v)
    res = r.run()
    assert res.stages[0].ok
    assert [e["stage"] for e in events(r.log, "stage_locked")] == ["stack_2", "stack_2"]
    rs = [x for x in routes(fake) if x in ("/start", "/stop")]
    assert rs[:4] == ["/start", "/stop", "/start", "/stop"]          # locked, resumed after the "no", locked again
    assert prompts(fake) == ["put a cube on top of the other cube"] * 2
    checks = events(r.log, "confirm_check")
    assert [c["yes"] for c in checks] == [False, True]
    # Dream was stopped before each confirm was asked
    ev = [e["event"] for e in events(r.log) if e["event"] in ("stage_locked", "confirm_check")]
    assert ev == ["stage_locked", "confirm_check", "stage_locked", "confirm_check"]


def test_without_stop_on_pass_dream_keeps_running_into_the_next_stage(fake, write_plan, tmp_path):
    path = write_plan([stage("a", confirm=1), stage("b", confirm=1)])
    r = build(fake, path, tmp_path, Script([mv(True, progress=1.0)]))
    r.run()
    assert not events(r.log, "stage_locked") and "/prompt" in routes(fake)


def test_writer_skip_skips_the_ball_stage(fake, write_plan, tmp_path):
    path = write_plan([stage("ball_1", prompt="pick up a ball", writer_notes="nearest legal ball, or SKIP",
                             max_attempts=1, on_fail="skip"),
                       stage("ball_2", prompt="pick up a ball", max_attempts=1, on_fail="skip")], write_prompts=True)
    w = Writer([mv(True, progress=1.0)], ("SKIP", "written", 0.5))
    r = build(fake, path, tmp_path, w)
    res = r.run()
    assert not res.stages[0].ok and "nothing to do" in res.stages[0].reason
    assert prompts(fake) == ["SKIP"]   # no notes on ball_2: SKIP is not special there (and is what it sent)


def test_writer_notes_reach_the_writer(write_plan):
    from test_v2 import Transport
    from harness.verifiers import MonitorVerifier
    plan = load_plan(write_plan([stage("ball_1", prompt="pick up a ball", verifier="monitor",
                                       writer_notes="Name the arm on the ball's side.")]))
    t = Transport("<prompt> With the left arm, pick up the green golf ball at the front left </prompt>")
    text, why, _ = MonitorVerifier(plan, transport=t).write_prompt(plan.stages[0], {"video.scene": b"\xff\xd8x\xff\xd9"})
    assert text.startswith("With the left arm")
    asked = t.payloads[0]["messages"][0]["content"][-1]["text"]
    assert "For this stage: Name the arm on the ball's side." in asked and "<prompt>SKIP</prompt>" in asked


def test_no_homing_between_cable_stages(fake, write_plan, tmp_path):
    fake.reset_s = 0.05
    path = write_plan([stage("cable_unplug", confirm=1), stage("cable_unplug_more", confirm=1),
                       stage("stack_2", confirm=1)], reset_first=True, stop_on_pass=True)
    r = build(fake, path, tmp_path, Script([mv(True, progress=1.0)]))
    assert r.run().ok
    rs = routes(fake)
    assert rs.count("/reset") == 1 and rs[0] == "/reset" and "/home" not in rs


def test_monitor_hedge_takes_the_first_good_answer(write_plan):
    import threading
    from test_v2 import reply_text
    from harness.verifiers import MonitorVerifier
    plan = load_plan(write_plan([stage("a", verifier="monitor")], claude={"model": "m", "hedge": 2}))
    n = {"k": 0}
    lock = threading.Lock()

    def transport(payload):
        with lock:
            n["k"] += 1
            k = n["k"]
        if k == 1:
            raise TimeoutError("slow copy")
        return {"content": [{"type": "text", "text": reply_text(stage_done=True, progress=1.0)}]}

    m = MonitorVerifier(plan, transport=transport)
    v = m.check_now(plan.stages[0], {"video.scene": b"\xff\xd8x\xff\xd9"}, 1.0)
    assert v.done is True and n["k"] == 2 and m.hedge_wins == [1]


def test_live_guard_trip_stops_the_ball_stage(fake, write_plan, tmp_path):
    from harness.live_guard import LiveGuard
    path = write_plan([stage("ball_1", verifier="monitor", timeout_s=3.0, max_attempts=1, on_fail="skip")],
                      verifier="monitor", guard={"question": "white ball held?", "stages": ["ball_"]})
    r = build(fake, path, tmp_path, Script([mv(None)]))
    n = {"k": 0}

    def frames():
        n["k"] += 1
        return {"wrist_r": f"frame{n['k']}".encode()}   # a new frame every tick

    r.live_guard = LiveGuard(frames, lambda f: (True, "white ball in the right gripper"), r.station.stop,
                             rate_hz=50, confirm_frames=3, log=r.log.event)
    r.live_guard.start()
    try:
        res = r.run()
    finally:
        r.live_guard.close()
    assert not res.ok and res.guard_tripped and "white ball" in res.reason
    assert any(e.get("source") == "live" for e in events(r.log, "guard_tripped"))
    assert "/stop" in routes(fake)


def test_live_run_refused_without_the_live_guard(write_plan, capsys):
    from harness.cli import main
    path = write_plan([stage("ball_1", verifier="monitor")], verifier="monitor",
                      guard={"question": "white ball held?", "stages": ["ball_"]})
    assert main([str(path), "--live"]) == 2
    assert "--live-guard" in capsys.readouterr().err
    assert main([str(path), "--live", "--live-guard"]) == 2   # needs --detect


def test_no_pull_from_table_height(fake, write_plan, tmp_path):
    fake.grip_readings = [0.9, 0.9, 0.9, 0.08]   # holding something...
    fake.hand_z = 0.02                           # ...at table height: a loose cable, not a plug in the strip
    r = build(fake, _cable_plan(write_plan, _assist(min_z=0.05, regrip=0), timeout_s=0.6), tmp_path, Script([mv(None)]))
    r.run()
    assert events(r.log, "grip_too_low")[0]["z"] == 0.02
    assert not events(r.log, "grip_pull")
    assert all(m["dxyz"][2] >= 0 and m["gripper"] == 0.6 for m in _moves(fake))   # let go and lift, nothing else


def test_a_pull_that_held_passes_once_confirm_agrees(fake, write_plan, tmp_path):
    fake.grip_readings = [0.9, 0.9, 0.9, 0.1]
    path = _cable_plan(write_plan, _assist(min_z=0.05), confirm_check="fewer plugs seated?", timeout_s=3.0)
    v = Confirming([mv(None, progress=0.5)], [True])   # the fast monitor never says done (8 Nov live run)
    r = build(fake, path, tmp_path, v)
    res = r.run()
    assert res.stages[0].ok and res.stages[0].reason == "grip assist pulled it out, confirm agrees"
    assert events(r.log, "pull_pass_check")[0]["passed"] is True


def test_an_early_stalled_look_does_not_end_the_attempt(fake, write_plan, tmp_path):
    path = write_plan([stage("stack_2", timeout_s=1.0, stall_s=0.6, confirm=1, on_fail="skip")])
    sv = Script([mv(None, "stalled")])
    t0 = time.monotonic()
    r = build(fake, path, tmp_path, sv).run().stages[0]
    assert r.reason.startswith("stalled") and time.monotonic() - t0 >= 0.6   # not on the first look


def test_fine_tune_plan_sends_only_its_trained_texts():
    trained = {"unplug the USB cable at the end of the hub nearest the right arm",
               "put the held cable down on the table away from the hub", "put a block on top of the stack",
               "put a golf ball in the bin", "open both grippers and move the arms up"}
    plan = load_plan(ROOT / "plans/bot_v1_ft.yaml")
    sent = {p for s in plan.stages for p in [s.prompt, s.retry_prompt, s.grip_assist and s.grip_assist.put_down_prompt,
                                             *(r.prompt for r in s.menu())] if p}
    assert sent <= trained and plan.checkpoint == "dream-1-ft:bb4b9e5f-relay-final"
    assert not plan.write_prompts and not plan.rewrite_prompts and not plan.prompt_prefix
    assert all(s.grip_assist for s in plan.stages if s.name.startswith("cable_unplug")) and plan.stop_on_pass


def test_hands_off_ignores_noisy_looks_and_never_drops_the_cube(fake, write_plan, tmp_path):
    rec = [{"name": "let_go_and_lift", "kind": "reset", "prompt": "open both grippers and move the arms up",
            "settle_s": 0.05}]
    path = write_plan([stage("stack_2", prompt="put a block on top of the stack", timeout_s=0.8, stall_s=0.05,
                             max_attempts=3, recovery=rec, confirm=1)], hands_off=True)
    noisy = [mv(None, "ok", progress=0.5), mv(None, "stalled", progress=0.0), mv(None, "failing", progress=0.1),
             mv(False, "stalled", progress=0.0)]
    r = build(fake, path, tmp_path, Script(noisy, repeat=True))
    res = r.run()
    assert not events(r.log, "regression") and not events(r.log, "recovery")
    assert prompts(fake) == ["put a block on top of the stack"]   # sent once, never resent, never "open both grippers"
    assert [e["reason"] for e in events(r.log, "attempt_end")][0].startswith("timeout")
    assert {e["mode"] for e in events(r.log, "retry")} == {"hands_off_keep_running"}


def test_lock_prompt_keeps_dream_running(fake, write_plan, tmp_path):
    path = write_plan([stage("stack_2", prompt="put a block on top of the stack", timeout_s=3.0, confirm=1,
                             confirm_check="stacked?"), stage("ball_1", prompt="put a golf ball in the bin", confirm=1)],
                      stop_on_pass=True, lock_prompt="open both grippers and move the arms up")
    v = Confirming([mv(True, progress=1.0)], [False, True, True])
    r = build(fake, path, tmp_path, v)
    assert r.run().ok
    rs = [(x, b.get("prompt")) for _, x, b in fake.posts() if x in ("/start", "/prompt", "/stop")]
    assert rs[0] == ("/start", "put a block on top of the stack")
    assert ("/stop", None) not in rs[:-1]                     # never stopped until the end of the plan
    assert [x for x, _ in rs].count("/start") == 1            # one warm run throughout
    sent = [p for _, p in rs if p]
    assert sent[1:4] == ["open both grippers and move the arms up", "put a block on top of the stack",
                         "open both grippers and move the arms up"]          # lock, resume after the "no", lock
    assert sent[-2:] == ["put a golf ball in the bin", "open both grippers and move the arms up"]   # its own lock


def test_skip_pressed_before_a_stage_skips_that_stage(fake, write_plan, tmp_path):
    from harness.operator import Operator
    path = write_plan([stage("cable_unplug", confirm=1), stage("stack_2", confirm=1)])
    r = build(fake, path, tmp_path, Script([mv(True, progress=1.0)]))
    r.operator = Operator(r.plan, r.log, transport=None)
    r.operator.submit("!skip")                    # pressed while nothing is running yet
    res = r.run()
    assert [(x.name, x.ok) for x in res.stages] == [("cable_unplug", False), ("stack_2", True)]
    assert prompts(fake) == ["do stack_2"]


def test_drop_dxyz_puts_the_cable_down_without_dream(fake, write_plan, tmp_path):
    fake.grip_readings = [0.9, 0.9, 0.9, 0.1]
    r = build(fake, _cable_plan(write_plan, _assist(drop_dxyz=[0.0, -0.20, -0.05], put_down_prompt=None, seat_dz=0.0)),
              tmp_path, Script([mv(None)]))       # passes on the pull itself (pass_on_pull, no confirm_check)
    assert r.run().stages[0].ok
    moves = [(m["dxyz"], m["gripper"]) for m in _moves(fake)]
    assert moves[-3:] == [([0.0, -0.1, -0.025], 0.0), ([0.0, -0.1, -0.025], 0.0), ([0.0, 0.0, 0.0], 0.8)]
    assert [b["prompt"] for _, rt, b in fake.posts() if rt == "/start"] == [f"{PRE} grip the plug and pull it up"]
    assert events(r.log, "grip_drop")


def test_unplug_loop_goes_straight_back_for_the_next_plug(fake, write_plan, tmp_path):
    fake.grip_readings = [0.9, 0.9, 0.9, 0.1]
    lifts = {"n": 0}

    def on_move(b):   # after each drop (open 0.8) Dream opens on the way in and grips the next plug
        if b["gripper"] == 0.8:
            fake.grip_readings[:] = [0.9, 0.9, 0.9, 0.1]

    fake.on_move = on_move
    path = _cable_plan(write_plan, _assist(drop_dxyz=[0.0, -0.1, 0.0], put_down_prompt=None, seat_dz=0.0,
                                           loop_until_s=2.5), timeout_s=10.0)
    r = build(fake, path, tmp_path, Script([mv(None)]))
    res = r.run()
    outs = events(r.log, "cable_out")
    assert res.stages[0].ok and len(outs) >= 2 and "cable(s) out" in res.stages[0].reason
    starts = [b["prompt"] for _, rt, b in fake.posts() if rt == "/start"]
    assert set(starts) == {f"{PRE} grip the plug and pull it up"}   # always the approach prompt, never a stage change


def test_engine_bot_plan_composes_every_stage_and_runs_the_grip_assist(fake, write_plan, tmp_path):
    plan = load_plan(ROOT / "plans/engine_bot.yaml")
    assert plan.prompts is not None and plan.prompts.mode == "compose" and not plan.write_prompts and plan.hands_off
    assert all(s.template for s in plan.stages if s.name != "arms_clear")
    assert plan.stages[0].grip_assist and plan.stages[0].grip_assist.loop_until_s
def test_shut_on_air_then_half_reopen_is_not_a_hold(fake, write_plan, tmp_path):
    fake.grip_readings = [0.85, 0.85, 0.035, 0.168, 0.12, 0.12, 0.12]   # 8 Nov live run 21:46
    r = build(fake, _cable_plan(write_plan, _assist(regrip=0), timeout_s=0.6), tmp_path, Script([mv(None)]))
    r.run()
    assert not events(r.log, "grip_assist") and not _moves(fake)


def test_stack_lock_flows_into_the_next_stage_and_tower_is_not_resent(fake, write_plan, tmp_path):
    third = "put the pink cube on top of the stacked blue cubes"
    path = write_plan([stage("stack_2", prompt="put the pink cube on top of the blue cube", confirm=1,
                             confirm_check="2 stacked?", lock_prompt=third),
                       stage("tower_pass", prompt=third, confirm=1, confirm_check="> 4 in?", resume_on_no=False,
                             timeout_s=1.0, max_attempts=1, on_fail="skip")],
                      stop_on_pass=True, lock_prompt="open both grippers and move the arms up", hands_off=True)
    v = Confirming([mv(True, progress=1.0)], [True] + [False] * 200)
    r = build(fake, path, tmp_path, v)
    r.run()
    sent = prompts(fake)
    assert sent[:3] == ["put the pink cube on top of the blue cube", third, "open both grippers and move the arms up"]
    assert sent.count(third) == 1                           # kept from the lock, never resent onto the tower
    assert events(r.log, "prompt_kept") and events(r.log, "lock_held")


def test_no_pull_while_dream_commands_the_jaws_open(fake, write_plan, tmp_path):
    fake.grip_readings = [0.9, 0.9, 0.9, 0.12]    # half-open approach posture reads like a plug...
    fake.grip_cmd = 0.5                            # ...but Dream is commanding the gripper open
    r = build(fake, _cable_plan(write_plan, _assist(regrip=0), timeout_s=0.6), tmp_path, Script([mv(None)]))
    r.run()
    assert not events(r.log, "grip_assist")


def test_a_plug_reading_005_after_the_lift_is_held_and_counts_the_cable(fake, write_plan, tmp_path):
    fake.grip_readings = [0.9, 0.9, 0.9, 0.08]
    fake.grip_cmd = 0.0
    fake.on_move = lambda b: b["dxyz"][2] > 0 and fake.grip_readings.__setitem__(slice(None), [0.05])
    path = _cable_plan(write_plan, _assist(seat_dz=0.0), confirm_check="fewer plugs?", timeout_s=3.0)
    v = Confirming([mv(None)], [False])            # the camera check would say no
    r = build(fake, path, tmp_path, v)
    res = r.run()
    assert events(r.log, "grip_pull")[0]["outcome"] == "pulled" and not events(r.log, "grip_slipped")
    assert events(r.log, "pull_counted") and res.stages[0].ok
