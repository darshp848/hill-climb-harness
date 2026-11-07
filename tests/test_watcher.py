"""Watcher-arm support: per-stage cameras, camera notes, frame history for a watching wrist camera, plan vars."""

from pathlib import Path

import pytest

from conftest import stage
from harness.frames import CameraTrip, FrameSource
from harness.log import RunLog
from harness.plan import load_plan, parse_plan
from harness.runner import Runner
from harness.station_client import StationClient
from harness.verifiers import MonitorVerifier, Verdict
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


def test_monitor_passes_dream_station_wrist_and_top_images():
    plan, st = _st(history_cameras=["wrist_l"], camera_notes={"wrist_l": "watcher wrist"})
    transport = Transport(reply_text())
    monitor = MonitorVerifier(plan, transport=transport)
    monitor.check_now(st, {"image_window.top": JPEG, "image_window.left": JPEG}, 2.0)
    content = transport.payloads[-1]["messages"][0]["content"]
    assert [b["type"] for b in content] == ["text", "image", "text", "image", "text"]
    assert "overhead" in content[0]["text"]
    assert "image_window.left [watcher wrist]" in content[2]["text"]


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


def test_full_plan_watcher_stages():
    plan = load_plan(Path(__file__).resolve().parent.parent / "plans" / "hill_climb_full.yaml", {"color": "white"})
    names = [s.name for s in plan.stages]
    assert names.index("unplug_cable") < names.index("plug_loose_cable") < names.index("stack_two")
    plug = plan.stages[names.index("plug_loose_cable")]
    assert plug.history_cameras == ["wrist_l"] and "right arm" in plug.prompt and plug.on_fail == "abandon"
    assert plug.requires_stage == "watcher_into_place"
    assert plug.required_cameras == ["overhead", "wrist_l"]


@pytest.mark.parametrize("mode", ["complete", "missing_wrist", "stale_wrist", "old_prompt", "repeated"])
def test_plug_waits_for_watcher_evidence(fake, write_plan, tmp_path, mode):
    path = write_plan([
        stage("watch", timeout_s=0.7, on_fail="skip", required_cameras=["overhead", "wrist_l"]),
        stage("plug", requires_stage="watch"),
        stage("stack"),
    ], confirm=2)
    plan = load_plan(path)
    seen = []

    class Evidence(FrameSource):
        def latest_trip(self, *, fallback=False):
            trip = super().latest_trip(fallback=fallback)
            if fallback or trip is None or trip.prompt != "do watch":
                return trip
            views = {"image_window.top": trip.frames["scene"]}
            if mode != "missing_wrist":
                views["image_window.left"] = trip.frames["wrist_l"]
            return CameraTrip(1 if mode == "repeated" else trip.n,
                              "old task" if mode == "old_prompt" else trip.prompt, views)

    class Judge:
        def check(self, stage, frames, elapsed_s):
            seen.append((stage.name, set(frames)))
            return Verdict(True, 1.0, "verified")

    class CameraStatus(StationClient):
        def status(self):
            status = super().status()
            if mode == "stale_wrist" and status.get("prompt") == "do watch":
                status["cameras"]["left"] = 10
            return status

    runner = Runner(plan, CameraStatus(fake.api_url), Evidence(fake.view_url),
                    {"manual": Judge()}, RunLog(tmp_path / "logs"))
    result = runner.run()
    assert result.stages[-1].name == "stack" and result.stages[-1].ok
    watch, plug = result.stages[:2]
    if mode == "complete":
        assert watch.ok and plug.ok
        assert seen[:2] == [("watch", {"image_window.top", "image_window.left"})] * 2
    else:
        assert not watch.ok and plug.attempts == 0
        assert sum(name == "watch" for name, _ in seen) == (1 if mode == "repeated" else 0)
        assert all(name != "plug" for name, _ in seen)
        assert not any(body.get("prompt") == "do plug" for _, _, body in fake.posts())


def test_time_verifier_cannot_pass_camera_gate(fake, write_plan, tmp_path):
    plan = load_plan(write_plan([stage("watch", required_cameras=["wrist_l"])], verifier="time"))
    runner = Runner(plan, StationClient(fake.api_url), FrameSource(fake.view_url), {}, RunLog(tmp_path / "logs"))
    assert "need a visual verifier" in runner.run().reason
    assert not fake.posts()
