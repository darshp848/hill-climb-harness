"""harness.cable_end: ferrule candidates and the USB-C / USB-A classifier."""

from pathlib import Path

import cv2
import numpy as np
import pytest

from harness.cable_end import CableEndClassifier, crop, ferrules

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "models" / "cable_end.onnx"
DATA = Path(__file__).parent / "data"
ORANGE = (0, 70, 235)   # BGR, the anodised ferrule


def _table():
    return np.full((360, 640, 3), (200, 205, 208), np.uint8)


def test_a_ferrule_is_a_candidate_and_a_cable_run_is_not():
    im = _table()
    cv2.rectangle(im, (300, 160), (340, 182), ORANGE, -1)        # ferrule: compact, filled
    cv2.line(im, (20, 300), (600, 330), ORANGE, 4)               # cable: long and thin
    c = ferrules(im)
    assert len(c) == 1 and abs(c[0]["cx"] - 320) < 3 and abs(c[0]["cy"] - 171) < 3
    assert c[0]["side"] > 40 * 2                                  # the crop keeps the tip and the relief in view
    assert crop(im, c[0]).shape == (96, 96, 3)


def test_no_orange_no_candidates():
    assert ferrules(_table()) == []


@pytest.mark.skipif(not MODEL.exists(), reason="no trained model")
def test_photo_ends_are_labelled():
    clf = CableEndClassifier(MODEL, min_prob=0.5)
    for name, want in (("cable_usbc_end.jpg", "usb_c"), ("cable_usba_end.jpg", "usb_a")):
        ends = clf.ends_jpeg((DATA / name).read_bytes())
        assert ends, name
        best = max(ends, key=lambda e: e.prob)
        assert best.label == want, (name, [(e.label, round(e.prob, 2)) for e in ends])


@pytest.mark.skipif(not MODEL.exists(), reason="no trained model")
def test_batches_of_any_size_in_any_order():
    # OpenCV 5.0 DNN crashed the process when a net was reused with a bigger batch than before (2, 5, 4, 4, 16)
    clf = CableEndClassifier(MODEL)
    rng = np.random.default_rng(0)
    one = rng.random((1, 3, 96, 96)).astype(np.float32)
    ref = clf._forward(one)
    for b in (2, 5, 4, 4, 16, 3, 24, 1, 9, 17):
        x = rng.random((b, 3, 96, 96)).astype(np.float32)
        x[0] = one[0]
        out = clf._forward(x)
        assert out.shape == (b, 3)
        assert np.abs(out[0] - ref[0]).max() < 1e-4


@pytest.mark.skipif(not MODEL.exists(), reason="no trained model")
def test_plain_table_has_no_ends():
    clf = CableEndClassifier(MODEL)
    assert clf.ends(_table()) == []
