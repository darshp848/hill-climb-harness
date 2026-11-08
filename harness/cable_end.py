"""Which end of the orange cable is where: USB-C end, USB-A end, or neither, from any camera frame.

Two steps, numpy + OpenCV only (no torch at runtime):
1. Candidates: compact orange blobs, i.e. the anodised ferrules (the cable is a long thin run, the strain-relief
   ribs are slivers). Skin, faces and the orange golf ball also pass; step 2 rejects them.
2. A small CNN (MobileNetV3-small, 96 x 96 crop, ~6 MB ONNX run by cv2.dnn) labels each crop usb_c / usb_a / none.
   Trained on Blender renders of this cable (tools/cable_end/render_cable.py: random pose, lighting, lens,
   backgrounds from station frames) plus hand-labelled station crops (tools/cable_end/train.py).

The station's cables are USB-A to USB-C; the USB-A end is the one that goes into the power strip's USB ports.
Used to draw "USB-C end" / "USB-A end" boxes on the frames the monitor sees, like harness.detect's boxes. It never
touches Dream's inputs or the prompts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

LABELS = ("usb_c", "usb_a", "none")
NAMES = {"usb_c": "USB-C end", "usb_a": "USB-A end"}
WORK_W = 640
SIZE = 96
BATCH = 8                 # fixed DNN input batch (see CableEndClassifier._forward)
CROP_SCALE = 2.6
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)


@dataclass
class End:
    label: str            # usb_c | usb_a
    prob: float
    x0: float
    y0: float
    x1: float
    y1: float             # fractions of the image


def ferrules(bgr: np.ndarray, work_w: int = WORK_W) -> list[dict]:
    """Candidate ferrules: [{cx, cy, side}] in the input image's pixels (side = crop side)."""
    h0, w0 = bgr.shape[:2]
    s = work_w / w0
    im = cv2.resize(bgr, (work_w, max(1, round(h0 * s))), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    mask = ((H >= 3) & (H <= 22) & (S >= 110) & (V >= 60)).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask)
    total = im.shape[0] * im.shape[1]
    out = []
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < 30 or area > 0.08 * total:
            continue
        ys, xs = np.nonzero(lab == i)
        (cx, cy), (w, h), _ = cv2.minAreaRect(np.stack([xs, ys], 1).astype(np.float32))
        L, W = max(w, h), max(1.0, min(w, h))
        if L / W > 4.0 or area / max(L * W, 1.0) < 0.5:
            continue
        out.append({"cx": cx / s, "cy": cy / s, "side": max(L * CROP_SCALE, 24) / s})
    return out


def crop(bgr: np.ndarray, cand: dict, size: int = SIZE) -> np.ndarray:
    side = cand["side"]
    x0, y0 = cand["cx"] - side / 2, cand["cy"] - side / 2
    M = np.float32([[size / side, 0, -x0 * size / side], [0, size / side, -y0 * size / side]])
    return cv2.warpAffine(bgr, M, (size, size), flags=cv2.INTER_AREA, borderMode=cv2.BORDER_REPLICATE)


def _prep(crops: list[np.ndarray]) -> np.ndarray:
    x = np.stack([c[:, :, ::-1].astype(np.float32) / 255.0 for c in crops])
    return ((x - MEAN) / STD).transpose(0, 3, 1, 2).copy()


class CableEndClassifier:
    def __init__(self, model_path: str | Path, min_prob: float = 0.7, max_candidates: int = 24):
        self.net = cv2.dnn.readNetFromONNX(str(model_path))
        self.min_prob = min_prob
        self.max_candidates = max_candidates

    def _forward(self, x: np.ndarray) -> np.ndarray:
        """Always the same input shape, padded: OpenCV 5.0's DNN kills the process when one net is reused with a
        batch larger than an earlier one (2, 5, 4, 4, then 16 crashed it, 7 Nov). Chunks of BATCH cost ~2.5 ms."""
        out = []
        for k in range(0, len(x), BATCH):
            chunk = x[k:k + BATCH]
            pad = np.zeros((BATCH,) + x.shape[1:], np.float32)
            pad[:len(chunk)] = chunk
            self.net.setInput(pad)
            out.append(self.net.forward()[:len(chunk)].copy())
        return np.concatenate(out)

    def ends(self, bgr: np.ndarray) -> list[End]:
        cands = ferrules(bgr)[: self.max_candidates]
        if not cands:
            return []
        logits = self._forward(_prep([crop(bgr, c) for c in cands]))
        e = np.exp(logits - logits.max(1, keepdims=True))
        probs = e / e.sum(1, keepdims=True)
        h, w = bgr.shape[:2]
        out = []
        for c, p in zip(cands, probs):
            k = int(p.argmax())
            if LABELS[k] == "none" or p[k] < self.min_prob:
                continue
            half = c["side"] / 2.6 * 0.75     # box around the ferrule and tip, not the whole crop
            out.append(End(LABELS[k], float(p[k]), max(0, c["cx"] - half) / w, max(0, c["cy"] - half) / h,
                           min(w, c["cx"] + half) / w, min(h, c["cy"] + half) / h))
        return out

    def ends_jpeg(self, jpeg: bytes) -> list[End]:
        bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        return [] if bgr is None else self.ends(bgr)

    def annotate(self, jpeg: bytes) -> bytes:
        bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            return jpeg
        found = self.ends(bgr)
        if not found:
            return jpeg
        h, w = bgr.shape[:2]
        th = max(2, w // 300)
        for e in found:
            col = (255, 0, 255) if e.label == "usb_c" else (0, 200, 255)   # magenta = USB-C, amber = USB-A
            p0, p1 = (int(e.x0 * w), int(e.y0 * h)), (int(e.x1 * w), int(e.y1 * h))
            cv2.rectangle(bgr, p0, p1, col, th)
            cv2.putText(bgr, f"{NAMES[e.label]} {e.prob:.2f}", (p0[0], max(12, p0[1] - 4)), 0,
                        max(0.4, w / 1600), col, max(1, th // 2))
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 88])
        return buf.tobytes() if ok else jpeg

    @staticmethod
    def legend() -> str:
        return ("MAGENTA boxes = USB-C end of a cable, AMBER boxes = USB-A end (the end that plugs into the power "
                "strip's USB ports); from a local classifier, can miss or mislabel.")


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="label cable ends in images")
    ap.add_argument("model")
    ap.add_argument("images", nargs="+")
    ap.add_argument("--out-dir", default=None)
    a = ap.parse_args(argv)
    clf = CableEndClassifier(a.model)
    for p in a.images:
        data = Path(p).read_bytes()
        for e in clf.ends_jpeg(data):
            print(f"{p}: {NAMES[e.label]} p={e.prob:.2f} box=({e.x0:.2f},{e.y0:.2f})-({e.x1:.2f},{e.y1:.2f})")
        if a.out_dir:
            Path(a.out_dir).mkdir(parents=True, exist_ok=True)
            (Path(a.out_dir) / (Path(p).stem + "_ends.jpg")).write_bytes(clf.annotate(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
