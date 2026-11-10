# 5. Every classifier and estimator

[Back to the index](../TECHNICAL_REPORT.md)

Verification marks used in this chapter: **[V]** recomputed or re-read from raw data/code in this report;
**[D]** taken from an in-repo document or commit message whose raw data is not in the repos; **[U]** could not be
verified (see §13).

## 5.0 Overview

| Component | Decides | Model | Latency | Evidence | On main? |
|---|---|---|---|---|---|
| VLM judge ("monitor") | stage done, progress, status, holding, guard, slot hints | Haiku 5.5, Gemini 3.1 Flash-Lite, GPT-6.1 Terra (OpenRouter); Sonnet 5.5 confirms | 2–9 s per call | 60-decision benchmark, 25 configurations [V] | yes |
| Colour + shape detector | balls, cubes, sticks, hole direction | HSV thresholds + blob shape tests, no learned weights | guard on both wrist views 76 ms (236 ms before the first speed-up) on 7 Nov [D: report-inputs `HARNESS_SPEED_STATE_SPACE`]; 5.3 ms per wrist frame after the mac-arm64 rewrite (19.2 ms before) on x86 [D] | 26 hand-labelled station frames [D] | old version only |
| Live illegal-ball guard | illegal ball between the fingers | detector on both wrist frames, 3 new frames in a row | 10 Hz, stop in 0.3–0.5 s [D] | 0 trips on 2,145 station wrist frames [D]; **main's config misses both held-white test frames [V]** | yes |
| Cable-end classifier | USB-A end / USB-C end / neither | orange-blob proposals + MobileNetV3-small, 96 px crops, ONNX via `cv2.dnn` | 12 ms per 640-px frame [D] | 137 held-out wrist crops from 25 runs [D] | **no** |
| Gripper-width rules | holding / closed on nothing / regrip / pull | thresholds on `/arms` readings | < 1 ms | 20 Hz joint logs of 101 rollouts [D] | yes |
| Joint fault nets | joint in fault now / within 0.25 s | dilated causal TCN or transformer encoder, 16-step windows | 0.83 ms for 12 joints (numpy) [D] | injected faults; 31 real rollouts [V] | **no** |
| Rule joint observer | limit approach, breach, overspeed, torque saturation, divergence, oscillation, overheat | hand rules | — | 90.7 alarms/joint-hour on untouched real runs [V] | **no** |
| Start-pose gate | arm far from every recorded start | L∞ joint distance to teleop starts per prompt (0.35 rad) | < 1 ms | recorded starts | **no** |
| USB port finder | free port, seated plug | strip template fit + patch CNN | — | **planned, not built** | no |

The design rule: slow, general judgements gate progress; fast, narrow ones guard safety and veto the slow ones.

## 5.1 VLM judges

### 5.1.1 What a look sends and receives

`MonitorVerifier._build` (`harness/verifiers.py:664-`) composes one request per check:

- **Images.** With `claude.stitch: true`, one labelled grid of the stage-start frame, up to `history_frames` (4 in
  `bot_v1`) earlier overhead frames and the newest frames of every camera (`stitch`, `verifiers.py:58`), with the
  local detector's boxes drawn on (`TableDetector.annotate_frames`). Frames are light 640-px JPEGs.
- **Text.** The plan's `system_prompt` (station rules and lessons: "the gripper sensors are measured and beat the
  images"; "every cable's far end lies loose on the table from the start"; …), the stage `success` question, `expect`,
  `camera_notes`, the done stages, operator notes (including a failed `confirm_check`'s reason), and a **grip note**
  from `/arms` (`runner._grip_note`, `runner.py:506-530`).
- **Reply** (JSON): `done`, `confidence`, `progress` (0–1), `status` (`progressing|stalled|failing`), `holding`
  (arm → object), `guard` (illegal ball held?), `tower` (bottom → top), `scene_ok`, `recovery` (menu index),
  `next_prompt`, and in composed mode a slot hint. A stage counts as done only for `done: true` with
  confidence ≥ 0.7 (README).

### 5.1.2 Scheduling

- `claude.parallel: N` keeps N calls in flight, launched at least latency/N apart. On the station this halved the time
  between verdicts from ~9.7 s to ~4.9 s with N = 2 [D: `FINAL_HARNESS.md`]. `bot_v1` uses `parallel: 3`.
- `claude.hedge: N` sends N identical requests per look and keeps the first that parses (`harness/hedge.py`);
  `bot_v1` uses 2.
- Backends: Anthropic API (`ANTHROPIC_API_KEY`), Claude subscription through `claude -p` (no tools, no MCP, API key
  stripped from its environment), or OpenRouter (`--monitor-model google/gemini-3.1-flash-lite`). In `bot_v1` the
  frequent looks go to Flash-Lite on OpenRouter and `confirm_model` / `writer_model` (Sonnet 5.5) stay on the
  Anthropic API. On 8 Nov evening the OpenRouter key hit its limit ("every look was HTTP 403"), so the panel was
  switched to Haiku on the Anthropic API (commits 8ec93b1, a516488).
- Alternating/race judges (`race`, `alternate`) were added for the cloud runs (65ce121).

### 5.1.3 The judge benchmark (`tools/bench_judges.py`, `hedged-calls` → cloud-sims)

Same frames for every configuration: the real `MonitorVerifier` request built from station photos and two MuJoCo
scenes with known answers, translated to OpenRouter's Chat Completions format (`docs/OPENROUTER_RESEARCH.md`).
60 calls per configuration, six fixed cases asked ten times each. 30 calls carry the guard question ("is a gripper holding the illegal ball?"): ten looks at one simulated held-8-ball image and twenty at frames with none. Results in
`task_simulation/results/judge_bench_20261108/bench_fair/*/summary.csv` [V], sorted by accuracy then latency:

| Config (backend:model, reasoning) | Done acc. % | Guard acc. % | Parse fail % | Transport fail % | p50 s | p90 s | $ per 60 decisions |
|---|---|---|---|---|---|---|---|
| openai_gpt-6.1-terra_rlow | 100 | 100 | 0 | 0 | 3.72 | 4.85 | $0.066 |
| openai_gpt-6.1-sol_rlow | 100 | 100 | 0 | 0 | 3.74 | 5.97 | $0.106 |
| subscription_claude-sonnet-5-5 | 100 | 100 | 0 | 0 | 7.14 | 9.34 | subscription |
| google_gemma-4-26b-a4b-it_rmedium | 100 | 100 | 0 | 0 | 11.51 | 24.36 | $0.024 |
| google_gemma-4-26b-a4b-it_rlow | 100 | 100 | 0 | 0 | 11.95 | 30.78 | $0.027 |
| openai_gpt-6.1-terra_rnone | 98 | 97 | 0 | 2 | 2.78 | 3.14 | $0.047 |
| anthropic_claude-haiku-5.5_rlow | 100 | 93 | 0 | 0 | 4.50 | 5.38 | $0.032 |
| anthropic_claude-haiku-5.5_rmedium | 100 | 90 | 0 | 0 | 5.03 | 6.05 | $0.035 |
| subscription_claude-haiku-5-5 | 100 | 90 | 0 | 0 | 8.86 | 11.01 | subscription |
| google_gemma-4-26b-a4b-it_rnone | 97 | 90 | 0 | 0 | 2.36 | 3.93 | $0.007 |
| openai_gpt-6-luna_rlow | 95 | 83 | 0 | 0 | 6.53 | 8.52 | $0.015 |
| google_gemini-3.1-flash-lite_rlow | 80 | 97 | 0 | 0 | 2.39 | 3.05 | $0.058 |
| anthropic_claude-haiku-5.5_rnone | 100 | 77 | 0 | 0 | 2.71 | 3.02 | $0.021 |
| google_gemini-3.1-flash-lite_rnone | 90 | 83 | 0 | 0 | 1.81 | 2.27 | $0.041 |
| google_gemini-3.1-flash-lite_rmedium | 80 | 87 | 0 | 0 | 3.53 | 4.96 | $0.092 |
| openai_gpt-5.6-luna_rlow | 98 | 67 | 0 | 0 | 5.79 | 10.33 | $0.031 |
| mistralai_mistral-small-2603_rnone | 88 | 63 | 0 | 0 | 2.44 | 3.04 | $0.017 |
| meta-llama_llama-4-scout_rnone | 87 | 63 | 10 | 0 | 4.19 | 8.08 | $0.023 |
| openai_gpt-5-nano_rlow | 77 | 67 | 0 | 0 | 8.06 | 10.80 | $0.020 |
| openai_gpt-5-nano_rmedium | 78 | 63 | 0 | 2 | 21.37 | 28.49 | $0.068 |
| openai_gpt-5.6-luna_rnone | 73 | 67 | 0 | 2 | 2.90 | 3.52 | $0.015 |
| meta-llama_llama-4-scout_rlow | 77 | 53 | 15 | 0 | 4.14 | 7.54 | $0.022 |
| mistralai_mistral-small-2603_rlow | 58 | 63 | 3 | 8 | 7.47 | 22.00 | $0.074 |
| openai_gpt-6-luna_rnone | 62 | 7 | 0 | 37 | 2.07 | 2.85 | $0.004 |
| openai_gpt-6.1-sol_rnone | 0 | 0 | 0 | 100 | 0.57 | 0.83 | $0.000 (every call failed) |

Readings:

- Done questions are easy for strong models; **the guard question separates them**. GPT-6.1 Terra with no reasoning:
  98 % done, 97 % guard at a 2.78 s median, **$0.047 per 60-decision match** [V]; with low reasoning 100 %/100 % at
  3.72 s, $0.066 (the paper's "about $0.07"). Haiku 5.5 low (OpenRouter) 100/93 at 4.5 s; Sonnet 5.5 (subscription CLI, low effort) 100/100 at
  7.1 s. Gemini Flash-Lite was the fastest usable judge but missed stage completions (80–90 % done).
- The 30 guard calls are repeats of a few images, ten of them on one held-8-ball render, so they are not 30 independent
  questions and the ranking is coarse.
- Held 8-ball catches out of 10 looks: Haiku 5.5 via OpenRouter 3 (no reasoning) and 8 (low), Haiku CLI 7, Gemma 4 7 and 10, Flash-Lite 5 (no
  reasoning), Terra 9 and 10, Sonnet 10 [V]. `bench_fair/TABLE.md` summarises Haiku at 55 %, Gemma 4 at 85 % and Flash-Lite at 70 %. **No judge stands alone on safety**, which is why the guard decision is
  on the local detector.
- `bench_fair` holds **25** configurations, 24 usable [V]. The `anthropic_claude-haiku-5.5_*` rows went through OpenRouter, and the
  `subscription_*` rows ran the Claude CLI with `--effort low`.

### 5.1.4 Judges in the stress sim

The stress sim's scripted judge answers from the table state at the newest frame's time after a virtual latency.
Judge **speed** is the main lever on points because a slow judge rarely reaches the balls: at the slower input latency of
6.5–13 s (taken from a Haiku check), configurations binned 0–0.09 balls per match; at 4–6 s ("fast"), 2.4–3.4 [V, §7.2]. A fast judge without
the live guard lost **28 of 185** matches to the illegal ball [V]. **The real judge was never run against the MuJoCo
referee**, so false and late done rates on simulated frames are unmeasured.

## 5.2 The HSV colour and shape detector (`harness/detect.py`, `plans/table_colors.yaml`)

### 5.2.1 Algorithm

1. Shrink to `WORK_W = 640` px; HSV on Pillow's 0–255 scale (hue 0–255, not 0–360).
2. Overhead only: ignore blobs outside `table_roi` (`[0.0, 0.32, 1.0, 1.0]`: people and laptops behind the table).
3. Per colour class (`classes:` in the YAML), threshold `[lo, hi]`, morphological close and hole fill, label
   connected blobs, keep blobs between `min_frac` and `max_frac` of the image.
4. Classify each blob by **shape**, not colour (`_kind`): compact round → ball; compact square → cube (an upright
   stick from above looks the same); long and thin → stick lying down. `kinds:` maps shape → role (`avoid` /
   `target` / `info`).
5. Overhead hole axis (`_hole_axis`, `HOLE_SURE = 0.6`): which way a cube's hole runs; an upright stick standing in a
   cube's hole (`_mark_upright`).
6. **Guard** (`TableDetector.guard`): on wrist views (`guard.wrist_roles`), any `avoid` ball whose centre lies in
   `grip_roi` and that fills at least `grip_min_frac` of the view → "held".

### 5.2.2 Station retune (branch `fix/classifiers-8nov`, PR 7) [D]

Checked against hand labels on Dream's own 8 Nov frames (640×360). An object counts as found when a box of the right
class covers its labelled centre.

| | Tuning (14 frames) before | after | Held-out (12 frames) before | after |
|---|---|---|---|---|
| Balls found | 24/46 | 42/46 | **20/27** | **26/27** |
| Ball boxes that are balls | 77 % | 100 % | 83 % | **100 %** |
| Blocks found | 32/40 | 39/40 | 16/23 | **23/23** |
| Block boxes that are blocks | 86 % | 95 % | 80 % | **92 %** |

All 2,010 overhead frames of 8 Nov: **the white bin was boxed as a white ball in 1,286 frames before, 0 after**;
merged "blue stick" boxes 0.36/frame → 0.11. The live guard tripped on none of 2,145 wrist frames before or after
(none shows a held white ball, so this measures false alarms only).

What changed: yellow now spans H 30–70 from S 50 (the pale yellow-green ball sat at H 56–65, between the old yellow
30–55 and green 72–125); yellow has no cubes on this table so a compact yellow blob is a ball (one centred on a cube
is an upright stick); a ball-coloured blob ≥ 50 px with elongation ≤ 2.1 and roundness ≥ 0.28 is a ball (half-lit
balls read as crescents); overhead, a ball-colour blob over 0.6 % of the image (the bin) is never a ball
(`OVERHEAD_BALL_MAX_FRAC`); a cube-colour blob ≥ 14 px wide is split at a dip in its distance ridge (two touching
cubes). Still missed: balls inside the bin (1 of 4 labelled), a white ball pressed against the bin. The labels and
frames stay outside the repos because the frames show people. Regression frames: `tests/data/table_real/`,
`tests/test_detect_real_table.py`.

### 5.2.3 The white ball on the wrist cameras (`fix/guard-real-frames`, dcd76c5) [D]

On the right wrist camera the white ball reads H 147, S 47, V 169–227 (median 193); the phone-photo range required
**V ≥ 225**, which matched 0.1 % of it. The retune moved white to `[[130, 22, 165], [170, 75, 255]]` with
`not_inside: [blue]` (a blue cube's pale rim also falls in that range), and tightened the grip zone to
`grip_roi: [0.33, 0.6, 0.67, 1.0]`, `grip_min_frac: 0.015` (the old box let balls lying past the fingertips through:
3 false trips in one take).

### 5.2.4 This report's check: main's live guard on the committed real frames [V]

`fix/classifiers-8nov` commits six station test frames (`tests/data/guard_real/`): two **composite** frames with a
white ball pasted between the right fingers, three real frames with a white ball visible but not held, one overhead.
Running `TableDetector.guard` with each branch's own `plans/table_colors.yaml`:

| Frame | main @ acc1672 | fix/classifiers-8nov @ 3202983 |
|---|---|---|
| held_white_composite_rw_ep39_2730 | **False** ("none held") | True (2.2 % of the view) |
| held_white_composite_rw_ep39_3720 | **False** | True |
| not_held_white_rw_ep40_1743 / 3366 / 3465 | False ×3 | False ×3 |
| overhead_green_ep39_0 | None (no wrist view) | None |

**Main's guard configuration does not see a held white ball on these station frames; the retuned configuration
does.** Caveats: the held frames are composites, and it is not recorded which `table_colors.yaml` the station laptop
actually loaded in the final runs. This corroborates audit finding A07 and is the most important open safety item
(§12).

### 5.2.5 Speed (mac-arm64) [D]

Exact-equivalence rewrite (`tests/test_guard_equivalence.py` compares verdicts, boxes and annotated JPEGs with the
reference detector): morphology without scipy's iterated loops, crop before labelling, per-blob reads inside
`find_objects` boxes, a guard that only runs colours that can trip it, uint8 HSV planes, a stitch tile cache.

| Component | before p50 / p95 ms | after p50 / p95 ms | Speed-up |
|---|---|---|---|
| Guard, one wrist frame (x86 laptop) | 19.2 / 21.7 | 5.3 / 7.4 | 3.6× |
| Fast-loop tick, 2 frames | 38.3 / 42.5 | 11.2 / 13.9 | 3.4× |
| Monitor annotate, 3 frames | 137 / 139 | 27.0 / 29.2 | 5.1× |
| Whole monitor request build | 152 / 158 | 32.9 / 36.1 | 4.6× |
| Guard on GitHub `macos-15` M1 VM | 34.1 | 5.7 | 6.0× [V: `bench_results/mac_bench_gha_macos15.json`] |

5 minutes of match load: process CPU 107 % → 26 % of one core; overruns 1–12 → 0. **The real MacBook Air was never
benchmarked** (no Mac was available), and the branch was never merged.

## 5.3 Cable-end classifier (`harness/cable_end.py`, `models/cable_end.onnx`)

**Not on main.** On `start-pose-live-guard` (ae30720) and the cloud-sims family; retrained on `fix/classifiers-8nov`.

Architecture: (1) proposals = compact orange blobs (the anodised plug shells; the long thin cable and slivers are
rejected; skin, faces and the orange golf ball pass and are left to step 2); (2) a 96×96 crop (`CROP_SCALE = 2.6`)
classified `usb_c | usb_a | none` by **MobileNetV3-small**, a ~6 MB ONNX file run by OpenCV `cv2.dnn` with a fixed
batch of 8 (OpenCV 5.0 kills the process when a net is reused with a larger batch), so the match laptop needs no
deep-learning runtime. Boxes (magenta USB-C, amber USB-A) are drawn on the monitor's frames; it never touches Dream's
inputs. 12 ms per 640-px frame [D].

Training (`tools/cable_end/train.py`): ImageNet-normalised crops. 7 Nov model: **891** hand-labelled crops from the
teleop videos and table photos, split by source video: 94 % held-out accuracy; at p ≥ 0.7 USB-C P 0.70 / R 0.82,
USB-A 0.96 / 0.84 (17 and 51 test crops) [D: commit ae30720]. Blender renders (`tools/cable_end/render_cable.py`:
random pose, lighting, lens, backgrounds from station frames) **did not help**: synthetic + real 92 % vs real-only 94 %,
and worse on photos, so the shipped model was real-only. (The module docstring still says "Blender renders plus
hand-labelled crops"; the commit message is the authority.)

8 Nov retrain [D: `docs/CLASSIFIERS_8NOV.md`]: 700 wrist crops from 89 runs hand-labelled, **478 usable** (115 USB-A,
82 USB-C, 281 neither; 222 unsure left out). Held out by run (25 runs, 137 crops), p ≥ 0.7:

| Training data | USB-C P | USB-C R | USB-A P | USB-A R | Accuracy |
|---|---|---|---|---|---|
| 7 Nov: 891 video and photo crops | 0.83 | 0.91 | 1.00 | **0.68** | 0.91 |
| + 478 station wrist crops | **0.92** | **1.00** | 0.95 | **0.84** | **0.96** |

The shipped model is retrained on all 1,369 crops (15 epochs, one seed). A hand check of every box on held-out run
103905 found about 62 of 64 right (the old model missed ~13 flat USB-A ends there), with one new false box on the
strip's red switch. The 7 Nov model called a USB-A end lying flat "USB-C" at 0.71–0.95. Neither model finds ends on
the overhead camera (a shell is a few pixels wide).

## 5.4 Gripper-width rules

From the station's 20 Hz joint logs (prompt study §7.6) [D]: jaws stop at **4.5–14 mm on a plug body**, ~3 mm on a
wire or nothing, ~48 mm on a golf ball, 25–55 mm on a cube. In `/arms` units on this station (0–1 opening):

| Reading | Meaning | Where |
|---|---|---|
| ≤ 0.06 | right gripper shut on nothing: "NOT holding anything" (overrides any judge claim) | `runner._grip_note`, `runner.py:520-521` |
| 0.06–0.15 | a thin plug or nothing; the wrist image decides | `runner.py:522-524` |
| commanded closed but stopped > 0.15 | holding something | `runner.py:527-529` |
| commanded > 0.15 | opening | `runner.py:518-519` |
| `hold: [0.062, 0.20]` | grip-assist band for a plug (0.053 was empty live; a pull from 0.27 lost it) | `plans/bot_v1.yaml:112` |
| two readings ≤ `hold[0]` after being open | regrip | `runner.py:866-867` |
| `empty_below: 0.04` after squeeze/lift | lost | `plan.py:91` |

Three uses: (1) "closed fully on nothing" overrides any judge `holding`; (2) a closure on nothing at the plug
triggers the regrip skill; (3) a gripper stopped on an object blocks re-prompting (and `needs_grip` stages ignore a
done verdict until something was held). The station grip hold torque was raised from 0.5 → 1.5 → **2.0 N·m** on both
arms at 19:14 UTC 8 Nov (`nuc/station.yaml` note on `station-tuning`); grippers latch off at 55 °C.

## 5.5 Joint-fault detectors (cloud-sims family; encoders on `joint-fault-encoder`)

Not on main and never used live. The arms have no brakes, so a per-joint fault monitor was built as a safety net.

**Inputs** (`docs/JOINT_FAULT_NET.md`): per joint, the last 16 readings (0.8 s at 20 Hz) of 8 features — position in
range (−1..1), margin to the nearer limit / 30°, velocity toward it / 200 °/s, tracking error / 10°, commanded
velocity toward the limit / 200 °/s, measured torque / limit, commanded torque / limit, their difference.
**Outputs**: two flags, *now* (in fault) and *soon* (now or within 0.25 s); an alert needs 2 readings in a row over
threshold. Runtime: numpy, 0.83 ms for all 12 joints per reading (~2 % of a core at 20 Hz).

**Data.** Real policy motion with injected faults: normal, near miss (fast move stopping 1–12° short: no fault),
limit run, blocked/crawling joint, oscillation (1.5–6 Hz), overload, overspeed. Labels come from the resulting
signals, not from rules.

### 5.5.1 Simulated motion with torque (cloud-sims, a73b628) [D]

6 sim matches (Dream driving both YAM arms), 72 joint series, 413k samples; split by match file (4 train = 16,000
clips, 1 val, 1 test). 15 architectures, 3,000 steps each, AdamW + one-cycle, BCE with a balanced positive weight;
threshold for ≤ 2 false alarms/joint-hour on val; ranked by "timely recall" (alarm ≤ 0.25 s after onset, mean over 5
fault kinds).

| Model | Weights | Val timely | AUROC soon |
|---|---|---|---|
| logistic | 258 | 0.00 | 0.600 |
| MLP 128-64 | 24,898 | 0.96 | 0.979 |
| GRU 32×2 | 10,434 | 0.95 | 0.979 |
| Transformer d32×2 | 18,018 | 0.98 | 0.980 |
| **TCN 32 ch × 4 layers** (dilations 1-2-4-8, kernel 3, residual, last-step + mean pooling) | **10,530** | **0.98** | **0.980** |

Top 3 over 3 seeds × 6,000 steps: TCN 0.987 ± 0.001, transformer d32×2 0.985 ± 0.004, d32×1 0.953 ± 0.013. Test
file at ≤ 1 false alarm/joint-hour (soon 0.998, now 0.95):

| | FA / joint-h | Timely | Limit run | Blocked | Oscillation | Overload | Overspeed |
|---|---|---|---|---|---|---|---|
| TCN (soon) | **0.2** | **0.91** | 1.00 | 0.94 | 0.92 | 0.99 | 0.68 |
| Rules | 197 | 0.50 | 1.00 | 0.01 | 0.22 | 0.34 | 0.93 |

The rules' false alarms are almost all `limit_approach`: its constant-acceleration projection fires on every fast move
toward a limit. Overspeed bursts last 1–3 readings, so the 2-in-a-row rule misses a third: keep both.

### 5.5.2 Real Metal rollouts, torque-free (`joint-fault-real-check` → `joint-fault-encoder`) [V]

`tools/joint_fault_real.py` on copies of the NUC's `last-run.csv` files (20 Hz measured and commanded joints, both
arms, **no torque**, so the torque features are zeroed and overload is dropped). Split by run: 31 rollouts with
enough ticks → **train 21 (5.07 joint-h), val 5 (1.38), test 5 (0.83)**; 3,000 steps, seed 0. Verified against
`harness/models/encoder/sweep_real.json` (created 2026-11-08T20:29:19Z, `fa_budget` 2.0):

| Model | Weights | Val timely | Blocked (timely) | Overspeed | Val FA / joint-h | Alarms on untouched test runs |
|---|---|---|---|---|---|---|
| TCN 32×4 | 10,530 | 0.75 | **0.20** | 1.00 | 1.9 | 0 |
| Encoder d16×1 | 2,690 | 0.67 | 0.42 | 0.36 | 1.9 | 0 |
| Encoder d32×1 | 9,474 | 0.86 | 0.67 | 0.90 | 1.7 | 0 |
| **Encoder d32×2** | **18,018** | **0.87** | **0.78** | 0.84 | **1.2** | 0 |
| Encoder d64×2 | 68,802 | 0.91 | 0.79 | 0.91 | 1.9 | 0 |
| Encoder d64×4 | 135,746 | 0.92 | 0.85 | 0.90 | 1.9 | 0 |
| Rule observer | — | — | — | — | — | **90.7** (limit_approach 87.0, tracking_divergence 3.6) |

Important nuance: the "blocked-joint recall 0.78 vs 0.20" in the brief is **timely** recall (alarm ≤ 0.25 s after
onset). Plain recall on blocked joints is ≥ 0.996 for every model in the JSON; without torque the TCN still
detects a blocked joint, just late (median latency 0.35 s vs 0.20 s for d32×2). The afternoon `--quick`/first sweep
(different, smaller split) gave d32×2 0.96 val timely, 0.87 blocked — the numbers differ because the split did.

No model alarmed on the 0.83 untouched test joint-hours, so that column cannot rank them. The rule observer fires
~90–100 times per joint-hour on real runs because `shoulder_lift` and `elbow_flex` rest at their 0° soft limit at
home and every move back home projects past it. One seed, small val/test sets, all faults injected: **none of these
models has seen a real fault.** Files: `harness/models/encoder/joint_fault_<name>.json` (weights, normalisation,
threshold, split, metrics), loadable with `FaultModel.load(...)`; the runtime zeroes torque when the file says
`torque_free: true`. The default `harness/models/joint_fault_net.json` is still the sim-trained TCN with torque, and
`JointTelemetry` loads it only when its file says it beat the rules on held-out traces (`beats_baseline`).

## 5.6 Start-pose gate

See §3.9. Reference starts per exact task text (`plans/start_poses.json`); 0.35 rad threshold; logs a `start_pose`
event before each stage and homes and restarts once on the unplug stage (`home_before`). Evidence: only 9 of 69 unplug demos start within
0.3 rad of home (§9).

## 5.7 Planned: USB port finder (`docs/USBC_PORT_CLASSIFIER_PLAN.md`, cloud-sims family)

Not built. A template fit to the strip's four rigid ports gives port indices that survive partial occlusion; a small
patch classifier labels each port empty / occupied / occluded; "seated" requires the judge plus three consecutive
"occupied" frames. Open question recorded in `docs/STATE_MACHINE.md`: the real strip's USB bank may be four USB-A
ports (no USB-C), while the rebuilt sim has USB-C + 3 USB-A; confirm with the operator before writing plug-in
templates.
