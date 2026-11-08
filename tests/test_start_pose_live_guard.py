"""plan.start_pose (home before a stage when the arms start far from every recorded start) and the live-frame guard."""

import json
import threading

import pytest

from conftest import stage, wait_until
from test_v2 import Script, build, events, mv, routes
from harness.live_guard import LiveFrames, LiveGuard
from harness.plan import parse_plan
from harness.start_pose import StartPoses, joints_from_arms, joints_from_observation

HOME = [0.0] * 6
RAISED = [0.3, -1.7, 0.75, 0.1, 0.05, -0.1]


def _ref(**by_prompt):
    return StartPoses({p: [{"left": l, "right": r, "episode": f"ep{i}"} for i, (l, r) in enumerate(starts)]
                       for p, starts in by_prompt.items()})


# -- start pose: the check --------------------------------------------------------------------------------------------
def test_check_statuses():
    ref = _ref(**{"do unplug": [(HOME, HOME)]})
    near = {"left": HOME, "right": [0.1, 0, 0, 0, 0, 0]}
    assert ref.check("do unplug", near, 0.35).status == "ok"
    far = ref.check("do unplug", {"left": HOME, "right": RAISED}, 0.35)
    assert far.status == "far" and far.distance["right"] == pytest.approx(1.7) and far.nearest == "ep0"
    assert ref.check("other prompt", near, 0.35).status == "no_reference"
    assert ref.check("do unplug", None, 0.35).status == "no_state"


def test_nearest_start_wins():
    ref = _ref(p=[(HOME, HOME), (HOME, RAISED)])
    chk = ref.check("p", {"left": HOME, "right": [0.25, -1.6, 0.7, 0.1, 0.0, -0.1]}, 0.35)
    assert chk.status == "ok" and chk.nearest == "ep1"


def test_joint_sources():
    assert joints_from_arms({"left": {"gripper": 1.0}, "right": {"gripper": 1.0}}) is None   # old add-on
    assert joints_from_arms({"right": {"positions_rad": RAISED}}) == {"right": RAISED}
    assert joints_from_observation({"state.left_arm": HOME, "state.right_arm": RAISED, "prompt": "x"}) == \
        {"left": HOME, "right": RAISED}
    assert joints_from_observation({"prompt": "x"}) is None


# -- start pose: the plan ---------------------------------------------------------------------------------------------
def _plan(**sp):
    return parse_plan({"task": "t", "start_pose": {"reference": "r.json", **sp},
                       "stages": [{"name": "unplug_cable", "prompt": "u", "success": "u?"}]})


def test_start_pose_parses():
    sp = _plan(home_before=["unplug"], tolerance_rad=0.2).start_pose
    assert sp.homes("unplug_cable") and sp.tolerance_rad == 0.2 and sp.max_homes == 1 and sp.home_pose["left"] == HOME


@pytest.mark.parametrize("sp, match", [
    ({"home_before": ["stack"]}, "matches no stage"),
    ({"home_pose": {"left": HOME}}, "home_pose"),
    ({"tolerance_rad": 0}, "tolerance_rad"),
    ({"homes": 2}, "unknown key"),
])
def test_bad_start_pose_rejected(sp, match):
    with pytest.raises(ValueError, match=match):
        _plan(**sp)


# -- start pose: the runner -------------------------------------------------------------------------------------------
def _pose_runner(fake, write_plan, tmp_path, *, right, home_before=("unplug_cable",), refs=None, verdicts=None,
                 max_attempts=2):
    path = write_plan([stage("unplug_cable", prompt="do unplug", max_attempts=max_attempts, timeout_s=0.6)],
                      start_pose={"reference": "unused.json", "home_before": list(home_before)})
    runner = build(fake, path, tmp_path, Script(verdicts or [mv(True)]))
    runner.start_poses = refs or _ref(**{"do unplug": [(HOME, HOME)]})
    runner.station.grippers = lambda: {"left": {"positions_rad": HOME}, "right": {"positions_rad": right}}
    return runner


def test_far_start_homes_before_the_first_prompt(fake, write_plan, tmp_path):
    runner = _pose_runner(fake, write_plan, tmp_path, right=RAISED)
    assert runner.run().ok
    r = routes(fake)
    assert r.index("/home") < r.index("/start")
    ev = events(runner.log, "start_pose")[0]
    assert ev["status"] == "far" and ev["source"] == "arms" and ev["why"] == "stage start"
    assert events(runner.log, "start_pose_home")[0]["done"] is True


def test_near_start_does_not_home(fake, write_plan, tmp_path):
    runner = _pose_runner(fake, write_plan, tmp_path, right=HOME)
    assert runner.run().ok
    assert "/home" not in routes(fake)
    assert events(runner.log, "start_pose")[0]["status"] == "ok"


def test_other_stages_only_log(fake, write_plan, tmp_path):
    runner = _pose_runner(fake, write_plan, tmp_path, right=RAISED, home_before=())
    assert runner.run().ok
    assert "/home" not in routes(fake) and events(runner.log, "start_pose")[0]["status"] == "far"


def test_no_home_when_home_is_not_a_recorded_start(fake, write_plan, tmp_path):
    refs = _ref(**{"do unplug": [(HOME, [1.5] * 6)]})
    runner = _pose_runner(fake, write_plan, tmp_path, right=RAISED, refs=refs)
    assert runner.run().ok
    assert "/home" not in routes(fake)
    assert events(runner.log, "start_pose_home")[0]["done"] is False


def test_max_homes_per_stage(fake, write_plan, tmp_path):
    runner = _pose_runner(fake, write_plan, tmp_path, right=RAISED, verdicts=[mv(False, "failing")], max_attempts=3)
    runner.run()
    assert routes(fake).count("/home") == 1          # stage start homed; the retries only logged
    homes = events(runner.log, "start_pose_home")
    assert homes[0]["done"] is True and all(h["done"] is False for h in homes[1:]) and len(homes) >= 2


def test_retry_after_far_start_homes_then_starts_fresh(fake, write_plan, tmp_path):
    arms = {"right": HOME, "failed": False}
    verdict = Script([lambda st, f, e: mv(True) if arms["failed"] else mv(False, "failing")])
    runner = _pose_runner(fake, write_plan, tmp_path, right=HOME, verdicts=None)
    runner.verifiers = {k: verdict for k in runner.verifiers}
    runner.station.grippers = lambda: {"left": {"positions_rad": HOME}, "right": {"positions_rad": arms["right"]}}
    orig = runner._attempt

    def attempt(*a, **k):
        out = orig(*a, **k)
        arms["right"], arms["failed"] = RAISED, True   # the failed attempt left the arm up over the hub
        return out
    runner._attempt = attempt
    assert runner.run().ok
    r = routes(fake)
    assert r.count("/home") == 1 and r.count("/start") == 2 and r.index("/home") < len(r) - 1
    assert [e["mode"] for e in events(runner.log, "retry")] == ["home_then_retry"]


def test_no_reference_file_means_no_checks(fake, write_plan, tmp_path):
    runner = _pose_runner(fake, write_plan, tmp_path, right=RAISED)
    runner.start_poses = None
    assert runner.run().ok and not events(runner.log, "start_pose")


def test_reference_tool(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    from tools.start_poses import main as build_ref
    ds = tmp_path / "set"
    (ds / "meta").mkdir(parents=True)
    (ds / "data" / "chunk-000").mkdir(parents=True)
    names = [f"left_joint_{j}" for j in range(6)] + ["left_gripper"] + [f"right_joint_{j}" for j in range(6)] + \
        ["right_gripper"]
    (ds / "meta" / "info.json").write_text(json.dumps({"features": {"observation.state": {"names": names}}}))
    (ds / "meta" / "tasks.jsonl").write_text('{"task_index": 0, "task": "a"}\n{"task_index": 1, "task": "b"}\n')
    rows = [[0.0] * 7 + [0.1] * 7, [0.0] * 7 + [0.2] * 7, [0.0] * 7 + [0.9] * 7]
    pq.write_table(pa.table({"observation.state": rows, "task_index": [0, 0, 1]}),
                   ds / "data" / "chunk-000" / "episode_000000.parquet")
    out = tmp_path / "ref.json"
    assert build_ref([str(out), str(ds)]) == 0
    ref = StartPoses.load(out)
    assert ref.by_prompt["a"][0]["right"] == [0.1] * 6 and ref.by_prompt["b"][0]["right"] == [0.9] * 6
    assert ref.by_prompt["b"][0]["episode"] == "set/episode_000000/seg1"


# -- live guard -------------------------------------------------------------------------------------------------------
class _Feed:
    """Distinct frames each grab unless frozen."""

    def __init__(self):
        self.n, self.frozen = 0, False

    def __call__(self):
        if not self.frozen:
            self.n += 1
        return {"wrist_r": b"jpeg%d" % self.n}


def _guard(answers, confirm=3):
    stops, log = [], []
    seq = list(answers)
    g = LiveGuard(_Feed(), lambda f: seq.pop(0) if seq else (False, "no"), lambda: stops.append(1),
                  confirm_frames=confirm, log=lambda name, **k: log.append((name, k)))
    return g, stops, log


def test_live_guard_needs_confirm_distinct_yes_in_a_row():
    g, stops, log = _guard([(True, "y"), (False, "n"), (True, "y"), (None, "?"), (True, "y"), (True, "y")])
    g.step()                                   # disarmed: nothing is looked at
    assert g.ticks == 0
    g.arm("ball_1")
    for _ in range(6):
        g.step()
    assert g.tripped == "y" and stops == [1]
    assert [k["count"] for n, k in log if n == "guard_yes"] == [1, 1, 2, 3]
    g.step()
    assert stops == [1]                        # stops once


def test_live_guard_frozen_frames_count_once():
    g, stops, _ = _guard([(True, "y")] * 5)
    g.arm("ball_1")
    g.source.frozen = True
    for _ in range(5):
        g.step()
    assert g.tripped is None and not stops


def test_live_guard_answer_time_and_disarm():
    t = [100.0]
    g = LiveGuard(_Feed(), lambda f: (False, "n"), lambda: None, clock=lambda: t[0])
    g.arm("ball_1")
    assert g.last_answer_t == 100.0
    t[0] = 105.0
    g.step()
    assert g.last_answer_t == 105.0
    g.disarm()
    t[0] = 110.0
    g.step()
    assert g.last_answer_t == 105.0


def test_live_frames_drop_a_camera_without_the_route(fake):
    lf = LiveFrames(fake.api_url, ("wrist_l", "wrist_r"), max_misses=2)
    assert lf.grab() == {} and lf.grab() == {}
    assert lf.roles == [] and "404" in lf.last_error


def test_live_guard_trip_ends_the_match_mid_stage(fake, write_plan, tmp_path):
    q = "Is a gripper holding a black ball?"
    path = write_plan([stage("stack", timeout_s=0.3, on_fail="skip"), stage("ball_1", timeout_s=5.0, on_fail="skip"),
                       stage("ball_2", timeout_s=0.3, on_fail="skip")],
                      verifier="monitor", guard={"question": q, "stages": ["ball_"]})
    runner = build(fake, path, tmp_path, Script([mv(None, guard=False)]))
    armed = threading.Event()
    g = LiveGuard(_Feed(), lambda f: (True, "white ball between the fingers"), runner.station.stop, rate_hz=50,
                  confirm_frames=3, log=runner.log.event)
    orig_arm = g.arm
    g.arm = lambda s: (orig_arm(s), armed.set())
    runner.live_guard = g
    g.start()
    try:
        result = runner.run()
    finally:
        g.close()
    assert armed.is_set()
    assert not result.ok and result.guard_tripped and "live frames" in result.reason
    assert [r.name for r in result.stages] == ["stack"]
    trip = [e for e in events(runner.log, "guard_tripped") if e.get("source") == "live"]
    assert trip and trip[0]["stage"] == "ball_1" and trip[0]["stop"] == "sent"
    assert routes(fake).count("/stop") >= 1
    assert not wait_until(lambda: g.ticks > 10_000, timeout=0.05)   # thread is idle after the trip
