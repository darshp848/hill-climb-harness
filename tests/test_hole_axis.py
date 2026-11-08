"""Which way a cube's hole runs, and upright sticks, on drawn test images."""

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from harness.detect import detect, load_config, top_cube_hole

CFG = load_config(Path(__file__).resolve().parents[1] / "plans" / "table_colors.yaml")
BLUE, PINK, YELLOW, DARK, WOOD = (40, 120, 190), (210, 100, 170), (235, 205, 60), (30, 35, 45), (232, 224, 210)


def jpeg(draw) -> bytes:
    im = Image.new("RGB", (640, 480), WOOD)
    draw(ImageDraw.Draw(im))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def cube(d, x=280, y=200, s=80, color=BLUE):
    d.rectangle([x, y, x + s, y + s], fill=color)


@pytest.mark.parametrize("hole, axis", [
    ((320, 235, 13, 13), "vertical"),       # on top: centred or a little high
    ((320, 262, 13, 13), "horizontal-y"),   # on the face toward the camera
    ((345, 240, 6, 13), "horizontal-x"),    # on a side face: off to one side and narrow
])
def test_hole_axis(hole, axis):
    cx, cy, rx, ry = hole

    def draw(d):
        cube(d)
        d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=DARK)
    box = next(b for b in detect(jpeg(draw), CFG, overhead=True) if b.cls == "blue cube")
    assert box.hole_axis == axis and box.confidence > 0.5
    assert f"hole {axis}" in box.text()
    assert box.info()["shape"] == "cube" and box.info()["colour"] == "blue"


def test_no_hole_means_it_runs_sideways_out_of_view():
    box = next(b for b in detect(jpeg(cube), CFG) if b.cls == "blue cube")
    assert box.hole_axis == "horizontal"


def test_only_the_overhead_view_is_trusted():
    def draw(d):
        cube(d)
        d.ellipse([307, 222, 333, 248], fill=DARK)
    box = next(b for b in detect(jpeg(draw), CFG, overhead=False) if b.cls == "blue cube")
    assert box.confidence < 0.6 and "hole" not in box.text()


def test_table_showing_through_counts_as_a_hole():
    def draw(d):
        cube(d, color=PINK)
        d.ellipse([306, 222, 334, 250], fill=WOOD)
    assert next(b for b in detect(jpeg(draw), CFG) if b.cls == "pink cube").hole_axis == "vertical"


def test_edge_shadow_is_not_a_hole():
    def draw(d):
        cube(d)
        d.rectangle([280, 262, 360, 280], fill=DARK)    # a shadow along the bottom edge
    assert next(b for b in detect(jpeg(draw), CFG) if "cube" in b.cls).hole_axis == "horizontal"


def test_sticks_have_no_hole_and_a_stick_on_a_cube_is_upright():
    def draw(d):
        cube(d)
        d.rectangle([312, 80, 328, 199], fill=YELLOW)
        d.rectangle([60, 400, 200, 418], fill=PINK)
    boxes = detect(jpeg(draw), CFG)
    upright = next(b for b in boxes if b.cls == "yellow stick")
    lying = next(b for b in boxes if b.cls == "pink stick")
    assert upright.orientation == "upright" and upright.hole_axis is None
    assert lying.orientation == "lying"


def test_top_cube_of_a_stack():
    def draw(d):
        cube(d, y=280)
        cube(d, y=199, color=PINK)
        d.ellipse([306, 222, 334, 250], fill=DARK)
    axis, conf = top_cube_hole(detect(jpeg(draw), CFG, overhead=True))
    assert axis == "vertical" and conf > 0.5
    assert top_cube_hole([]) == (None, 0.0)
