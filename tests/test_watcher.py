"""Watcher-arm support: per-stage cameras, camera notes, frame history for a watching wrist camera, plan vars."""

from pathlib import Path

import pytest

from harness.plan import load_plan, parse_plan
from harness.verifiers import MonitorVerifier
from test_v2 import JPEG, Transport, reply_text


def _st(**kw):
    plan = parse_plan({"task": "t", "stages": [{"name": "plug", "prompt": "plug it", "success": "in?", **kw}]})
    return plan, plan.stages[0]


def test_stage_camera_fields_parse_and_validate():
    _, s = _st(cameras=["scene", "wrist_l"], history_cameras=["wrist_l"],
               camera_notes={"wrist_l": "side view", "scene": "top view"})
    assert s.cameras == ["scene", "wrist_l"] and s.history_cameras == ["wrist_l"]
    assert s.note_for("wrist_l") == "side view" and s.note_for("wrist_r") == ""
    with pytest.raises(ValueError, match="camera_notes"):
        _st(camera_notes={"wrist_l": ""})
    with pytest.raises(ValueError, match="history_cameras"):
        _st(history_cameras="wrist_l")


def test_monitor_keeps_history_for_watcher_camera_and_labels_notes():
    plan, st = _st(history_cameras=["wrist_l"], camera_notes={"wrist_l": "side view from the watching arm"})
    t = Transport(reply_text())
    mon = MonitorVerifier(plan, transport=t, history_frames=2)
    frames = lambda n: {"video.scene": JPEG + bytes([n]), "video.wrist_l": b"l" + bytes([n]),  # noqa: E731
                        "video.wrist_r": b"r" + bytes([n])}
    for i in range(4):
        mon.check_now(st, frames(i), 3.0 * i)
    content = t.payloads[-1]["messages"][0]["content"]
    labels = [b["text"] for b in content if b["type"] == "text"][:-1]
    watch = [lbl for lbl in labels if "wrist_l" in lbl]
    assert watch[0].startswith("t=+0s video.wrist_l [side view from the watching arm] (stage start)")
    assert len(watch) == 4                                   # start + 2 history + newest
    assert sum("wrist_r" in lbl for lbl in labels) == 1      # not tracked: newest only
    assert sum("overhead" in lbl for lbl in labels) == 4
    assert "Camera notes" in content[-1]["text"] and "side view from the watching arm" in content[-1]["text"]


def test_stage_cameras_filter_frames(tmp_path):
    from harness.runner import Runner

    class Frames:
        def latest(self):
            return {"scene": b"s", "wrist_l": b"l", "wrist_r": b"r"}

    plan, st = _st(cameras=["wrist_l"])
    r = Runner(plan, station=None, frames=Frames(), verifiers={}, log=None)
    assert set(r._frames_for_check(st)) == {"wrist_l"}
    plan.cameras_for_check = ["scene"]
    _, st2 = _st()
    assert set(r._frames_for_check(st2)) == {"scene"}


def test_plan_vars_defaults_and_override(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text('task: t\nvars: {arm: left}\nstages:\n  - {name: a, prompt: "use the {arm} arm", success: "ok?"}\n')
    assert load_plan(p).stages[0].prompt == "use the left arm"
    assert load_plan(p, {"arm": "right"}).stages[0].prompt == "use the right arm"


def test_v4_plan_has_no_plug_in_and_notes_the_stacking_camera():
    plan = load_plan(Path(__file__).resolve().parent.parent / "plans" / "hill_climb_v4.yaml")
    names = [s.name for s in plan.stages]
    assert not any(n.startswith("plug") for n in names)
    assert not any(s.prompt.startswith("plug") for s in plan.stages)
    assert names.index("unplug_cable") < names.index("stack_2")
    assert "wrist_r" in plan.stages[names.index("stack_2")].camera_notes
