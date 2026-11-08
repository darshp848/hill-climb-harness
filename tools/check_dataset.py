"""Deep checks of LeRobot v2.1 datasets before a fine-tune, beyond Dream's own validate_dataset.

    python tools/check_dataset.py DATASET_OR_PARENT [...] [--tasks plans/hill_climb_v4.yaml] [--videos]

Per dataset folder (a parent of several is searched), per episode:
- meta: info totals == episodes.jsonl; every task in tasks.jsonl; one episodes_stats line per episode;
- table: rows == length; frame_index 0..n-1; timestamp == frame_index / fps; episode_index and task_index right;
  `index` continuous over the dataset; no NaN or inf;
- values (the Metal kit's 14-wide joint layout): joints within +-pi; grippers 0..100 mm; commanded vs measured
  joints within 1 rad (teleop lag); 1.6 s <= length <= 50 s;
- arms: exactly one arm works (its gripper closes); the other is parked (leader takes: constant) or nearly still;
- task ends: an unplug ends with the working gripper commanded closed (holding the plug); every other task ends with
  it open (released);
- duplicates: two episodes with the same length and the same first and last rows;
- with --videos (needs ffmpeg/ffprobe): every camera has exactly `length` frames, and a frozen camera (the leader's
  wrist, by motion) is reported;
- with --tasks PLAN: every task text is listed as prompted by the plan or not.
Exit 1 if any check fails (warnings do not fail). Read only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

UNPLUG = "unplug the USB cable at the end of the hub nearest the right arm"
SIDES = {"left": (slice(0, 6), 6), "right": (slice(7, 13), 13)}
MIN_S, MAX_S = 1.6, 50.0


def datasets(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        p = p.expanduser()
        if (p / "meta" / "info.json").is_file():
            out.append(p)
        else:
            out += sorted(m.parent.parent for m in p.glob("*/meta/info.json"))
    return out


def frames(path: Path) -> int:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                        "stream=nb_read_frames", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return int(r.stdout.strip() or -1)


def motion(path: Path) -> float:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "select='not(mod(n\\,15))',scale=64:36",
                          "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True).stdout
    f = np.frombuffer(raw, np.uint8).reshape(-1, 36, 64, 3).astype(np.float32)
    return float(np.abs(f - f[0]).mean()) if len(f) > 1 else 0.0


def check(ds: Path, videos: bool) -> tuple[list[str], list[str], Counter, list[tuple]]:
    errors, warns, tasks_n, fingerprints = [], [], Counter(), []
    info = json.loads((ds / "meta" / "info.json").read_text())
    fps = float(info["fps"])
    eps = [json.loads(x) for x in (ds / "meta" / "episodes.jsonl").read_text().splitlines() if x.strip()]
    tasks = {t["task_index"]: t["task"] for t in map(json.loads, (ds / "meta" / "tasks.jsonl").read_text().splitlines()) if t}
    stats = [json.loads(x) for x in (ds / "meta" / "episodes_stats.jsonl").read_text().splitlines() if x.strip()]
    name = ds.name
    if info["total_episodes"] != len(eps): errors.append(f"{name}: info total_episodes {info['total_episodes']} != {len(eps)}")
    if info["total_frames"] != sum(e["length"] for e in eps): errors.append(f"{name}: info total_frames mismatch")
    if len(stats) != len(eps): errors.append(f"{name}: {len(stats)} stats lines for {len(eps)} episodes")
    keys = [k for k, f in info["features"].items() if f.get("dtype") == "video"]
    width = info["features"]["observation.state"]["shape"][0]
    next_index = 0
    for e in eps:
        i, n, task = e["episode_index"], e["length"], e["tasks"][0]
        tag = f"{name} ep{i}"
        tasks_n[task] += 1
        if task not in tasks.values(): errors.append(f"{tag}: task not in tasks.jsonl")
        t = pq.read_table(ds / info["data_path"].format(episode_chunk=i // info["chunks_size"], episode_index=i))
        if t.num_rows != n: errors.append(f"{tag}: {t.num_rows} rows, episodes.jsonl says {n}")
        fi = t["frame_index"].to_numpy(); ts = t["timestamp"].to_numpy(); ix = t["index"].to_numpy()
        if not np.array_equal(fi, np.arange(n)): errors.append(f"{tag}: frame_index not 0..n-1")
        if np.abs(ts - fi / fps).max() > 1e-3: errors.append(f"{tag}: timestamps off frame_index/fps")
        if ix[0] != next_index or not np.array_equal(ix, np.arange(ix[0], ix[0] + n)):
            errors.append(f"{tag}: index not continuous (starts {ix[0]}, expected {next_index})")
        next_index = ix[0] + n
        if set(t["episode_index"].to_numpy()) != {i}: errors.append(f"{tag}: episode_index column wrong")
        ti = set(t["task_index"].to_numpy())
        if len(ti) != 1 or tasks.get(ti.pop()) != task: errors.append(f"{tag}: task_index does not name its task")
        s = np.array(t["observation.state"].to_pylist(), dtype=np.float64)
        a = np.array(t["action"].to_pylist(), dtype=np.float64)
        if not (np.isfinite(s).all() and np.isfinite(a).all()): errors.append(f"{tag}: NaN or inf"); continue
        sec = n / fps
        if not MIN_S <= sec <= MAX_S: (errors if sec < MIN_S else warns).append(f"{tag}: {sec:.1f} s long")
        fingerprints.append((n, tuple(np.round(s[0], 5)), tuple(np.round(s[-1], 5)), tag))
        if width < 14:
            continue
        for side, (j, g) in SIDES.items():
            if np.abs(s[:, j]).max() > np.pi + 0.1: errors.append(f"{tag}: {side} joint beyond +-pi")
            if s[:, g].min() < -1 or s[:, g].max() > 101 or a[:, g].min() < -1 or a[:, g].max() > 101:
                errors.append(f"{tag}: {side} gripper outside 0..100")
            lag = np.abs(a[:, j] - s[:, j]).max()
            if lag > 1.0: warns.append(f"{tag}: {side} commanded vs measured up to {lag:.2f} rad")
        rng = {side: float(np.ptp(s[:, j], axis=0).max()) for side, (j, _) in SIDES.items()}
        grips = {side: bool((a[:, g] < 50).any()) for side, (_, g) in SIDES.items()}
        # the working arm: the one whose gripper closes; if both do (a parked leader's grip can be recorded
        # closed), the one that moves more
        working = [sd for sd in SIDES if grips[sd]] or list(SIDES)
        work = max(working, key=lambda sd: rng[sd]); other = "left" if work == "right" else "right"
        if rng[other] > 0.05: warns.append(f"{tag}: the idle {other} arm moves {rng[other]:.2f} rad")
        g_end = a[-1, SIDES[work][1]]
        if task == UNPLUG and g_end >= 50: errors.append(f"{tag}: unplug ends with the {work} gripper open")
        if task != UNPLUG and g_end < 50: errors.append(f"{tag}: '{task[:30]}' ends with the {work} gripper still closed")
        if videos:
            for k in keys:
                v = ds / info["video_path"].format(episode_chunk=i // info["chunks_size"], video_key=k, episode_index=i)
                nf = frames(v)
                if nf != n: errors.append(f"{tag}: {k} has {nf} frames, table {n}")
            leader_cam = f"observation.images.{other}_wrist"
            if rng[other] < 1e-6 and leader_cam in keys:
                m = motion(ds / info["video_path"].format(episode_chunk=0, video_key=leader_cam, episode_index=i))
                # a frozen camera scores ~0; a real parked view 3-12 (people, the other arm); an unfrozen leader ~60
                if m > 20.0: errors.append(f"{tag}: parked {other} arm's wrist camera moves ({m:.1f}): not frozen?")
    return errors, warns, tasks_n, fingerprints


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--videos", action="store_true", help="also count video frames and check frozen cameras (ffmpeg)")
    ap.add_argument("--tasks", help="a plan YAML: report which task texts it prompts")
    args = ap.parse_args(argv)
    dss = datasets(args.paths)
    if not dss:
        print("no datasets found", file=sys.stderr); return 2
    errors, warns, tasks, prints = [], [], Counter(), []
    for ds in dss:
        e, w, t, f = check(ds, args.videos)
        errors += e; warns += w; tasks.update(t); prints += f
        print(f"{ds.name:38s} {sum(t.values()):4d} episodes  {len(e)} errors  {len(w)} warnings")
    seen = defaultdict(list)
    for n, first, last, tag in prints:
        seen[(n, first, last)].append(tag)
    for tags in seen.values():
        if len(tags) > 1: errors.append(f"duplicate episodes: {', '.join(tags)}")
    prompted = set()
    if args.tasks:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from harness.plan import load_plan
        from harness.tasks import plan_prompts
        prompted = set(plan_prompts(load_plan(args.tasks)))
    print(f"\n{sum(tasks.values())} episodes in {len(dss)} datasets:")
    for t, k in tasks.most_common():
        mark = "" if not args.tasks else ("  (prompted by the plan)" if t in prompted else "  (not prompted yet)")
        print(f"  {k:4d}  {t}{mark}")
    for w in warns: print("WARN ", w)
    for e in errors: print("ERROR", e)
    print(f"\n{len(errors)} errors, {len(warns)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
