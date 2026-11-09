"""Convert a LeRobot v3.0 dataset (what the MakerMods recorder writes) to the v2.1 layout Dream's fine-tune takes.

v3.0 packs many episodes into one parquet and one mp4 per camera; v2.1 wants one of each per episode:
  meta/info.json, meta/episodes.jsonl, meta/tasks.jsonl, meta/embodiment.json (the config validate/upload take)
  data/chunk-000/episode_000000.parquet
  videos/chunk-000/<video key>/episode_000000.mp4      (re-encoded H.264, frame-accurate cut, one frame per row)

Values are copied as recorded (MakerMods: LeRobot degrees, gripper in its own range); see --deg2rad / --grip.
Several inputs become several session folders under OUT, which is how Dream takes a multi-session dataset.

  prism/.venv-dream/bin/python -I tools/v3_to_v21.py data/raw/place_motor --out data/dream/place-motor
  prism/.venv-dream/bin/python -I tools/v3_to_v21.py IN1 IN2 --out OUT --drop 3,7 --role side=overhead
  ... --validate-only OUT                # just run Dream's local checks (no key, nothing sent)
"""

import argparse
import json
import math
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

WORKERS = 4  # ffmpeg jobs at once; the Mac has fallen over on more


def read_v3(src):
    info = json.loads((src / "meta/info.json").read_text())
    if not str(info.get("codebase_version", "")).startswith("v3"):
        raise SystemExit(f"{src}: codebase_version {info.get('codebase_version')!r}, expected v3.x")
    eps = pa.concat_tables([pq.read_table(p) for p in sorted((src / "meta/episodes").rglob("*.parquet"))])
    eps = [{k: v for k, v in r.items() if not k.startswith("stats/")} for r in eps.to_pylist()]
    tasks = {r["task_index"]: r["task"] for r in pq.read_table(src / "meta/tasks.parquet").to_pylist()}
    data = pa.concat_tables([pq.read_table(p) for p in sorted((src / "data").rglob("*.parquet"))])
    data = data.replace_schema_metadata(None)
    return info, sorted(eps, key=lambda e: e["episode_index"]), tasks, data


def video_file(src, info, key, ep):
    return src / info["video_path"].format(video_key=key, chunk_index=ep[f"videos/{key}/chunk_index"],
                                          file_index=ep[f"videos/{key}/file_index"])


def cut(src_mp4, start, frames, fps, out, size):
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = [f"fps={fps}"] + ([f"scale={size}"] if size else [])
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.6f}", "-i", str(src_mp4),
                    "-frames:v", str(frames), "-vf", ",".join(vf), "-fps_mode", "cfr", "-an",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-g", str(fps),
                    "-movflags", "+faststart", str(out)], check=True)


def vec_column(rows, width, deg2rad, grip):
    a = np.asarray(rows, np.float64)
    if deg2rad:
        a[:, :width - 1] = np.deg2rad(a[:, :width - 1])
    if grip:
        lo, hi = grip  # recorded closed, recorded open -> 0..1
        a[:, width - 1] = np.clip((a[:, width - 1] - lo) / (hi - lo), 0, 1)
    return pa.array(a.astype(np.float32).tolist(), pa.list_(pa.float32(), width))


def convert(src, dst, drop=(), roles=None, deg2rad=False, grip=None, size=None):
    info, eps, tasks, data = read_v3(src)
    fps = int(info["fps"])
    vkeys = [k for k, f in info["features"].items() if f["dtype"] == "video"]
    width = info["features"]["observation.state"]["shape"][0]
    awidth = info["features"]["action"]["shape"][0]
    ep_col = np.asarray(data.column("episode_index"))
    for sub in ["meta", "data/chunk-000"] + [f"videos/chunk-000/{k}" for k in vkeys]:
        (dst / sub).mkdir(parents=True, exist_ok=True)

    jobs, ep_lines, total, used_tasks = [], [], 0, set()
    kept = [e for e in eps if e["episode_index"] not in drop]
    for new, ep in enumerate(kept):
        rows = data.filter(pa.array(ep_col == ep["episode_index"])).sort_by("frame_index")
        n = rows.num_rows
        if n != ep["length"]:
            raise SystemExit(f"{src}: episode {ep['episode_index']} has {n} rows, meta says {ep['length']}")
        task_idx = rows.column("task_index").to_pylist()
        used_tasks.update(task_idx)
        table = pa.table({
            "observation.state": vec_column(rows.column("observation.state").to_pylist(), width, deg2rad, grip),
            "action": vec_column(rows.column("action").to_pylist(), awidth, deg2rad, grip),
            "timestamp": pa.array([round(i / fps, 6) for i in range(n)], pa.float32()),
            "frame_index": pa.array(range(n), pa.int64()),
            "episode_index": pa.array([new] * n, pa.int64()),
            "index": pa.array(range(total, total + n), pa.int64()),
            "task_index": pa.array(task_idx, pa.int64()),
        })
        pq.write_table(table, dst / "data/chunk-000" / f"episode_{new:06d}.parquet")
        for k in vkeys:
            jobs.append((video_file(src, info, k, ep), ep[f"videos/{k}/from_timestamp"], n, fps,
                         dst / "videos/chunk-000" / k / f"episode_{new:06d}.mp4", size))
        ep_lines.append({"episode_index": new, "tasks": sorted({tasks[t] for t in set(task_idx)}), "length": n})
        total += n
    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(lambda j: cut(*j), jobs))

    features = {}
    for k, f in info["features"].items():
        f = json.loads(json.dumps(f))
        if f["dtype"] == "video":
            h, w = (int(x) for x in size.split(":")[::-1]) if size else f["shape"][:2]
            f["shape"] = [h, w, 3]
            f["info"] = {"video.fps": fps, "video.height": h, "video.width": w, "video.channels": 3,
                         "video.codec": "h264", "video.pix_fmt": "yuv420p", "video.is_depth_map": False,
                         "has_audio": False}
        features[k] = f
    v2 = {
        "codebase_version": "v2.1", "robot_type": info.get("robot_type", "unknown"),
        "total_episodes": len(kept), "total_frames": total, "total_tasks": len(used_tasks),
        "total_videos": len(kept) * len(vkeys), "total_chunks": 1, "chunks_size": 1000, "fps": fps,
        "splits": {"train": f"0:{len(kept)}"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": features,
    }
    (dst / "meta/info.json").write_text(json.dumps(v2, indent=2))
    (dst / "meta/episodes.jsonl").write_text("".join(json.dumps(l) + "\n" for l in ep_lines))
    (dst / "meta/tasks.jsonl").write_text("".join(json.dumps({"task_index": i, "task": tasks[i]}) + "\n"
                                                  for i in sorted(used_tasks)))
    roles = roles or {}
    emb = {
        "state": {"arm": [0, width - 1], "gripper": [width - 1, width]},
        "action": {"arm": [0, awidth - 1], "gripper": [awidth - 1, awidth]},
        "video": {roles.get(k.split(".")[-1], k.split(".")[-1]): k for k in vkeys},
    }
    (dst / "meta/embodiment.json").write_text(json.dumps(emb, indent=2))
    print(f"{src.name}: {len(kept)} episodes ({len(eps) - len(kept)} dropped), {total} frames at {fps} fps -> {dst}")
    return emb


def validate(root, config):
    from dream import datasets
    rep = datasets.validate_dataset(str(root), config)
    print(rep)
    return rep.ok


def config_of(root):
    root = Path(root)
    found = [root / "meta/embodiment.json"] if (root / "meta/embodiment.json").exists() else \
        sorted(root.glob("*/meta/embodiment.json"))
    cfgs = {p.read_text() for p in found}
    if len(cfgs) != 1:
        raise SystemExit(f"{root}: expected one shared embodiment.json, found {len(cfgs)}")
    return json.loads(cfgs.pop())


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="*", type=Path, help="v3.0 dataset dirs (each has meta/info.json)")
    ap.add_argument("--out", type=Path, help="output dir; one input -> the session itself, several -> OUT/<name>/")
    ap.add_argument("--drop", default="", help="episode indices to leave out, e.g. 3,7 (single input only)")
    ap.add_argument("--role", action="append", default=[], help="camera role name, e.g. side=overhead")
    ap.add_argument("--deg2rad", action="store_true", help="joints recorded in degrees -> radians (not the gripper)")
    ap.add_argument("--grip", default=None, help="CLOSED,OPEN recorded gripper values -> 0 closed .. 1 open")
    ap.add_argument("--size", default=None, help="resize video, ffmpeg W:H, e.g. 320:240")
    ap.add_argument("--validate-only", type=Path, default=None)
    a = ap.parse_args()
    if a.validate_only:
        raise SystemExit(0 if validate(a.validate_only, config_of(a.validate_only)) else 1)
    if not a.inputs or not a.out:
        ap.error("give inputs and --out")
    drop = {int(x) for x in a.drop.split(",") if x}
    if drop and len(a.inputs) > 1:
        ap.error("--drop works on one input at a time")
    roles = dict(r.split("=", 1) for r in a.role)
    grip = tuple(float(x) for x in a.grip.split(",")) if a.grip else None
    if grip and math.isclose(grip[0], grip[1]):
        ap.error("--grip closed and open must differ")
    for src in a.inputs:
        dst = a.out if len(a.inputs) == 1 else a.out / src.name.lower().replace("_", "-")[:64]
        cfg = convert(src.resolve(), dst, drop, roles, a.deg2rad, grip, a.size)
    raise SystemExit(0 if validate(a.out, cfg) else 1)
