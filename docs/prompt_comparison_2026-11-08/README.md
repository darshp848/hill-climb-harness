# Prompt vs. execution, by task — Sun 8 Nov, 09:56–13:29 (101 rollouts, 171 prompt steps)

Source: every archived station rollout today (`~/rollouts/zeroshot_20261108T*`).
- Scored by `prompt_stats_remote.py`, run read-only on the station; it moves nothing.
- Grouped by `prompt_families.py`.
- Raw rows: `segments_20261108.jsonl`. Family scores: `families_20261108.json`.

> **Note for `main`:** b1f6e4b defaults the match plans to the fine-tune `dream-1-ft:bb4b9e5f-relay-final`.
> In today's runs the fine-tune made **0 cable grips in about 10 min** (U1/U2 below). Base Dream-1 made every
> grip, unplug, ball drop and stack listed here.

**Follow-up:** [replay_1347-1428/](replay_1347-1428/README.md) reviews the last 40 minutes (13:47–14:28) and asks why
performance is poor, prompts or something else. The replay viewer tools are in [`tools/replay/`](../../tools/replay/README.md).

**Re-run** (from the repo root, with SSH access to the station):

    ssh <station> 'python3 - 20261108' < docs/prompt_comparison_2026-11-08/prompt_stats_remote.py \
        > docs/prompt_comparison_2026-11-08/segments_20261108.jsonl
    python3 docs/prompt_comparison_2026-11-08/prompt_families.py docs/prompt_comparison_2026-11-08/segments_20261108.jsonl

**How "success" is judged.**
- **Outcomes (did it really happen):** the first and last overhead frames of every ball and block run were compared.
  Cable outcomes come from the earlier video reviews (10:55 run, Opus session, last 30 min).
- **Execution proxies (from the 20 Hz joint log):**
  - **Grip on the object:** jaws stop at the object's width. Plug body 4.5–14 mm, versus wire or empty at about 3 mm.
    Ball about 48 mm. Cube 25–55 mm.
  - **Dream lifted it:** the tool rose 5 cm or more while holding the object.
  - **Scripted pull worked:** after the harness's +10 cm `/move`, the jaws were still at plug width (a plug-width
    grip that survives a 10 cm lift means the plug is out of the strip).
  - **Cycling:** gripper closures per minute (higher means more dithering).
  - **Frozen:** percentage of the step during which the arms were frozen.

All runs are base Dream-1 unless they are marked FT (`dream-1-ft:bb4b9e5f-relay-final`).

## Unplug (goal: plug out of the strip)

| Rating | Prompt (short) | Steps / time | Grip at end of step: plug / wire-or-empty | Lifted by Dream | Cables out (confirmed) |
|---|---|---|---|---|---|
| **GOOD (with scripted pull)** | U10 "With the right gripper **half open**, move down onto the plug of the cable still plugged…, close…" + scripted +10 cm `/move` (Opus 11:22–11:37, 12:29–13:04) | 26 / 8.6 min | **15 / 7** (58% plug) | 0 of 26 | 12 scripted pulls, **8 still holding the plug after the lift**; cables 2 and 3 (Opus), and 12:58 |
| MIXED | U7 "Remove the first cable on the right from the power strip [by pulling its plug straight up], then place it on the right side" (Opus 11:10–11:19) | 6 / 4.0 min | 2 / 4 | **2** | **Cable 1 at 11:11:40 by Dream alone**; 0 in the next 4 repeats |
| promising, n=1 | U6 "With the right arm, grip the plug body of the rightmost cable still plugged into the strip and pull it straight up" (10:56) | 1 / 1.0 min | 1 / 0 | held 58 s, **lifted 23 cm** | **1** (the 10:55 run) |
| BAD | U3 writer default "With the right arm, grip the **hard** plug body of the rightmost USB cable still plugged… and pull…" (10:21–11:09) | 13 / 7.3 min | 4 / **7** | 1 | 0 confirmed; 17 wire closures |
| TERRIBLE | U5 574-character "Keep the other arm back… Come in to the plug straight on… turn the cable vertical… two or three at once" (10:34–10:39) | 4 / 3.0 min | 2 / 1 | 0 | 0 in 286 s |
| TERRIBLE | U4 writer micro-rewrites: "Open the right gripper…, regrip / hover / lower / close on the plug body" (10:27–11:00) | 15 / 4.2 min | 6 / 4 | 0 | 0. **9 of 15 were sent while the arm already held the plug**, and the plug was released in 5. Most cycling of the day (6.7 closures/min) |
| TERRIBLE on FT | U1 the FT's trained text "unplug the USB cable at the end of the hub nearest the right arm" (09:57–10:17) | FT: 10 / about 10 min | FT: 0 new grips | 0 | 0. On **base** (09:57, one 32 s run) the same text grasped the plug body (9.6 mm) and lifted it 11 cm |
| TERRIBLE (FT) | U2 "With the right arm only, unplug the rightmost USB cable… keep…" (10:18) | 2 / 1.5 min | 0 / 0 | 0 | 0 |
| n=1 | U8 "…lift it vertically straight up toward the ceiling" | 1 | 0 / 0 | 0 | grasped, then let go |
| n=1 | U9 "With the right gripper **wide** open, move down onto the plug…" + pull | 1 | 1 / 0 | 0 | still holding after the pull; superseded by "half open" |

**Put down** ("Put the held cable down on the table to the right, away from the power strip, and open the gripper.")
- **GOOD when actually holding:** 16 steps. 11 started holding a plug; Dream released it in 7 of those.
- 5 steps were wasted after failed grips (nothing to put down).
- The FT version ("…away from the hub", 2 steps) and "lay the cable…" (1 step) never held anything.

## Golf balls (goal: ball in the white basket)

| Rating | Prompt | Runs | Grip (about 48 mm) + lift + release | In basket |
|---|---|---|---|---|
| **GOOD (if the ball is near the right arm)** | B1 "With the right arm, pick up the <colour> golf ball <where> and drop it into the white basket/bin." | 5 | 3 of 5 (lifted 14–16 cm) | **2 of 5**: orange 11:42 ✓, yellow 11:43 ✓ (both by the right gripper). Green ball in mid-table (12:34, 12:35, 12:36) ✗ ✗ ✗; the third was grasped and lifted but dropped outside the basket |
| BAD (n=1) | B2 "With the left arm, pick up the green golf ball between the two blue cubes…" | 1 | no grip | ✗ |
| TERRIBLE (n=1) | B3 two arms in one prompt ("left arm orange…, and with the right arm…") | 1 | no grip | ✗ |

**Push tower** (T1, "With the left arm, push the stacked tower of blocks over…", 11:50): ✗. The tower was still standing at the end.

## Stacking (goal: one cube resting on another)

| Rating | Prompt | Steps / time | New grip / lifted / released | Stacks seen in end frames |
|---|---|---|---|---|
| **GOOD** | S8 "stack the blocks and sticks as high as possible" (the rubric's own wording) | 6 steps over 3 runs / 4.7 min | 2 / 2 / 2 | **2 of 3 runs**: 12:34 blue on pink ✓; 13:09–13:10 pink on blue ✓ (in the last step of the 13:06 run) |
| **GOOD / MIXED** | S2 "put the pink cube on top of a / the blue cube (next to it / in front of the robot / stacked blue cubes)" | 6 / 5.1 min | **6 / 5 / 6** (most decisive grasping of any block prompt) | **12:13 pink on blue ✓**. At 12:15–12:17 it grabbed a **blue** cube instead and built blue on blue (a stack, but the wrong object). 11:53, 12:18 and 12:21 ✗ |
| BAD | S1 "pick up the X cube…, lift it high (above the tower) and lower it gently straight down onto Y, then open" | 10 / 7.3 min | 3 / 3 / 3 | 0 |
| TERRIBLE | S3 "build a tower on X: put A on it, then B, then C" (90 s each) | 4 / 6.0 min | 2 / 1 / 2 | 0 |
| TERRIBLE | S4 approach geometry: "come down from directly above", "eagle's-eye view", "pink square block with a hole" | 3 / 2.5 min | 0 | 0 |
| TERRIBLE | S7 "set the cube you are holding flat on top of the other cube and let go" | 1 / 0.8 min | 1, then **10.4 closures/min** | 0 |
| TERRIBLE | S10 "stand the pink stick upright in the hole of the pink cube" | 1 | 0 | ✗, and it knocked apart the 12:13 stack |
| works (n=2) | S6 setup moves: "move a blue cube to the middle of the table…", "put the blue cube down on the table and open the gripper" | 2 / 1.1 min | 1 / 1 / 2 | moved and set down as asked |
| BAD (FT) | S11 FT "put a block on top of the stack" | 3 / 5.5 min | 1 (13:18: grabbed a cube, carried it about 110 s) | 0. This was the only time the FT closed on anything today |
| too short | S9 "put a cube on top of the other cube" (13:04, 13:06) | 2 / 0.8 min | 0 | the 13:06 run then switched to S8 and stacked |

## Patterns across tasks

1. **Name the goal, not the motion.**
   - For blocks: "stack the blocks…as high as possible" and "put the pink cube on top of a blue cube" produced every stack.
     The motion-script prompts ("lift it high… lower it gently… then open", "build a tower…then…then", "from directly
     above", "set it flat and let go") produced none.
   - Chained "then" steps: Dream does the first verb and dithers on the rest.
2. **Exception: the cable grasp.** Describing the gripper *opening* helped. "Half open, move down onto the plug"
   ended on the plug body 58% of the time, versus 31% for "grip the hard plug body", whose wire grabs ran 54%.
3. **Dream almost never does the pull.** Only 4 Dream-made lifts with a plug all day: 09:57 (base, FT text), 10:56
   (U6), 11:11 and 11:18 (U7). The other cables came out through the scripted +10 cm `/move`.
4. **Never re-prompt a held grasp.** The writer's "open the right gripper… regrip" rewrites opened good plug grips 5 times.
5. **One arm, one object, within reach.** The two balls next to the right gripper both went in. Mid-table balls, the
   left arm, and the two-arm prompt all failed.
6. **Step length and freezing.** Opus's short steps (15–18 s) spend 31–45% of the time frozen at step starts and
   prompt changes. The harness's 25–90 s steps freeze 6–16%. The arms can't move while frozen.
7. **Fine-tune:** 0 cable grips in about 10 min. One block carried at 13:18, with no stack.

## Caveats

- Sample sizes are small, and humans rearranged the scene between runs. Ball and cube positions differ from run to run.
- Steps differ in length (Opus 15–30 s, harness 25–90 s, block runs 45–90 s).
- Grip classes are proxies: a 5–14 mm gap is "plug body" and about 3 mm is "wire or empty".
  "Still holding after a 10 cm pull" stands in for "cable out".
- New prompts after 13:26 ("…move down onto a plug standing up…", "grab the orange cable") are too few to score yet.
