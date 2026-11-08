"""The table detector on the station's own overhead camera (8 Nov rollouts); everything above the table (people)
is blanked to table colour.

Each case is a failure the 7-8 Nov tuning had on these frames, checked against hand labels: the pale yellow-green
golf ball fell between the yellow and green hue ranges; the white bin was boxed as a white ball (64% of frames);
two touching blue cubes read as one "blue stick"; small half-lit balls were dropped as neither round nor square.
Full check: nuc_analysis_20261108/classifiers/gt (14 tuning + 12 held-out labelled frames)."""

from pathlib import Path

import pytest

from harness import detect as D

DATA = Path(__file__).parent / "data" / "table_real"
CFG = D.load_config(Path(__file__).parents[1] / "plans" / "table_colors.yaml")
W, H = 640, 360


def boxes(name):
    return D.detect((DATA / name).read_bytes(), CFG, overhead=True)


def at(bs, cls, x, y, tol=8):
    return [b for b in bs if b.cls == cls and b.x0 * W - tol <= x <= b.x1 * W + tol
            and b.y0 * H - tol <= y <= b.y1 * H + tol]


@pytest.mark.parametrize("name,objs", [
    ("f2_yellow_ball_merged_blue.jpg", [("yellow ball", 193, 173), ("white ball", 161, 193), ("orange ball", 251, 183),
                                        ("green ball", 251, 226), ("blue cube", 279, 225), ("blue cube", 259, 245),
                                        ("pink cube", 277, 260)]),
    ("f5_white_ball_touching_bin.jpg", [("yellow ball", 475, 307), ("green ball", 273, 183), ("orange ball", 278, 214),
                                        ("blue cube", 293, 227), ("blue cube", 314, 227), ("pink cube", 329, 210)]),
    ("f9_late_table.jpg", [("blue cube", 263, 155), ("pink cube", 298, 163), ("white ball", 460, 235),
                           ("green ball", 470, 300), ("orange ball", 472, 325)]),
])
def test_finds_the_labelled_objects(name, objs):
    bs = boxes(name)
    for cls, x, y in objs:
        assert at(bs, cls, x, y), (cls, x, y, [(b.cls, round(b.cx * W), round(b.cy * H)) for b in bs])


@pytest.mark.parametrize("name", sorted(p.name for p in DATA.glob("*.jpg")))
def test_the_bin_is_not_a_ball(name):
    assert not [b for b in boxes(name) if b.cls.endswith(" ball") and b.frac > D.OVERHEAD_BALL_MAX_FRAC]


def test_touching_blue_cubes_are_two_cubes():
    bs = boxes("f2_yellow_ball_merged_blue.jpg")
    assert not [b for b in bs if b.cls == "blue stick"]
    assert len([b for b in bs if b.cls == "blue cube"]) == 2


def test_a_lying_stick_stays_whole():
    bs = boxes("f2_yellow_ball_merged_blue.jpg")
    assert len(at(bs, "yellow stick", 221, 250)) == 1 and len(at(bs, "pink stick", 171, 283)) == 1
