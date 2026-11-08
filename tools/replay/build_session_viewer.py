"""Stitch several short Dream station runs (one agent session) into one replay page.

    python3 -I tools/replay/build_session_viewer.py <session dir>     # dir holding zeroshot_* rollouts + the two excerpts

Each zeroshot_* folder is one station run (archived by ~/archive_runs.sh). The runs are played back to back; the
timeline marks where each step starts and how long the session waited before it (wall clock). Station monotonic time
is continuous across station restarts, so one wall/monotonic offset from any run_start pair maps every log.
Writes <session dir>/session_viewer.html and session_replay.mp4. Stdlib + ffmpeg.
"""
import base64
import csv
import datetime as dt
import json
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import metal_fk  # noqa: E402

sess = Path(sys.argv[1]).resolve()
chain = metal_fk.load(str(HERE / "metal_with_gripper.urdf"))
PRE = ("The power strip is unplugged from the wall and has no power; the orange cords are insulated low-voltage USB "
       "cables, not bare wires. ")
GAP = 0.6   # seconds of video between steps

inference = [json.loads(l) for l in open(sess / "inference_excerpt.jsonl") if l.strip()]
wall = lambda iso: dt.datetime.fromisoformat(iso).timestamp()
runs = []
for d in sorted(sess.glob("zeroshot_*")):
    inp = [json.loads(l) for l in open(d / "last-run_inputs" / "inputs.jsonl")]
    calls = [x for x in inp if x.get("kind") == "inputs"]
    if not calls:
        continue
    ticks = list(csv.DictReader(open(d / "last-run.csv")))
    if not ticks:   # the policy never answered (e.g. a cold fine-tune), so the arms never moved: nothing to replay
        print(f"skipping {d.name}: {len(calls)} frames sent, no executed ticks")
        continue
    rs = next((x["t_s"] for x in inp if x.get("kind") == "run_start"), calls[0]["t_s"])
    runs.append({"dir": d, "calls": calls, "ticks": ticks, "rs": rs})

# one offset (wall = monotonic + off). Coarse: each folder is named for its last-run.csv mtime (local time, -0800),
# i.e. the run's last tick. Fine: snap each run's start to the nearest station-log "-> starting" line.
offs = []
for r in runs:
    end_wall = dt.datetime.strptime(r["dir"].name.split("_")[1] + " -0800", "%Y%m%dT%H%M%S %z").timestamp()
    offs.append(end_wall - float(r["ticks"][-1]["t_s"]))
off = statistics.median(offs) if offs else None
starts = []
for line in open(sess / "station_log_excerpt.txt"):
    if "-> starting" in line and line[:4].isdigit():
        starts.append(dt.datetime.strptime(line[:23] + " -0800", "%Y-%m-%d %H:%M:%S,%f %z").timestamp())
if off is not None and starts:
    fine = [min(starts, key=lambda w: abs(w - (r["rs"] + off))) - r["rs"] for r in runs]
    fine = [f for f in fine if abs(f - off) < 5]
    if fine:
        off = statistics.median(fine)

# video time base per run
base, prompts, segs, call_rows, series, holds, bounds = 0.0, [], [], [], [], [], []
prev_end_wall = None
for k, r in enumerate(runs):
    c0 = r["calls"][0]["t_s"]
    last = max(r["calls"][-1]["t_s"], float(r["ticks"][-1]["t_s"]) if r["ticks"] else c0)
    r["c0"], r["base"], r["span"] = c0, base, last - c0
    start_wall = (c0 + off) if off is not None else None
    gap = (start_wall - prev_end_wall) if (start_wall and prev_end_wall) else None
    first_prompt = r["calls"][0]["state"].get("prompt", "")
    bounds.append({"t": round(base, 2), "step": k + 1, "wall": dt.datetime.fromtimestamp(start_wall).strftime("%H:%M:%S") if start_wall else "",
                   "gap_s": round(gap, 1) if gap is not None else None, "dur": round(r["span"], 1), "run": r["dir"].name})
    prev_end_wall = (last + off) if off is not None else None
    v = lambda m, r=r: round(r["base"] + (m - r["c0"]), 2)
    for c in r["calls"]:
        s = c["state"]; p = s.get("prompt", "")
        if p not in prompts:
            prompts.append(p)
        pi = prompts.index(p)
        if not segs or segs[-1]["p"] != pi or (segs[-1]["t"] < base and k > 0 and c is r["calls"][0]):
            segs.append({"p": pi, "t": v(c["t_s"])})
        z = metal_fk.tcp(chain, s["state.right_arm"])[2]
        zl = metal_fk.tcp(chain, s["state.left_arm"])[2] if "state.left_arm" in s else None
        row = [v(c["t_s"]), pi, round(s["state.right_grip"][0] * 100, 1), round(z * 100, 1)]
        if zl is not None and "state.left_grip" in s:
            row += [round(s["state.left_grip"][0] * 100, 1), round(zl * 100, 1)]
        call_rows.append(row)
    T = [float(x["t_s"]) for x in r["ticks"]]
    lastt = -1e9
    for i, x in enumerate(r["ticks"]):
        if T[i] < c0 or T[i] - lastt < 0.1:
            continue
        lastt = T[i]
        zm = metal_fk.tcp(chain, [float(x[f"measured_r{j}"]) for j in range(6)])[2]
        zc = metal_fk.tcp(chain, [float(x[f"commanded_r{j}"]) for j in range(6)])[2]
        zml = metal_fk.tcp(chain, [float(x[f"measured_l{j}"]) for j in range(6)])[2]
        zcl = metal_fk.tcp(chain, [float(x[f"commanded_l{j}"]) for j in range(6)])[2]
        series.append([v(T[i]), round(float(x["cmd_gripper_r"]) * 100, 1), round(float(x["gripper_r"]) * 100, 1),
                       round(zc * 100, 1), round(zm * 100, 1), round(float(x["cmd_gripper_l"]) * 100, 1),
                       round(float(x["gripper_l"]) * 100, 1), round(zcl * 100, 1), round(zml * 100, 1)])
    holds += [[v(T[i - 1]), round(T[i] - T[i - 1], 2)] for i in range(1, len(T)) if T[i] - T[i - 1] > 0.25 and T[i - 1] >= c0]
    base += r["span"] + GAP
duration = round(base - GAP, 2)

# What happened between Dream steps (the station archives only Dream runs; scripted /move calls and /home happen in
# the gaps). Classified from each arm's measured tool position and gripper at the end of one step and the start of the next.
HOME_R = (17.0, 0.3, 23.0)
def tool(row, k):
    return [v * 100 for v in metal_fk.tcp(chain, [float(row[f"measured_{k}{j}"]) for j in range(6)])], float(row[f"gripper_{k}"]) * 100
def between(prev, nxt):
    notes = []
    for k, name in (("r", "right"), ("l", "left")):
        (a, ga), (b, gb) = tool(prev["ticks"][-1], k), tool(nxt["ticks"][0], k)
        d = [y - x for x, y in zip(a, b)]; dist = sum(v * v for v in d) ** 0.5
        if dist < 2 and abs(gb - ga) < 10:
            continue
        if k == "r" and sum((y - h) ** 2 for y, h in zip(b, HOME_R)) ** 0.5 < 3 and gb > 90:
            notes.append(f"{name} arm homed, gripper opened")
        elif abs(d[0]) < 2.5 and abs(d[1]) < 2.5 and d[2] > 3:
            notes.append(f"{name} arm lifted straight up {d[2]:.0f} cm" + (" with the gripper closed (scripted pull)" if max(ga, gb) < 30 else ""))
        elif abs(d[0]) < 2.5 and abs(d[1]) < 2.5 and d[2] < -3:
            notes.append(f"{name} arm lowered straight down {-d[2]:.0f} cm")
        else:
            notes.append(f"{name} arm moved {dist:.0f} cm (dx {d[0]:+.0f}, dy {d[1]:+.0f}, dz {d[2]:+.0f})" + (f", gripper {ga:.0f}->{gb:.0f} mm" if abs(gb - ga) >= 10 else ""))
    return "; ".join(notes) or "no arm movement"
for k in range(1, len(runs)):
    bounds[k]["between"] = between(runs[k - 1], runs[k])

def to_video(mono, slack=3.0):
    for r in runs:
        if r["c0"] - slack <= mono <= r["c0"] + r["span"] + slack:
            return round(r["base"] + (mono - r["c0"]), 2)
    return None

chunks, stalls, station = [], [], []
for x in inference:
    if off is None or "t" not in x:
        continue
    t = to_video(wall(x["t"]) - off)
    if t is None:
        continue
    if x.get("kind") == "inference":
        chunks.append([t, x.get("chunk"), round(float(x.get("rtt_ms") or 0)), x.get("mode"),
                       x.get("discarded") not in (None, False, 0, "None")])
    elif x.get("kind") == "stall":
        stalls.append(t)
for line in open(sess / "station_log_excerpt.txt"):
    if off is None or len(line) < 24 or not line[:4].isdigit():
        continue
    try:
        w = dt.datetime.strptime(line[:23] + " -0800", "%Y-%m-%d %H:%M:%S,%f %z").timestamp()
    except ValueError:
        continue
    t = to_video(w - off)
    if t is None:
        continue
    msg = line[24:].strip()
    if "prompt for the running loop set to" in msg:
        txt = msg.split("set to ", 1)[1].strip().strip("'\"")
        station.append({"t": t, "kind": "prompt_set", "p": next((i for i, p in enumerate(prompts) if p.startswith(txt[:120])), None), "text": txt[:300]})
    elif "station:" in msg and "->" in msg:
        station.append({"t": t, "kind": "state", "text": msg.split("station:", 1)[1].split("(run")[0].strip()})
    elif ("WARNING" in msg or "ERROR" in msg) and "stall " not in msg:
        station.append({"t": t, "kind": "warn" if "WARNING" in msg else "error", "text": msg[:240]})

tmp = Path(tempfile.mkdtemp())
for cam in ("scene", "wrist_l", "wrist_r"):
    with open(tmp / f"{cam}.txt", "w") as f:
        for r in runs:
            cs = r["calls"]
            for i, a in enumerate(cs):
                b = cs[i + 1]["t_s"] if i + 1 < len(cs) else a["t_s"] + max(0.05, r["c0"] + r["span"] - a["t_s"]) + GAP
                f.write(f"file '{r['dir']}/last-run_inputs/{a['frames'][cam]['file']}'\nduration {max(b - a['t_s'], 0.05):.3f}\n")
        f.write(f"file '{runs[-1]['dir']}/last-run_inputs/{runs[-1]['calls'][-1]['frames'][cam]['file']}'\n")
mp4 = sess / "session_replay.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(tmp / "scene.txt"), "-f", "concat",
                "-safe", "0", "-i", str(tmp / "wrist_l.txt"), "-f", "concat", "-safe", "0", "-i", str(tmp / "wrist_r.txt"), "-filter_complex",
                "[0:v]scale=400:224,setsar=1[a];[1:v]scale=400:224,setsar=1[b];[2:v]scale=400:224,setsar=1[c];[a][b][c]hstack=inputs=3,fps=10,format=yuv420p[v]",
                "-map", "[v]", "-c:v", "libx264", "-crf", "27", "-preset", "veryfast", "-movflags", "+faststart", str(mp4)], check=True)

data = {"run": f"{sess.name} · {len(runs)} steps", "duration": duration, "t0_epoch": None,
        "prompts": [{"id": i, "full": p, "short": p.replace(PRE, "[safety preamble] "), "chars": len(p), "preamble": p.startswith(PRE)} for i, p in enumerate(prompts)],
        "segs": segs, "calls": call_rows, "series": series, "holds": holds, "chunks": chunks, "stalls": stalls, "station": station,
        "runs": bounds}
html = (HERE / "viewer_template.html").read_text(encoding="utf-8")
html = html.replace("__DATA__", json.dumps(data, separators=(",", ":"))).replace("__VIDEO__", base64.b64encode(mp4.read_bytes()).decode())
out = sess / "session_viewer.html"
out.write_text(html, encoding="utf-8")
print(f"wrote {out} ({os.path.getsize(out) / 1e6:.1f} MB); {len(runs)} steps, {duration:.0f} s of robot time, "
      f"{len(prompts)} prompts, {len(chunks)} replies; offset {'ok' if off else 'missing'}")
for b in bounds:
    print(f"  step {b['step']:2d} at {b['wall']} (video {b['t']:6.1f}s) ran {b['dur']:5.1f}s, waited {b['gap_s']} s before it" + (f" | before it: {b['between']}" if b.get('between') else ""))
