"""Hill Climb relay table for Dream's YAM MuJoCo sim, modelled on the real station, plus a rubric v4 referee.

patch(yam_env) wraps yam_env.build_xml(): the fruit, bowl, plate and bottle are removed and the relay objects added,
keeping Dream's calibrated arms, cameras and lights. Every object follows the real table (Darsh's photos, Drive
IMG_2680/2698/2702-2704, 7 Nov) and its parts' published sizes; positions are a plausible layout, not a measured one.
The scale reference is the power strip's 25.7 cm length (a golf ball measures 4.3 cm against it, as it should):

  Power strip  ALESTOR 12-outlet surge protector with 4 USB ports (Amazon B08P5LRY37: 10.1 x 3.8 x 1.2 in,
               "4 USB ports (1 USB C)"): black body, two raised outlet panels of 6 outlets, red rocker switch,
               Protected/Grounded lights and the black power cord at one end, the USB ports in a row on the top of
               the end nearest the robots: USB-C first from the robot's left, then three USB-A. Ports are real slots:
               plugs go in vertically.
  Cables       orange USB-A to USB-C cables (~20 cm): silver USB-A tongue, orange metal shells, black ribbed strain
               reliefs, an orange cord as a chain of ball-jointed segments. Two start plugged into USB-A ports (their
               USB-A end), two lie loose on the table.
  Blocks       foam, 6 cm cubes with a through hole (2.8 cm, fits both sticks): blue, magenta, blue (the target tower
               order), a yellow 2 x 2 x 10 cm square stick and a magenta 2.5 x 11 cm round stick.
  Balls        golf balls (4.27 cm): green, yellow and orange are legal; the WHITE golf ball is illegal (any eight_*
               in the bin loses the match). The photos show no black 8-ball; SIM_BLACK_8BALLS=N adds N (rubric v4).
  Basket       white plastic slotted basket, ~26 x 18 x 9 cm.
  Table        light grey-white wood laminate.

The arms are Dream's YAM arms, not the station's MakerMods Metal arms: Dream's sim ships only YAM.

World frame: table top at z = 0.76, arm bases at x = +-0.305 (left arm at +x), y = 0.24; objects sit in front of the
arms at y < 0.2. The strip and cables are in front of the RIGHT arm and the basket by the left one (with them in front
of the left arm, Dream-1 reached across the blocks and balls: 3 DQs in 3 sim matches). HillClimbReferee reads contacts
and poses each physics step and reports the score, the pass bars and any DQ (a gripper touching a later stage's
object before the current pass bar).
"""

from __future__ import annotations

import math
import os
import xml.etree.ElementTree as ET
from typing import Any

TABLE_Z = 0.76
INCH = 0.0254
TOWER_BAR_M = 4 * INCH
FREE_STANDING_S = 3.0

# ---- power strip (ALESTOR 12-outlet, 4 USB): 25.7 x 9.7 x 3.0 cm, long axis along y, USB end toward the robots.
STRIP_HALF = (0.0485, 0.1285, 0.015)
STRIP_CENTER = (-0.14, -0.07)
HUB_HALF = STRIP_HALF                       # the referee's name for the port block: its top is the strip's top
HUB_POS = (STRIP_CENTER[0], STRIP_CENTER[1] + 0.105)   # centre of the USB port row
PORT_X = (0.030, 0.010, -0.010, -0.030)     # port 0 is the robot's left (+x): the USB-C port
PORT_KIND = ("c", "a", "a", "a")
# Slots run lengthwise along the strip (photo IMG_2703): the long side of each opening is along y.
SLOT_HALF = (0.00325, 0.007)                # USB-A opening with clearance (x, y)
SLOT_HALF_C = (0.0016, 0.0045)              # USB-C opening with clearance (x, y)
SLOT_DEPTH = 0.012
PLUGGED_PORTS = (1, 3)                      # two cables start in USB-A ports, as in the photos
LOOSE_PLUGS = ((-0.25, -0.02), (-0.29, -0.06))

# ---- cables
SEGMENTS, SEG_LEN, CORD_R = 7, 0.02, 0.0021
ORANGE, BLACK, SILVER = ".95 .42 .08 1", ".05 .05 .055 1", ".78 .78 .8 1"

# ---- blocks
CUBE_HALF = 0.03
HOLE_HALF = 0.014
CUBES = ((0.02, -0.02), (-0.04, -0.09), (0.07, -0.11))
CUBE_RGBA = (".2 .52 .86 1", ".7 .22 .66 1", ".2 .52 .86 1")      # blue, magenta, blue
STICKS = (("box", (0.01, 0.01, 0.05), (0.02, -0.19), ".95 .85 .15 1"),        # yellow square stick
          ("cylinder", (0.0125, 0.055), (-0.06, -0.25), ".7 .22 .66 1"))       # magenta round stick
CYL_R, CYL_HALF = 0.0125, 0.055            # kept for callers that size the round stick

# ---- basket
BIN_POS = (0.28, 0.06)
BIN_HALF = (0.13, 0.09, 0.045)             # inner x, y half sizes and wall half height

# ---- balls
GOLF_R, EIGHT_R = 0.02135, 0.028575
GOLF = ((0.15, -0.08), (0.21, -0.15), (0.27, -0.22))
GOLF_RGBA = (".15 .7 .3 1", ".9 .9 .15 1", "1 .45 .05 1")       # green, yellow, orange
EIGHT = ((0.34, -0.12),) + tuple(((0.08 + 0.06 * i, -0.30) for i in range(int(os.environ.get("SIM_BLACK_8BALLS", "0")))))
ILLEGAL = ((GOLF_R, "0.0459", ".97 .97 .97 1"),) + tuple(
    (EIGHT_R, "0.16", ".03 .03 .03 1") for _ in range(len(EIGHT) - 1))   # eight_0 white golf ball, then black 8-balls

REMOVED = ("apple", "banana", "orange", "water_bottle", "bowl", "plate")
GRIPPER_BODIES = tuple(f"{side}_{part}" for side in ("left", "right")
                       for part in ("link6", "gripper", "tip_left", "tip_right"))
VISUAL = {"contype": "0", "conaffinity": "0"}


def _n(*values: float) -> str:
    return " ".join(f"{v:.5g}" for v in values)


# ------------------------------------------------------------------------------------------------ power strip
def _strip(world: ET.Element) -> None:
    hx, hy, hz = STRIP_HALF
    cx, cy = STRIP_CENTER
    body = ET.SubElement(world, "body", name="hub", pos=_n(cx, cy, TABLE_Z))
    black, panel = ".05 .05 .055 1", ".09 .09 .1 1"
    floor_h = 2 * hz - SLOT_DEPTH
    ET.SubElement(body, "geom", type="box", size=_n(hx, hy, floor_h / 2), pos=_n(0, 0, floor_h / 2), rgba=black)
    top_z, row = floor_h + SLOT_DEPTH / 2, HUB_POS[1] - cy
    band = 0.009
    # Solid top layer before and after the port row.
    for lo, hi in ((-hy, row - band), (row + band, hy)):
        ET.SubElement(body, "geom", type="box", size=_n(hx, (hi - lo) / 2, SLOT_DEPTH / 2),
                      pos=_n(0, (lo + hi) / 2, top_z), rgba=black)
    # The port row: walls between and around the slots, so plugs seat in real holes.
    slots = sorted((x, (SLOT_HALF_C if k == "c" else SLOT_HALF)) for x, k in zip(PORT_X, PORT_KIND))
    edge = -hx
    for x, (sx, sy) in slots:
        if x - sx > edge:
            ET.SubElement(body, "geom", type="box", size=_n((x - sx - edge) / 2, band, SLOT_DEPTH / 2),
                          pos=_n((x - sx + edge) / 2, row, top_z), rgba=black)
        for sign in (-1, 1):
            ET.SubElement(body, "geom", type="box", size=_n(sx, (band - sy) / 2, SLOT_DEPTH / 2),
                          pos=_n(x, row + sign * (sy + band) / 2, top_z), rgba=black)
        edge = x + sx
    ET.SubElement(body, "geom", type="box", size=_n((hx - edge) / 2, band, SLOT_DEPTH / 2),
                  pos=_n((hx + edge) / 2, row, top_z), rgba=black)
    top = 2 * hz
    # Visual details: USB panel plate with the port shells, two raised outlet panels with 12 outlets, switch, lights.
    for x, (sx, sy) in slots:
        for sign in (-1, 1):
            ET.SubElement(body, "geom", type="box", size=_n(sx, 0.0004, 0.0005), pos=_n(x, row + sign * sy, top),
                          rgba=SILVER, **VISUAL)
    for px in (-0.022, 0.022):
        y0, y1 = -hy + 0.035, row - 0.022
        ET.SubElement(body, "geom", type="box", size=_n(0.0205, (y1 - y0) / 2, 0.001),
                      pos=_n(px, (y0 + y1) / 2, top + 0.001), rgba=panel, **VISUAL)
        for i in range(6):
            oy = y0 + (i + 0.5) * (y1 - y0) / 6
            for dx in (-0.0062, 0.0062):          # the two blade slots
                ET.SubElement(body, "geom", type="box", size=_n(0.0009, 0.0034, 0.0004),
                              pos=_n(px + dx, oy + 0.002, top + 0.0021), rgba=".01 .01 .01 1", **VISUAL)
            ET.SubElement(body, "geom", type="cylinder", size=_n(0.0025, 0.0004),
                          pos=_n(px, oy - 0.006, top + 0.0021), rgba=".01 .01 .01 1", **VISUAL)
    ET.SubElement(body, "geom", type="box", size=_n(0.01, 0.006, 0.003), pos=_n(0.012, -hy + 0.015, top + 0.002),
                  rgba=".75 .08 .06 1", **VISUAL)      # red rocker switch
    for lx in (-0.012, -0.022):
        ET.SubElement(body, "geom", type="box", size=_n(0.003, 0.0015, 0.0004), pos=_n(lx, -hy + 0.02, top),
                      rgba=".55 .55 .5 1", **VISUAL)   # Protected / Grounded lights
    # The power cord leaves the switch end toward the table's front edge.
    ET.SubElement(world, "geom", type="capsule", size="0.0035",
                  fromto=_n(cx, cy - hy - 0.005, TABLE_Z + 0.008, cx + 0.03, cy - hy - 0.3, TABLE_Z + 0.0035),
                  rgba=BLACK, **VISUAL)


# ------------------------------------------------------------------------------------------------ cables
def _cable(world: ET.Element, name: str, pos: tuple[float, float, float], quat: str, cord_quat: str) -> None:
    """USB-A plug (body `name`, its tongue below the origin, as a port expects) -> orange cord of SEGMENTS
    ball-jointed links -> USB-C plug (body `name`_c, tongue toward +z, site `name`_c_tip at the tongue)."""
    body = ET.SubElement(world, "body", name=name, pos=_n(*pos), quat=quat)
    ET.SubElement(body, "freejoint", name=f"{name}_free")
    ET.SubElement(body, "geom", name=f"{name}_metal", type="box", size=_n(0.006, 0.00225, 0.006),
                  pos=_n(0, 0, -0.006), mass="0.002", rgba=SILVER)
    ET.SubElement(body, "geom", name=f"{name}_mold", type="box", size=_n(0.0075, 0.0042, 0.009),
                  pos=_n(0, 0, 0.009), mass="0.005", rgba=ORANGE)
    ET.SubElement(body, "geom", name=f"{name}_relief", type="capsule", fromto=_n(0, 0, 0.018, 0, 0, 0.03),
                  size="0.0034", mass="0.002", rgba=BLACK)
    parent, at, q = body, 0.03, cord_quat
    for k in range(SEGMENTS):
        seg = ET.SubElement(parent, "body", name=f"{name}_seg{k}", pos=_n(0, 0, at), quat=q)
        ET.SubElement(seg, "joint", type="ball", damping="0.0008", stiffness="0.002", armature="0.00002")
        ET.SubElement(seg, "geom", type="capsule", fromto=_n(0, 0, 0, 0, 0, SEG_LEN), size=str(CORD_R),
                      mass="0.0015", rgba=ORANGE)
        parent, at, q = seg, SEG_LEN, "1 0 0 0"
    c = ET.SubElement(parent, "body", name=f"{name}_c", pos=_n(0, 0, SEG_LEN))
    ET.SubElement(c, "joint", type="ball", damping="0.0008", stiffness="0.002", armature="0.00002")
    ET.SubElement(c, "geom", type="capsule", fromto=_n(0, 0, 0, 0, 0, 0.012), size="0.0032", mass="0.002", rgba=BLACK)
    ET.SubElement(c, "geom", type="box", size=_n(0.0055, 0.0035, 0.0075), pos=_n(0, 0, 0.0195), mass="0.003",
                  rgba=ORANGE)
    ET.SubElement(c, "geom", type="box", size=_n(0.0041, 0.0012, 0.0035), pos=_n(0, 0, 0.0305), mass="0.001",
                  rgba=SILVER)
    ET.SubElement(c, "site", name=f"{name}_c_tip", pos=_n(0, 0, 0.0305), size="0.001")


# ------------------------------------------------------------------------------------------------ basket
def _bin(world: ET.Element) -> None:
    bx, by, bh = BIN_HALF
    t = 0.0025
    body = ET.SubElement(world, "body", name="bin", pos=_n(BIN_POS[0], BIN_POS[1], TABLE_Z))
    white, slot = ".95 .95 .95 1", ".72 .72 .72 1"
    ET.SubElement(body, "geom", type="box", size=_n(bx + t, by + t, t), pos=_n(0, 0, t), rgba=white)
    for sign in (-1, 1):
        ET.SubElement(body, "geom", type="box", size=_n(t, by + t, bh), pos=_n(sign * (bx + t), 0, bh), rgba=white)
        ET.SubElement(body, "geom", type="box", size=_n(bx + t, t, bh), pos=_n(0, sign * (by + t), bh), rgba=white)
        # the slots of the real basket, as darker vertical stripes on each wall (visual only)
        for i in range(-5, 6):
            ET.SubElement(body, "geom", type="box", size=_n(0.004, 0.0004, bh * 0.6),
                          pos=_n(i * bx / 6, sign * (by + 2 * t + 0.0004), bh), rgba=slot, **VISUAL)
        for i in range(-3, 4):
            ET.SubElement(body, "geom", type="box", size=_n(0.0004, 0.004, bh * 0.6),
                          pos=_n(sign * (bx + 2 * t + 0.0004), i * by / 4, bh), rgba=slot, **VISUAL)


# ------------------------------------------------------------------------------------------------ the table
def add_objects(world: ET.Element) -> None:
    _strip(world)
    hub_top = TABLE_Z + 2 * HUB_HALF[2]
    # A seated plug is turned so its tongue's long side runs along the strip, as the slots do; its cord leaves
    # upward and lies back along the strip (-y).
    seated_quat = "0.7071068 0 0 0.7071068"     # plug's local x -> world y
    seated_cord = "0.7071068 0 -0.7071068 0"    # cord: local z -> local -x = world -y
    for i, port in enumerate(PLUGGED_PORTS):
        _cable(world, f"plugged_{i}", (HUB_POS[0] + PORT_X[port], HUB_POS[1], hub_top + 0.0005), seated_quat,
               seated_cord)
    lying = "0.7071068 0.7071068 0 0"            # plug axis along -y, lying on its wide face; the cord carries on
    for i, (x, y) in enumerate(LOOSE_PLUGS):
        _cable(world, f"loose_{i}", (x, y, TABLE_Z + 0.0045), lying, "1 0 0 0")
    for i, (x, y) in enumerate(CUBES):       # holes face up, as in the photos
        body = ET.SubElement(world, "body", name=f"cube_{i}", pos=_n(x, y, TABLE_Z + CUBE_HALF + 0.0005))
        ET.SubElement(body, "freejoint")
        w = (CUBE_HALF - HOLE_HALF) / 2
        for sign in (-1, 1):
            ET.SubElement(body, "geom", type="box", size=_n(w, CUBE_HALF, CUBE_HALF),
                          pos=_n(sign * (HOLE_HALF + w), 0, 0), mass="0.0025", rgba=CUBE_RGBA[i])
            ET.SubElement(body, "geom", type="box", size=_n(HOLE_HALF, w, CUBE_HALF),
                          pos=_n(0, sign * (HOLE_HALF + w), 0), mass="0.0015", rgba=CUBE_RGBA[i])
    for i, (kind, size, (x, y), rgba) in enumerate(STICKS):
        r = size[0]
        body = ET.SubElement(world, "body", name=f"cylinder_{i}", pos=_n(x, y, TABLE_Z + r + 0.0005),
                             quat="0.7071068 0 0.7071068 0")
        ET.SubElement(body, "freejoint")
        ET.SubElement(body, "geom", type=kind, size=_n(*size), mass="0.004", rgba=rgba)
    _bin(world)
    for i, (x, y) in enumerate(GOLF):
        body = ET.SubElement(world, "body", name=f"golf_{i}", pos=_n(x, y, TABLE_Z + GOLF_R + 0.0005))
        ET.SubElement(body, "freejoint")
        ET.SubElement(body, "geom", type="sphere", size=str(GOLF_R), mass="0.0459",
                      rgba=GOLF_RGBA[i % len(GOLF_RGBA)])
    for i, (x, y) in enumerate(EIGHT):
        radius, mass, rgba = ILLEGAL[i]
        body = ET.SubElement(world, "body", name=f"eight_{i}", pos=_n(x, y, TABLE_Z + radius + 0.0005))
        ET.SubElement(body, "freejoint")
        ET.SubElement(body, "geom", type="sphere", size=str(radius), mass=mass, rgba=rgba)


def build_xml(original_xml: str) -> str:
    root = ET.fromstring(original_xml)
    root.set("model", "Dream YAM Sim: Hill Climb relay table (station replica)")
    world = root.find("worldbody")
    for body in list(world):
        if body.tag == "body" and body.get("name") in REMOVED:
            world.remove(body)
    # The station's table: light grey-white wood laminate, not Dream's oak.
    asset = root.find("asset")
    if asset is None:
        asset = ET.SubElement(root, "asset")
    ET.SubElement(asset, "texture", name="laminate", type="2d", builtin="flat", width="512", height="512",
                  rgb1=".72 .69 .63", mark="random", random="0.03", markrgb=".65 .62 .57")
    ET.SubElement(asset, "material", name="laminate", texture="laminate", texrepeat="3 3", specular=".1",
                  shininess=".2")
    for geom in world.iter("geom"):
        if geom.get("name") == "table":
            geom.set("material", "laminate")
    add_objects(world)
    return ET.tostring(root, encoding="unicode")


def patch(yam_env: Any) -> None:
    """Make yam_env.YamEnv() build the relay table. Idempotent."""
    if getattr(yam_env, "_hill_climb_patched", False):
        return
    original = yam_env.build_xml
    yam_env.build_xml = lambda: build_xml(original())
    yam_env._hill_climb_patched = True


# ----------------------------------------------------------------------------------------------- referee
class HillClimbReferee:
    """Ground-truth rubric v4 scoring from the sim state. Call update(model, data) after each physics step."""

    def __init__(self, model: Any) -> None:
        import mujoco
        self._mj = mujoco
        self._model = model
        names = [model.body(i).name for i in range(model.nbody)]
        self.id = {n: i for i, n in enumerate(names)}
        self.gripper = {self.id[n] for n in GRIPPER_BODIES if n in self.id}
        self.groups = {
            1: {self.id[n] for n in names if n.startswith(("plugged_", "loose_"))},
            2: {self.id[n] for n in names if n.startswith(("cube_", "cylinder_"))},
            3: {self.id[n] for n in names if n.startswith(("golf_", "eight_"))},
        }
        self.hub_top = TABLE_Z + 2 * HUB_HALF[2]
        self.stage1_at: float | None = None
        self.stage2_at: float | None = None
        self.dq: str | None = None
        self.first_touch: dict[str, float] = {}
        self._last_block_touch = -math.inf
        self._t = 0.0

    def _pos(self, data: Any, name: str) -> tuple[float, float, float]:
        p = data.xpos[self.id[name]]
        return float(p[0]), float(p[1]), float(p[2])

    def _unplugged(self, data: Any) -> int:
        out = 0
        for i, port in enumerate(PLUGGED_PORTS):
            x, y, z = self._pos(data, f"plugged_{i}")
            in_slot = math.hypot(x - HUB_POS[0] - PORT_X[port], y - HUB_POS[1]) < 0.006
            if not in_slot or z - 0.012 > self.hub_top + 0.001:   # metal tongue fully clear of the slot
                out += 1
        return out

    def _seated(self, data: Any) -> int:
        """Loose cables plugged into a free port of their kind: the USB-A end in a free USB-A port, or the USB-C end
        (its tongue, pointing down) in the USB-C port. Each cable counts once."""
        free = [p for p in range(len(PORT_X)) if p not in PLUGGED_PORTS]
        seated = 0
        for i in range(len(LOOSE_PLUGS)):
            x, y, z = self._pos(data, f"loose_{i}")
            a_in = any(PORT_KIND[p] == "a" and math.hypot(x - HUB_POS[0] - PORT_X[p], y - HUB_POS[1]) < 0.003
                       for p in free) and abs(z - self.hub_top) < 0.004
            tip = data.site_xpos[self._model.site(f"loose_{i}_c_tip").id]
            c_in = any(PORT_KIND[p] == "c" and math.hypot(tip[0] - HUB_POS[0] - PORT_X[p], tip[1] - HUB_POS[1]) < 0.003
                       for p in free) and self.hub_top - 0.009 < tip[2] < self.hub_top
            seated += bool(a_in or c_in)
        return seated

    def _tower_m(self, data: Any) -> float:
        top = TABLE_Z
        for b in self.groups[2]:
            # A block's highest point over all its geoms (a holed cube is four walls), orientation-proof.
            adr, num = int(self._model.body_geomadr[b]), int(self._model.body_geomnum[b])
            for gid in range(adr, adr + num):
                top = max(top, float(data.geom_xpos[gid][2]) + self._half_height(data, gid))
        return top - TABLE_Z

    def _half_height(self, data: Any, gid: int) -> float:
        size, kind = self._model.geom_size[gid], self._model.geom_type[gid]
        R = data.geom_xmat[gid].reshape(3, 3)
        if kind == self._mj.mjtGeom.mjGEOM_BOX:
            return float(sum(abs(R[2, k]) * size[k] for k in range(3)))
        r, h = float(size[0]), float(size[1])   # cylinder: radius, half-length along local z
        axis_z = abs(R[2, 2])
        return h * axis_z + r * math.sqrt(max(0.0, 1 - axis_z * axis_z))

    def _in_bin(self, data: Any, prefix: str) -> int:
        bx, by, bh = BIN_HALF
        count = 0
        for name in self.id:
            if name.startswith(prefix) and "_" in name and name.split("_", 1)[1].isdigit():
                x, y, z = self._pos(data, name)
                if abs(x - BIN_POS[0]) < bx and abs(y - BIN_POS[1]) < by and z < TABLE_Z + 2 * bh:
                    count += 1
        return count

    def update(self, model: Any, data: Any) -> None:
        self._model = model
        self._t = float(data.time)
        touched: set[int] = set()
        for k in range(data.ncon):
            c = data.contact[k]
            b1, b2 = int(model.geom_bodyid[c.geom1]), int(model.geom_bodyid[c.geom2])
            for a, b in ((b1, b2), (b2, b1)):
                if a in self.gripper:
                    touched.add(b)
        for b in touched:
            self.first_touch.setdefault(model.body(b).name, round(self._t, 2))
        if touched & self.groups[2]:
            self._last_block_touch = self._t
        if self.stage1_at is None and self._unplugged(data) >= 1:
            self.stage1_at = self._t
        if self.dq is None:
            if touched & self.groups[2] and self.stage1_at is None:
                self.dq = f"block touched at {self._t:.1f} s before a cable was unplugged"
            elif touched & self.groups[3] and self.stage2_at is None:
                self.dq = f"ball touched at {self._t:.1f} s before the tower passed"
        if (self.stage1_at is not None and self.stage2_at is None and self._tower_m(data) >= TOWER_BAR_M
                and self._t - self._last_block_touch >= FREE_STANDING_S):
            self.stage2_at = self._t

    def report(self, data: Any) -> dict[str, Any]:
        unplugged, seated = self._unplugged(data), self._seated(data)
        tower = self._tower_m(data)
        golf, eight = self._in_bin(data, "golf_"), self._in_bin(data, "eight_")
        points = 0
        if unplugged:
            points += 10 + 5 * (unplugged - 1)
        points += 20 * min(seated, 4)
        if self.stage2_at is not None:
            points += 20 + 3 * int(tower / INCH)
        points += 5 * golf
        return {
            "t": round(self._t, 2), "points": 0 if self.dq else points, "dq": self.dq,
            "loses": bool(self.dq or eight), "unplugged": unplugged, "seated": seated,
            "tower_in": round(tower / INCH, 2), "stage1_at": self.stage1_at, "stage2_at": self.stage2_at,
            "golf_in_bin": golf, "eight_in_bin": eight, "first_touch": dict(self.first_touch),
        }
