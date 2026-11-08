"""Group today's per-prompt segments (segments_<day>.jsonl, from prompt_stats_remote.py) into prompt
families per task and score how well each family executed. Writes families_<day>.json next to it and prints a table.

    python3 -I docs/prompt_comparison_2026-11-08/prompt_families.py docs/prompt_comparison_2026-11-08/segments_20261108.jsonl
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

src = Path(sys.argv[1])
rows = [json.loads(l) for l in open(src)]

# (task, family id, label, regex on the prompt with the safety preamble shown as "[pre] ")
FAM = [
    ("reset", "R1", "open both grippers and move/raise the arms up", r"open both grippers"),
    ("unplug", "U1", "FT trained text: unplug the USB cable at the end of the hub nearest the right arm", r"^unplug the usb cable at the end of the hub"),
    ("unplug", "U2", "right arm only, unplug the rightmost USB cable and put it on the table; keep...", r"right arm only, unplug the rightmost"),
    ("unplug", "U5", "574-char: Keep the other arm back... come in straight on... turn vertical...", r"keep the other arm back"),
    ("unplug", "U6", "grip the plug body of the rightmost cable ... and pull it straight up (no 'hard')", r"grip the plug body of the rightmost cable still plugged into the strip and pull"),
    ("unplug", "U3", "writer default: grip the hard plug body of the rightmost USB cable still plugged... pull", r"^\[pre\] with the right arm, grip the hard plug body of the (rightmost|usb)"),
    ("unplug", "U4", "writer micro-rewrites: open / regrip / hover / lower / close on the plug body", r"^\[pre\] (open the (right )?gripper|close the right gripper|lower the right gripper)"),
    ("unplug", "U7", "Opus: remove the first cable on the right [by pulling its plug straight up], place it right", r"remove the first cable on the right"),
    ("unplug", "U8", "Opus: grip the first cable's plug ... lift it vertically straight up", r"lift it vertically"),
    ("unplug", "U9", "Opus: right gripper WIDE open, move down onto the plug ... close", r"gripper wide open, move down onto the plug"),
    ("unplug", "U10", "Opus: right gripper HALF open, move down onto the plug ... close (+ scripted pull)", r"gripper half open, move down onto the plug"),
    ("put_down", "P1", "put the held cable down on the table to the right ... and open the gripper", r"put the held cable down on the table to the right"),
    ("put_down", "P2", "FT: put the held cable down on the table away from the hub", r"put the held cable down on the table away from the hub"),
    ("put_down", "P3", "open the gripper and lay the cable on the table to the right", r"lay the cable"),
    ("ball", "B3", "two arms in one prompt (left: orange ball, right: ...)", r"with the left arm, pick up the orange golf ball .* with the right arm"),
    ("ball", "B2", "left arm, single ball into the basket", r"^with the left arm, pick up the \w+ golf ball"),
    ("ball", "B1", "right arm, one named ball (+ where) into the white basket/bin", r"^with the right arm, pick up the \w+ golf ball"),
    ("push_tower", "T1", "left arm, push the stacked tower over", r"push the stacked tower"),
    ("stack", "S3", "build a tower on X: put A on it, then B, then C (multi-object)", r"build a tower"),
    ("stack", "S4", "approach-geometry wording (from directly above / eagle's-eye / square block with a hole)", r"directly above|eagle|square block with a hole"),
    ("stack", "S1", "pick up the X cube, lift it high (above the tower) and lower it gently straight down onto Y", r"lift it high"),
    ("stack", "S12", "move the right gripper to the pink cube, close on its body, then lift it high", r"move the right gripper to the pink cube"),
    ("stack", "S5", "With the right arm, pick up the pink cube.", r"^with the right arm, pick up the pink cube\.$"),
    ("stack", "S2", "short goal: put the pink cube on top of a/the blue cube (...)", r"put the pink cube on top of"),
    ("stack", "S9", "put a cube on top of the other cube", r"^put a cube on top of the other cube"),
    ("stack", "S7", "set the cube you are holding flat on top of the other cube and let go", r"set the cube you are holding"),
    ("stack", "S6", "set-up moves: move a blue cube to the middle / put the blue cube down", r"move a blue cube|put the blue cube down"),
    ("stack", "S10", "stand the pink stick upright in the hole of the pink cube", r"stand the pink stick"),
    ("stack", "S8", "rubric goal: stack the blocks and sticks as high as possible", r"stack the blocks and sticks"),
    ("stack", "S11", "FT: put a block on top of the stack", r"put a block on top of the stack"),
]


def RANGE_OF(task):
    return {"ball": (30, 55), "stack": (8, 62), "push_tower": (8, 62)}.get(task, (4.5, 14))


def family(p):
    low = p.lower()
    for task, fid, label, rx in FAM:
        if re.search(rx, low):
            return task, fid, label
    return "other", "X", "unclassified"


fams = defaultdict(lambda: {"segs": []})
for r in rows:
    task, fid, label = family(r["prompt"])
    f = fams[(task, fid)]
    f.update(task=task, fid=fid, label=label)
    f["segs"].append(r)

out = []
for (task, fid), f in fams.items():
    S = f["segs"]
    dur = sum(s["dur"] for s in S)
    # a NEW object grasp in this segment (not just still holding what the previous prompt grabbed)
    new_grasp = [s for s in S if s["obj_holds"] >= (2 if s["start_holding"] else 1)]
    lifted = [s for s in S if s["max_lift_cm"] >= 5]
    released = [s for s in S if s["obj_released"]]
    pulls = [s for s in S if s["scripted_pull_after"]]
    out.append({
        "task": task, "fid": fid, "label": f["label"], "n": len(S), "minutes": round(dur / 60, 1),
        "ckpt": sorted({s["ckpt"].replace("dreamlabs/", "") for s in S}), "arms": "".join(sorted({s["arm"] for s in S})),
        "times": sorted({s["start"][:5] for s in S}),
        "new_grasp": len(new_grasp), "wire_or_empty": sum(s["wire_holds"] for s in S),
        "obj_holds": sum(s["obj_holds"] for s in S), "closures": sum(s["closures"] for s in S),
        "dream_lift5": len(lifted), "released_after_hold": len(released), "scripted_pull_after": len(pulls),
        "started_holding": sum(1 for s in S if s["start_holding"]),
        "end_on_object": sum(1 for s in S if s["end_cmd"] < 40 and RANGE_OF(task)[0] <= s["end_gap"] <= RANGE_OF(task)[1]),
        "end_on_wire_or_empty": sum(1 for s in S if s["end_cmd"] < 40 and s["end_gap"] < 4.5),
        "dream_lift_with_new_grasp": sum(1 for s in S if s["obj_holds"] >= (2 if s["start_holding"] else 1) and s["max_lift_cm"] >= 5),
        "pull_still_holding": sum(1 for s in S if s["scripted_pull_after"] and s["pull_after_gap"] is not None and 4.5 <= s["pull_after_gap"] <= 14),
        "held_then_opened": sum(1 for s in S if s["start_holding"] and s["end_open"]),
        "closures_per_min": round(sum(s["closures"] for s in S) / max(dur / 60, 1e-6), 1),
        "frozen_pct": round(100 * sum(s["frozen_s"] for s in S) / max(dur, 1e-6)),
        "median_first_grasp_s": (sorted(s["first_obj_s"] for s in S if s["first_obj_s"] is not None) or [None])[
            len([s for s in S if s["first_obj_s"] is not None]) // 2],
        "example": S[0]["prompt"][:400],
    })
order = ["unplug", "put_down", "ball", "push_tower", "stack", "reset", "other"]
out.sort(key=lambda o: (order.index(o["task"]), o["times"][0]))
(src.parent / src.name.replace("segments_", "families_").replace(".jsonl", ".json")).write_text(json.dumps(out, indent=1))
for o in out:
    print(f"{o['task']:<10}{o['fid']:<4} n{o['n']:>3} {o['minutes']:5.1f}m {','.join(o['ckpt']):<22} {o['arms']:<2} "
          f"grasp {o['new_grasp']:>2}/{o['n']:<2} wire {o['wire_or_empty']:>2} lift5 {o['dream_lift5']:>2} rel {o['released_after_hold']:>2} "
          f"pull {o['scripted_pull_after']:>2} held0 {o['started_holding']:>2} cl/min {o['closures_per_min']:4.1f} frz {o['frozen_pct']:>2}% "
          f"endObj {o['end_on_object']:>2} endWire {o['end_on_wire_or_empty']:>2} dreamOut {o['dream_lift_with_new_grasp']:>2} "
          f"pullHold {o['pull_still_holding']:>2} heldOpened {o['held_then_opened']:>2} | {' '.join(o['times'])}")
