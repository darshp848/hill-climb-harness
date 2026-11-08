"""harness.detect on synthetic frames: black 8-ball vs the (also black) gripper fingers, golf balls, blocks."""

import io
from pathlib import Path

from PIL import Image, ImageDraw

from harness.detect import TableDetector, detect, load_config
from harness.verifiers import valid_jpeg

COLORS = Path(__file__).resolve().parents[1] / "plans" / "table_colors.yaml"
TABLE = (205, 205, 200)


def jpeg(draw_fn, size=(640, 360)) -> bytes:
    im = Image.new("RGB", size, TABLE)
    draw_fn(ImageDraw.Draw(im))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def fingers(d):
    d.rectangle((200, 120, 240, 360), fill=(15, 15, 15))     # left finger: long, thin, black
    d.rectangle((400, 120, 440, 360), fill=(15, 15, 15))     # right finger


def test_black_ball_between_the_fingers_trips_the_local_guard():
    det = TableDetector.from_file(COLORS)
    frame = jpeg(lambda d: (fingers(d), d.ellipse((280, 220, 360, 300), fill=(10, 10, 10))))
    yes, why = det.guard({"wrist_r": frame})
    assert yes is True and "black 8-ball" in why


def test_empty_gripper_is_not_a_black_ball():
    det = TableDetector.from_file(COLORS)
    yes, _ = det.guard({"wrist_r": jpeg(fingers), "scene": jpeg(lambda d: None)})
    assert yes is False


def test_golf_ball_in_the_fingers_is_not_a_black_ball():
    det = TableDetector.from_file(COLORS)
    frame = jpeg(lambda d: (fingers(d), d.ellipse((280, 220, 360, 300), fill=(250, 250, 250))))
    yes, _ = det.guard({"wrist_r": frame})
    assert yes is False


def test_unreadable_wrist_frame_is_no_answer():
    assert TableDetector.from_file(COLORS).guard({"wrist_r": b"\xff\xd8garbage\xff\xd9"})[0] is None


def test_overhead_boxes_and_annotation():
    cfg = load_config(COLORS)

    def scene(d):
        d.ellipse((50, 50, 80, 80), fill=(250, 250, 250))       # golf ball
        d.ellipse((120, 50, 152, 82), fill=(12, 12, 12))        # black 8-ball
        d.rectangle((300, 100, 350, 150), fill=(220, 30, 30))   # red block
        d.rectangle((420, 200, 520, 230), fill=(40, 170, 60))   # green cylinder lying down
        d.ellipse((560, 60, 600, 100), fill=(40, 170, 60))      # green cylinder standing up
    frame = jpeg(scene)
    found = {(b.cls, b.orientation) for b in detect(frame, cfg)}
    assert {("golf ball", None), ("black 8-ball", None), ("red block", None),
            ("green cylinder", "lying"), ("green cylinder", "upright")} <= found
    det = TableDetector(cfg)
    boxed = det.annotate_frames({"scene": frame})["scene"]
    assert boxed != frame and valid_jpeg(boxed)
    assert "RED boxes" in det.legend() and "black 8-ball" in det.legend()
