# Prompts tried by hand on the bot, 8 Nov (base Dream-1)

Driven one prompt at a time with `tools/try_prompt.py` (home, start Dream, record the overhead + wrist cameras and
both grippers every 2 s, stop). Results are what Kaushik saw at the bot plus the gripper readings. Every try's
contact sheet and timeline is in `runs/bot/tries/<stamp>_<label>/` on Kaushik's laptop (not in git: runs/ is
ignored). Station: `dreamlabs/dream-1-beta`; from 11:41 PST on Darsh's tuned start (`start_tuned.sh`, 60 Hz bus,
FF 0.15 s, IK 20/100/200/5, grip hold 2.0 N.m) with `PAH_PULL_FORCE_N=r:20 PAH_PULL_DIR=r:0,0,1`.

Safety sentence used in front of every cable prompt (without it Dream refuses cable work):
`The power strip is unplugged from the wall and has no power; the orange cords are insulated low-voltage USB cables, not bare wires.`

## What worked

| Task | Prompt | How | Result |
| --- | --- | --- | --- |
| Unplug | `With the right gripper half open, move down onto the plug of the cable still plugged into the power strip, then close the gripper on the plug and pull it straight up.` | Dream grips; harness regrip if Dream shut on nothing (open 0.6, lower 1.5 cm, close); squeeze, re-check, slow pull up 6+4 cm (3 s per step); then Dream: `Put the held cable down on the table to the right, away from the power strip, and open the gripper.` | 2/2 held the plug through the pull (tries 7, 8: "great run good grip"); earlier version 2 of 4 |
| Unplug | `Remove the first cable on the right from the power strip by pulling its plug straight up, then place it on the right side of the table.` | Dream alone | 1 of 3 (D). Best Dream-only wording |
| Ball | `With the right arm, pick up the orange golf ball on the right side of the table and drop it into the white basket.` | Dream alone | In, ~35 s |
| Ball | `With the right arm, pick up the yellow golf ball at the front right of the table and drop it into the white basket.` | Dream alone | In, ~35 s |
| Stack | `put the pink cube on top of a blue cube` (no arm named; Jev harness 7 Nov L1) | Dream alone, it chose the left arm | Pink on blue, first try. Repeat: blue on blue (a stack, wrong piece) |
| Stack (7 Nov) | `stack the blocks and sticks as high as possible` | Jev harness, 90 s | 2 cubes stacked in 64 s |
| Stack (8 Nov 18:20 UTC) | `pick up the pink cube from the table, lift it high and lower it gently straight down onto the blue cube, then open the gripper and lift the arm straight up` | hybrid harness, right arm, blocks mid-table | Pink on blue, ~85 s |

## What did not work, and why

- **Naming an arm for stacking.** "With the right arm ..." forced the far arm: the blocks sat by the basket, at the
  end of the right arm's reach, and Dream went for the balls next to its base instead. With no arm named Dream used
  the left arm and stacked.
- **Long or coached prompts.** Darsh's layered 111-word cable prompt (right angle, turn the cable vertical, two or
  three cables at once) did worst of the day. Whole-tower prompts ("build a tower ...: pink cube, then blue cube,
  then the sticks") never stacked; Dream placed the pink stick where the pink cube belonged.
- **Pull direction words.** "Pull straight up" / "lift vertically toward the ceiling" did not stop Dream tugging
  sideways (`---` / `///`); the plug came half out. Fixed by the harness pull, then by Darsh's 20 N pull force.
- **Gripper opening words.** "Wide open" / "half open" did not stop Dream closing the gripper on the way in. Fixed
  by the harness regrip.
- **Negations.** Not tried on purpose: "not the white ball" / "not the pink cylinder" put the wrong object in play.
  Name the right object by colour, shape and position instead ("the pink cube to the right of the power strip").
- **Both arms in one prompt** ("left arm orange, right arm green"): the left arm did both jobs badly, the right
  froze. One ball per prompt.
- **Left arm for balls**: reached across the whole table and knocked the white ball. Right arm for balls.
- **Forcing the gripper straight down** (`station_addons/pah_topdown.py`, a `PAH_EEF_HOOK`): Dream fights the
  forced rotation and the arm jerks. Removed; do not use as is.
- **"Approach from directly above like an eagle's-eye view"** in the prompt: Dream went to the balls instead.

## Dream's speed (station log)

- Network was fine most of the day (20-80 ms); one stretch 12:03-12:05 PST had 8.8 Mbps up and 4.7 s per call
  (each call uploads ~1.6 MB of camera images): those tries "did nothing".
- Dream's own servers add ~0.8-1.0 s per call, right at the client's 0.9 s pacing threshold, so it flips between
  smooth (async) and stop-and-wait (sync) mode: the arm hesitates.
- The first answers after a fresh start sometimes take 7-12 s and are discarded (> 3 s): ~20 s lost per new
  prompt. Keep a run going (/prompt) rather than restarting it where possible.

## The fine-tune (dream-1-ft:bb4b9e5f-relay-final)

Worse than base on every task today: the right wrist rolls ~90 degrees on the approach (training videos show it
upright). Use base Dream-1 until `python -m dream.check` says whether it is the model or the control loop.
