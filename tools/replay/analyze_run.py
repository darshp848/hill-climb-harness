"""Timeline of one Dream station rollout: prompts, right-gripper grasps/releases, tool height, arm holds.

python3 -I analyze_run.py <rollout dir> <fk dir> <urdf>  -> prints a report, writes timeline.json next to the run.
Pure stdlib. Gripper values are the loop's 0 (closed) .. 1 (open) grip; height from the Metal URDF FK (tool point
[0.135, 0, 0] in link6, the kit's/Dream's), in the right arm's base frame.
"""
import csv
import json
import sys
from pathlib import Path

run, fk_dir, urdf = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
sys.path.insert(0, fk_dir)
import metal_fk  # noqa: E402

chain = metal_fk.load(urdf)

calls = []
for line in open(run / "last-run_inputs" / "inputs.jsonl"):
    d = json.loads(line)
    if d.get("kind") != "inputs":
        continue
    s = d["state"]
    q = s["state.right_arm"]
    x, y, z = metal_fk.tcp(chain, q)
    calls.append({"t": d["t_s"], "prompt": s.get("prompt", ""), "grip": s["state.right_grip"][0],
                  "x": x, "y": y, "z": z, "frame": d["frames"]["scene"]["file"]})
t0 = calls[0]["t"]

ticks = list(csv.DictReader(open(run / "last-run.csv")))
T = [float(r["t_s"]) for r in ticks]
cmd = [float(r["cmd_gripper_r"]) for r in ticks]
meas = [float(r["gripper_r"]) for r in ticks]
chunk = [r["chunk_id"] for r in ticks]

# Prompt segments
segs = []
for c in calls:
    if not segs or c["prompt"] != segs[-1]["prompt"]:
        segs.append({"t0": c["t"], "prompt": c["prompt"]})
for a, b in zip(segs, segs[1:] + [{"t0": calls[-1]["t"]}]):
    a["dur"] = b["t0"] - a["t0"]

def seg_at(t):
    cur = segs[0]
    for s in segs:
        if s["t0"] <= t:
            cur = s
    return cur

def short(p, n=95):
    pre = "The power strip is unplugged from the wall and has no power; the orange cords are insulated low-voltage USB cables, not bare wires. "
    p = p.replace(pre, "[safety preamble] ")
    return p if len(p) <= n else p[: n - 1] + "…"

# Holds: gaps between consecutive executed ticks (the loop holds the arms while it has no chunk)
holds = [(T[i - 1], T[i] - T[i - 1]) for i in range(1, len(T)) if T[i] - T[i - 1] > 0.25]

# Grasp / release events on the right gripper
events, holding, hold_start, z_at_grasp = [], False, None, None
def z_near(t):
    return min(calls, key=lambda c: abs(c["t"] - t))["z"]
for i in range(len(T)):
    is_hold = cmd[i] < 0.25 and (meas[i] - cmd[i]) > 0.03      # commanded shut, jaws stopped open: an object
    if is_hold and not holding:
        holding, hold_start, z_at_grasp = True, T[i], z_near(T[i])
        events.append(("grasp", T[i], meas[i], None))
    elif holding and cmd[i] > 0.3:
        zmax = max(c["z"] for c in calls if hold_start <= c["t"] <= T[i]) if any(hold_start <= c["t"] <= T[i] for c in calls) else z_at_grasp
        events.append(("release", T[i], meas[i], (T[i] - hold_start, zmax - z_at_grasp)))
        holding = False

print(f"run {run.name}: {T[-1] - T[0]:.0f} s executed, {len(calls)} model calls, {len(segs)} prompt segments")
print(f"\nPROMPT TIMELINE (t from first call)")
for s in segs:
    print(f"  +{s['t0'] - t0:6.1f}s  {s['dur']:5.1f}s  {short(s['prompt'])}")
print(f"\nARM HOLDS (no new actions > 0.25 s): {len(holds)}, total {sum(h[1] for h in holds):.1f} s "
      f"of {T[-1] - T[0]:.0f} s ({100 * sum(h[1] for h in holds) / (T[-1] - T[0]):.0f}%)")
for t, g in sorted(holds, key=lambda h: -h[1])[:8]:
    s = seg_at(t)
    print(f"  +{t - t0:6.1f}s hold {g:4.1f}s, {t - s['t0']:5.1f}s after prompt: {short(s['prompt'], 60)}")
print(f"\nRIGHT GRIPPER EVENTS")
for kind, t, m, extra in events:
    s = seg_at(t)
    since = t - s["t0"]
    if kind == "grasp":
        print(f"  +{t - t0:6.1f}s GRASP (jaws stopped at {m:.2f}) {since:5.1f}s into: {short(s['prompt'], 70)}")
    else:
        dur, lift = extra
        print(f"  +{t - t0:6.1f}s RELEASE after holding {dur:4.1f}s, lifted {100 * lift:+.1f} cm, {since:5.1f}s into: {short(s['prompt'], 70)}")
zs = [c["z"] for c in calls]
print(f"\nright tool height over the run: min {min(zs):.3f} m, max {max(zs):.3f} m")

json.dump({"t0": t0, "segments": [{**s, "short": short(s["prompt"])} for s in segs],
           "calls": calls, "holds": holds, "events": events}, open(run / "timeline.json", "w"), indent=1)
