"""One table of every live match: cloud Batch (gs://), the home cluster (ssh inspiron) and this laptop (local folder).

    python tools/review_runs.py r1-            # job prefix
Per match: where, job, match, wall s, sim s, sim rate since the first snapshot, stage, points, Dream Hz, round trip,
Dream errors. Reads only the live/status.json + timeline.jsonl files the matches publish.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PREFIX = sys.argv[1] if len(sys.argv) > 1 else "r1-"
GCLOUD = shutil.which("gcloud") or "gcloud"
LAPTOP = Path(__file__).resolve().parents[2] / "laptop_runs"
CLUSTER = "/cluster/shared/dev/darsh/projects/hill_climb_sims/results"


def row(where, path, timeline_lines):
    rows = [json.loads(x) for x in timeline_lines if x.strip()]
    if not rows:
        return None
    a, b = rows[0], rows[-1]
    d = b.get("dream") or {}
    dw = b["wall_s"] - a["wall_s"]
    rate = round((b["sim_t"] - a["sim_t"]) / dw, 2) if dw > 0 and b.get("sim_t") is not None else None
    parts = Path(path).parts
    return (where, parts[-5] if len(parts) >= 5 else "?", parts[-3], b["wall_s"], b.get("sim_t"), rate, b.get("stage"),
            b.get("points"), d.get("call_hz"), d.get("rtt_p50_s"), d.get("errors"))


out = []
listing = subprocess.run([GCLOUD, "storage", "ls", f"gs://hill-climb-sims-results/runs/{PREFIX}*/**/live/timeline.jsonl"],
                         capture_output=True, text=True)
for url in listing.stdout.split():
    text = subprocess.run([GCLOUD, "storage", "cat", url], capture_output=True, text=True).stdout
    r = row("cloud", url.replace("gs://", "/"), text.splitlines())
    if r:
        out.append(r)
ssh = subprocess.run(["ssh", "inspiron", f"for f in $(find {CLUSTER} -path '*{PREFIX}*/live/timeline.jsonl'); do "
                      f"echo \"### $f\"; cat \"$f\"; done"], capture_output=True, text=True)
for chunk in ssh.stdout.split("### ")[1:]:
    path, _, body = chunk.partition("\n")
    r = row("cluster", path, body.splitlines())
    if r:
        out.append(r)
for f in LAPTOP.glob(f"{PREFIX}*/**/live/timeline.jsonl"):
    r = row("laptop", str(f), f.read_text(encoding="utf-8").splitlines())
    if r:
        out.append(r)
print(f"{'where':8}{'job':18}{'match':14}{'wall':>6}{'sim':>7}{'rate':>6}  {'stage':16}{'pts':>4}{'Hz':>6}{'rtt':>6}{'err':>4}")
for r in sorted(out, key=lambda r: (r[0], r[1], r[2])):
    print(f"{r[0]:8}{r[1][:17]:18}{r[2][:13]:14}{r[3]:>6}{str(r[4]):>7}{str(r[5]):>6}  {str(r[6])[:15]:16}{str(r[7]):>4}"
          f"{str(r[8]):>6}{str(r[9]):>6}{str(r[10]):>4}")
