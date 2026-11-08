"""Local colour-blob detector for the relay table: golf balls, the black 8-balls, blocks and cylinders.

It runs on the laptop in a few milliseconds per frame, with no API call, so it serves two jobs the Claude monitor
is too slow or too fragile for:

1. A fast black-ball guard. Every harness poll on a guarded stage looks at the wrist cameras; a black ball between
   the fingers counts as a guard "yes" at once. This also keeps the guard answering when the Claude API is down.
2. Boxes on the frames the monitor sees, labelled with class, colour and (for cylinders) upright/lying, so the
   monitor counts balls in the bin, black balls and tower blocks from marked objects instead of guessing.

It does NOT change what Dream-1 sees or is told: the station builds the policy's camera input itself, the fine-tune
was trained on plain frames, and prompts must stay the recorded task texts word for word.

Colours live in a YAML file (plans/table_colors.yaml). Tune them on a real frame of the table:
    python -m harness.detect plans/table_colors.yaml frame.jpg --out boxed.jpg
"""

from __future__ import annotations

import argparse
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from PIL import Image, ImageDraw

try:
    from scipy import ndimage as _ndi
except ImportError:   # pragma: no cover
    _ndi = None

WORK_W = 320           # frames are shrunk to this width before detection
COLORS = {"avoid": (230, 20, 20), "target": (20, 200, 60), "info": (30, 120, 230)}


@dataclass
class ClassSpec:
    name: str
    hsv_lo: tuple[int, int, int]
    hsv_hi: tuple[int, int, int]
    shape: str = "any"             # round | box | cylinder | any
    min_frac: float = 0.0005       # blob area as a fraction of the image
    max_frac: float = 0.15
    role: str = "info"             # avoid (draw red, guard) | target | info
    label: str = ""


@dataclass
class Box:
    cls: str
    x0: float
    y0: float
    x1: float
    y1: float                      # fractions of the image, 0-1
    frac: float
    orientation: str | None = None
    role: str = "info"

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def text(self) -> str:
        return self.cls + (f" {self.orientation}" if self.orientation else "")


@dataclass
class DetectConfig:
    classes: list[ClassSpec]
    guard_class: str = "black_ball"
    wrist_roles: list[str] = field(default_factory=lambda: ["wrist"])
    grip_roi: tuple[float, float, float, float] = (0.25, 0.35, 0.75, 1.0)   # x0 y0 x1 y1 between the fingers
    grip_min_frac: float = 0.01   # a ball this big in the wrist view is in or at the fingers
    annotate: bool = True


def load_config(path: str | Path) -> DetectConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    classes = []
    for name, c in (raw.get("classes") or {}).items():
        lo, hi = c["hsv"]
        classes.append(ClassSpec(name=name, hsv_lo=tuple(lo), hsv_hi=tuple(hi), shape=c.get("shape", "any"),
                                 min_frac=float(c.get("min_frac", 0.0005)), max_frac=float(c.get("max_frac", 0.15)),
                                 role=c.get("role", "info"), label=c.get("label", name)))
    g = raw.get("guard") or {}
    return DetectConfig(classes=classes, guard_class=g.get("class", "black_ball"),
                        wrist_roles=list(g.get("wrist_roles", ["wrist"])),
                        grip_roi=tuple(g.get("grip_roi", (0.25, 0.35, 0.75, 1.0))),
                        grip_min_frac=float(g.get("grip_min_frac", 0.01)), annotate=bool(raw.get("annotate", True)))


def _load(jpeg: bytes) -> Image.Image | None:
    try:
        im = Image.open(io.BytesIO(jpeg))
        im.load()
        return im.convert("RGB")
    except Exception:   # noqa: BLE001 - a bad frame is simply not looked at
        return None


def _hsv(im: Image.Image) -> np.ndarray:
    w, h = im.size
    small = im.resize((WORK_W, max(1, round(h * WORK_W / w))))
    return np.asarray(small.convert("HSV"), dtype=np.int16)


def _label(mask: np.ndarray) -> tuple[np.ndarray, int]:
    if _ndi is not None:
        return _ndi.label(mask)
    lab = np.zeros(mask.shape, dtype=np.int32)   # small 4-connected flood fill for machines without scipy
    n = 0
    for y, x in zip(*np.nonzero(mask)):
        if lab[y, x]:
            continue
        n += 1
        stack = [(y, x)]
        lab[y, x] = n
        while stack:
            cy, cx = stack.pop()
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                if 0 <= ny < mask.shape[0] and 0 <= nx < mask.shape[1] and mask[ny, nx] and not lab[ny, nx]:
                    lab[ny, nx] = n
                    stack.append((ny, nx))
    return lab, n


def _in_range(hsv: np.ndarray, lo: tuple[int, int, int], hi: tuple[int, int, int]) -> np.ndarray:
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    hue = (h >= lo[0]) & (h <= hi[0]) if lo[0] <= hi[0] else (h >= lo[0]) | (h <= hi[0])   # red wraps around
    return hue & (s >= lo[1]) & (s <= hi[1]) & (v >= lo[2]) & (v <= hi[2])


def _shape_ok(spec: ClassSpec, w: int, h: int, fill: float) -> tuple[bool, str | None]:
    aspect = w / h if h else 99.0
    if spec.shape == "round":
        return 0.7 <= aspect <= 1.43 and 0.55 <= fill <= 0.95, None
    if spec.shape == "box":
        return 0.4 <= aspect <= 2.5 and fill >= 0.7, None
    if spec.shape == "cylinder":
        if 0.7 <= aspect <= 1.43 and fill <= 0.92:
            return True, "upright"       # round from above: standing on its end
        if (aspect > 1.6 or aspect < 0.62) and fill >= 0.6:
            return True, "lying"
        return fill >= 0.6, None
    return True, None


def detect(jpeg: bytes, cfg: DetectConfig) -> list[Box]:
    im = _load(jpeg)
    if im is None:
        return []
    hsv = _hsv(im)
    H, W = hsv.shape[:2]
    total = float(H * W)
    out: list[Box] = []
    for spec in cfg.classes:
        lab, n = _label(_in_range(hsv, spec.hsv_lo, spec.hsv_hi))
        if n == 0:
            continue
        sizes = np.bincount(lab.ravel())
        for i in range(1, n + 1):
            area = sizes[i]
            frac = area / total
            if not spec.min_frac <= frac <= spec.max_frac:
                continue
            ys, xs = np.nonzero(lab == i)
            x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
            fill = area / float((x1 - x0) * (y1 - y0))
            ok, orient = _shape_ok(spec, x1 - x0, y1 - y0, fill)
            if ok:
                out.append(Box(spec.label or spec.name, x0 / W, y0 / H, x1 / W, y1 / H, frac, orient, spec.role))
    return out


def held(boxes: list[Box], cfg: DetectConfig, cls: str | None = None) -> Box | None:
    """A box of `cls` (default the guard class) between the fingers of a wrist camera, big enough to be held."""
    cls = cls or cfg.guard_class
    cls = next((c.label or c.name for c in cfg.classes if c.name == cls), cls)   # boxes carry the label
    rx0, ry0, rx1, ry1 = cfg.grip_roi
    for b in boxes:
        if b.cls == cls and b.frac >= cfg.grip_min_frac and rx0 <= b.cx <= rx1 and ry0 <= b.cy <= ry1:
            return b
    return None


def annotate(jpeg: bytes, boxes: list[Box]) -> bytes:
    im = _load(jpeg)
    if im is None or not boxes:
        return jpeg
    d = ImageDraw.Draw(im)
    W, H = im.size
    width = max(2, W // 200)
    for b in boxes:
        col = COLORS.get(b.role, COLORS["info"])
        xy = (b.x0 * W, b.y0 * H, b.x1 * W, b.y1 * H)
        d.rectangle(xy, outline=col, width=width)
        d.text((xy[0] + 2, max(0, xy[1] - 12)), b.text().upper() if b.role == "avoid" else b.text(), fill=col)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


class TableDetector:
    """What the runner and the monitor use."""

    def __init__(self, cfg: DetectConfig) -> None:
        self.cfg = cfg

    @classmethod
    def from_file(cls, path: str | Path) -> TableDetector:
        return cls(load_config(path))

    def boxes(self, frames: dict[str, bytes]) -> dict[str, list[Box]]:
        return {role: detect(j, self.cfg) for role, j in frames.items()}

    def guard(self, frames: dict[str, bytes]) -> tuple[bool | None, str]:
        """(True, why) if a wrist camera shows the guard class between the fingers; (False, ...) if the wrist views
        were readable and showed none; (None, ...) if no wrist view could be read."""
        looked = False
        for role, jpeg in frames.items():
            if not any(w in role for w in self.cfg.wrist_roles):
                continue
            boxes = detect(jpeg, self.cfg)
            if _load(jpeg) is None:
                continue
            looked = True
            b = held(boxes, self.cfg)
            if b is not None:
                return True, f"local detector: {b.cls} between the fingers in {role} ({b.frac:.1%} of the view)"
        return (False, "local detector: none held") if looked else (None, "local detector: no wrist view")

    def annotate_frames(self, frames: dict[str, bytes]) -> dict[str, bytes]:
        if not self.cfg.annotate:
            return frames
        return {role: annotate(j, detect(j, self.cfg)) for role, j in frames.items()}

    def legend(self) -> str:
        avoid = [c.label or c.name for c in self.cfg.classes if c.role == "avoid"]
        return ("Boxes on the images come from a local colour detector and can miss or mislabel objects: "
                f"RED boxes = {', '.join(avoid) or 'objects to avoid'} (never pick these); green = targets; "
                "blue = other objects (blocks: colour, cylinders: upright or lying).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the table detector on one image and draw the boxes.")
    ap.add_argument("config")
    ap.add_argument("image")
    ap.add_argument("--out", default="boxed.jpg")
    args = ap.parse_args(argv)
    det = TableDetector.from_file(args.config)
    jpeg = Path(args.image).read_bytes()
    boxes = detect(jpeg, det.cfg)
    for b in boxes:
        print(f"{b.text():24s} x {b.x0:.2f}-{b.x1:.2f} y {b.y0:.2f}-{b.y1:.2f} area {b.frac:.2%}")
    Path(args.out).write_bytes(annotate(jpeg, boxes))
    print(f"{len(boxes)} boxes -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
