"""Per-prompt execution stats for every archived Dream station rollout of a day. Read-only; prints JSON lines.

Runs ON THE STATION (piped over ssh into ~/dream-env/bin/python -). Reads ~/rollouts/zeroshot_<DAY>*/ (last-run.csv +
last-run_inputs/inputs.jsonl) and ~/.dreamlabs/station.log (which checkpoint each run used). Touches no arm.
One line per prompt segment (a stretch of one rollout where the prompt text did not change).
"""
import csv
import datetime as dt
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DAY = sys.argv[1] if len(sys.argv) > 1 else "20261108"
HOME = Path.home()
URDF = HOME / ".cache/dream_hackathon/urdf/metal_with_gripper.urdf"
TCP = (0.135, 0.0, 0.0)

# ---------------------------------------------------------------- FK (Metal URDF, tool point in link6)
def _mm(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]
def _rpy(rpy, xyz):
    r, p, y = rpy; cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    R = [[cy*cp, cy*sp*sr - sy*cr, cy*sp*cr + sy*sr], [sy*cp, sy*sp*sr + cy*cr, sy*sp*cr - cy*sr], [-sp, cp*sr, cp*cr]]
    return [R[0] + [xyz[0]], R[1] + [xyz[1]], R[2] + [xyz[2]], [0, 0, 0, 1]]
def _ax(a, q):
    x, y, z = a; n = math.sqrt(x*x + y*y + z*z) or 1; x, y, z = x/n, y/n, z/n; c, s, t = math.cos(q), math.sin(q), 1 - math.cos(q)
    return [[t*x*x + c, t*x*y - s*z, t*x*z + s*y, 0], [t*x*y + s*z, t*y*y + c, t*y*z - s*x, 0], [t*x*z - s*y, t*y*z + s*x, t*z*z + c, 0], [0, 0, 0, 1]]
def _f(t, d):
    return tuple(float(v) for v in t.split()) if t else d
_root = ET.parse(URDF).getroot(); _j = {}
for j in _root.findall("joint"):
    o, a = j.find("origin"), j.find("axis")
    _j[j.find("child").get("link")] = dict(type=j.get("type"), parent=j.find("parent").get("link"),
        xyz=_f(o.get("xyz") if o is not None else None, (0, 0, 0)), rpy=_f(o.get("rpy") if o is not None else None, (0, 0, 0)),
        axis=_f(a.get("xyz") if a is not None else None, (1, 0, 0)))
CHAIN, _l = [], "link6"
while _l in _j:
    CHAIN.append(_j[_l]); _l = _j[_l]["parent"]
CHAIN.reverse()
def tool_z(q):
    T = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]; i = 0
    for j in CHAIN:
        T = _mm(T, _rpy(j["rpy"], j["xyz"]))
        if j["type"] in ("revolute", "continuous"):
            T = _mm(T, _ax(j["axis"], q[i])); i += 1
    v = list(TCP) + [1]
    return [sum(T[r][k] * v[k] for k in range(4)) for r in range(3)]

# ---------------------------------------------------------------- which checkpoint each run used
clients = []
for line in open(HOME / ".dreamlabs/station.log", errors="replace"):
    if line.startswith(f"{DAY[:4]}-{DAY[4:6]}-{DAY[6:]}") and "dream client for " in line:
        m = re.search(r"dream client for (\S+?):", line)
        if m:
            clients.append((dt.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S"), m.group(1)))

PRE = "The power strip is unplugged from the wall and has no power; the orange cords are insulated low-voltage USB cables, not bare wires. "
def task_of(p):
    s = p.replace(PRE, "").lower()
    if "put the held cable down" in s or "lay the cable" in s or "put it down" in s: return "put_down"
    if re.search(r"\bplug (the|a) (loose )?usb cable .*into|plug .* into a free port", s): return "plug_in"
    if any(k in s for k in ("unplug", "remove the first cable", "move down onto the plug", "grip the hard plug", "grip the plug",
                            "lift it vertically", "pull it straight", "beside the rightmost plug", "rightmost plug")): return "unplug"
    if "push the stacked tower" in s or "push the tower" in s: return "push_tower"
    if "ball" in s: return "ball"
    if any(k in s for k in ("cube", "block", "tower", "stack", "cylinder", "stick")): return "stack"
    if "open both grippers" in s or "raise both arms" in s or "move the arms up" in s: return "reset"
    return "other"
RANGE = {"unplug": (4.5, 14), "put_down": (4.5, 14), "plug_in": (4.5, 14), "ball": (30, 55), "stack": (8, 62), "push_tower": (8, 62)}
CLOSE = 40  # a closure = commanded gap under 40 mm (Dream closes only partway on balls/cubes)

rolls = sorted(p for p in (HOME / "rollouts").glob(f"zeroshot_{DAY}T*") if (p / "last-run.csv").exists())
loaded = []
for d in rolls:
    try:
        calls = [x for x in (json.loads(l) for l in open(d / "last-run_inputs/inputs.jsonl") if l.strip()) if x.get("kind") == "inputs"]
        ticks = list(csv.DictReader(open(d / "last-run.csv")))
    except Exception:
        continue
    if len(calls) < 2 or len(ticks) < 20:
        continue
    loaded.append((d, calls, ticks))

def arm_state(row, k):
    q = [float(row[f"measured_{k}{j}"]) for j in range(6)]
    x, y, z = tool_z(q)
    return x * 100, y * 100, z * 100, float(row[f"gripper_{k}"]) * 100, float(row[f"cmd_gripper_{k}"]) * 100

for idx, (d, calls, ticks) in enumerate(loaded):
    end = dt.datetime.strptime(d.name.split("_")[1], "%Y%m%dT%H%M%S")
    T = [float(r["t_s"]) for r in ticks]
    start = end - dt.timedelta(seconds=T[-1] - T[0])
    ckpt = next((c for t, c in reversed(clients) if t <= start + dt.timedelta(seconds=3)), "unknown")
    # after this rollout: did the arm get lifted straight up while closed before the next one (a scripted pull)?
    pull_after = {}
    if idx + 1 < len(loaded):
        nd, _, nt = loaded[idx + 1]
        gap = (dt.datetime.strptime(nd.name.split("_")[1], "%Y%m%dT%H%M%S") - end).total_seconds() - (float(nt[-1]["t_s"]) - float(nt[0]["t_s"]))
        for k in ("r", "l"):
            a, b = arm_state(ticks[-1], k), arm_state(nt[0], k)
            pull_after[k] = (0 < gap < 400 and abs(b[0] - a[0]) < 2.5 and abs(b[1] - a[1]) < 2.5 and b[2] - a[2] > 6
                             and a[4] < CLOSE and b[4] < CLOSE, round(b[2] - a[2], 1), round(b[3], 1))
    segs = []
    for c in calls:
        p = c["state"].get("prompt", "")
        if not segs or segs[-1]["prompt"] != p:
            segs.append({"prompt": p, "t0": c["t_s"]})
    for i, s in enumerate(segs):
        t1 = segs[i + 1]["t0"] if i + 1 < len(segs) else T[-1] + 0.05
        rows = [r for r, t in zip(ticks, T) if s["t0"] <= t < t1]
        if len(rows) < 10:
            continue
        task = task_of(s["prompt"]); low = s["prompt"].lower()
        st = {k: [arm_state(r, k) for r in rows[::2]] for k in ("r", "l")}
        travel = {k: sum(math.dist(a[:3], b[:3]) for a, b in zip(st[k], st[k][1:])) for k in st}
        arm = "l" if ("left arm" in low or "left gripper" in low) and "right arm" not in low else "r" if "right" in low else max(travel, key=travel.get)
        S = st[arm]; ts = [t for t in T if s["t0"] <= t < t1][::2]
        lo, hi = RANGE.get(task, (4.5, 62))
        closures, i0 = [], None
        for j, (x, y, z, g, cmd) in enumerate(S):
            if cmd < CLOSE and i0 is None: i0 = j
            if (cmd >= CLOSE or j == len(S) - 1) and i0 is not None:
                if j - i0 >= 5:
                    tail = sorted(v[3] for v in S[(i0 + j) // 2: j + 1]); gap = tail[len(tail) // 2]
                    blocked = sorted(v[3] - v[4] for v in S[(i0 + j) // 2: j + 1])[len(tail) // 2] > 1.5
                    zs = [v[2] for v in S[i0:j + 1]]
                    closures.append({"t": ts[i0] - s["t0"], "dur": ts[min(j, len(ts) - 1)] - ts[i0], "gap": gap,
                                     "lift": max(zs) - zs[0], "released": cmd >= CLOSE, "blocked": blocked})
                i0 = None
        obj = [c for c in closures if lo <= c["gap"] <= hi and c["blocked"]]
        wire = [c for c in closures if 2.0 <= c["gap"] < 4.5] if task in ("unplug", "plug_in") else []
        frozen = sum(b - a for a, b in zip([t for t in T if s["t0"] <= t < t1], [t for t in T if s["t0"] <= t < t1][1:]) if b - a > 0.25)
        dur = (min(t1, T[-1]) - s["t0"])
        last_seg = i == len(segs) - 1
        pa = pull_after.get(arm, (False, 0, 0)) if last_seg else (False, 0, 0)
        print(json.dumps({
            "rollout": d.name[-15:], "start": (start + dt.timedelta(seconds=s["t0"] - T[0])).strftime("%H:%M:%S"), "ckpt": ckpt,
            "task": task, "arm": arm, "prompt": s["prompt"].replace(PRE, "[pre] "), "dur": round(dur, 1),
            "frozen_s": round(frozen, 1), "closures": len(closures), "obj_holds": len(obj), "wire_holds": len(wire),
            "first_obj_s": round(obj[0]["t"], 1) if obj else None, "max_obj_hold_s": round(max((c["dur"] for c in obj), default=0), 1),
            "max_lift_cm": round(max((c["lift"] for c in obj), default=0), 1), "obj_released": any(c["released"] for c in obj),
            "obj_gap_mm": round(sorted(c["gap"] for c in obj)[len(obj) // 2], 1) if obj else None,
            "start_holding": lo <= S[0][3] <= hi and S[0][4] < CLOSE, "end_open": S[-1][3] > 30,
            "scripted_pull_after": pa[0], "pull_after_dz": pa[1] if pa[0] else 0, "pull_after_gap": pa[2] if pa[0] else None,
            "end_gap": round(S[-1][3], 1), "end_cmd": round(S[-1][4], 1), "travel_cm": round(travel[arm], 1),
        }))
