# Last night's work, 7-8 Nov 2026

Written 8 Nov from the session logs, the three repos and the station logs copied to Darsh's laptop. The newest
station log on that laptop ends at 05:22 UTC on 8 Nov, so anything the team ran later is not counted here. All
times are UTC.

## Short version

- The harness got much tougher in simulation. Faults that used to end a match now cost a few points at most.
- We have no proof yet that it works on the real table. No per-stage success numbers exist from real runs.
- The big cloud test (round 1) died at 12:05 when the Dream account ran out of credits. Its results can't be used
  to compare anything.
- Darsh's handwritten strategy notes are coded, but only on branch `cloud-sims`, which is 56 commits ahead of
  `main`. The team needs to decide which branch is the match harness.

## What got built

| Where | What |
|---|---|
| `main` | Stress-test fixes, tower target and `tower_prompts`, local ball and block detector, monitor by Claude subscription, live sim viewers, all 42 teleop takes split and labelled |
| `start-pose-live-guard` | A 10 Hz guard on the wrist cameras that sends `/stop` by itself |
| `hybrid-harness` | The monitor rewrites Dream prompts, shorter whole-task prompts, fixes from station run 3 |
| `hedged-calls` | Several model calls for one decision, first answer wins, plus the OpenRouter judge benchmark |
| `final-harness` | Merge of the best branch ideas, see `FINAL_HARNESS.md` |
| `mac-arm64` | Changes for running the match laptop on Apple Silicon |
| `cloud-sims` | Everything above plus Darsh's notes (`plans/operator_notes.yaml`), Dream at 2 Hz in async mode, prompt levels (`harness/promptgen.py`), GCP Batch jobs, a sim table modelled on the real one, cable-end classifier (`models/cable_end.onnx`), live joint telemetry, learned joint fault detector |
| `codex/dual-arm-experiment` | Sequential against combined two-arm prompts in the sim. Pushed 8 Nov, not merged |
| [follower_code](https://github.com/darshp848/follower_code) `quest-continuity` | Quest teleop fixes, Metal leader/follower |
| [task_simulation](https://github.com/darshp848/task_simulation) | Sims, scenarios, cluster scripts and all result files |

The last full test run on `cloud-sims` (13:07) was 407 passed, 4 failed, 5 skipped. The failures are two cases of
`test_async_pacing_calls_at_2hz_and_physics_keeps_running`, plus `test_cli_transport_sends_the_request_unchanged`
and `test_backend_switch`. Nobody has fixed them yet.

## Results

Keep these three groups apart. The offline sim is scripted, so it tells us the harness logic is sound and says
little about whether a robot can do the task.

### Offline stress sim (simulated, scripted policy, virtual clock)

Points per seed, 4 seeds. "Before" is main at `3055bf3`, "after" is main at `d938644`.

| Scenario | Before | After |
|---|---|---|
| Happy path | 87 74 90 116 | 83 94 95 95 |
| Dream refuses on the first start | 0 0 0 0, match ended | 83 94 95 95 |
| Refused twice | 0 0 0 0, match ended | 78 74 95 90 |
| Action buffer empties mid-run | 10 10 15 15, match ended | 79 94 95 100 |
| SVD did not converge | 10 0 10 10, match ended | 83 84 95 95 |
| Policy keeps holding the unplugged plug | plugged it back in 4 of 4 | never |
| 12 s of corrupt frames | 20 15 20 20 | 83 94 95 95 |
| Arm dies | match ended, then sent `/start` | 15 10 15 15, no `/start` |
| Illegal ball, 6 seeds, frames 2 s old | lost 4 of 6 | lost 2 of 6 with the local detector |
| Unplug brushes a block | DQ 4 of 4 | DQ 4 of 4 |

Full run: 37 scenarios, 5 seeds each, 185 matches per config.

| Config | Mean points | Lost to DQ | Lost to illegal ball | Mean balls |
|---|---|---|---|---|
| `main`, v4 plan | 60.5 | 4 | 0 | 0.00 |
| hybrid plan | 61.2 | 4 | 1 | 0.09 |
| hybrid plan, fast judge | 70.5 | 4 | 28 | 3.01 |
| final, v4 plan, fast judge | 66.6 | 6 | 0 | 2.37 |
| final, hybrid plan, fast judge | 69.8 | 4 | 0 | 2.90 |

A fast judge reaches the balls, and without the live guard it then loses matches to the illegal ball. With the
guard it lost none in 185.

Stage pass counts, simulated, final hybrid config (passed/tried across the 185 matches): first unplug 174/174, put
cable down 156/174, stack_2 157/157, stack_3 149/149, tower_pass 149/149, first ball 104/112. These logs don't keep
stage times, so there is no median time per stage. The unplug rate is a sim assumption of 0.8 per try. Base Dream-1
on the real robot manages about 1 try in 6, so read the `calibrated_base` rows in `FINAL_HARNESS.md` for anything
close to reality.

### MuJoCo with the real Dream-1 API (simulated table, real model)

25 matches on the cluster, excluding one batch with a referee bug.

| Task | Result | Time |
|---|---|---|
| Unplug | a cable came out in 14 of 25 matches | first unplug median 57 s (range 28-269 s), a successful attempt about 27 s |
| Put cable down | 0 of 54 tries | never worked, the policy keeps holding the plug |
| Stack | 0 of 25 tries | |
| Golf balls | never reached | |

3 DQs, all with the hub on the left.

Cloud round 1 (MuJoCo with real Dream) stopped at 12:05. 10 of 28 matches were marked valid, scoring 0-42 points.
Arm B (async 2 Hz plus notes) had no valid matches at all, so the table in `HILL_CLIMB_LOG.md` stays empty. Don't
draw conclusions from it. Six of the invalid laptop-run errors were Dream refusing the plug-in prompt.

### Real station

52 policy runs and 1650 Dream calls between 18:06 on 7 Nov and 05:22 on 8 Nov. The logs don't record whether a run
succeeded, so **we have no real success rate for any task**. Humans on teleop: unplug median 8.5 s (15 demos),
put-down 4.8 s (9 demos).

## Dream latency

From `robots-inference.jsonl`, 1650 calls on the station: round trip median 1115 ms, 90th percentile 2732 ms, worst
8655 ms. Of the median, the model itself takes about 293 ms, the network about 40 ms, and the rest is Dream's
gateway (992 ms in total as logged). One blocking call at a time can't reach 2 Hz, hence async mode and pipelining on
`cloud-sims`. Pipelining doubles Dream usage, so credits go twice as fast.

The monitor is the slow part. Haiku 5.5 takes 6.5-13 s per call. In the 60-decision benchmark, GPT-6.1 Terra through
OpenRouter was the best per-decision judge: 98% correct on done and 97% on the guard at 2.8 s with no reasoning,
100% on both at 3.7 s with low reasoning. Sonnet 5.5 was also 100% but took about 7 s.

We have not compared the harness against plain Dream on the station. See the data list below.

## Dream errors

Station, 18:06 to 05:22, 16 errors. None of them are billing errors.

| Type | Count | Text |
|---|---|---|
| Safety refusal | 4 (19:55, 20:33, 23:01, 23:30) | `DA-4000 BAD_REQUEST (malformed): unsafe instruction` |
| SVD | 4 (23:03, 23:34, 23:50, 03:11) | `LinAlgError: SVD did not converge` |
| Empty action buffer | 4 (19:57, 23:37, 03:10, 03:12) | `action buffer empty for 241 consecutive ticks (12.05 s)` |
| Session replaced | 4 (23:36, 03:11 three times) | `DA-0001 DreamSessionExpired` |

Sim and cloud runs, 8 Nov:

| Type | When | Text |
|---|---|---|
| Key refused | 12:04:48 to the end of the runs, 83 lines | `DA-1000 UNAUTHORIZED (disabled): 'nuck5-dp'` |
| Safety refusal | during round 1, 6 lines | `DA-4000 ... Plugging cords into power strips carries electrical/equipment...` |
| Gateway timeout | night of 8 Nov, 2 mentions | `DA-6000 GATEWAY_FAULT (connection_timed_out)` |

**Did credits run out?** Yes for the 12:04 burst. Darsh saw the account ran out and has topped it up. Dream reports
this as `DA-1000 disabled`, which reads like a revoked key, and account calls such as listing models kept working.
About 28 sessions were open at the time. We never read Dream's billing page from a script, so the exact moment the
balance hit zero isn't known. The earlier errors (03:10-03:12 and before) are a different problem. They are empty
buffers, SVD errors and session swaps, and credits don't explain them.

No HTTP 402 or 429 appears anywhere in the logs.

## Refusals

4 refusals in about 1650 calls on the station. Dream's reasons were "inserting metal or objects into a power strip",
"a person's hand is holding the plug", "unplug cables from a live power strip" and "unplug live bare wires". The
second is a real scene problem, keep hands out of frame. We did not try to trick the filter. Stating the truth in
the prompt (the strip is off the wall, the cords are low-voltage USB) is fine. Plug-in prompts still get refused
in the sim, so test the exact match prompts before the match.

## Tower target and the photo question

`plans/hill_climb_v4.yaml` (`tower_target`) encodes IMG_2697, bottom to top: blue cube, pink cube, blue cube (holes
sideways), yellow square stick, pink round stick. `tower_prompts` names the next piece, for example "put the pink
cube on top of the stack". The order is checked and logged but doesn't block a stage.

**IMG_2699 shows a different tower**: pink, blue, blue, bottom to top. Someone needs to say which one is the target.
Also, Darsh's notes want the last cube's hole facing up, which `main` doesn't encode. `cloud-sims` does.

## Darsh's strategy notes

Handwritten PDF on branch `final-harness` as `docs/decisions.pdf`, typed in `docs/DECISIONS.md`.

"Text only" means `plans/operator_notes.yaml` holds the instruction for the monitor. It goes in the Dream prompt
only on plans with `prompt_technique: true`, which is `hybrid_v4` and not `hill_climb_v4`.

| Note | `main` | `cloud-sims` |
|---|---|---|
| Blocks first, cylinders after | Yes, by stage order | Yes |
| Come down on blocks from above, no overhang | No | Text only |
| Last cube hole up, cylinder upright in the hole | No, all holes sideways | Text, plus `tower_target_last_cube` |
| Next cylinder on top, slowly | No | Text only |
| Push white balls off the table | **No** | `ball_push_white` stage, 1 try, skipped if the path isn't clear. Untested on the robot |
| Coloured balls in the basket, avoid white | Yes | Yes |
| Other arm watches the cables | No | Text only. The v4 and hybrid plans don't position a watcher arm |
| Grip at max strength | Station setting is 1.5 N·m | Same. Going to 2.0 is a manual step in `docs/STATION_TOMORROW.md` |
| Pull 2-3 cables, vertical, lay flat near the hub | No | Text only |
| Plug USB-C by the black part, first port from the left | No plug stage in v4 | Text for `plug_*`, but the v4 plan has no plug stage |
| Detect errors | Generic monitor | `watch_for` per stage |
| Failed twice, move on (3 tries if high value) | **No**, pass-bar stages use `max_attempts: 99` | 2 tries, 3 at 20+ points, except pass-bar stages |

Pass-bar stages still retry until the clock runs out on `cloud-sims`. Moving on from one means touching the next
stage's objects, and the rubric disqualifies that. That is a deliberate exception to the note. Tell us if you want
it the other way.

## Data still to collect on the station

Dataset counts are from the copies on Darsh's laptop (`nuc_data/hackathon_data`, 3 GB, not in git). The newest
dataset there dates from 7 Nov 23:00 Central. Newer recordings on the NUC are not counted.

| Data | Have | Still needed |
|---|---|---|
| Unplug and put-down demos | Quest 40 and 34, leader 15 and 7 | Enough for now. Add the 2-3 cable vertical pull if we want that technique |
| Plug-in demos | 16 | More if plug-in comes back |
| Stack in target order | 2 block demos, none confirmed in order | Record in the chosen order, after settling the photo question |
| Upright stick demos | 8 | More, and on the right tower |
| Golf ball demos | 1 | Many more |
| Watcher-arm pose demos | None found | Record, if we keep the watcher idea |
| Harness runs on the station, per-stage success | None on the laptop. Run 3 at 05:49 is mentioned in `hybrid-harness` commits | Copy logs off the NUC, then run matches and log each stage |
| Real motor torque while pulling a cable | Estimate from `last-run.csv` only. `candump` isn't on the NUC | Run `harness/can_live.py` (receive only), see `docs/STATION_TOMORROW.md` |
| Grip at 2.0 N·m | Not done | Set it, then watch gripper temperature (latches off at 55 C) |
| Station camera frames for detector colours | Tuned on phone photos | Overhead and wrist frames, gripper open and closed around the white ball |
| Monitor latency in a real match | Offline only | Log it during station runs |
| Harness against plain Dream latency | None | Same prompt, both ways, compare `rtt_ms` |
| `quest_collect.py --dry-run` on the NUC | Not run. Held at 02:49 while the team recorded | Run it when the arms are free |
| Black 8-ball photos | None in the photos we have | Take them if black balls will be on the table |
| Block measurements | No record | Measure them, `tower_pass` depends on the 4 inch bar |

## To do, most urgent first

| # | Task | Owner |
|---|---|---|
| 1 | Pick the match branch: `main` or `cloud-sims`. Then merge | |
| 2 | Settle the tower order, IMG_2697 or IMG_2699 | |
| 3 | Add a one-call Dream check before any batch that stops on `DA-1000` (asked for 14:57, not built) | |
| 4 | Fix the 4 failing tests on `cloud-sims` | |
| 5 | Copy station logs from the NUC, including run 3 | |
| 6 | Run harness matches on the real table, log per-stage success | |
| 7 | Test the exact match prompts for refusals, keep hands out of frame | |
| 8 | Record stack demos in the target order, plus golf balls | |
| 9 | Rerun round 1 with fewer sessions and credits checked first | |
| 10 | Decide if pass-bar stages should follow "fail twice, move on" | |
| 11 | Add a minimum gap between confirming verdicts (two false "cable out" verdicts caused a DQ with two calls in flight) | |
| 12 | Set grip to 2.0 N·m and run `can_live.py` | |
| 13 | `quest_collect.py --dry-run` and the 5 new Quest tests on the NUC | |
| 14 | Re-tune detector colours on station frames | |
| 15 | Build the USB-C port classifier. Only the plan exists (`docs/USBC_PORT_CLASSIFIER_PLAN.md`), the cable-end classifier is done | |
