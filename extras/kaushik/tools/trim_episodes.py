"""Trim a dhk-collect LeRobot v2.1 dataset to the grasp: keep each episode from a little before the gripper starts
closing to a little after it opens again; drop episodes where it never closes. Writes a new dataset.

  python tools/trim_episodes.py data/station/unplug-v1 data/station/unplug-v1-trim [--arm right] [--pre 1.5] [--post 1.0]

The gripper column is `<arm>_gripper` in the dataset's units (dhk-collect: 100 open .. 0 closed). An episode keeps
the window from `pre` s before its first close (< --closed) to `post` s after its last frame below that. Videos are
re-cut frame-exactly (H.264, the same size and rate), parquet rows renumbered, meta rewritten. The input is untouched.
"""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path)
    ap.add_argument("dst", type=Path)
    ap.add_argument("--arm", default="right")
    ap.add_argument("--closed", type=float, default=60.0, help="gripper below this counts as closing (100 = open)")
    ap.add_argument("--pre", type=float, default=1.5)
    ap.add_argument("--post", type=float, default=1.0)
    ap.add_argument("--min-grasp", type=float, default=0.5, help="drop episodes closed for less than this, s")
    a = ap.parse_args()
    info = json.loads((a.src / "meta/info.json").read_text())
    fps, names = int(info["fps"]), info["features"]["observation.state"]["names"]
    gcol = names.index(f"{a.arm}_gripper")
    vkeys = [k for k, f in info["features"].items() if f["dtype"] == "video"]
    episodes = [json.loads(l) for l in (a.src / "meta/episodes.jsonl").read_text().splitlines()]
    if a.dst.exists():
        shutil.rmtree(a.dst)
    for sub in ["meta", "data/chunk-000"] + [f"videos/chunk-000/{k}" for k in vkeys]:
        (a.dst / sub).mkdir(parents=True)

    kept, lines, total = 0, [], 0
    for ep in episodes:
        i = ep["episode_index"]
        t = pq.read_table(a.src / f"data/chunk-000/episode_{i:06d}.parquet")
        g = np.array(t.column("observation.state").to_pylist())[:, gcol]
        closed = np.where(g < a.closed)[0]
        st = np.array(t.column("observation.state").to_pylist())
        arm = slice(0, 6) if a.arm == "left" else slice(7, 13)
        moved = float(np.degrees(np.ptp(st[:, arm], axis=0)).max())
        if moved < 10:
            print(f"ep {i}: dropped (the {a.arm} arm barely moved: {moved:.0f} deg)")
            continue
        if len(closed) < a.min_grasp * fps:
            print(f"ep {i}: dropped (gripper closed {len(closed) / fps:.1f} s)")
            continue
        s, e = int(max(0, closed[0] - int(a.pre * fps))), int(min(len(g), closed[-1] + int(a.post * fps) + 1))
        n = e - s
        rows = t.slice(s, n).to_pydict()
        rows["frame_index"] = list(range(n))
        rows["timestamp"] = [round(k / fps, 6) for k in range(n)]
        rows["episode_index"] = [kept] * n
        rows["index"] = list(range(total, total + n))
        pq.write_table(pa.table(rows, schema=t.schema), a.dst / f"data/chunk-000/episode_{kept:06d}.parquet")
        for k in vkeys:
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i",
                            str(a.src / f"videos/chunk-000/{k}/episode_{i:06d}.mp4"),
                            "-vf", f"trim=start_frame={s}:end_frame={e},setpts=PTS-STARTPTS", "-an",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps), "-g", str(fps), "-crf", "20",
                            "-movflags", "+faststart",
                            str(a.dst / f"videos/chunk-000/{k}/episode_{kept:06d}.mp4")], check=True)
        lines.append({**ep, "episode_index": kept, "length": n})
        print(f"ep {i}: kept as {kept}, frames {s}..{e} ({len(g) / fps:.1f} s -> {n / fps:.1f} s)")
        total += n
        kept += 1
    info.update(total_episodes=kept, total_frames=total, total_videos=kept * len(vkeys), splits={"train": f"0:{kept}"})
    (a.dst / "meta/info.json").write_text(json.dumps(info, indent=4))
    (a.dst / "meta/episodes.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))
    shutil.copy(a.src / "meta/tasks.jsonl", a.dst / "meta/tasks.jsonl")
    print(f"{kept}/{len(episodes)} episodes, {total} frames ({total / fps:.0f} s) -> {a.dst}")


if __name__ == "__main__":
    main()
