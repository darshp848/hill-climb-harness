"""Is the robot starting a stage from a pose the policy was trained to start from?

VLAs lose more to an unfamiliar start pose than to anything else measured in LIBERO-Plus (arXiv 2510.13626,
OpenVLA-OFT: robot initial state -59.9 points, camera -37.4, language -15.6). A fine-tune only saw the starts in
its teleop episodes. The reference (tools/start_poses.py) is the first frame of every task segment in those
episodes, keyed by the exact task text, which v4 prompts match byte for byte.

Joint state comes from GET /arms `positions_rad` (station add-on; works while idle) or else from the newest
debug-view trip's observation `state.left_arm` / `state.right_arm` (only while a run is going).
Grippers are not compared: open or closed both occur at every start in the data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SIDES = ("left", "right")


@dataclass
class PoseCheck:
    status: str                    # ok | far | no_reference | no_state
    distance: dict[str, float]     # per arm: max |joint - reference| (rad) to the nearest reference start
    nearest: str | None            # the reference episode it was nearest to
    source: str | None             # where the joint state came from
    joints: dict[str, list[float]] | None

    def as_event(self) -> dict[str, Any]:
        return {"status": self.status, "distance_rad": {k: round(v, 3) for k, v in self.distance.items()},
                "nearest": self.nearest, "source": self.source,
                "joints": {k: [round(x, 3) for x in v] for k, v in (self.joints or {}).items()}}


class StartPoses:
    """prompt -> list of {"left": [6 rad], "right": [6 rad], "episode": "dataset/episode/segment"}."""

    def __init__(self, by_prompt: dict[str, list[dict[str, Any]]]):
        self.by_prompt = by_prompt

    @classmethod
    def load(cls, path: str | Path) -> StartPoses:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        by = data.get("by_prompt") if isinstance(data, dict) else None
        if not isinstance(by, dict):
            raise ValueError(f"{path}: expected {{'by_prompt': {{prompt: [start, ...]}}}}")
        return cls(by)

    def nearest(self, prompt: str, joints: dict[str, list[float]]) -> tuple[dict[str, float], str | None]:
        """Per-arm distance to the reference start that is nearest overall (the larger of the two arm distances)."""
        best: tuple[float, dict[str, float], str | None] | None = None
        for ref in self.by_prompt.get(prompt, []):
            d = {s: _linf(joints[s], ref[s]) for s in SIDES if s in joints and s in ref}
            if not d:
                continue
            worst = max(d.values())
            if best is None or worst < best[0]:
                best = (worst, d, ref.get("episode"))
        return (best[1], best[2]) if best else ({}, None)

    def check(self, prompt: str, joints: dict[str, list[float]] | None, tolerance_rad: float,
              source: str | None = None) -> PoseCheck:
        if prompt not in self.by_prompt:
            return PoseCheck("no_reference", {}, None, source, joints)
        if not joints:
            return PoseCheck("no_state", {}, None, source, joints)
        dist, ep = self.nearest(prompt, joints)
        if not dist:
            return PoseCheck("no_state", {}, None, source, joints)
        status = "ok" if max(dist.values()) <= tolerance_rad else "far"
        return PoseCheck(status, dist, ep, source, joints)


def _linf(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    return max((abs(float(a[i]) - float(b[i])) for i in range(n)), default=0.0)


def joints_from_arms(arms: dict[str, Any]) -> dict[str, list[float]] | None:
    """{side: [rad]} from GET /arms when the add-on reports positions_rad."""
    out = {}
    for side in SIDES:
        q = (arms or {}).get(side, {}).get("positions_rad") if isinstance((arms or {}).get(side), dict) else None
        if isinstance(q, list) and q:
            out[side] = [float(x) for x in q]
    return out or None


def joints_from_observation(obs: dict[str, Any] | None) -> dict[str, list[float]] | None:
    """{side: [rad]} from a debug-view trip observation (`state.left_arm`, `state.right_arm`)."""
    out = {}
    for side in SIDES:
        q = (obs or {}).get(f"state.{side}_arm")
        if isinstance(q, list) and q and all(isinstance(x, (int, float)) for x in q):
            out[side] = [float(x) for x in q]
    return out or None
