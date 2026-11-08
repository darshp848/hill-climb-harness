"""The decide shadow inside a real Runner: logs `decide` events, agrees with the runner on defaults, never acts."""

import pytest
from conftest import stage
from test_v2 import Script, build, events, mv

from harness.log import RunLog
from harness.plan import load_plan


def run(fake, write_plan, tmp_path, stages, items, **top):
    path = write_plan(stages, verifier="monitor", **top)
    log = RunLog(tmp_path / "runs")
    result = build(fake, path, tmp_path, Script(items, repeat=False), log).run()
    return result, events(log, "decide"), log


def test_shadow_passes_on_the_same_look_as_the_runner(fake, write_plan, tmp_path):
    items = [mv(None, progress=0.3, frames_sig="a"), mv(True, progress=1.0, frames_sig="b"),
             mv(True, progress=1.0, frames_sig="b"),           # same frames: not a second look
             mv(True, progress=1.0, frames_sig="c")]
    result, dec, log = run(fake, write_plan, tmp_path, [stage("a", timeout_s=10)], items, confirm=2)
    assert result.ok
    assert [d["would_pass"] for d in dec] == [False, False, False, True]
    assert [d["done_score"] for d in dec] == [0.0, 1.0, 1.0, 2.0]


def test_stage_confirm_1_is_followed(fake, write_plan, tmp_path):
    result, dec, _ = run(fake, write_plan, tmp_path, [stage("a", timeout_s=10, confirm=1)],
                         [mv(None, frames_sig="a"), mv(True, frames_sig="b")], confirm=2)
    assert result.ok and [d["would_pass"] for d in dec] == [False, True]


def test_shadow_fails_on_the_same_look_as_the_runner(fake, write_plan, tmp_path):
    items = [mv(None, "stalled"), mv(None, "ok"), mv(None, "stalled"), mv(None, "stalled")]
    result, dec, _ = run(fake, write_plan, tmp_path, [stage("a", timeout_s=10, stall_s=100, on_fail="skip")],
                         items, confirm=2)
    assert result.stages[0].reason.startswith("stalled")
    assert [d["would_fail"] for d in dec] == [False, False, False, True]
    assert dec[-1]["terms"]["stalled"] == 1.0


def test_plan_weights_change_only_the_log(fake, write_plan, tmp_path):
    items = [mv(True, frames_sig="a"), mv(None, frames_sig="b"), mv(True, frames_sig="c"), mv(True, frames_sig="d")]
    result, dec, _ = run(fake, write_plan, tmp_path, [stage("a", timeout_s=10)], items, confirm=2,
                         decide={"done": {"theta": 1.0}})
    assert result.ok
    assert dec[0]["would_pass"] is True        # the shadow (threshold 1) would have passed on the first done
    assert len(dec) == 3                       # the runner still decided (second done within 15 s): the shadow did not act


def test_unknown_weight_is_a_plan_error(write_plan):
    path = write_plan([stage("a")], decide={"done": {"w_monitr": 2}})
    with pytest.raises(ValueError, match="unknown weight"):
        load_plan(path)
