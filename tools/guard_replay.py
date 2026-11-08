"""Replay recorded wrist video through the illegal-ball guard and count false alarms: offline, read only.

For a LeRobot v2.1 dataset (e.g. ~/hackathon_data/plug-unplug-v1), every Nth frame of each wrist camera (N = fps /
--hz, so 10 Hz like the live guard) is JPEG-encoded and judged by TableDetector.guard. It counts yes answers and
trips (--confirm yes in a row). In takes where nobody held an illegal ball, every yes is a false alarm. Yes frames
are saved with the detector's boxes drawn, to look at.

    python tools/guard_replay.py ~/hackathon_data/plug-unplug-v1 --episodes 39-41 --out guard_replay/

Needs av (pip install av) to decode the videos.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.detect import TableDetector, annotate, detect  # noqa: E402

CAMERAS = {"left_wrist": "wrist_l", "right_wrist": "wrist_r"}


def episodes_arg(text: str | None, all_eps: list[int]) -> list[int]:
    if not text:
        return all_eps
    out: set[int] = set()
    for part in text.split(","):
        a, dash, b = part.partition("-")
        last = int(b) if b else (max(all_eps) if dash else int(a))
        out |= set(range(int(a), last + 1))
    return [e for e in all_eps if e in out]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset")
    ap.add_argument("--episodes", help="e.g. 39-41, 39-, 3,7 (default: all)")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "plans" / "table_colors.yaml"))
    ap.add_argument("--hz", type=float, default=10.0)
    ap.add_argument("--confirm", type=int, default=3)
    ap.add_argument("--out", help="save yes frames here (at most 20 per camera and episode)")
    a = ap.parse_args(argv)
    import av  # noqa: PLC0415 - only this tool needs it

    ds = Path(a.dataset).expanduser()
    info = json.loads((ds / "meta/info.json").read_text())
    eps = [json.loads(line)["episode_index"] for line in (ds / "meta/episodes.jsonl").read_text().splitlines() if line]
    step = max(1, round(info["fps"] / a.hz))
    det = TableDetector.from_file(a.config)
    out = Path(a.out) if a.out else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    total = {"looks": 0, "yes": 0, "trips": 0}
    for ep in episodes_arg(a.episodes, eps):
        for cam, role in CAMERAS.items():
            key = f"observation.images.{cam}"
            if key not in info["features"]:
                continue
            path = ds / info["video_path"].format(episode_chunk=ep // info.get("chunks_size", 1000), video_key=key,
                                                  episode_index=ep)
            looks = yes = trips = run = saved = 0
            t0 = time.perf_counter()
            with av.open(str(path)) as c:
                for i, f in enumerate(c.decode(video=0)):
                    if i % step:
                        continue
                    buf = io.BytesIO()
                    f.to_image().save(buf, "JPEG", quality=85)
                    jpeg = buf.getvalue()
                    ans, why = det.guard({role: jpeg})
                    looks += 1
                    if ans:
                        yes += 1
                        run += 1
                        trips += run == a.confirm
                        if out and saved < 20:
                            (out / f"ep{ep}_{cam}_{i:05d}.jpg").write_bytes(annotate(jpeg, detect(jpeg, det.cfg)))
                            saved += 1
                        print(f"  yes ep{ep} {cam} {i / info['fps']:.1f}s: {why}")
                    else:
                        run = 0
            ms = 1000 * (time.perf_counter() - t0) / max(looks, 1)
            print(f"ep{ep} {cam}: {looks} looks, {yes} yes, {trips} trips ({ms:.0f} ms/look)", flush=True)
            total["looks"] += looks
            total["yes"] += yes
            total["trips"] += trips
    print("total:", json.dumps(total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
