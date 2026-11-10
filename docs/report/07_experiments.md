# 7. Experiments and results

[Back to the index](../TECHNICAL_REPORT.md)

Data sources are kept apart: **(a)** the station's inference log (real, outcomes not logged), **(b)** 101 scored
station rollouts (real, small counts), **(c)** the stress sim (scripted policy), **(d)** MuJoCo matches (simulated
table, real policy), **(e)** offline benches. Marks: **[V]** recomputed here from raw files; **[D]** from an in-repo
document whose raw data is not in the repos; **[U]** not verifiable.

## 7.1 Latency and the serving loop (real station) [V]

Raw file: `task_simulation/data/station_nuc_20261108_late/robots-inference.jsonl` (also on the `paper` branch).
52 `run_start` events, 1,650 `inference` rows, 18:06 UTC 7 Nov → 05:22 UTC 8 Nov.

| Quantity | Value |
|---|---|
| Round trip (rtt_ms): median / p10 / p90 / max | **1,116 / 800 / 2,733 / 8,655 ms** |
| Model serving (`serve_infer_ms`) median | 293 ms |
| Network (`network_ms`) median | 40 ms |
| Gateway (`gateway_ms`, as logged; includes upload) median | 993 ms |
| RTT − serve − network, median over all chunks | 740 ms |
| Chunks with uplink ≥ 50 Mbit/s: n, RTT median, gateway median | 619, 900 ms, 780 ms (the separately logged medians do not add up, so no fixed overhead follows by subtraction) |
| Chunks with uplink ≤ 9 Mbit/s: n, RTT median | 266, 2,228 ms |
| Request size median | 1.61 MB |
| Sync vs async chunks | **1,101 sync (66.7 %)**, 549 async |
| Inter-chunk gap per session, median | sync **3.54 s**, async **1.55 s** |
| `stall` events | 100 |
| Errors | 16 (table in §10) |

The first 19 runs alone (`data/station_nuc_20261107/`, 1,186 chunks to 00:02 UTC 8 Nov) give median 1,062 ms, 767
sync / 419 async. The paper's "at 8.8 Mbit/s up, calls took 4.7 s" is a single example from the project notes, taken at 12:03–12:05 PST on 8 Nov, outside this log's window.

Interpretation: the pacer switches to stop-and-wait above a 0.9 s median, and the median sits right on it, so the
arms had queued motion for about two thirds of each stop-and-wait cycle (2.4 s per 3.54 s; computed, not observed). The harness adds 5–8 s per stage boundary (a poll, a
judge call, a confirming look), an estimated 25–40 s over four or five stages [D: `HARNESS_SPEED_STATE_SPACE_2026-11-07.md`, which says of a 3-minute match].

### 7.1.1 Metal driver on a simulated Damiao bus [D]

`station_addons/bench_metal_tick.py` on `station-tuning`: Dream's own Metal driver, 20 Hz rows interpolated at
250 Hz, 0.4 Hz sweep, gripper late half the time. Tracking RMS per joint, degrees:

| Setting | Bus tick (Hz) | j0 | j1 | j2 | j3 | j4 | j5 |
|---|---|---|---|---|---|---|---|
| Stock: 30 Hz, FF τ 0.8 s | 30.0 | 2.24 | 2.89 | 3.64 | 4.46 | 1.73 | 1.74 |
| 60 Hz requested | 47–52 | 2.16 | 2.80 | 3.53 | 4.44 | 1.63 | 1.63 |
| 30 Hz, FF τ 0.15 s | 29.8 | 1.37 | 1.73 | 2.19 | 2.31 | 1.18 | 1.19 |
| 60 Hz, FF τ 0.15 s | 50.4 | **1.27** | **1.60** | **2.03** | **2.27** | **1.05** | **1.05** |

The feed-forward filter, not the bus rate, limits tracking (−40 to −50 % RMS). Measured on the station (run 3, the
fine-tune): the arm trails the command by 150–250 ms. These knobs (`PAH_FF_TAU_S`, `PAH_METAL_TICK_HZ`) were never
A/B-tested on the arm.

## 7.2 Robustness in the stress simulator

### 7.2.1 Before and after the overnight fixes (main 3055bf3 → d938644, 4 seeds) [V]

Files: `task_simulation/results/stress/main_3055bf3_part{1,2}.txt`, `main_d938644.txt`, `illegal_ball_guard_6seeds.txt`.

| Scenario | Before (3055bf3) | After (d938644) |
|---|---|---|
| Happy path | 87, 74, 90, 116 | 83, 94, 95, 95 |
| Policy refuses on first start | 0, 0, 0, 0 (match ended) | 83, 94, 95, 95 |
| Empty action buffer mid-run | 10, 10, 15, 15 (ended) | 79, 94, 95, 100 |
| IK SVD failure | 10, 0, 10, 10 (ended) | 83, 84, 95, 95 |
| Corrupt first frame | 20, 15, 20, 20 | 83, 94, 95, 95 (the paper prints "83–90") |
| Policy keeps the unplugged plug | re-plugged it 4/4 (`replug=4`) | never |
| Illegal ball, 6 seeds, frames 2 s old (a separate comparison on one build: without vs with the local detector) | lost 4/6 without (`black_ball_lagged`) | lost 2/6 with the local detector guard |
| Unplug brushes a block (`reach_across`) | DQ 4/4 | DQ 4/4 |

The plug case mattered most: when the policy kept the unplugged plug, the old harness's next prompt led it to plug the
cable back in, which the rubric forbids.

### 7.2.2 The 185-match configuration sweep (37 scenarios × 5 seeds) [V from raw rows]

Recomputed with `results/stress/final_20261108/analyze.py` over `out/*.jsonl`:

| Config | Matches | Mean pts | Lost (DQ / illegal ball) | Unplug passed | Tower ≥ 3 at end | Mean balls | Ended early |
|---|---|---|---|---|---|---|---|
| main_v4 | 185 | 60.5 | 4 (4 / 0) | 179 | 143 | 0.00 | 5 |
| guard_v4 (per-poll guard) | 185 | 56.8 | 4 (4 / 0) | 179 | 139 | 0.00 | 5 |
| guard_v4_live (10 Hz guard) | 185 | 56.8 | 4 (4 / 0) | 179 | 139 | 0.00 | 5 |
| hybrid_v4 | 185 | 61.2 | 5 (4 / 1) | 179 | 149 | 0.09 | 5 |
| hybrid_v4_fast (4–6 s judge) | 185 | 70.5 | **32 (4 / 28)** | 179 | 140 | 3.01 | 31 |
| hybrid_long (fast) | 185 | 63.2 | **39 (5 / 34)** | 179 | 156 | 3.38 | 134 |
| final_v4_fast_live | 185 | 66.6 | 6 (6 / 0) | 177 | 136 | 2.37 | 35 |
| **final_hybrid_fast_live** | 185 | **69.8** | **4 (4 / 0)** | 179 | 139 | 2.90 | 35 |
| build_v4 / build_hybrid (8 scenarios, 5 seeds) | 40 / 40 | 50.0 / 52.0 | 0 | 35 | 29 | 0.00 / 0.03 | 0 |

`FINAL_HARNESS.md` and the paper give a 95 % CI of 65.3–74.4 for final_hybrid_fast_live, which a later recomputation with `paper/make_figures.py` reproduced.
Readings: judge speed is the main lever on points (a slow judge rarely reaches the balls); a fast judge
**without** the live guard lost 28 (hybrid) and 34 (long plan) matches to the illegal ball; the final build with the guard
lost none. Both runs used `--grippers`, the builds differ, and the guard's detector here reads the simulated held object. The 4 shared losses are `reach_across` DQs, which no supervisor can undo. Two extra DQs in
final_v4_fast_live came from two correlated false "cable out" verdicts with two calls in flight (§4.5). Removing
synchronous entry checks from stages the sensors can answer raised mean points 54.9 → 60.0 with no new losses
(24 scenarios × 4 seeds, `--latency 6.5-13`; task_simulation README) [D].

**Caveat:** the sweep's 13 extra scenarios are not in any committed file and the worktree commits it used are not
recorded, so it cannot be re-run exactly (§13).

### 7.2.3 This report's reruns of the committed stress sim (9 Nov) [V]

`python experiments/stress_match.py --seeds 5 -j 8` on each branch's own code and default per-scenario settings
(each scenario's own plan, default latency, no `--live-guard`), 24 scenarios × 5 seeds = 120 matches:

| Branch | Mean pts | Losses | DQ | Illegal ball in bin |
|---|---|---|---|---|
| main @ acc1672 | **71.8** | 17 | 4 (`reach_across`) | 13 (`black_ball` 4, `black_ball_slow_monitor` 4, `black_ball_lagged` 4, `black_ball_local_guard` 1) |
| cloud-sims @ d2203e0 | **53.5** | 7 | 4 (`reach_across`) | 3 (`black_ball` 2, `black_ball_lagged` 1) |

These numbers are **not** comparable with the 185-match table (different scenario set, no live guard, default
latency). They show the harness code on both lines still runs end to end in the stress sim, and that without the
live guard the monitor-only guard still loses matches to the illegal ball on main. cloud-sims scores lower on the
happy path (70 vs 96) because its v4 plan carries the operator-notes stages and retry rules.

## 7.3 Judges

See §5.1.3 (full 25-configuration table, verified).

## 7.4 Local classifiers on station data

See §5.2 (detector: held-out ball recall 20/27 → 26/27 at 100 % precision; blocks 16/23 → 23/23), §5.3 (cable ends:
USB-A recall 0.68 → 0.84, USB-C precision 0.83 → 0.92), §5.5 (joint faults), and this report's main-vs-retune guard check
(§5.2.4).

## 7.5 The policy under the harness in MuJoCo (table v1)

### 7.5.1 Cluster matches, 7–8 Nov [V]

`task_simulation/results/cluster_mujoco/` (excluding `dream_20261107_oracle_bug`, a batch with a referee bug):

| Batch | Matches | ≥ 1 cable out | DQ | Points per match |
|---|---|---|---|---|
| dream_20261107b | 6 | 4 | 0 | 10, 10, 0, 10, 0, 10 |
| dream_20261107c | 6 | 4 | 0 | 0, 10, 10, 10, 10, 0 |
| dream_20261107d_dropcable | 6 | 5 | 0 | 10, 10, 10, 0, 10, 10 |
| main_v4_20261108 (hub on the **left**) | 3 | 0 | **3** | 0, 0, 0 |
| main_v4_righthub | 3 | 1 | 0 | 0, 0, 10 |
| smoke | 1 | 0 | 0 | 0 |
| **Total** | **25** | **14** | **3** | |

Base Dream-1 unplugged a cable in 14 of 25 matches (one of the 25 is a smoke test). A successful unplug stage took a median of 57 s, range 28–269 s; the referee's time to first unplug has a median of 38.3 s (17.6–189.4 s) [V; notes in
`LAST_NIGHT_2026-11-08.md`]. Then it **kept holding the plug**: put-down
0 of 54 tries [D, notes only]; a stacking stage started in 9 matches and all 23 tries failed [V, `events.jsonl`]; balls never reached. All 3 DQs (two block touches, one ball touch) came with the hub on the left, where the prompts no
longer matched the scene.

### 7.5.2 Arm naming (dual-arm benchmark, `codex/dual-arm-experiment`) [D]

YAM sim, n = 12 (`runs_dual_arm/20261107T230412Z/results.jsonl`, not in the repo): "with the left arm, …" succeeded
**0/4** and the right arm still moved for 10–19 s; "left does X while right does Y" 0/4; two goals without naming
arms finished one goal in 2/4 trials. Hence: name the object and its position, not the arm (and on the station:
"the right arm" sent the far arm for stacking and failed; with no arm named Dream used the near arm and stacked;
`plans/bot_v1.yaml:8-9`).

### 7.5.3 Sim-time and relay-order batches (`prompt-engine`, table v1) [U]

Reported in the paper: six sim-time matches of the template-engine plan scored 42, 42, 41, 15, 15, 15 (mean 28.3),
passed the tower bar in 3 of 6 and binned no balls; a relay-order batch got the first unplug in 12 of 12 matches and
a second in 8 of 12, seated no plug, then crashed at the first stacking stage on a slot-filler bug (fixed with a
fallback and a test). **No logs for these batches are in any of the three repos.**

## 7.6 The prompt study on the station: 101 rollouts (Daniel Carrera) [D]

`docs/prompt_comparison_2026-11-08/` (branch `docs/prompt-comparison-2026-11-08`, PR 6). Every archived station
rollout 09:56–13:29 on 8 Nov (times as written in the study, station-local; the zone is not stated): **101 rollouts, 171 prompt steps**, scored read-only on the station by
`prompt_stats_remote.py`, grouped by `prompt_families.py`. The committed `families_20261108.json` has 30 families
whose steps sum to **171** [V]; the raw `segments_20261108.jsonl` and the two scripts are referenced but not
committed.

Outcomes come from first/last overhead frames (balls, blocks) and earlier video reviews (cables); execution proxies
from the 20 Hz joint log: grip width (plug body 4.5–14 mm vs wire/empty ~3 mm; ball ~48 mm; cube 25–55 mm), Dream
lift ≥ 5 cm while holding, scripted pull still holding after +10 cm, closures/min, % frozen.

| Task | Prompt family (abridged) | Outcome |
|---|---|---|
| Unplug | U10 "With the right gripper **half open**, move down onto the plug…, close…" + scripted +10 cm `/move`, sent by hand in a Claude Opus session (11:10–13:04) | jaws on the plug body in **15 of 26** steps (58 %); 12 scripted pulls, **8 still held the plug** after the lift |
| Unplug | U7 "Remove the first cable on the right… then place it on the right side" | one cable by Dream alone, then 0 in 4 repeats |
| Unplug | U6 "grip the plug body of the rightmost cable… pull it straight up" | 1 cable, lifted 23 cm (n = 1) |
| Unplug | U3 writer default "grip the **hard** plug body…" | 4 plug / 7 wire grips (31 % on the plug); 0 confirmed |
| Unplug | U5 574-character coached prompt | **0 cables in 286 s** |
| Unplug | U4 writer micro-rewrites ("regrip", "hover", "lower") | 0; **9 of 15 sent while holding the plug**, the plug released in 5; 6.7 closures/min |
| Unplug (FT) | U1 the fine-tune's trained text | **0 new grips in ~10 min** (on base, the same text grasped and lifted 11 cm once) |
| Put down | "Put the held cable down on the table to the right…" | 11 steps started holding; released in 7 |
| Ball | B1 "With the right arm, pick up the <colour> golf ball <where> and drop it into the white basket" | **2 of 5** in (both near the right gripper); the misses were mid-table |
| Ball | B2 left arm / B3 two arms in one prompt | no grip |
| Stack | S8 "stack the blocks and sticks as high as possible" (the rubric's wording) | stacks in **2 of 3** runs |
| Stack | S2 "put the pink cube on top of a blue cube" | most decisive grasps (6 of 6); 1 correct stack, 1 wrong cube |
| Stack | S1 motion script ("lift it high… lower it gently… then open") | 0 of 10 steps |
| Stack | S3 "build a tower on X: put A, then B, then C" | 0 |

Patterns: **name the goal, not the motion** (Dream does the first verb of a chained prompt and dithers on the rest);
the cable grasp is the exception where describing the gripper opening helped; Dream almost never does the pull itself
(4 Dream-made lifts all day); never re-prompt a held grasp; one arm, one object, within reach; short steps (15–18 s)
spend 31–45 % of the time frozen at step starts vs 6–16 % for 25–90 s harness steps.

**Replay review 13:47–14:28** (`replay_1347-1428/README.md`, 23 runs) [D]: 1 cable fully out (with the "…and toss it
onto the table to the right" prompt), **4 balls in the basket** (all right arm), a 2-high and a **3-high stack**
(blue/pink/blue, the best of the day) — **both stacks destroyed by the next prompt**. Root causes ranked: (1) no
stop-on-success; (2) steps cut short and reworded too fast (6 of 10 failed ball steps ran ≤ 17 s; the orange ball
got 5 wordings in 3.5 min); (3) resets sent while holding (8 of 17 "open both grippers…" dropped a held object);
(4) reach and placement; (5) cable grasp precision (7 of 14 unplug steps ended on wire or nothing); (6) fine-tune
cold starts; and the pull-force add-on firing 70 times uselessly. "Was it the prompts? **Mostly not**" — it was when
prompts were sent. These findings produced `stop_on_pass`, the flowing `lock_prompt`s and `hands_off` on the bot
line.

## 7.7 Cloud round 1 (8 Nov, MuJoCo, real Dream-1) [V]

`task_simulation/results/cloud_round1_20261108/`: 29 `match.json` files.

| Arm | Valid | Invalid | Valid points | Dream call Hz (valid) |
|---|---|---|---|---|
| r1-a-baseline (`--execution-horizon 0`, notes off, oracle judge) | 2 | 6 | 10, 42 | n/a (sim rate 0.64) |
| r1-b-async2hz (`--execution-horizon 10 --dream-pipeline 2`, notes on) | **0** | 7 | — | — |
| r1-c-haiku (B + OpenRouter Haiku 5.5 low) | 4 | 4 | 0, 0, 10, 15 | 0.16–1.18 |
| laptop r1-E-gemini | 4 | 2 | 15, 15, 15, 15 | 1.62–5.52 |
| **Total** | **10** | **19** | **0–42** | |

Invalid reasons: "harness stopped on an error" + "no judge verdicts" (11), "harness stopped on an error" (8). The run
stopped at **12:04:48 UTC** when every Dream call began returning `DA-1000 UNAUTHORIZED (disabled)` — the account had
run out of inference credit while account calls (listing models) kept working; ~28 sessions were open. Arm B has no
valid match at all, so `HILL_CLIMB_LOG.md` stays empty and **round 1 cannot separate the arms**. A rerun of the
relay batch hit the same limit 34–99 s into each match [U, no log in any repo]. Six invalid laptop-run errors were Dream refusing the
plug-in prompt (DA-4000 "Plugging cords into power strips…"). Lesson: probe Dream with one call before every batch
(asked for at 14:57, never built; `LAST_NIGHT` to-do #3).

## 7.8 The fine-tune vs base on the station

See §9 (FT diagnosis). Summary [D]: 7 fine-tune unplug runs reached a minimum tool height of 8.3–12.8 cm (median
11.7) against 0–6 cm for base the same day; 0 cable grips in ~10 min; the only time the fine-tune closed on anything
all day was one block carried for ~110 s at 13:18; cold starts of 3+ min.

## 7.9 What is missing

There is **no logged per-stage success rate from complete five-minute harness matches on the station**. The
inference log records calls and errors, not outcomes; the prompt study scores individual rollouts; the team placed 5th of 8, and per-match results
(if any were recorded) are not in the repos. The harness was also never compared against plain Dream-1 on the station
with the same prompt.
