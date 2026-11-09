# 12. Known issues, open TODOs, dead ends, and what we would do next

[Back to the index](../TECHNICAL_REPORT.md)

## 12.1 Known issues on main (acc1672), most serious first

| # | Issue | Evidence | Fix |
|---|---|---|---|
| 1 | **The live guard's white range on main is the phone-photo range**; it missed both held-white-ball test frames that the station retune catches | §5.2.4 (our run); audit A07; `fix/guard-real-frames` dcd76c5 | Port `plans/table_colors.yaml` and the `detect.py` changes from `fix/classifiers-8nov` (PR 7) to main; re-check on fresh wrist frames with a real held white ball |
| 2 | A missing or frozen wrist frame on the carrying arm is masked by a clear view on the other wrist (`TableDetector.guard` returns "none held") | audit A06 | Require the carrying arm's wrist frame to be fresh; report blindness per camera |
| 3 | Blocking I/O can carry motion past the deadline (station client timeout 70 s, monitor transport 60–90 s, synchronous entry/confirm checks) | audit A09; `harness/station_client.py:31` | Bound every synchronous call by the time left; send `/stop` when the deadline passes during a call |
| 4 | A station restart after a fault keeps old done evidence (`_poll_health` → `_start_run`, not `_issue`, so `notify_prompt_change` and `last_done_t` are not reset) | audit A11; `runner.py:341` | Clear done state and bump the verifier generation on every restart |
| 5 | Correlated confirming looks: two in-flight calls on near-identical frames can satisfy `confirm: 2`; the 15 s sticky-done window can pass a re-placed tower | stress sweep (2 DQs); audit A03 | Minimum gap between confirming verdicts; second look from a different model (as the bot line's Sonnet `confirm_check` does for unplug) |
| 6 | `tower_pass` in `hill_climb_v4.yaml` passes on one synchronous entry look (`skip_if_done: true`) without the 3 s dwell | audit A02 (critical); `plans/hill_climb_v4.yaml:188` | Drop `skip_if_done` there (done on `prompt-engine`'s relay plan); `bot_v1` is not affected |
| 7 | `test_bot_v1_plan` fails: the cable prompt contains "toss" | §13.1; 716c4d0 vs bec9975 | Decide which is right (the only confirmed 13:57 unplug used "toss"; earlier "toss it" sent Dream to the balls) and align test and plan |
| 8 | `tests/test_hill_climb_scene.py` errors at collection without `dreamlabs` | §13.1 | Wrap `find_spec` in `try/except ModuleNotFoundError` or use `pytest.importorskip("dream_examples.yam_mujoco")` |
| 9 | Main lacks the last bot fixes (`confirm_every_s` tower re-check, stacking option B, orange-cable prompt) | §2.4.2 | Merge `claude/engine-bot-live` (6 commits) |
| 10 | Pull-force add-on fires on every right-gripper close, including empty closes at home | prompt-study replay | Gate to unplug steps or keep off |
| 11 | The rule joint observer's `limit_approach` fires ~90–100 times per joint-hour on real runs (shoulder_lift and elbow_flex rest at the 0° soft limit at home) | §5.5.2 | Fix the margin or the home pose before enabling the rules (not on main anyway) |
| 12 | The subscription transport sends only `CLI_SYSTEM_PROMPT`, dropping the plan's system prompt | audit A21 | Pass the plan's system prompt through `claude -p` |

## 12.2 Open TODOs carried over (from `docs/LAST_NIGHT_2026-11-08.md`, `STATE_MACHINE.md`, the paper)

- Run full five-minute harness matches **on the station with per-stage logging**; there is still no real per-stage
  success rate.
- Compare the harness against plain Dream-1 on the station with the same prompt.
- A one-call Dream probe before every batch that stops on DA-1000.
- Fix the failing tests on `cloud-sims` (at 13:07 UTC 8 Nov: 407 passed, 4 failed, 5 skipped; see §13.1 for today's run).
- Test the exact match prompts for refusals; keep hands out of the camera view.
- Record more stack and golf-ball demos, and unplug demos from the home pose.
- Rerun cloud round 1 with fewer sessions and credit checked first; run the **real judge against the MuJoCo referee**
  on the rebuilt table to measure false and late done rates.
- Fine-tune on gravity-corrected data and compare with base on the same seeded scenes (needs a Metal sim or station
  slots).
- Build the USB port finder and the insert skill if plug-in comes back (`STATE_MACHINE.md`: "Not built: the harness
  insert skill, the real-table port and cable classifier slots, and a Claude filler prompt for plug-in").
- Decide whether pass-bar stages follow Darsh's "fail twice, move on" (they currently cannot without a DQ).
- Measure the block height: `tower_pass` depends on the 4-inch bar, and if two blocks already exceed it the third is
  wasted risk (audit A14).
- Benchmark the detector on the actual MacBook Air under sustained load.
- Re-tune and test `quest-continuity` on the NUC (`quest_collect.py --dry-run`, the 5 new tests).

## 12.3 Dead ends and things that did not work

| Idea | What happened | Source |
|---|---|---|
| The fine-tune `relay-final` | worse than base on every task: aimed 5.5 cm high (Quest gravity sag), 0 cable grips in ~10 min, cold starts of 3+ min | §9 |
| Monitor-written free-text rewrites | judge-written micro-rewrites unplugged nothing; 9 of 15 were sent mid-grasp and 5 dropped the plug; Flash-Lite rewrote 11 times in one run | §7.6; `bot_v1.yaml` notes |
| Long, coached prompts | a 574-character cable prompt unplugged nothing in 286 s; motion scripts for stacking stacked 0 of 10 | §7.6 |
| Naming an arm | 0/4 in sim; "the right arm" sent the far arm for stacking on the station; left-arm prompts 0 of 4 on 8 Nov | §7.5.2 |
| Two arms in one prompt | no grip (station), 2/4 single-goal successes (sim) | §7.5.2, §7.6 |
| Watcher arm | depends on Dream holding one arm still on command, which it does not reliably do | `DECISION_MODEL.md` §2 |
| Plug-in stages | base Dream-1 never seated a plug; dropped from the match plan on 7 Nov; plug-in prompts were refused in the sim | TELEOP_PLAN, §10.1 |
| Blender synthetic data for the cable-end CNN | synthetic + real 92 % vs real-only 94 %, worse on photos | §5.3 |
| `hybrid_v4_long.yaml` (whole-task prompts) | lost 34 of 185 sim matches to the illegal ball and ended early in 134 | §7.2.2 |
| Second unplug in the v4 plan | +6 points in the sim but two more illegal-ball losses where only the monitor guards | task_simulation README |
| Gravity scale alone to fix the fine-tune | no env-var-only setting reached grasp height in the replay | §9.5 |
| Cloud round 1 | killed by DA-1000 (out of credit) with ~28 sessions; 10 of 29 matches valid, arm B none | §7.7 |
| ~28 concurrent Dream sessions | the key was disabled twice | §10.1.2 |
| Pull-force add-on | fired 70 times, mostly on empty or irrelevant closes | §3.6 |
| A harness wait at timeout for an in-flight verdict | no difference (54.9 vs 54.8), dropped | task_simulation README |

## 12.4 What we would do next, in order

1. **Make main safe to run as is**: port the station colour retune (issue 1), fix the guard's per-camera blindness
   (2), bound blocking calls by the deadline (3), clear done evidence on restart (4), merge `engine-bot-live` (9), and
   get the test suite green (7, 8).
2. **Get real per-stage numbers**: 10+ full matches on the station with the bot plan, logging each stage, plus a
   same-prompt harness-off control.
3. **Close the sim gap**: run the real judge (Flash-Lite + Sonnet confirm) against the MuJoCo referee on the rebuilt
   table; calibrate the stress sim's probabilities (unplug ~1/6 per try, put-down, stacking) from step 2; only then
   tune the decision model's weights (§4.6).
4. **Fine-tune again on corrected data** (§9.6), with unplug demos from home and an action − state screen in
   `check_dataset.py`; A/B against base on Stage 1 (5 + 5 runs).
5. **Serving loop**: wired network and light frames everywhere; measure the async share before and after; A/B the
   station tuning knobs (`PAH_FF_TAU_S=0.15`, `PAH_METAL_TICK_HZ=60`) one at a time.
6. **Local evidence first**: let the gripper-width rules, the detector and the cable-end CNN pass non-pass-bar stages
   (weights in `decide.py`), keeping the VLM for pass bars with a second-model confirm.
