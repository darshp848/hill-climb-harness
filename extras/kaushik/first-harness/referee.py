"""Claude referee for the Hill Climb relay: reads the table from the station cameras and returns what the rubric scores.

Jev answers one yes/no per call; the referee answers the whole relay state in one call (cables still in the hub,
stack height, whether anything holds the stack, whether a gripper touches a later stage's objects, the 8-ball),
so the harness can gate stages, catch a fallen stack, and say when to call stop.

  .venv/bin/python referee.py --model claude-haiku-5-5 img1.jpg [img2.jpg ...]      # one judgement
  doppler run -p nous -c dev -- .venv/bin/python referee.py --eval                  # score both models on cases.json
"""

import argparse
import base64
import json
import time
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5")

SYSTEM = """You referee a robot task on a table. You see camera images taken at the same moment: the first looks \
straight down from above the table; any others come from cameras on the two robot grippers and see the scene from \
the side, closer up. Report only what the images show. When the views disagree, trust the closest clear view. When \
something is hidden (a cable, a gripper or another object blocks the view), say so in `hidden` and lower \
`confidence`; never guess a pass.

The task, in stages:
1. Unplug: USB cables start plugged into a hub (a USB hub, or the USB ports of a black power strip). A cable counts \
as unplugged when its connector is fully out of the port.
2. Stack: blocks/shapes stacked into one structure. Count layers from the table up: one piece on the table is 1 \
layer, a piece resting on it is 2, and so on. Free-standing means no gripper touches the stack.
3. Balls: ping-pong balls go into a bin; a black ball is the 8-ball."""


class RelayState(BaseModel):
    cables_in_hub: int = Field(description="USB cables whose connectors are still in a hub or strip port")
    cables_removed_since_start: int = Field(description="Compared with the START images: how many cables that were plugged in at the start are now fully out of their ports (0 if no START images or no change)")
    cables_added_since_start: int = Field(description="Compared with the START images: how many ports that were empty at the start now have a cable plugged in (0 if no START images or no change)")
    cables_out: int = Field(description="Cables lying loose that were visibly pulled out (0 if unknown)")
    cable_in_gripper: bool = Field(description="A gripper is holding a cable or its connector right now")
    grip_on: Literal["connector_body", "wire", "nothing", "unclear"] = Field(description="What the gripper nearest the hub is closed on: the hard plastic/metal connector body or its black rubbery strain-relief part (both count as connector_body), the thin soft wire, nothing, or unclear from these views")
    grip_near_port: bool = Field(description="That gripper's fingers are within about 2 cm of the port the connector sits in")
    grip_fix: str = Field(description="If the grip is not on the connector body near the port: one short instruction to the robot to fix it, e.g. 'open the gripper and grip the connector body closer to the port'. Empty if the grip is right.")
    stack_layers: int = Field(description="Height of the tallest stack, in layers (0 = no pieces on the table)")
    stack_touched: bool = Field(description="A gripper is touching or holding a piece of the tallest stack")
    gripper_on_blocks: bool = Field(description="A gripper touches or holds any block/stacking piece")
    gripper_on_balls: bool = Field(description="A gripper touches or holds any ping-pong ball")
    eight_ball_risk: bool = Field(description="The black 8-ball is in a gripper or inside a bin")
    hidden: str = Field(description="What is blocked from view, or empty")
    confidence: Literal["low", "medium", "high"]
    note: str = Field(description="One short sentence: what changed or went wrong")


class Referee:
    def __init__(self, model="claude-haiku-5-5", effort="low"):
        self.client, self.model, self.effort = anthropic.Anthropic(), model, effort

    def read(self, jpegs, start_jpegs=None, crop=None):
        """`jpegs`: this moment's frames, overhead first. `start_jpegs`: the run's first frames, for 'what changed'."""
        content = []
        if crop:  # a zoomed crop of the overhead view (the hub), appended to both moments
            jpegs = list(jpegs) + [_crop(jpegs[0], crop)]
            start_jpegs = list(start_jpegs) + [_crop(start_jpegs[0], crop)] if start_jpegs else None
        if start_jpegs:
            content.append({"type": "text", "text": "START images (the same cameras at the start of the run; the last one is a zoom on the hub):"})
            content += [_img(j) for j in start_jpegs]
            content.append({"type": "text", "text": "NOW images (same cameras, same order):"})
        content += [_img(j) for j in jpegs]
        content.append({"type": "text", "text": "Report the task state NOW."})
        t = time.perf_counter()
        r = self.client.messages.parse(
            model=self.model, max_tokens=2000, system=SYSTEM, output_config={"effort": self.effort},
            messages=[{"role": "user", "content": content}], output_format=RelayState)
        return r.parsed_output, round(time.perf_counter() - t, 2), r.usage


def _crop(jpeg, box):
    """Crop `box` = [x0, y0, x1, y1] (fractions of the image) and enlarge it 2x, as JPEG."""
    import cv2
    import numpy as np
    im = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    h, w = im.shape[:2]
    c = im[int(box[1] * h):int(box[3] * h), int(box[0] * w):int(box[2] * w)]
    return cv2.imencode(".jpg", cv2.resize(c, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))[1].tobytes()


def _img(jpeg):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(jpeg).decode()}}


def evaluate(cases_path, models):
    """Each case: {"name", "images": [...], "truth": {field: value, ...}}; scores only the fields a case states."""
    cases = json.loads(Path(cases_path).read_text())
    for model in models:
        ref, hits, total, secs = Referee(model), 0, 0, []
        print(f"\n== {model}")
        for c in cases:
            state, dt, _ = ref.read([Path(p).read_bytes() for p in c["images"]])
            secs.append(dt)
            got = state.model_dump()
            wrong = {k: (got[k], v) for k, v in c["truth"].items() if got[k] != v}
            hits += len(c["truth"]) - len(wrong)
            total += len(c["truth"])
            print(f"  {c['name']:28s} {dt:5.1f}s  {'ok' if not wrong else 'WRONG ' + str(wrong)}  | {state.note}")
        print(f"  -> {hits}/{total} fields right, median {sorted(secs)[len(secs) // 2]:.1f}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="*", type=Path)
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--cases", default=HERE / "referee_cases.json")
    a = ap.parse_args()
    if a.eval:
        evaluate(a.cases, MODELS)
    else:
        s, dt, u = Referee(a.model).read([p.read_bytes() for p in a.images])
        print(json.dumps(s.model_dump(), indent=2), f"\n{dt}s, {u.input_tokens} in / {u.output_tokens} out")
