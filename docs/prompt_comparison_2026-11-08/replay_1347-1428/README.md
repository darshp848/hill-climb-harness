# Replay review and root cause: 13:47–14:28, Sun 8 Nov (last 40 min)

- **Window:** 23 station runs, 20 steps in the replay, 24 min of arm motion, base Dream-1 unless marked FT.
- **Replay:** build it with `tools/replay/build_session_viewer.py` on the 23 runs (the "video" times below are positions in it).
- **Data:** `segments_last40_1427.jsonl`, one row per prompt step.
- **Outcomes:** checked from overhead and wrist frames. The joint log alone can't confirm a result.

## What happened

| Task | Steps | Real outcome |
|---|---|---|
| Unplug | 14 steps in 12 runs | **1 cable fully out**: the 13:57:09 run, the "…and toss it onto the table to the right" prompt (video 5:27). 3 plugs were in at 13:56 and 2 at 14:03. People re-plugged before 14:18. 14:18–14:20 is not confirmed. |
| Balls | 14 steps | **4 in the basket**, all with the right arm: orange front-right 13:53 (34 s, video 3:40); yellow-green right side 14:07 (47 s, video 12:32); green just right of the yellow stick 14:10 (26 s, video 15:12); orange right side 14:12 (25 s, video 16:35). |
| Stacking | 13:49–13:52 run | "put a cube on top of the other cube" → **pink on blue ✓** at 13:50 (video 1:17). The next prompt, "stack the blocks and sticks as high as possible" (102 s), **took it apart**. |
| Stacking | 14:03–14:07 run | "put the pink cube on top of the blue cube in front of the robot" → **pink on blue ✓** (80 s). Then "put the other blue cube on top of the pink cube" → **blue/pink/blue, 3 high ✓**, the best result of the day; it stood about 30 s (video 11:27). The **same prompt was sent again** at 14:06:09; Dream picked the top cube back off, and **all three were apart by 14:06:25** (video 11:41–11:57). |
| Stacking | 14:20–14:28 | "put the pink cube on top of the blue cube…" (79 s) ✗. The FT "put a block on top of the stack" (155 s, 5 steps with resets) ✗. "put one blue cube on top of the other blue cube" (81 s) ✗. |

## Why it performed poorly (ranked by impact; evidence from this window)

1. **Successes were undone by the next prompt.** Both stacks, including the 3-high tower, were destroyed by the step that followed.
   - Nothing checks "goal met → stop".
   - Re-sending the same prompt, or widening it ("…as high as possible"), makes Dream keep manipulating the finished tower.
   - *This is sequencing, not wording.*
2. **Steps cut short and reworded too fast.**
   - Ball successes took 25–47 s. 6 of the 10 failed ball steps ran 17 s or less (5, 5, 7, 7, 12, 16 and 17 s).
   - The orange ball got 5 different wordings in 3.5 min (14:08:57–14:12:35).
   - Every prompt change restarts Dream's chunk.
3. **Resets sent while holding.** 8 of the 17 "open both grippers and move the arms up" were sent while a gripper held a cube, ball or plug, which dropped it.
4. **Object placement and reach, not wording.**
   - Every ball success was on the right half of the table.
   - Balls left of the power strip or between the basket and the strip failed under 6 different wordings.
   - Left-arm prompts went 0 of 2 here (0 of 4 today).
5. **Cable grasp precision is the model's limit.**
   - 7 of 14 unplug steps ended closed on the wire or on nothing; only 5 ended on the plug body.
   - Dream lifted a plug-width grip 10–23 cm 5 times (versus 4 all morning), but the strip frames confirm only 1 cable fully out. Another lift may have re-grabbed the already-loose cable.
6. **Lost time.**
   - Arms idle between runs: 14 min of the 40.
   - At 14:13–14:16, three fine-tune runs got **no action at all**: "the policy is still starting… a cold one can take 15 minutes". The FT run at 14:22 made no stack either.
   - One run was stopped by a DA-4000 refusal at 13:54:23: a person's arm was over the green ball.
7. **The pull-force add-on does nothing useful yet.** This is `ee-pull-force` (`PAH_PULL_FORCE_N=r:20`, upward, 12 mm travel stop).
   - It fired **70 times**: on every right-gripper close, including empty closes at the home pose (tool 22–29 cm up) and during ball and block runs.
   - Dream's own motion covers the 12 mm in 0.07–0.57 s, so 65 of 70 ended with "moved: out, or nothing held it". That verdict can't tell a pulled plug from an empty gripper.
   - Only 2 ended on the time limit (something held fast) and 3 with "gripper let go".

## Was it the prompts?

**Mostly not.** The prompts that ran here did their job when given time:
- The new plug prompt ("…a plug **standing upright in a port on top of the black power strip**, then close the gripper on that plug and pull it straight up [and toss it onto the table to the right]") got Dream to lift plugs by itself for the first time today. The toss variant produced the one confirmed unplug.
- Short goal prompts built a 2-high and a 3-high stack.

What went wrong was **when prompts were sent**:
- re-sending after success;
- broadening the goal on top of a finished stack;
- steps under 20 s;
- resets while holding;
- objects out of the right arm's reach.

The prompt-specific problems:
- "stack the blocks and sticks as high as possible" sent onto an existing stack took the stack apart.
- Location rewording did not fix reach.
- Left-arm prompts fail.

## What to change for the match

- **Stop on success.** After each step, judge the overhead frame. If the cube is on the cube, or the ball is in the basket, end the run and home without opening near the tower. Never re-send the step that just worked.
- **Stacking script that worked (3 high in about 2.5 min):**
  1. "put the pink cube on top of the blue cube in front of the robot" (about 80 s).
  2. "put the other blue cube on top of the pink cube", **once**.
  3. Stop.
- **Pick and place:** at least 40–60 s per step; no rewording mid-step.
- **Resets:** no "open both grippers…" while a gripper is holding something.
- **Balls:** right arm only, and only balls on the right half. Move the rest within reach between attempts.
- **Unplug:** keep the upright-plug toss prompt. Judge success by counting plugs in the strip on the overhead frame, not by the pull verdict. Gate the pull add-on to unplug steps and raise its travel stop, or turn it off.
- **Model:** base Dream-1. The FT cold-started for 3+ min and produced no success in this window.
- **People:** keep hands out of the camera view during runs, or DA-4000 refusals will stop the run.
