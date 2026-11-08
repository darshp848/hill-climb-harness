"""Watch the illegal-ball guard on the station's live wrist cameras, without a match: read only.

It polls GET /frame/<camera>.jpg (the station add-on, station_addons/sitecustomize.py) at --hz, runs the same judge
the live guard uses (TableDetector.guard) per camera, and prints each answer change, plus WOULD TRIP when a camera
gives --confirm yes answers in a row on new frames, as the live guard would (it would POST /stop there; this tool
sends nothing). With --arms it also prints each gripper's opening from GET /arms.

    # on the station (or through ssh -L 8091:localhost:8091), with the agent started with the add-on:
    PYTHONPATH=~/hill-climb-harness/station_addons dreamcli robot up --view --view-port 8093
    python tools/guard_watch.py --arms --save-dir guard_watch/

Then, with no run going and the arms still: hold the white ball between each gripper's fingertips (expect WOULD
TRIP within ~0.3 s), lay it on the table near the fingers (expect no), and the same with a black ball if there is
one (a black ball touching the fingers is a known miss: expect no). Ctrl+C prints a summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.detect import TableDetector, annotate, detect  # noqa: E402
from harness.live_guard import LiveFrames  # noqa: E402


def arms(base: str) -> str:
    try:
        with urllib.request.urlopen(f"{base}/arms", timeout=0.3) as r:
            data = json.load(r)
        return " ".join(f"{k}:{v['gripper']:.2f}" for k, v in sorted(data.items()))
    except Exception as exc:  # noqa: BLE001 - the add-on may not be loaded
        return f"(no /arms: {exc})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--station", default="http://127.0.0.1:8091")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "plans" / "table_colors.yaml"))
    ap.add_argument("--cameras", default="wrist_l,wrist_r")
    ap.add_argument("--hz", type=float, default=10.0)
    ap.add_argument("--confirm", type=int, default=3, help="yes answers in a row on new frames that trip (live default 3)")
    ap.add_argument("--arms", action="store_true", help="also print the grippers' opening from GET /arms")
    ap.add_argument("--save-dir", help="save every yes frame, with the detector's boxes drawn")
    a = ap.parse_args(argv)

    det = TableDetector.from_file(a.config)
    cams = tuple(c.strip() for c in a.cameras.split(",") if c.strip())
    src = LiveFrames(a.station, cams)
    found = src.probe()
    if not found:
        print(f"no frames from {a.station}/frame/<camera>.jpg for {cams}: is the agent up with the add-on? "
              f"({src.last_error})", file=sys.stderr)
        return 1
    print(f"watching {found} at {a.hz:g} Hz, trip after {a.confirm} yes in a row; Ctrl+C to stop")
    save = Path(a.save_dir) if a.save_dir else None
    if save:
        save.mkdir(parents=True, exist_ok=True)
    last_sig: dict[str, str] = {}
    last_ans: dict[str, object] = {}
    run: dict[str, int] = {c: 0 for c in cams}
    stats = {c: {"looks": 0, "yes": 0, "trips": 0} for c in cams}
    period = 1.0 / a.hz
    try:
        while True:
            t = time.monotonic()
            for cam, jpeg in src.grab().items():
                sig = hashlib.sha1(jpeg).hexdigest()
                if sig == last_sig.get(cam):
                    continue                      # the same frame again is not a new look (as in the live guard)
                last_sig[cam] = sig
                ans, why = det.guard({cam: jpeg})
                stats[cam]["looks"] += 1
                if ans is True:
                    stats[cam]["yes"] += 1
                    run[cam] += 1
                    if save:
                        name = f"{time.strftime('%H%M%S')}_{int(time.time() * 1000) % 1000:03d}_{cam}.jpg"
                        (save / name).write_bytes(annotate(jpeg, detect(jpeg, det.cfg)))
                    if run[cam] == a.confirm:
                        stats[cam]["trips"] += 1
                        extra = f"  grippers {arms(a.station)}" if a.arms else ""
                        print(f"{time.strftime('%H:%M:%S')}  {cam}: WOULD TRIP ({why}){extra}", flush=True)
                elif ans is False:
                    run[cam] = 0
                if ans != last_ans.get(cam):
                    extra = f"  grippers {arms(a.station)}" if a.arms else ""
                    print(f"{time.strftime('%H:%M:%S')}  {cam}: {ans}  {why}{extra}", flush=True)
                    last_ans[cam] = ans
            time.sleep(max(0.0, period - (time.monotonic() - t)))
    except KeyboardInterrupt:
        pass
    print("\nsummary:", json.dumps(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
