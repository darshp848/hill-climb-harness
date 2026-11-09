# 13. Verification log

[Back to the index](../TECHNICAL_REPORT.md)

What was run, re-derived or only read for this report (9 Nov 2026, Linux container, Python 3.13.18, numpy, Pillow,
OpenCV 5.0.0, MuJoCo 4.0.0 with OSMesa, scipy, pyarrow, psutil; **no `dreamlabs`, no `i2rt`, no `livekit`, no
`dimos`, no torch**). No Dream, Anthropic, OpenRouter or other paid API was called. No robot, station or cloud
resource was touched.

## 13.1 Test suites

`python -m pytest -q --continue-on-collection-errors` in a clean worktree of each branch head (main also with
`--ignore=tests/test_hill_climb_scene.py`).

| Branch @ head | Passed | Failed | Skipped | Collection errors | Failures and why |
|---|---|---|---|---|---|
| **main @ acc1672** (scene test ignored) | **334** | **1** | 10 | (1 if not ignored) | `test_grip_assist.py::test_bot_v1_plan`: the cable prompt contains "toss" (716c4d0) — **a real plan/test disagreement** |
| final-harness @ 777e389 | 303 | 0 | 7 | 1 | — |
| start-pose-live-guard @ c2098dc | 270 | 0 | 6 | 1 | — |
| hedged-calls @ bf33014 | 348 | 0 | 7 | 1 | — |
| mac-arm64 @ ad2a55f | 354 | 0 | 7 | 1 | — |
| cloud-sims @ d2203e0 | 392 | 5 | 27 | 1 | 5 × `test_async_pacing.py`: need Dream's SDK (environment) |
| fix/classifiers-8nov @ 3202983 | 407 | 5 | 27 (+1 xfail) | 1 | same 5 (environment) |
| joint-fault-encoder @ 6d8ea7e | 398 | 5 | 28 | 1 | same 5 (environment) |
| station-tuning @ 8a96aac | 249 | 0 | 8 | 1 | — |
| prompt-engine @ 3e7866c | 275 | 0 | 7 | 1 | — |
| claude/harness-stitch @ 28336b0 | 300 | 0 | 7 | 1 | — |
| claude/engine-bot-live @ d7aac1e | 316 | 1 | 7 | 1 | `test_bot_v1_plan` ("toss"), as on main |
| relay-on-stitch @ f8771cd | 320 | 1 | 7 | 1 | `test_livekit_observer.py::test_minted_token_is_subscribe_only`: imports `livekit`, but only skips on a missing `jwt` (environment) |

So the only **logic** failure on any branch we ran is `test_bot_v1_plan`, on main and `engine-bot-live`.

Every branch has the same collection error in `tests/test_hill_climb_scene.py`: `importlib.util.find_spec(
"dream_examples.yam_mujoco")` raises `ModuleNotFoundError` when the parent package is absent, instead of returning
`None`, so the module-level skip never runs. The five `tests/test_async_pacing.py` failures on the cloud-sims family
all end in `SystemExit: Cannot find Dream's yam_mujoco example`: they need Dream's SDK and fail instead of skipping
(Codex audit A24). On `LAST_NIGHT_2026-11-08.md`'s machine (with the SDK) cloud-sims was 407 passed, 4 failed,
5 skipped at 13:07 UTC 8 Nov. The single real logic failure on main is `test_bot_v1_plan` ("toss" in the cable prompt,
716c4d0). Skips are optional dependencies (`pyarrow`/`dimos`/`jwt`/`livekit`/`dreamlabs`).

**follower_code**: `main` 1 failed (`tests/test_sim.py`: no `i2rt`), 2 collection errors (`metal/tests`,
`quest/tests` need the hackathon kit); `quest-continuity` **7 passed** (the kit-free `quest/tests/test_continuity.py`),
1 failed, 2 errors for the same reasons. The kit tests were reported passing on the NUC (10 Metal, 14 Quest) [D].

## 13.2 Simulations run

| Run | Result |
|---|---|
| `experiments/stress_match.py --seeds 5 -j 8` on main @ acc1672 | 120 matches, mean 71.8 pts, 17 losses (4 DQ, 13 illegal ball) — §7.2.3 |
| same on cloud-sims @ d2203e0 | 120 matches, mean 53.5 pts, 7 losses (4 DQ, 3 illegal ball) |
| `results/stress/final_20261108/analyze.py` | reproduces every row of the 185-match table (§7.2.2) from the committed `out/*.jsonl` |
| Guard check: `TableDetector.guard` on the six `tests/data/guard_real/` frames with main's and fix/classifiers-8nov's configs | main 0/2 held-white detected; retune 2/2; no false trips on 3 not-held frames (§5.2.4) |
| PDF build (pandoc 3.1.3 + XeLaTeX) | 62 pages, no missing glyphs |

Not re-run: MuJoCo matches with the live policy (needs a Dream key with credit), cloud batches (spend money), judge
benchmarks (paid APIs), cable-end and joint-fault training (frames with people are not in the repos; joint traces
were not copied), the Metal replay sim (needs the NUC's copied run files and Dream's driver).

## 13.3 Numbers checked

| Claim (brief / paper / docs) | Check | Verdict |
|---|---|---|
| 52 runs, 1,650 chunks; RTT median 1,116 ms, p10 800, p90 2,733, max 8,655 | recomputed from `robots-inference.jsonl` | **verified** |
| 67 % of chunks stop-and-wait (1,101/1,650); sync gap 3.54 s, async 1.55 s | recomputed | **verified** (66.7 %) |
| Serve 293 ms, network 40 ms | recomputed | **verified** |
| ~650 ms fixed gateway overhead; sync above ~900 ms median RTT | fast-uplink chunks: 900 ms RTT − 293 ms serve ≈ 607 ms; 900 ms threshold from the station log quoted in `DECISION_MODEL.md` | **consistent** (threshold read from docs, not code) |
| Fault mix DA-4000 ×4, SVD ×3, buffer ×2, expired ×1 (digest) vs 4/4/4/4 (paper) | both logs | **both verified**: 7 Nov subset vs full log |
| Frame every 3.4 s (last-run.csv) | `last-run.csv` present; not recomputed | documented only [D] |
| Stress: 69.8 / 66.6 / 60.5; 28 illegal-ball losses for the fast judge without the guard | `analyze.py` on raw rows | **verified**; CI 65.3–74.4 not recomputed |
| 37 scenarios | raw rows contain 37; committed code has 24 | **partly**: 13 scenario definitions not committed |
| Before/after fixes table | raw `.txt` files | **verified**; the paper's "83–90" for corrupt frames should read 83, 94, 95, 95 |
| Judge benchmark: Terra none 98/97 at 2.8 s, $0.047/match; low 100/100 at 3.7 s; Sonnet 100/100 at ~7 s; Haiku low 100/93 at 4.5 s | `bench_fair/*/summary.csv` | **verified** |
| Judge benchmark "30 configurations" | 25 in `bench_fair` (one all-failed) | **not verified** (5 missing) |
| Haiku caught a held 8-ball 55 % on a 120-decision run | `bench_fair/TABLE.md` | **verified** |
| Detector 20/27 → 26/27 balls, 16/23 → 23/23 blocks, bin 1,286 → 0 of 2,010 | labels and frames kept outside the repos | documented only [D] |
| Detector 76 ms on two wrists (236 ms before) | `HARNESS_SPEED_STATE_SPACE_2026-11-07.md` (report-inputs); mac-arm64 bench JSON gives per-frame ms on other hardware | documented only [D] |
| Cable-end USB-A recall 0.68 → 0.84; 891 + 478 = 1,369 crops; Blender 92 % vs 94 % | `CLASSIFIERS_8NOV.md`, commit ae30720; crops not in the repos | documented only [D] |
| Joint faults: d32×2 blocked 0.78 vs TCN 0.20 without torque | `sweep_real.json` | **verified** (timely recall; plain recall ≈ 1.0) |
| Joint faults on sim with torque: TCN 0.91 timely at 0.2 FA/joint-h; rules 0.50 at 197 | `JOINT_FAULT_NET.md`; `joint_fault_search.json` not re-parsed | documented only [D] |
| MuJoCo v1: 14 of 25 with a cable out; 3 DQs, all hub-left | `cluster_mujoco/*/matches.jsonl` | **verified** |
| First unplug median 57 s (28–269 s); put-down 0/54; stack 0/25 | event logs present; not recomputed | documented only [D] |
| Sim-time six matches 42, 42, 41, 15, 15, 15; relay order 12/12 and 8/12 | no logs in any repo | **not verifiable** [U] |
| Unplug stage curve p_max 0.64, none before ~23 s, **median 56 s** | `stage_curves_sim.json`: p_max 0.64, t_min 22.84 s, τ_c 105.6 s → half-ceiling time ≈ 96 s | **inconsistent** with the JSON's parametric fit; the 56 s is probably an empirical median not stored [U] |
| Shadow decide = runner on 5,000 random sequences; state-space on 3,000 | test code read (`tests/test_decide.py:48`, `STATE_SPACE.md`); tests pass in the branch runs | **verified** (tests) |
| 5.96 requests/s at 0.63 s median | `live/status.json` of one laptop match: 5.96 Hz, p50 0.625 s; its final `match.json`: 5.52 Hz | **verified** (live snapshot) |
| Cloud round 1: 10 of 28 valid, 0–42 pts | 29 `match.json`, 10 valid, points 0–42 | **verified** (29, not 28) |
| 101 rollouts, 171 steps; 8 of 12 pulls kept the plug; 9 of 15 rewrites mid-grasp | `families_20261108.json` steps sum to 171; the rest from the README (raw segments not committed) | **171 verified**, rest [D] |
| FT: 170 episodes = 73 Quest + 97 leader; 48,413 frames | TELEOP_PLAN folder table sums to 73 + 97 | **verified** (sums); offsets [D] |
| FT offsets +0.078/+0.047/+0.043 rad, ~5.5 cm; 9 of 69 unplug demos near home | FT diagnosis (two identical copies) | documented only [D] |
| Metal driver bench RMS table | `STATION_TUNING.md` | documented only [D] |
| mac-arm64 guard 3.6× (x86), 6.0× (GHA M1) | x86 from doc; GHA from `bench_results/mac_bench_gha_macos15.json` | **GHA verified**, x86 [D] |
| Codex audit: 26 issues, 2 critical | `AUDIT_FINDINGS.md` headings A01–A26; A01, A02 critical | **verified** |
| Dream key disabled twice at ~28 concurrent sessions | one DA-1000 burst (12:04:48 UTC, 83 lines) is in the logs; "twice" from the digest | **partly** |
| Hold torque 2.0 N·m since 19:14 UTC 8 Nov | `nuc/README.md` on station-tuning | documented only [D] |
| `test_bot_v1_plan` fails on 716c4d0 ("toss") | ran it | **verified** |

## 13.4 `report-inputs` and the local-only material

The brief said to check for a `report-inputs` branch in task_simulation at the end. It **did not exist** when this
work started (`git fetch --all` showed only `main` and `paper`) and **appeared later** (6cc3224, 9 Nov 07:09 UTC,
darshp848). It was fetched and folded in:

| report-inputs file | Status | Where it is used |
|---|---|---|
| `docs/HARNESS_DECISION_MODEL_2026-11-07.md` | **byte-identical** to `docs/DECISION_MODEL.md` on `final-harness` | §4 |
| `docs/HARNESS_SPEED_STATE_SPACE_2026-11-07.md` | new to us; source of the 76 ms detector, 12.4 ms cable-end, 5–8 s per stage boundary, estimator proposal | §3.7, §5.0, §13.3 |
| `docs/HARNESS_DIRECTIONS_2026-11-07.md` | new to us; seven directions from the literature (LIBERO-Plus start state, Code-as-Monitor, conformal calibration, STEP/SIMPLER trial counts, RaC, AHA) | §13.5 |
| `docs/HARNESS_SIM_INSTRUCTIONS.md` | Windows sim instructions (dreamcli needs `fcntl`, so it does not run on Windows; `sim_station.py` does); 7 Nov test result 127 passed, 1 failed (a timing race in `test_v2.py`) | §11, §13.5 |
| `docs/FT_DIAGNOSIS_2026-11-08.md` | identical to `ft-diagnosis` except four redactions (dataset owner, a name, image references) | §9 |
| `docs/codex_cloud_report.md` | Codex's report on the GCP runner: 318 passed; smoke match 301 s wall, 137 CPU-s, 689 MiB peak; 1,000 matches ≈ $2.50 of Spot VM at an **assumed** $0.030/h, Dream usage extra | §6.5 |
| `docs/codex_gcp_console_report.md` | budget $50 with 50/90/100 % alerts; **both CPU quota requests (→ 200) denied**, limits stayed 32 and 24; pilots `pilot2-*` ended CANCELLED with upload failures | §6.5, §12 |
| `docs/codex_openrouter_report.md` | `bench_judges.py` + 32 tests; 354 passed; dry-run 108 trials over 18 configs before any paid call | §5.1.3 |
| `codex_branch_patches/0001-0003` | 9e97ba8 (match supervisor), 569607e (LiveKit observer), 189c39d (validate the decision tree on 300 s runs) | these three commits are **also on GitHub**, reachable from `codex/hill-climb-review-fixes` and `codex/dual-arm-experiment` |

The same seven design docs are also on **task_simulation `main`** under `docs/notes_20261108/`, so the digest's
"local only" list was partly out of date even before `report-inputs`.

Still **not on GitHub** in any form: `codex/hill-climb-five-minute-validation`, `codex/hill-climb-livekit`,
`codex/hill-climb-match-supervisor` and `port-sim` as **branches** (their known commits are reachable as above);
three commits of `stress-test-fixes`; `port-to-main`; `nuc-snapshot` and `ft-joint-offset` as branch names (their
content is on `station-tuning`); the raw prompt-study segments; detector labels and station frames (they show
people); the 13 extra stress scenarios; the sim-time batch logs; the GCS-only `joints.jsonl` traces; the 79 station
rollouts and the 3 GB `nuc_data`; and any match-day results.

## 13.5 Extra material from the 7 Nov directions doc

`HARNESS_DIRECTIONS_2026-11-07.md` ranked seven directions given that the harness can only send prompts and
start/stop/home/reset: (1) match the start state to the training data (LIBERO-Plus: initial state −59.9 points vs
language −15.6) → built as the start-pose gate; (2) close the guard race with live frames (Code-as-Monitor) → built as
the 10 Hz live guard; (3) geometric stage checks first, VLM as tie-breaker → designed as `decide.py` weights, not
enabled; (4) calibrate the monitor's thresholds with conformal prediction on successful runs (FAIL-Detect, FIPER,
SAFE) → not done; (5) enough trials (STEP: 30–50-point gaps need ~7–35 trials, a 9-point gap ~500) → the GOAL.md
≥ 20 paired matches rule; (6) record harness-triggered recovery + correction episodes for training (RaC,
IntervenGen) → not done; (7) failure-reasoning models (AHA) → not worth it that weekend.

## 13.6 Branches found but not fully explained

| Branch | What we could and could not establish |
|---|---|
| `assets` | One commit of PR 9 screenshots on an otherwise unrelated history (1 ahead / 123 behind); not opened here because screenshots may show people |
| `claude/engine-bot-local` | Same commit as `claude/engine-bot-live`; why a second name exists is not recorded |
| `kaushik/cloud-sims-bot-plan`, `claude/sim-hillclimb` | Pushed at 03:16–03:17 UTC 9 Nov, after the event; whether they were ever run is not recorded |
| `claude/bot-prompt-writer` tip (21bad3a) | `tools/full_run.sh` pushed after the event; usage unknown |
| `sim-and-move-fix` | Kaushik's separate MuJoCo station sim (`sim/`, 5.7k lines, calibration JSONs); never compared with `sim_station.py` |
| `hybrid-harness` tip (b5883e4) | One operator lesson added after the event; the "run 055635Z" it cites is not in the repos |
| Which config ran in the actual match | Not recorded anywhere: main@acc1672 or engine-bot-live@d7aac1e, which `table_colors.yaml`, base or fine-tune per stage, and any score |
