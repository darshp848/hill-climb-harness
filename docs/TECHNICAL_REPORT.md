# Hill Climb: technical report and practical guide

**Physical AI Hacks (MakerMods × Dream Labs), 7–8 November 2026.** Team: Daniel Carrera, David Carrera, Kaushik
Kanna, Muhammad Mukthar, Darsh Patel. Written 9 Nov 2026 from the three public repositories
([hill-climb-harness](https://github.com/darshp848/hill-climb-harness),
[task_simulation](https://github.com/darshp848/task_simulation),
[follower_code](https://github.com/darshp848/follower_code)), the `report-inputs` branch of task_simulation, the
paper on task_simulation's `paper` branch (`paper/main.tex`), and a session digest standing in for the local
Claude/Codex transcripts.

This is the long form of the paper. **Accuracy over polish**: every number carries a mark — **[V]** recomputed or
re-read from raw data or code for this report, **[D]** taken from an in-repo document or commit whose raw data is not
in the repos, **[U]** could not be verified — and §13 lists everything unverified. Code references are
`path:line` on **`main@acc1672`** unless a branch is named.

## Contents

| § | Chapter | File |
|---|---|---|
| 1 | Task, rubric v4, scoring and the station (Metal arms, cameras, Dream-1 serving stack, station API) | [report/01_task_and_station.md](report/01_task_and_station.md) |
| 2 | Repositories, every branch, PRs, lineage, what main does and does not contain | [report/02_branches_and_lineage.md](report/02_branches_and_lineage.md) |
| 3 | Harness architecture: state machine, two-look confirmation, live guard, start-pose gate, guarded `/reset`, prompt engine, grip assist, lock, hedging and pacing, observers, DimOS | [report/03_harness_architecture.md](report/03_harness_architecture.md) |
| 4 | The decision model: log-odds evidence, fail hazard, Q-value, optimal timeouts, the α^k bound, state-space form, shadow mode | [report/04_decision_model.md](report/04_decision_model.md) |
| 5 | Every classifier: VLM judges and the 25-config benchmark, HSV detector, live guard, cable-end CNN, gripper rules, joint-fault TCN/encoders, start-pose gate, the planned port finder | [report/05_classifiers.md](report/05_classifiers.md) |
| 6 | Simulators: virtual-clock stress sim, MuJoCo station stand-in, table v1 vs the rebuilt table, the referee, cluster and GCP runners | [report/06_simulators.md](report/06_simulators.md) |
| 7 | Experiments and results: latency, stress, judges, classifiers, MuJoCo, the 101-rollout prompt study, cloud round 1 | [report/07_experiments.md](report/07_experiments.md) |
| 8 | Teleop and data: Metal leader/follower, Quest and the continuity fixes, LeRobot v2.1 vs v3.0, `add_eef`, the 170-episode dataset | [report/08_teleop_and_data.md](report/08_teleop_and_data.md) |
| 9 | Fine-tuning: why `relay-final` aimed 5.5 cm high, the replay fixes, the EEF override, the recipe | [report/09_finetune.md](report/09_finetune.md) |
| 10 | Operations and failure modes: Dream errors (DA-1000, DA-4000, SVD, buffer-empty, session expiry), the macOS match laptop, secrets, safety | [report/10_operations.md](report/10_operations.md) |
| 11 | **Practical guide**: install, tests, stress sim, MuJoCo match, cluster/cloud, live station run, teleop, fine-tune, add a classifier, add a stage or prompt | [report/11_guide.md](report/11_guide.md) |
| 12 | Known issues, open TODOs, dead ends, what to do next | [report/12_known_issues.md](report/12_known_issues.md) |
| 13 | Verification log: test runs, number audit, unverified numbers, gaps, `report-inputs` | [report/13_verification.md](report/13_verification.md) |

## Executive summary

**The problem.** A five-minute, no-human-input relay on two brakeless MakerMods Metal arms (7 Damiao motors each,
three 640×360 cameras), driven by Dream Labs' hosted Dream-1 VLA behind an HTTP station agent. Stage 1: unplug USB
cables (pass bar ≥ 1); stage 2: a free-standing tower > 4 in; stage 3: legal golf balls into the bin, where binning
the white ball loses the match. Touching a later stage's object early is a DQ. The policy returns 48-row chunks
played at 20 Hz; the Metal bus runs at a fixed 30 Hz. The only controls are the words sent, when to stop/home/reset,
and fine-tune data.

**What we built.** A supervisory harness that never commands a motor (§3): a YAML stage state machine with two-look
confirmation, a success lock that stops the policy from undoing a pass, a 10 Hz local illegal-ball guard with its own
`/stop`, a guarded `/reset`, a prompt engine that composes every instruction from linted templates, a grip-assist
skill that regrips and pulls the plug with bounded `/move`s, station-side hedged Dream calls, VLM judges
(Gemini Flash-Lite for frequent looks, Sonnet to confirm), and read-only LiveKit and DimOS integrations. Around it:
a decision model with log-odds evidence, a hazard and a Q-value (§4, shadow mode only); seven classifiers (§5); two
simulators (§6); teleop recorders and a 170-episode fine-tune (§8–9).

**What the final code is.** `main@acc1672` (00:06 UTC 9 Nov) is Kaushik's bot line (`claude/engine-bot` +
`engine-bot-live`, with Darsh's prompt engine and the PR 9 LiveKit viewer) merged onto the 7 Nov team line. The match
plan is `plans/bot_v1.yaml` on **base** Dream-1 (`dreamlabs/dream-1-beta`), because the fine-tune was worse on every
task. Much of the work described in the paper — the station-retuned detector, the cable-end CNN, joint-fault nets,
`decide.py`, the start-pose gate, the realistic sim table, the cloud runners, the mac-arm64 speed-ups — lives on the
unmerged `cloud-sims` family, and the last six bot commits (`confirm_every_s`, stacking option B) are on
`claude/engine-bot-live`, not main (§2.4.2).

**Key results.**

| Result | Value | Mark |
|---|---|---|
| Dream round trip on the station (1,650 chunks, 52 runs) | median 1,116 ms, p90 2,733 ms; 66.7 % of chunks stop-and-wait; sync gap 3.54 s vs async 1.55 s | [V] |
| Station Dream errors (52 runs) | 4 DA-4000 refusals, 4 SVD, 4 empty buffers, 4 session replacements (7 Nov subset: 4/3/2/1) | [V] |
| Stress sim, 185 matches/config | final hybrid + live guard 69.8 pts, 0 illegal-ball losses; fast judge without the guard lost 28 | [V] from rows |
| Judge benchmark (60 decisions, 25 configs) | GPT-6.1 Terra 98 %/97 % at 2.8 s, $0.047/match; Sonnet 5.5 100/100 at 7.1 s; Haiku 5.5 low 100/93 at 4.5 s | [V] |
| Detector retune on station frames | held-out ball recall 20/27 → 26/27 at 100 % precision; bin no longer a "white ball" (1,286 → 0 of 2,010 frames) | [D] |
| **Main's live guard on committed held-white station frames** | **misses 2/2; the retuned config catches 2/2** | [V] |
| Cable-end CNN (MobileNetV3-small, ONNX via cv2.dnn) | USB-A recall 0.68 → 0.84 with 478 wrist crops; Blender data did not help | [D] |
| Joint faults, torque-free, real rollouts | encoder d32×2 blocked-joint timely recall 0.78 vs TCN 0.20 (plain recall ≈ 1.0 for both) | [V] |
| MuJoCo v1 table, base Dream-1, 25 matches | cable out in 14; put-down 0/54; stack 0/25; 3 DQs, all hub-left | [V] / [D] |
| Prompt study, 101 station rollouts / 171 steps | name the goal, not the motion; 8 of 12 harness pulls kept the plug; 9 of 15 mid-grasp rewrites sent while holding | [D] (171 [V]) |
| Cloud round 1 | 10 of 29 matches valid, 0–42 pts; killed by DA-1000 (out of credit) at ~28 sessions | [V] |
| Fine-tune `relay-final` | Quest demos' actions 5.5 cm above the tool (no gravity feed-forward); live tool stays 8–13 cm up vs 4–6 cm grasp height | [D] |
| Tests on main (9 Nov, this report) | 334 passed, 1 failed (`test_bot_v1_plan`), 10 skipped, 1 collection error without Dream's SDK | [V] |

**What is still unknown.** There is no per-stage success rate from full harness matches on the station; the real
judge was never run against the MuJoCo referee; the decision model never made a decision; the joint-fault nets have
never seen a real fault; and the most important open safety item is that main's illegal-ball colour config predates
the station retune (§12.1).

## How to read the numbers

- Simulation (stress sim, MuJoCo) and station numbers are kept apart in every table. The stress sim's unplug
  probability (0.8 per try) is far above base Dream-1's real ~1 in 6; its scores test harness logic only.
- The MuJoCo arm is an i2rt YAM, not a Metal arm; the first table (v1) used white balls as legal.
- Small counts throughout: 12 held-out detector frames, 137 cable crops, 5 test rollouts for joint faults, 30 guard
  questions per judge config, 3–26 steps per prompt family.

## Building the PDF and the Word file

`docs/TECHNICAL_REPORT.pdf` and `docs/TECHNICAL_REPORT.docx` are built from this file and `docs/report/*.md`:

```bash
docs/report/build/build.sh all
```

Needs pandoc; XeLaTeX with the DejaVu fonts for the PDF; and for the Word file Python with `lxml`, plus Node with
mermaid-cli and a Chromium to render the diagrams (`MMDC`, `PUPPETEER_CONFIG` and `PYTHON` override the defaults).
`build/pandoc_math.lua` turns the ```` ```math ```` blocks into equations (native Word equations in the .docx) and
sizes table columns by content; `build/mermaid_png.lua` renders each Mermaid diagram to a PNG for the .docx (the PDF
shows them as code); `build/docx_cleanup.lua` drops the "Back to the index" links; `build/reference.docx` sets
US Letter pages, 0.75 in margins and bordered tables; `build/fix_docx.py` puts `settings.xml` in schema order and
asks Word to fill in the table of contents when the file is opened (accept the prompt to update fields).
