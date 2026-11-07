"""Run full Hill Climb matches against the simulated relay table and score each one with the referee.

The harness runs unchanged (Runner + a match plan) against sim_station.py --scene hill_climb. Its verifier is
an oracle that answers each stage's question from the sim's ground truth (/objects), so the matches test the
harness's decisions and the policy's behaviour, not camera judgement. Each match ends with the referee's
rubric v4 score, DQ and 8-ball status next to what the harness believed.

    python sim_station.py --policy dream --scene hill_climb            # terminal 1
    python experiments/sim_match.py --matches 3 --out ../runs_sim_match  # terminal 2

Sim only: it calls /reset before each match.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.frames import FrameSource  # noqa: E402
from harness.log import RunLog  # noqa: E402
from harness.plan import Stage, load_plan  # noqa: E402
from harness.runner import Runner  # noqa: E402
from harness.station_client import StationClient  # noqa: E402
from harness.verifiers import MonitorVerdict  # noqa: E402
from sim_scenes import hill_climb_table as h  # noqa: E402

CLEAR_M = 0.08          # a TCP this far from an object counts as clear of it
OPEN = 0.6              # gripper opening (0-1) that counts as open


def get(base: str, route: str) -> dict:
    with urllib.request.urlopen(base + route, timeout=5) as resp:
        return json.loads(resp.read())


class OracleVerifier:
    """Answers the full plan's stage questions from sim ground truth. Unknown stages: can't tell."""

    def __init__(self, station_url: str) -> None:
        self.url = station_url
        self.start: dict[str, dict] = {}   # referee report when the stage was first checked

    def _near(self, snap: dict, xyz: list[float]) -> float:
        return min(math.dist(snap["tcp"][side], xyz) for side in ("left", "right"))

    def _open(self, snap: dict) -> bool:
        return all(snap["joints"][side][6] >= OPEN for side in ("left", "right"))

    def check(self, stage: Stage, frames: dict[str, bytes], elapsed_s: float) -> MonitorVerdict:
        snap = get(self.url, "/objects")
        ref, bodies = snap["referee"], snap["bodies"]
        base = self.start.setdefault(stage.name, ref)
        name = stage.name
        if name.startswith("ball_"):
            if ref["eight_in_bin"]:
                return MonitorVerdict(False, 1.0, "oracle: a black 8-ball is in our bin", status="failing",
                                      recovery=0, scene_ok=False)
            done = ref["golf_in_bin"] > base["golf_in_bin"]
            return MonitorVerdict(done, 1.0, f"oracle: {ref['golf_in_bin']} golf balls in bin", scene_ok=True)
        if ref["eight_in_bin"]:   # a reset prompt for the 8-ball runs under the ball stage's name only
            return MonitorVerdict(None, 1.0, "oracle: 8-ball in bin", scene_ok=False)
        tower = ref["tower_in"] * h.INCH
        if name == "unplug_cable":
            done = ref["unplugged"] >= 1
        elif name == "put_cable_down":
            out = [b for b in bodies if b.startswith("plugged_")]
            done = self._open(snap) and all(self._near(snap, bodies[b]) > 0.05 for b in out) and ref["unplugged"] >= 1
        elif name == "watcher_into_place":
            hub = bodies["hub"]
            done = any(0.06 < math.dist(snap["tcp"][s][:2], hub[:2]) < 0.2 and snap["tcp"][s][2] > hub[2] + 0.04
                       for s in ("left", "right"))
        elif name == "plug_loose_cable":
            done = ref["seated"] > base["seated"]
        elif name == "release_before_stack":
            done = self._open(snap) and self._near(snap, bodies["hub"]) > CLEAR_M
        elif name == "stack_two":
            done = tower >= 3 * h.CUBE_HALF and self._open(snap)
        elif name == "raise_stack":
            done = tower >= h.TOWER_BAR_M and self._open(snap)
        elif name == "clear_of_stack":
            top = max((b for b in bodies if b.startswith(("cube_", "cylinder_"))), key=lambda b: bodies[b][2])
            done = ref["stage2_at"] is not None and self._near(snap, bodies[top]) > CLEAR_M
        else:
            return MonitorVerdict(None, 0.0, f"oracle: no rule for {name}")
        return MonitorVerdict(done, 1.0, f"oracle: {name} {'met' if done else 'not met'}",
                              scene_ok=self._open(snap))


def run_match(args: argparse.Namespace, n: int, out: Path) -> dict[str, Any]:
    station = StationClient(args.station, timeout_s=10.0)
    if station.status().get("state") not in ("idle", "error"):
        station.stop()
        station.wait_for_state({"idle", "error"}, 60)
    urllib.request.urlopen(urllib.request.Request(args.station + "/reset", data=b"{}", method="POST",
                                                  headers={"Content-Type": "application/json"}), timeout=60).read()
    station.wait_for_state({"idle"}, 60)
    plan = load_plan(args.plan, dict(v.split("=", 1) for v in args.var))
    log = RunLog(out)
    t0 = time.time()
    result = Runner(plan, station, FrameSource(args.view), {"monitor": OracleVerifier(args.station)}, log).run()
    referee = get(args.station, "/objects")["referee"]
    summary = {"match": n, "wall_s": round(time.time() - t0, 1), "log": str(log.path),
               "harness": {k: v for k, v in asdict(result).items() if k != "stages"},
               "stages": [asdict(s) for s in result.stages], "referee": referee}
    (log.path / "referee.json").write_text(json.dumps(summary, indent=2))
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--station", default="http://127.0.0.1:8091")
    ap.add_argument("--view", default="http://127.0.0.1:8093")
    ap.add_argument("--plan", default=str(Path(__file__).resolve().parents[1] / "plans" / "hill_climb_full.yaml"))
    ap.add_argument("--var", action="append", default=[], metavar="NAME=VALUE")
    ap.add_argument("--matches", type=int, default=1)
    ap.add_argument("--out", default="runs_sim_match")
    args = ap.parse_args()
    status = get(args.station, "/status")
    if not status.get("sim") or "referee" not in get(args.station, "/objects"):
        raise SystemExit("refusing to run: needs sim_station.py --scene hill_climb (this script resets the scene)")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for n in range(1, args.matches + 1):
        print(f"match {n} ...", flush=True)
        s = run_match(args, n, out)
        with (out / "matches.jsonl").open("a") as f:
            f.write(json.dumps(s) + "\n")
        r, hr = s["referee"], s["harness"]
        print(f"  harness ok={hr['ok']} gates={hr['match_gates_passed']} ({hr['reason']}); referee points={r['points']} "
              f"dq={r['dq']} 8ball_in_bin={r['eight_in_bin']} unplugged={r['unplugged']} tower={r['tower_in']} in "
              f"golf={r['golf_in_bin']}", flush=True)


if __name__ == "__main__":
    main()
