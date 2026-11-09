# Final harness (branch `final-harness`)

This branch is `main` with `start-pose-live-guard` and `hybrid-harness` merged in, the LiveKit observer from
`codex/hill-climb-livekit`, and two fixes the merge needed. This file records what was kept, what was left out and
why, and how to run a match. The how-to for every flag is in [HARNESS_GUIDE.md](HARNESS_GUIDE.md).

## Run a match

```bash
python -m harness plans/hill_climb_v4.yaml --live --match --detect plans/table_colors.yaml --live-guard
```

`--match` refuses to start (exit 2) with `--budget`, a `manual` / `+manual` verifier, or a stage a person would
answer. It turns the operator console off, reads practice lessons without writing new ones, and records
`match: true` in the manifest. It warns if `--detect ... --live-guard` is missing. Without the live guard, nothing
stops an 8-ball between monitor verdicts.

For base Dream-1 (no fine-tune), use `plans/hybrid_v4.yaml` instead. It has specific colour and position prompts,
monitor-written prompt rewrites, `reset_first` and the "no power" prefix. Drop its cable 3 and 4 stages, which
cost about 18 points a match in the sim:

```bash
python -m harness plans/hybrid_v4.yaml --live --match --skip cable_extra --detect plans/table_colors.yaml --live-guard
```

For the LiveKit points, show the referee `python -m harness.livekit_observer` before the match (guide §6h). It only
subscribes and is never part of the run.

## What's in, and where it came from

| Piece | From | Why it's in |
|---|---|---|
| Runner state machine, `confirm: 2`, monitor verifier, `guard:` with `max_blind_s` | main | The core. A false "done" is a DQ, so every gated stage needs two agreeing verdicts. |
| Local colour detector `--detect` | main | Local, so it answers in milliseconds where the monitor takes seconds. It feeds both guards and draws boxes on the monitor's frames. |
| Live guard `--live-guard` (10 Hz on `/frame/<wrist>.jpg`, 3 frames in a row, sends `/stop` itself) | start-pose-live-guard | The only thing fast enough to stop an 8-ball. See the stress results below. |
| Start-pose gate (`start_pose:` + `plans/start_poses.json`) | start-pose-live-guard | It won't send a prompt from a pose the policy never started from. |
| `decide.py` | start-pose-live-guard | Kept in shadow mode only: it logs what it would do and decides nothing. |
| Stitched history grid + `parallel: 2` (now in `hill_climb_v4.yaml` too) | hybrid-harness | Measured on the station: a verdict every ~4.9 s instead of ~9.7 s. Judge speed is the main lever in the sim. |
| Light frames (`/frame/<cam>.jpg?w=640`, ~35 KB) | hybrid-harness | Dream's round trip went from 2–6 s to ~0.8 s once the harness stopped pulling ~2 MB debug-view trips. |
| Gripper sensors (`GET /arms`) overrule the monitor's "holding" | hybrid-harness | A gripper closed on nothing is not holding a cable, whatever the image seems to show. |
| `rewrite_prompts` (monitor `next_prompt`, 15 s cooldown, max 3 per attempt) | hybrid-harness | It gets base Dream-1 off the wrong object. Use `--no-rewrite` for a fine-tune, which only follows its recorded texts. |
| `prompt_prefix` "no power" sentence | hybrid-harness | Dream refuses cable work without it. **Keep it only while it is literally true** (the station C notes say the strip is unplugged). |
| `reset_first`, auto `/reset` after `SVD did not converge` (max 2) | hybrid-harness | Both grippers start open, and it recovers from Dream's IK error. Now goes through the motion checks (below). |
| `requires_stage`, relative ball stages, `ARCHIVE_WAIT_S` 4 | hybrid-harness | Balls only after the tower passes. "One more ball" stages are counted from the start of that stage. |
| Operator console | hybrid-harness | **Practice only.** Off under `--match`. |
| LiveKit observer | codex/hill-climb-livekit | +10 for showing it to the referee. Read-only. Ported without that branch's plan rewrites. |
| `--match` | new here | It makes the DQ rules something the harness checks, so nobody has to remember them on the day. |

## Fixes the merge needed

- **`/reset` bypassed the motion checks.** hybrid's `_reset_and_wait` posted `/reset` directly, so an operator stop
  or a tripped guard didn't block it. `/reset` moves both arms and can't be cancelled once it starts. It now goes
  through `_motion`, and it is skipped (`reset_skipped` event) while the live guard is armed.
  `tests/test_final_harness.py` covers both.
- Conflicts in `cli.py`, `plan.py` and `runner.py` were additive. `_PLAN_KEYS` is the union of both branches.

## Left out

| Idea | Where | Why not |
|---|---|---|
| `hybrid_v4_long.yaml` as the match plan | hybrid-harness | In the sim it lost 34 of 185 matches to an 8-ball and ended early in 134 of 185. Keep it for practice comparisons. |
| Operator console, `--budget`, `monitor+manual` in a match | hybrid / main | Any human input after go is a DQ. `--match` refuses them. |
| `hill_climb_full` / `hill_climb_sprint` rewrites | codex/hill-climb-livekit | `hill_climb_v4.yaml` and `hybrid_v4.yaml` already follow rubric v4. |
| `decide.py` making decisions | start-pose-live-guard | No match evidence that it beats the plan order yet. Shadow mode first. |
| The LiveKit branch's `required_cameras` plan field | codex/hill-climb-livekit | It needs that branch's plan and frames changes. The observer works without it. |

## Stress results (virtual-clock sim, `experiments/stress_match.py` scenarios, 5 seeds each)

37 scenarios × 5 seeds = 185 matches per config. Each scenario is a table the harness has to cope with: refusals,
frozen or empty cameras, API outages, false "done" verdicts, an 8-ball near the bin, a knocky tower, and so on.
"fast" means a judge call takes 4–6 s; otherwise 6.5–13 s (Haiku as measured). "final" is this merge with
`--live-guard` and gripper sensors.

| config | mean pts | lost to DQ | lost to 8-ball | mean balls |
|---|---|---|---|---|
| main, v4 plan | 60.5 | 4 | 0 | 0.00 |
| hybrid, hybrid plan | 61.2 | 4 | 1 | 0.09 |
| hybrid, hybrid plan, fast | 70.5 | 4 | **28** | 3.01 |
| hybrid, long plan, fast | 63.2 | 5 | **34** | 3.38 |
| **final, v4 plan, fast** | 66.6 | 6 | **0** | 2.37 |
| **final, hybrid plan, fast** | **69.8** | 4 | **0** | 2.90 |

- **The live guard is what makes a fast judge safe.** Without it, a fast judge reaches the balls and loses 28 of
  185 matches to the 8-ball. With it, none are lost, at a cost of 0.7 points on average.
- At Haiku's real latency, the live guard and the per-poll guard score the same (`guard_v4` and `guard_v4_live`
  match row for row). Neither reaches the ball stage, so there is no 8-ball to stop.
- The 4 DQs every config shares are all `reach_across` (see below).
- **Open risk, v4 plan with a fast judge:** in 2 matches (`false_done_unplug` and `false_done_all`, same seed),
  two false "cable out" verdicts landed seconds apart and passed `confirm: 2`. The robot then touched a block.
  With two calls in flight, the confirming verdicts are close in time and can share the same error. A minimum gap
  between confirming verdicts would close this. Not done yet.

**This branch as committed**, at Haiku's real latency (6.5–13 s a call), with `--live-guard` and gripper sensors,
on 8 of the scenarios (40 matches each):

| scenario | main, v4 | this branch, v4 | hybrid, hybrid plan | this branch, hybrid plan |
|---|---|---|---|---|
| happy | 74.2 | 75.4 | 74.4 | 75.6 |
| realistic | 47.6 | 57.6 | 60.0 | 59.2 |
| realistic_faults | 44.6 | 46.4 | 45.6 | 44.0 |
| calibrated_base | 35.4 | 32.6 | 49.4 | 54.0 |
| calibrated_base_faults | 28.4 | 36.2 | 31.8 | 31.8 |
| knocky_tower | 58.0 | 61.6 | 59.8 | 63.2 |
| black_heavy_lagged | 89.2 | 90.4 | 90.0 | 87.8 |
| unplug_never | 0 | 0 | 0 | 0 |

No matches were lost in either config. The stitched, two-in-flight judge in the v4 plan is worth about +10 on
`realistic`. On `calibrated_base` (base Dream-1's real unplug rate), the hybrid plan beats the v4 plan by about 20
points.

`hybrid_v4.yaml` needs `--skip cable_extra` in a match. Without it, the cable 3 and 4 stages take about 90 s, and
the same 8 scenarios score 33.5 instead of 52.0.

## Not solved by the harness

- **Stuck cable / never unplugs:** 0–10 points in every config. Base Dream-1 unplugs about 1 try in 6
  (TELEOP_PLAN). The sim's default of 0.8 per try is too optimistic, so treat the `calibrated_*` rows as the
  realistic ones. A fine-tune for the unplug is worth more than any harness change.
- **`reach_across`:** the robot touches a later-stage object early and gets a DQ in every config (4 of 5 seeds).
  The guard sees it only after it happens.
- **Judge latency:** with Haiku at 6.5–13 s a call, even two calls in flight rarely reach the ball stage. Balls
  showed up only at 4–6 s a call.
