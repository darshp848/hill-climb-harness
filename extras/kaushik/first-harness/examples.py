"""Checker examples from recorded demos: each episode's last frames are "done", its first frames "not done".

Writes examples/<task slug>/{done,not_done}/<episode>_<camera>.jpg, the folder a plan's `examples` names.
Holds out the last `--holdout` episodes so they can test the checker (`--test`) on demos it has not seen.

  python examples.py ../data/dream/place-motor --camera overhead=observation.images.side
  doppler run -p typesafe -c dev -- python examples.py ../data/dream/place-motor --test   # Jev on held-out eps
"""

import argparse
import json
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def grab(mp4, t, out=None):
    """One frame at `t` seconds (negative = from the end), as JPEG bytes, 320 wide."""
    pre = ["-sseof", f"{t}"] if t < 0 else ["-ss", f"{t}"]
    jpg = subprocess.run(["ffmpeg", "-loglevel", "error", *pre, "-i", str(mp4), "-frames:v", "1",
                          "-vf", "scale=320:-2", "-q:v", "3", "-f", "mjpeg", "-"], capture_output=True, check=True).stdout
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(jpg)
    return jpg


def episodes(ds):
    for line in (ds / "meta/episodes.jsonl").read_text().splitlines():
        e = json.loads(line)
        yield e["episode_index"], e["tasks"][0]


def video(ds, key, ep):
    return ds / "videos/chunk-000" / key / f"episode_{ep:06d}.mp4"


def build(ds, keys, holdout, out_root):
    eps = list(episodes(ds))
    train = eps[:-holdout] if holdout else eps
    for ep, task in train:
        for role, key in keys.items():
            d = out_root / slug(task)
            grab(video(ds, key, ep), 0.3, d / "not_done" / f"{ep:03d}_{role}.jpg")
            grab(video(ds, key, ep), -0.3, d / "done" / f"{ep:03d}_{role}.jpg")
    print(f"examples for {len({t for _, t in train})} task(s) from {len(train)} episodes -> {out_root}")
    return eps[len(train):]


def test(ds, keys, held, out_root, check, use_examples=True):
    """Jev on each held-out episode's first and last frame; prints the threshold that separates them."""
    import os
    import harness
    role, key = next(iter(keys.items()))
    j = harness.JevChecker(os.environ["TYPESAFE_API_KEY"])
    starts, ends = [], []
    for ep, task in held:
        sub = {"prompt": task, "check": check, "examples": str(out_root / slug(task)) if use_examples else ""}
        starts.append(j.ask(sub, [grab(video(ds, key, ep), 0.3)]))
        ends.append(j.ask(sub, [grab(video(ds, key, ep), -0.3)]))
        print(f"held-out ep {ep}: P(done) start {starts[-1]:.2f}  end {ends[-1]:.2f}")
    lo, hi = max(starts), min(ends)
    if lo < hi:
        th = round(lo + 0.6 * (hi - lo), 2)  # leaning toward "not done": a false done costs more
        print(f"separated: not-done max {lo:.2f} < done min {hi:.2f} -> put \"threshold\": {th} in the plan")
    else:
        print(f"NOT separated (not-done max {lo:.2f} >= done min {hi:.2f}): reword `check`, change camera")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset", type=Path, help="a v2.1 session folder")
    ap.add_argument("--camera", action="append", default=[], help="role=video key; default: every camera")
    ap.add_argument("--holdout", type=int, default=4)
    ap.add_argument("--out", type=Path, default=HERE / "examples")
    ap.add_argument("--test", action="store_true", help="ask Jev about the held-out episodes' first and last frames")
    ap.add_argument("--no-examples", action="store_true", help="--test without the example images")
    ap.add_argument("--check", default="Is the task finished?", help="the question, as a plan's `check`")
    a = ap.parse_args()
    info = json.loads((a.dataset / "meta/info.json").read_text())
    keys = dict(c.split("=", 1) for c in a.camera) or \
        {k.split(".")[-1]: k for k, f in info["features"].items() if f["dtype"] == "video"}
    held = build(a.dataset, keys, a.holdout, a.out)
    if a.test:
        test(a.dataset, keys, held, a.out, a.check, not a.no_examples)
