"""The optimised detector must answer exactly as the one the match was tested with.

tests/_detect_reference.py is harness/detect.py as of f756095, before the Apple Silicon work. On every recorded frame
in tests/data/guard_frames the guard verdict, every box and every annotated JPEG must be identical; golden.json holds
the verdicts the reference gave when the frames were added. The frames (8 Nov, all chosen with no people in view, the
repo is public): real_* station wrist frames from the NUC's last-run_inputs, sim_* Dream YAM sim wrist frames, synth_*
real frames with a white or black ball painted at the fingers (five are guard yeses), table_* overhead views. Set HARNESS_GUARD_FRAMES=<folder> to also compare on a folder of your own frames (searched recursively).
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest

from harness import detect
from harness.detect import TableDetector

ndi = pytest.importorskip("scipy.ndimage")

HERE = Path(__file__).resolve().parent
FRAMES = HERE / "data" / "guard_frames"
CONFIG = HERE.parent / "plans" / "table_colors.yaml"


def _reference():
    spec = importlib.util.spec_from_file_location("_detect_reference", HERE / "_detect_reference.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


REF = _reference()


def _frames() -> list[Path]:
    files = sorted(FRAMES.glob("*.jpg"))
    extra = os.environ.get("HARNESS_GUARD_FRAMES")
    if extra:
        files += sorted(Path(extra).rglob("*.jpg"))
    return files


def _role(path: Path) -> str:
    return "wrist_l" if "wrist_l" in str(path) else "wrist_r"


@pytest.fixture(scope="module")
def dets():
    return TableDetector.from_file(CONFIG), REF.TableDetector.from_file(CONFIG)


def test_golden_guard_verdicts(dets):
    new, _ = dets
    golden = json.loads((FRAMES / "golden.json").read_text(encoding="utf-8"))
    assert len(golden) >= 20 and sum(v["guard"] is True for v in golden.values()) >= 3   # positives are in the set
    for name, want in golden.items():
        got = new.guard({want["role"]: (FRAMES / name).read_bytes()})
        assert got == (want["guard"], want["why"]), name


def test_guard_boxes_and_annotations_match_the_reference(dets):
    new, ref = dets
    files = _frames()
    assert files
    for f in files:
        jpeg, role = f.read_bytes(), _role(f)
        assert new.guard({role: jpeg}) == ref.guard({role: jpeg}), f
        assert new.guard({"scene": jpeg, role: jpeg}) == ref.guard({"scene": jpeg, role: jpeg}), f
        for overhead in (False, True):
            got = [asdict(b) for b in detect.detect(jpeg, new.cfg, overhead=overhead)]
            want = [asdict(b) for b in REF.detect(jpeg, ref.cfg, overhead=overhead)]
            assert got == want, (f, overhead)
    trio = {"scene": files[-1].read_bytes(), "wrist_l": files[0].read_bytes(), "wrist_r": files[1].read_bytes()}
    assert new.annotate_frames(trio) == ref.annotate_frames(trio)


def test_unreadable_and_non_wrist_frames_match(dets):
    new, ref = dets
    for frames in ({"wrist_r": b"not a jpeg"}, {"scene": (FRAMES / "golden.json").read_bytes()}, {}):
        assert new.guard(frames) == ref.guard(frames)


def _random_masks():
    rng = np.random.default_rng(3)
    shapes = [(1, 1), (1, 9), (9, 1), (2, 2), (3, 5), (7, 7), (40, 64), (360, 640)]
    for shape in shapes:
        for density in (0.0, 0.05, 0.3, 0.6, 0.95, 1.0):
            yield rng.random(shape) < density
    # blobs with holes and blobs touching the edges
    m = np.zeros((60, 80), bool)
    m[5:25, 5:25] = True
    m[10:15, 10:15] = False
    m[40:60, 60:80] = True
    m[0:10, 40:50] = True
    m[48:52, 68:72] = False
    yield m


def test_close_and_fill_are_scipys():
    for m in _random_masks():
        want_c = ndi.binary_closing(m, iterations=2)
        got_c = detect._close2(m)
        assert np.array_equal(got_c, want_c), m.shape
        assert np.array_equal(detect._fill_holes(want_c), ndi.binary_fill_holes(want_c)), m.shape
        assert np.array_equal(detect._fill_holes(m), ndi.binary_fill_holes(m)), m.shape


def test_in_range_is_the_int16_comparison():
    rng = random.Random(5)
    img = np.random.default_rng(5).integers(0, 256, (37, 53, 3), dtype=np.uint8)
    planes = tuple(np.ascontiguousarray(img[..., c]) for c in range(3))
    wide = img.astype(np.int16)
    edges = [-5, 0, 1, 37, 128, 200, 254, 255, 256, 300]
    for _ in range(400):
        lo = tuple(rng.choice(edges + [rng.randint(0, 255)]) for _ in range(3))
        hi = tuple(rng.choice(edges + [rng.randint(0, 255)]) for _ in range(3))
        assert np.array_equal(detect._in_range(planes, lo, hi), REF._in_range(wide, lo, hi)), (lo, hi)


def test_stitch_tile_cache_gives_the_same_grid():
    from harness import verifiers

    files = _frames()[:9]
    imgs = [(f.name, f.read_bytes()) for f in files]
    verifiers._TILES.clear()
    cold = verifiers.stitch(imgs)
    warm = verifiers.stitch(imgs)                    # every tile from the cache
    verifiers._TILES.clear()
    mixed_first = verifiers.stitch(imgs[:5])
    mixed = verifiers.stitch(imgs)                   # half cached, half new
    assert cold is not None and cold == warm == mixed and mixed_first != cold
    assert len(verifiers._TILES) <= verifiers._TILE_CACHE
