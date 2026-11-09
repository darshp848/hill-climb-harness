"""Build the start-pose reference for harness.start_pose from LeRobot teleop datasets.

    python tools/start_poses.py OUT.json DATASET_DIR [DATASET_DIR ...]

For every episode, the first frame of each task segment (a change of task_index starts a segment) becomes one
reference start under that task's exact text. observation.state is [left 6 joints, left gripper, right 6 joints,
right gripper] (the Metal/YAM follower layout in meta/info.json); grippers are dropped. Needs pyarrow.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def segment_starts(dataset: Path) -> list[tuple[str, dict]]:
    import pyarrow.parquet as pq  # noqa: PLC0415 - only this tool needs it

    info = json.loads((dataset / "meta" / "info.json").read_text(encoding="utf-8"))
    names = info["features"]["observation.state"]["names"]
    idx = {side: [names.index(f"{side}_joint_{j}") for j in range(6)] for side in ("left", "right")}
    tasks = {}
    for line in (dataset / "meta" / "tasks.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            t = json.loads(line)
            tasks[t["task_index"]] = t["task"]
    out = []
    for f in sorted((dataset / "data").glob("chunk-*/episode_*.parquet")):
        cols = pq.read_table(f, columns=["observation.state", "task_index"]).to_pydict()
        prev, seg = None, 0
        for state, ti in zip(cols["observation.state"], cols["task_index"]):
            if ti != prev:
                start = {side: [round(float(state[i]), 4) for i in ids] for side, ids in idx.items()}
                start["episode"] = f"{dataset.name}/{f.stem}/seg{seg}"
                out.append((tasks[ti], start))
                prev, seg = ti, seg + 1
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    by: dict[str, list[dict]] = {}
    for d in argv[1:]:
        for task, start in segment_starts(Path(d)):
            by.setdefault(task, []).append(start)
    Path(argv[0]).write_text(json.dumps({"by_prompt": by}, indent=1) + "\n", encoding="utf-8")
    for task, starts in by.items():
        print(f"{len(starts):3d}  {task}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
