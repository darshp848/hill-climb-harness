"""Build a self-contained HTML replay of one Dream station rollout: video + synced traces.

    python3 -I tools/replay/build_viewer.py <rollout dir>        # writes <rollout dir>_viewer.html (and _replay.mp4)

Inputs, all inside the rollout dir (as archived by ~/archive_runs.sh, plus two excerpts copied from the station):
  last-run_inputs/inputs.jsonl + scene/ wrist_r/ jpgs   what Dream-1 was sent, once per call (prompt, joints, frames)
  last-run.csv                                          20 Hz executed rows: commanded/measured joints and grippers
  inference_excerpt.jsonl  (optional)                   ~/robots-inference.jsonl lines for this run (RTT per chunk)
  station_log_excerpt.txt  (optional)                   ~/.dreamlabs/station.log lines for this run (prompt sets)
The harness's own judge log (runs/<ts>/events.jsonl on the laptop that ran it) is dropped onto the page.
Stdlib + ffmpeg only. Heights from the Metal URDF FK in tools/ (tool point [0.135, 0, 0] in link6).
"""
import base64
import csv
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import metal_fk  # noqa: E402

run = Path(sys.argv[1]).resolve()
chain = metal_fk.load(str(HERE / "metal_with_gripper.urdf"))
PRE = ("The power strip is unplugged from the wall and has no power; the orange cords are insulated low-voltage USB "
       "cables, not bare wires. ")

inputs = [json.loads(line) for line in open(run / "last-run_inputs" / "inputs.jsonl")]
calls = [x for x in inputs if x.get("kind") == "inputs"]
rs_mono = next((x["t_s"] for x in inputs if x.get("kind") == "run_start"), calls[0]["t_s"])
c0 = calls[0]["t_s"]

# wall clock (epoch) <- station monotonic, anchored on the run_start both logs carry
inference = []
if (run / "inference_excerpt.jsonl").exists():
    inference = [json.loads(line) for line in open(run / "inference_excerpt.jsonl") if line.strip()]
rs_wall = next((dt.datetime.fromisoformat(x["t"]).timestamp() for x in inference if x.get("kind") == "run_start"), None)
off = (rs_wall - rs_mono) if rs_wall is not None else None
t0_epoch = (c0 + off) if off is not None else None

prompts, segs = [], []
for c in calls:
    p = c["state"].get("prompt", "")
    if p not in prompts:
        prompts.append(p)
    if not segs or segs[-1]["p"] != prompts.index(p):
        segs.append({"p": prompts.index(p), "t": round(c["t_s"] - c0, 2)})

call_rows = []
for c in calls:
    s = c["state"]
    x, y, z = metal_fk.tcp(chain, s["state.right_arm"])
    call_rows.append([round(c["t_s"] - c0, 2), prompts.index(s.get("prompt", "")), round(s["state.right_grip"][0] * 100, 1),
                      round(z * 100, 1)])

ticks = list(csv.DictReader(open(run / "last-run.csv")))
T = [float(r["t_s"]) for r in ticks]
series, last = [], -1.0
for i, r in enumerate(ticks):
    t = T[i] - c0
    if t - last < 0.1:
        continue
    last = t
    zm = metal_fk.tcp(chain, [float(r[f"measured_r{j}"]) for j in range(6)])[2]
    zc = metal_fk.tcp(chain, [float(r[f"commanded_r{j}"]) for j in range(6)])[2]
    series.append([round(t, 2), round(float(r["cmd_gripper_r"]) * 100, 1), round(float(r["gripper_r"]) * 100, 1),
                   round(zc * 100, 1), round(zm * 100, 1)])
holds = [[round(T[i - 1] - c0, 2), round(T[i] - T[i - 1], 2)] for i in range(1, len(T)) if T[i] - T[i - 1] > 0.25]

chunks, stalls = [], []
for x in inference:
    if off is None or "t" not in x:
        continue
    rel = round(dt.datetime.fromisoformat(x["t"]).timestamp() - t0_epoch, 2)
    if x.get("kind") == "inference":
        chunks.append([rel, x.get("chunk"), round(float(x.get("rtt_ms") or 0)), x.get("mode"),
                       x.get("discarded") is not None and x.get("discarded") not in (False, 0, "None")])
    elif x.get("kind") == "stall":
        stalls.append(rel)

station = []
if (run / "station_log_excerpt.txt").exists() and off is not None:
    for line in open(run / "station_log_excerpt.txt"):
        if len(line) < 24 or not line[:4].isdigit():
            continue
        try:
            w = dt.datetime.strptime(line[:23] + " -0800", "%Y-%m-%d %H:%M:%S,%f %z").timestamp()
        except ValueError:
            continue
        msg = line[24:].strip()
        rel = round(w - t0_epoch, 2)
        if "prompt for the running loop set to" in msg:
            txt = msg.split("set to ", 1)[1].strip().strip("'\"")
            match = next((i for i, p in enumerate(prompts) if p.startswith(txt[:120]) or txt.startswith(p[:120])), None)
            station.append({"t": rel, "kind": "prompt_set", "p": match, "text": txt[:300]})
        elif "station:" in msg and "->" in msg:
            station.append({"t": rel, "kind": "state", "text": msg.split("station:", 1)[1].split("(run")[0].strip()})
        elif "WARNING" in msg or "ERROR" in msg:
            station.append({"t": rel, "kind": "warn" if "WARNING" in msg else "error", "text": msg[:240]})

# video: overhead | right wrist, each frame shown until the next call
tmp = Path(tempfile.mkdtemp())
for cam in ("scene", "wrist_r"):
    with open(tmp / f"{cam}.txt", "w") as f:
        for a, b in zip(calls, calls[1:] + [calls[-1]]):
            f.write(f"file '{run}/last-run_inputs/{a['frames'][cam]['file']}'\nduration {max(b['t_s'] - a['t_s'], 0.05):.3f}\n")
        f.write(f"file '{run}/last-run_inputs/{calls[-1]['frames'][cam]['file']}'\n")
mp4 = run.parent / f"{run.name}_replay.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(tmp / "scene.txt"), "-f", "concat",
                "-safe", "0", "-i", str(tmp / "wrist_r.txt"), "-filter_complex",
                "[0:v]scale=480:270,setsar=1[a];[1:v]scale=480:270,setsar=1[b];[a][b]hstack,fps=10,format=yuv420p[v]",
                "-map", "[v]", "-c:v", "libx264", "-crf", "27", "-preset", "veryfast", "-movflags", "+faststart", str(mp4)],
               check=True)
video_b64 = base64.b64encode(mp4.read_bytes()).decode("ascii")

data = {
    "run": run.name, "duration": round(max(T[-1], calls[-1]["t_s"]) - c0, 2), "t0_epoch": t0_epoch,
    "prompts": [{"id": i, "full": p, "short": p.replace(PRE, "[safety preamble] "), "chars": len(p),
                 "preamble": p.startswith(PRE)} for i, p in enumerate(prompts)],
    "segs": segs, "calls": call_rows, "series": series, "holds": holds, "chunks": chunks, "stalls": stalls,
    "station": station,
}
html = (HERE / "viewer_template.html").read_text(encoding="utf-8")
html = html.replace("__DATA__", json.dumps(data, separators=(",", ":"))).replace("__VIDEO__", video_b64)
out = run.parent / f"{run.name}_viewer.html"
out.write_text(html, encoding="utf-8")
print(f"wrote {out} ({os.path.getsize(out) / 1e6:.1f} MB), video {mp4.name} ({os.path.getsize(mp4) / 1e6:.1f} MB); "
      f"{len(prompts)} prompts, {len(segs)} segments, {len(chunks)} chunks, {len(holds)} holds, "
      f"clock anchor {'ok' if off is not None else 'missing (no inference excerpt)'}")
