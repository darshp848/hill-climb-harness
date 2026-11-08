"""The illegal-ball guard and the colour detector on the station's own cameras (tests/data/guard_real/).

Frames are from plug-unplug-v1 takes 39-40 (7 Nov, evening light), 640x360, top blurred (people). There is no footage
of a held ball yet, so the held_white_* frames are composites: a real wrist frame with the real white ball (cut from
another frame of the same camera) pasted between the fingertips at the size a held golf ball has there.

Before these, the white range was tuned on a phone photo (V >= 225) and matched 0.1% of the real ball, and the grip
zone let a ball lying past the fingertips trip the guard (3 trips in one take once white was visible).
"""

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from harness.detect import TableDetector, detect

ROOT = Path(__file__).resolve().parents[1]
COLORS = ROOT / "plans" / "table_colors.yaml"
REAL = ROOT / "tests" / "data" / "guard_real"


@pytest.fixture(scope="module")
def det() -> TableDetector:
    return TableDetector.from_file(COLORS)


@pytest.mark.parametrize("name", sorted(p.name for p in REAL.glob("held_white_*.jpg")))
def test_held_white_ball_trips_the_guard(det, name):
    yes, why = det.guard({"wrist_r": (REAL / name).read_bytes()})
    assert yes is True and "white ball" in why


@pytest.mark.parametrize("name", sorted(p.name for p in REAL.glob("not_held_white_*.jpg")))
def test_white_ball_lying_near_the_fingers_is_seen_but_does_not_trip(det, name):
    jpeg = (REAL / name).read_bytes()
    assert any(b.cls == "white ball" for b in detect(jpeg, det.cfg))   # visible: the colour range fits the camera
    assert det.guard({"wrist_r": jpeg})[0] is False                    # but not between the fingertips


def test_overhead_finds_the_green_ball_and_one_white_ball_not_the_blue_cube(det):
    boxes = detect((REAL / "overhead_green_ep39_0.jpg").read_bytes(), det.cfg, overhead=True)
    classes = [b.cls for b in boxes]
    assert "green ball" in classes                                     # H 114: the old range stopped at 110
    whites = [b for b in boxes if b.cls == "white ball"]
    assert len(whites) == 1                                            # the real ball by the power strip
    cubes = [b for b in boxes if b.cls == "blue cube"]
    assert not any(c.x0 <= whites[0].cx <= c.x1 and c.y0 <= whites[0].cy <= c.y1 for c in cubes)


def _black_ball_touching_fingers(gap: int) -> bytes:
    im = Image.new("RGB", (640, 480), (200, 190, 175))
    d = ImageDraw.Draw(im)
    cx, cy, r = 320, 384, 60
    d.rectangle((cx - r - gap - 50, 150, cx - r - gap, 480), fill=(20, 20, 22))
    d.rectangle((cx + r + gap, 150, cx + r + gap + 50, 480), fill=(20, 20, 22))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(15, 15, 15))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def test_black_ball_clear_of_the_fingers_trips(det):
    assert det.guard({"wrist_r": _black_ball_touching_fingers(20)})[0] is True


@pytest.mark.xfail(strict=True, reason="known gap: a black ball touching the black fingers merges with them into one "
                   "non-round blob. From the image alone it looks like closed fingers; the gripper opening (stopped "
                   "at ball width vs fully closed, GET /arms) is the signal that separates them.")
def test_black_ball_touching_the_fingers_trips(det):
    assert det.guard({"wrist_r": _black_ball_touching_fingers(0)})[0] is True
