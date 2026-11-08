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

SMALL_BALL_FRAC = 0.004  # a golf ball in the overhead view is ~0.05-0.1% of the image
# Measured on the station's overhead camera (8 Nov, 14 hand-labelled frames): the farthest golf ball covers ~75 px
# at WORK_W and the nearest ~0.2% of the image. A small, half-lit ball reads as a crescent (elongation up to 2.0,
# roundness down to 0.30); a cube's pale rim is a thin ring (roundness <= 0.15).
BALL_MIN_PX = 50
BALL_MAX_ELONG = 2.1
BALL_MIN_ROUND = 0.28
# Overhead: two touching cubes merge into a blob ~21 px wide at WORK_W; a lying stick is ~9 px wide, and shading
# can put a dip in its ridge, so only blobs at least this wide are split.
SPLIT_MIN_WIDTH_PX = 14.0
# Overhead: a ball-only colour blob bigger than this is the white bin (or a ball touching it), never a ball itself.
# A ball pressed against the bin merges with it and is missed; looking for round lobes on the bin's edge also fired
# on the corners of a bin seen at an angle, so it is not done.
OVERHEAD_BALL_MAX_FRAC = 0.006
WORK_W = 640           # frames are shrunk to this width before detection
COLORS = {"avoid": (230, 20, 20), "target": (20, 200, 60), "info": (30, 120, 230)}
# Where a held ball sits in a wrist view, measured on the station's right wrist camera (plans/table_colors.yaml says
# how): its centre between the fingertips, and at least this share of the view. A ball lying past the fingertips is
# smaller and higher in the image, so it does not trip the guard.
GRIP_ROI = (0.33, 0.6, 0.67, 1.0)
GRIP_MIN_FRAC = 0.015


@dataclass
class ClassSpec:
    name: str
    hsv_lo: tuple[int, int, int]
    hsv_hi: tuple[int, int, int]
    min_frac: float = 0.0005       # blob area as a fraction of the image
    max_frac: float = 0.15
    label: str = ""                # colour name, e.g. "blue"
    # which shapes of this colour to report, and how: kind (ball | cube | stick) -> role (avoid | target | info).
    # Empty = every kind, role info. Black lists only ball, so the black gripper fingers are never reported.
    kinds: dict[str, str] = field(default_factory=dict)
    small_ball: bool = False       # read a small compact blob of this colour as a ball (not a cube): the colour has
                                   # no cubes on the table (it may have sticks; one standing on a cube reads upright)
    # classes whose objects this colour also shows up on: a blob of this colour centred inside one of their boxes is
    # that object's highlight or rim, not an object of its own (white: the pale face of a blue cube)
    not_inside: tuple[str, ...] = ()


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
    hole_axis: str | None = None   # cubes: vertical | horizontal-x | horizontal-y | horizontal | unknown
    confidence: float = 0.0        # of hole_axis
    spec: str = ""                 # the colour class (ClassSpec.name) it came from

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def text(self) -> str:
        hole = f" hole {self.hole_axis}" if self.hole_axis not in (None, "unknown") and self.confidence >= HOLE_SURE else ""
        return self.cls + (f" {self.orientation}" if self.orientation else "") + hole

    def info(self) -> dict[str, Any]:
        """One object: {shape, colour, hole_axis, orientation, confidence}."""
        colour, _, shape = self.cls.partition(" ")
        return {"shape": shape, "colour": colour, "hole_axis": self.hole_axis, "orientation": self.orientation,
                "confidence": round(self.confidence, 2)}


@dataclass
class DetectConfig:
    classes: list[ClassSpec]
    guard_class: str = "black_ball"
    wrist_roles: list[str] = field(default_factory=lambda: ["wrist"])
    grip_roi: tuple[float, float, float, float] = GRIP_ROI   # x0 y0 x1 y1: where a held ball's centre sits
    grip_min_frac: float = GRIP_MIN_FRAC   # a ball this big in the wrist view is in or at the fingers
    table_roi: tuple[float, float, float, float] | None = None   # overhead view: ignore blobs outside (people, chairs)
    annotate: bool = True


def load_config(path: str | Path) -> DetectConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    classes = []
    for name, c in (raw.get("classes") or {}).items():
        lo, hi = c["hsv"]
        classes.append(ClassSpec(name=name, hsv_lo=tuple(lo), hsv_hi=tuple(hi),
                                 min_frac=float(c.get("min_frac", 0.0005)), max_frac=float(c.get("max_frac", 0.15)),
                                 label=c.get("label", name), kinds=dict(c.get("kinds") or {}),
                                 small_ball=bool(c.get("small_ball", False)),
                                 not_inside=tuple(c.get("not_inside") or ())))
    g = raw.get("guard") or {}
    return DetectConfig(classes=classes, guard_class=g.get("class", "black_ball"),
                        wrist_roles=list(g.get("wrist_roles", ["wrist"])),
                        grip_roi=tuple(g.get("grip_roi", GRIP_ROI)),
                        grip_min_frac=float(g.get("grip_min_frac", GRIP_MIN_FRAC)), annotate=bool(raw.get("annotate", True)),
                        table_roi=tuple(raw["table_roi"]) if raw.get("table_roi") else None)


def _load(jpeg: bytes, min_w: int | None = None) -> Image.Image | None:
    """Decoded RGB image, or None for a bad frame. With min_w, a JPEG is decoded at the smallest 1/2, 1/4 or 1/8
    scale that is still at least min_w wide (libjpeg's draft mode): a 1280 px station frame decodes at 640."""
    try:
        im = Image.open(io.BytesIO(jpeg))
        if min_w and im.format == "JPEG" and im.width > min_w:
            im.draft("RGB", (min_w, max(1, im.height * min_w // im.width)))
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


def _shape(ys: np.ndarray, xs: np.ndarray) -> tuple[float, float]:
    """(elongation, roundness) of one blob: sqrt of the covariance eigenvalue ratio, and its area over the area of
    the circle around it (disc ~1, square ~0.64, thin bar small)."""
    pts = np.stack([xs, ys], axis=1).astype(float)
    c = pts.mean(axis=0)
    ev = np.linalg.eigvalsh(np.cov((pts - c).T)) if len(xs) > 2 else np.array([1.0, 1.0])
    elong = float(np.sqrt(max(ev[1], 1e-9) / max(ev[0], 1e-9)))
    rmax = float(np.sqrt(((pts - c) ** 2).sum(axis=1).max())) + 0.5
    return elong, len(xs) / (np.pi * rmax * rmax)


def _width(blob: np.ndarray) -> float:
    """A blob's width across its long axis, px (sqrt(12) x the minor standard deviation)."""
    ys, xs = np.nonzero(blob)
    if len(xs) < 3:
        return 0.0
    ev = np.linalg.eigvalsh(np.cov(np.stack([xs, ys]).astype(float)))
    return float(np.sqrt(12 * max(ev[0], 0.0)))


def _peaks(d: np.ndarray, lo: float, hi: float, size: int) -> list[tuple[int, int]]:
    """Local maxima of a distance map with lo <= d <= hi, one point per plateau, highest first."""
    peak = (d == _ndi.maximum_filter(d, size=size)) & (d >= lo) & (d <= hi)
    lab, n = _ndi.label(peak, structure=np.ones((3, 3)))
    pts = [tuple(int(v) for v in np.unravel_index(np.argmax(np.where(lab == i, d, -1)), d.shape))
           for i in range(1, n + 1)]
    return sorted(pts, key=lambda p: -d[p])


def _split(blob: np.ndarray) -> list[np.ndarray]:
    """Touching objects of one colour (two blue cubes side by side read as one long blob, a "stick") as separate
    blobs: peaks of the distance to the blob's edge with a clear dip between them. A lying stick has a flat ridge
    along its length, with no dip, so it stays whole."""
    if _ndi is None:
        return [blob]
    ys, xs = np.nonzero(blob)
    y0, x0 = ys.min(), xs.min()
    b = blob[y0:ys.max() + 1, x0:xs.max() + 1]
    d = _ndi.distance_transform_edt(np.pad(b, 1))[1:-1, 1:-1]
    if d.max() < 3:
        return [blob]
    t = np.linspace(0, 1, 24)
    keep: list[tuple[int, int]] = []
    for p in _peaks(d, 0.55 * d.max(), np.inf, 3):
        def dip(q, p=p):
            line = d[np.round(p[0] + t * (q[0] - p[0])).astype(int), np.round(p[1] + t * (q[1] - p[1])).astype(int)]
            return line.min() < 0.7 * min(d[p], d[q])
        if all(dip(q) for q in keep):
            keep.append(p)
    if len(keep) < 2:
        return [blob]
    by, bx = np.nonzero(b)
    near = np.argmin([(by - py) ** 2 + (bx - px) ** 2 for py, px in keep], axis=0)
    out = []
    for k in range(len(keep)):
        m = np.zeros_like(blob)
        m[by[near == k] + y0, bx[near == k] + x0] = True
        out.append(m)
    return out


def _kind(ys: np.ndarray, xs: np.ndarray) -> tuple[str | None, str | None]:
    """(kind, orientation) of one blob from its pixels, independent of rotation.
    ball: compact and fills its circumscribed circle; cube: compact but square (or an upright stick seen from above);
    stick: long and thin, i.e. lying on its side."""
    n = len(xs)
    if n < 6:
        return None, None
    pts = np.stack([xs, ys], axis=1).astype(float)
    c = pts.mean(axis=0)
    ev = np.linalg.eigvalsh(np.cov((pts - c).T)) if n > 2 else np.array([1.0, 1.0])
    elong = float(np.sqrt(max(ev[1], 1e-9) / max(ev[0], 1e-9)))
    rmax = float(np.sqrt(((pts - c) ** 2).sum(axis=1).max())) + 0.5
    roundness = n / (np.pi * rmax * rmax)          # disc ~1, square ~0.64, thin bar small
    if elong >= 2.2:
        return "stick", "lying"
    if elong <= 1.5 and roundness >= 0.72:
        return "ball", None
    if elong <= 1.6:
        return "cube", None
    return None, None


def _dilate(m: np.ndarray) -> np.ndarray:
    """One step of 4-neighbour dilation; scipy is optional in this module, so these three use numpy only."""
    out = m.copy()
    out[1:] |= m[:-1]
    out[:-1] |= m[1:]
    out[:, 1:] |= m[:, :-1]
    out[:, :-1] |= m[:, 1:]
    return out


def _erode(m: np.ndarray) -> np.ndarray:
    return ~_dilate(~np.pad(m, 1))[1:-1, 1:-1]


def _fill_holes(m: np.ndarray) -> np.ndarray:
    """m plus every background region that doesn't reach its border."""
    lab, _ = _label(~np.pad(m, 1))
    return (lab != lab[0, 0])[1:-1, 1:-1]


HOLE_SURE = 0.6     # at or above this a hole was really seen on the overhead view; only then is it shown or used
_HOLE_DARK = 0.62   # a hole is at least this much darker than the cube's median brightness


def _hole_axis(raw: np.ndarray, blob: np.ndarray, value: np.ndarray) -> tuple[str, float]:
    """Which way a cube's round hole runs, from one view that sees the cube's top: (axis, confidence).

    The hole is a roughly round patch inside the blob that is not the cube's colour, or is much darker than it.
    vertical = hole on the top face (near the blob's centre or upper half). horizontal-y = on the face toward the
    camera (lower half). horizontal-x = on a side face (off to one side and narrow). horizontal = no hole seen, so it
    runs sideways through a face this view can't see. Foam shadow and small cubes make this a guess: use the
    confidence, and don't gate a stage on it without checking it on the station's cameras."""
    ys, xs = np.nonzero(blob)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    b, r, v = _fill_holes(blob[y0:y1, x0:x1]), raw[y0:y1, x0:x1], value[y0:y1, x0:x1]
    body = b & r
    if body.sum() < 40:
        return "unknown", 0.0
    H, W = b.shape
    cand = _erode(b) & (~r | (v < _HOLE_DARK * float(np.median(v[body]))))
    lab, n = _label(cand)
    best, area = None, 0
    for i in range(1, n + 1):
        comp = lab == i
        size = int(comp.sum())
        if size > area and not (_dilate(comp) & ~b).any():
            best, area = comp, size
    frac = area / float(b.sum())
    if best is None or frac < 0.02:
        return "horizontal", 0.5
    hy, hx = np.nonzero(best)
    w, h = int(hx.max() - hx.min() + 1), int(hy.max() - hy.min() + 1)
    fill = area / (np.pi / 4 * w * h)
    if frac > 0.40 or min(w, h) < 3 or not 0.55 <= fill <= 1.25:
        return "unknown", 0.3
    dx, dy = (hx.mean() - (W - 1) / 2) / W, (hy.mean() - (H - 1) / 2) / H
    conf = 0.85 if frac >= 0.05 and 0.7 <= fill <= 1.1 else 0.6
    if abs(dx) > 0.15 and w < 0.85 * h:
        return "horizontal-x", conf
    return ("horizontal-y", conf) if dy > 0.1 else ("vertical", conf)


def _mark_upright(boxes: list[Box], ratio: float) -> None:
    """A tall thin stick standing on a cube in the image is upright. A stick lying toward the camera can look the
    same, so this only applies on top of a cube. `ratio` is the frame's height / width."""
    cubes = [b for b in boxes if b.cls.endswith(" cube")]
    for s in boxes:
        if not (s.cls.endswith(" stick") and s.orientation == "lying"):
            continue
        tall = (s.y1 - s.y0) * ratio > 1.6 * (s.x1 - s.x0)
        if tall and any(abs(c.y0 - s.y1) < 0.03 and c.x0 <= s.cx <= c.x1 for c in cubes):
            s.orientation = "upright"


def _top_cube(boxes: list[Box]) -> Box | None:
    """The highest cube in the image that sits on another cube; else the highest cube."""
    cubes = sorted((b for b in boxes if b.cls.endswith(" cube")), key=lambda b: b.y0)
    stacked = [c for c in cubes if any(o is not c and abs(o.y0 - c.y1) < 0.04 and o.x0 <= c.cx <= o.x1 for o in cubes)]
    return (stacked or cubes or [None])[0]


def top_cube_hole(boxes: list[Box]) -> tuple[str | None, float]:
    """(hole axis, confidence) of the top cube, for the note 'last cube hole vertical before inserting the cylinder'.
    Trust "vertical" only at confidence >= HOLE_SURE from the overhead camera. It informs; it does not gate anything."""
    c = _top_cube(boxes)
    return (None, 0.0) if c is None else (c.hole_axis, c.confidence)


def detect(jpeg: bytes, cfg: DetectConfig, *, overhead: bool = False) -> list[Box]:
    im = _load(jpeg, WORK_W)
    return [] if im is None else detect_image(im, cfg, overhead=overhead)


def detect_image(im: Image.Image, cfg: DetectConfig, *, overhead: bool = False,
                 classes: list[ClassSpec] | None = None) -> list[Box]:
    """detect() on a decoded image; `classes` limits the colours looked for (default: all of cfg's)."""
    hsv = _hsv(im)
    H, W = hsv.shape[:2]
    total = float(H * W)
    out: list[Box] = []
    wanted = list(cfg.classes if classes is None else classes)
    names = {s.name for s in wanted}
    # classes named in a wanted class's not_inside are looked for too, only to filter by (then dropped)
    helpers = [s for s in cfg.classes if s.name not in names and any(s.name in w.not_inside for w in wanted)]
    for spec in wanted + helpers:
        raw = mask = _in_range(hsv, spec.hsv_lo, spec.hsv_hi)
        if _ndi is not None:   # close specular highlights and the cubes' holes, so shapes read whole
            mask = _ndi.binary_fill_holes(_ndi.binary_closing(mask, iterations=2))
        lab, n = _label(mask)
        if n == 0:
            continue
        sizes = np.bincount(lab.ravel())
        pieces: list[np.ndarray] = []
        for i in range(1, n + 1):
            frac = sizes[i] / total
            if not spec.min_frac <= frac <= spec.max_frac:
                continue
            blob = lab == i
            if spec.small_ball and overhead and frac > OVERHEAD_BALL_MAX_FRAC:
                continue   # the white bin (with any ball touching it): never one ball
            split = _split(blob) if overhead and "cube" in spec.kinds and _width(blob) >= SPLIT_MIN_WIDTH_PX else [blob]
            pieces += [(p, len(split) > 1) for p in split]
        for blob, cut in pieces:
            ys, xs = np.nonzero(blob)
            frac = len(xs) / total
            if frac < spec.min_frac:
                continue
            kind, orient = _kind(ys, xs)
            if "cube" in spec.kinds and "ball" not in spec.kinds and (
                    kind == "ball" or (cut and kind is None and _shape(ys, xs)[0] <= 2.0)):
                kind, orient = "cube", None   # no balls of this colour: a cube seen corner-on, or one cut from a
                                              # neighbour by _split, reads rounder than a square
            if spec.small_ball and frac <= SMALL_BALL_FRAC and kind != "ball" and len(xs) >= BALL_MIN_PX:
                # a colour with no cubes: a small, half-lit ball reads square-ish or as a crescent
                # (the size cap keeps the white bin out; a held ball fills the wrist view and is round)
                if kind == "cube" or kind is None:
                    elong, roundness = _shape(ys, xs)
                    if kind == "cube" or (elong <= BALL_MAX_ELONG and roundness >= BALL_MIN_ROUND):
                        kind, orient = "ball", None
            if kind is None or (spec.kinds and kind not in spec.kinds):
                continue
            role = spec.kinds.get(kind, "info")
            label = f"{spec.label or spec.name} {kind}"
            axis, conf = _hole_axis(raw, blob, hsv[..., 2]) if kind == "cube" else (None, 0.0)
            conf = conf if overhead else min(conf, 0.4)   # away from overhead, a centred hole can be on any face
            out.append(Box(label, xs.min() / W, ys.min() / H, (xs.max() + 1) / W, (ys.max() + 1) / H, frac,
                           orient, role, axis, conf, spec.name))
    by_name = {s.name: s for s in wanted}
    out = [b for b in out
           if b.spec in by_name and not any(o.spec in by_name[b.spec].not_inside and o.x0 <= b.cx <= o.x1
                                            and o.y0 <= b.cy <= o.y1 for o in out)]
    for b in out:   # a compact blob of a colour that also comes as sticks, on a cube: a stick standing in its hole
        spec = by_name[b.spec]
        if b.cls.endswith(" ball") and "stick" in spec.kinds and any(
                c.cls.endswith(" cube") and c.x0 <= b.cx <= c.x1 and c.y0 <= b.cy <= c.y1 for c in out):
            b.cls, b.orientation, b.role = f"{spec.label or spec.name} stick", "upright", spec.kinds["stick"]
    _mark_upright(out, H / W)
    if overhead and cfg.table_roi:
        x0, y0, x1, y1 = cfg.table_roi
        out = [b for b in out if x0 <= b.cx <= x1 and y0 <= b.cy <= y1]
    return out


def held(boxes: list[Box], cfg: DetectConfig) -> Box | None:
    """A ball the plan must never bin (role avoid) between the fingers of a wrist camera, big enough to be held."""
    rx0, ry0, rx1, ry1 = cfg.grip_roi
    for b in boxes:
        if b.role == "avoid" and b.frac >= cfg.grip_min_frac and rx0 <= b.cx <= rx1 and ry0 <= b.cy <= ry1:
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
        self.cable_ends = None   # harness.cable_end.CableEndClassifier: USB-C / USB-A end boxes (--cable-ends)

    @classmethod
    def from_file(cls, path: str | Path) -> TableDetector:
        return cls(load_config(path))

    def _overhead(self, role: str) -> bool:
        return not any(w in role for w in self.cfg.wrist_roles)

    def boxes(self, frames: dict[str, bytes]) -> dict[str, list[Box]]:
        return {role: detect(j, self.cfg, overhead=self._overhead(role)) for role, j in frames.items()}

    def guard(self, frames: dict[str, bytes]) -> tuple[bool | None, str]:
        """(True, why) if a wrist camera shows the guard class between the fingers; (False, ...) if the wrist views
        were readable and showed none; (None, ...) if no wrist view could be read."""
        looked = False
        avoid = [c for c in self.cfg.classes if "avoid" in c.kinds.values()]   # only the colours that can trip it
        for role, jpeg in frames.items():
            if not any(w in role for w in self.cfg.wrist_roles):
                continue
            im = _load(jpeg, WORK_W)
            if im is None:
                continue
            looked = True
            b = held(detect_image(im, self.cfg, classes=avoid), self.cfg)
            if b is not None:
                return True, f"local detector: {b.cls} between the fingers in {role} ({b.frac:.1%} of the view)"
        return (False, "local detector: none held") if looked else (None, "local detector: no wrist view")

    def annotate_frames(self, frames: dict[str, bytes]) -> dict[str, bytes]:
        if not self.cfg.annotate:
            return frames
        out = {role: annotate(j, detect(j, self.cfg, overhead=self._overhead(role))) for role, j in frames.items()}
        if self.cable_ends is not None:
            out = {role: self.cable_ends.annotate(j) for role, j in out.items()}
        return out

    def legend(self) -> str:
        avoid = [f"{c.label or c.name} {k}" for c in self.cfg.classes for k, r in c.kinds.items() if r == "avoid"]
        return ("Boxes on the images come from a local colour detector and can miss or mislabel objects: "
                f"RED boxes = {', '.join(avoid) or 'objects to avoid'} (never pick these); green = targets; "
                "blue = other objects (blocks: colour and which way the hole runs, a guess; cylinders: upright or lying)."
                + (" " + self.cable_ends.legend() if self.cable_ends is not None else ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the table detector on one image and draw the boxes.")
    ap.add_argument("config")
    ap.add_argument("image")
    ap.add_argument("--out", default="boxed.jpg")
    ap.add_argument("--wrist", action="store_true", help="the image is a wrist view (no table_roi)")
    args = ap.parse_args(argv)
    det = TableDetector.from_file(args.config)
    jpeg = Path(args.image).read_bytes()
    boxes = detect(jpeg, det.cfg, overhead=not args.wrist)
    for b in boxes:
        print(f"{b.text():24s} x {b.x0:.2f}-{b.x1:.2f} y {b.y0:.2f}-{b.y1:.2f} area {b.frac:.2%}")
    Path(args.out).write_bytes(annotate(jpeg, boxes))
    print(f"{len(boxes)} boxes -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
