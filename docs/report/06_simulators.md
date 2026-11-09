# 6. Simulators

[Back to the index](../TECHNICAL_REPORT.md)

Four simulators exist, plus two offline benches. They test different things and must not be mixed.

| Simulator | Where | Policy | Judge | Clock | Tests | Blind spot |
|---|---|---|---|---|---|---|
| Virtual-clock stress sim | `experiments/stress_match.py` (main and every branch) | rule-based table model | scripted from table state at frame time | virtual (5-min match in ~1–5 s) | harness logic under faults | no physics; judge never errs unless told; unplug p = 0.8/try is far above reality |
| MuJoCo station stand-in, v1 hub table | `sim_station.py` + `sim_scenes/hill_climb_table.py` (main) | **live Dream-1** API | oracle over `/objects` | real time (or sim time on `prompt-engine`) | policy + harness on a physical table | YAM arm, not Metal; stand-in objects; white balls legal |
| MuJoCo, rebuilt realistic table | same files on **`cloud-sims`** (9c89ae3, 0f47c0d) | live Dream-1 | oracle or API judge | real or sim time | as above, closer to the station | YAM arm; no clean comparison runs (credits ran out) |
| Kaushik's MuJoCo station sim | `sim/` on `sim-and-move-fix` | live Dream-1 | rubric eval | real time | wrist-camera calibration, gains (`sim/calib/`) | abandoned branch |
| Metal driver bench | `station_addons/bench_metal_tick.py`, `fake_damiao.py` (`station-tuning`) | replayed rows | — | — | Dream's Metal driver on a fake Damiao bus | no physics beyond the bus |
| Metal replay sim | `station_addons/bench_replay.py`, `metal_plant.py`, `sweep_replay.py` (`station-tuning`) | replayed `commanded_r` rows | — | — | gravity-sag and EEF fixes for the fine-tune (§9) | open loop, no contact |

## 6.1 The virtual-clock stress simulator (`experiments/stress_match.py`)

The real `Runner` and real `MonitorVerifier` run **unchanged**. Around them:

- **`VClock`**: virtual time in 0.25 s steps (`STEP`); every harness sleep advances it, so a judge call returns only
  after its simulated latency and the races between checks, verdicts and the policy happen as they would live.
- **`Table` + `Behaviour`**: cables in/out of the hub, what each gripper holds, the tower, the bin. Each task prompt
  (T1–T6 of TELEOP_PLAN) has a try time and a success chance. Switches: `releases` / `open_frees` (the policy won't
  let go of the plug), `black_p` (picks an illegal ball), `reach_across_p` (brushes a block during unplug = DQ),
  `knock_p`, `unplug_never`.
- **`SimStation`**: the station API in-process with `Fault(at, kind)` and `refuse={prompt substring: n}` using the
  exact error types and messages from `data/station_nuc_20261107/robots-inference.jsonl` (DA-4000, ControlLoopError,
  LinAlgError, DA-5000, dead arm).
- **`SimFrames`**: frames carry their capture time: freeze, empty, corrupt (non-JPEG) and lag (`frame_lag_s`).
- **`Scripted`**: replaces only the HTTP call to Claude. It finds the stage in the real prompt the harness built and
  answers from the table **as it was at the newest frame's time**. Options: `false_done`, API `outages`, 400 on a
  corrupt image, `fast_guard` (stands in for the local detector).
- **`referee()`**: rubric v4 from the table's event log: points, DQ (block touched before a cable is out, ball before
  the tower passes), illegal ball in the bin = loss.

**Scenarios.** The committed `SCENARIOS` dict has **24** entries on every branch (`--list`): happy, unplug_never,
slow_unplug, no_release, stuck_cable, false_done_unplug, refusal_at_start, refusal_twice, starve_mid, linalg_mid,
gateway_x3, refusal_put_down, arm_loss, api_outage_30s, api_outage_balls, black_ball, black_ball_slow_monitor,
black_ball_lagged, black_ball_local_guard, black_ball_local_guard_api_down, frozen_cam, empty_frames,
corrupt_first_frame, reach_across. The **37-scenario** sweep cited everywhere (`FINAL_HARNESS.md`, the paper) also
contains api_flaky, black_heavy_lagged, black_heavy_lagged_local, calibrated_base, calibrated_base_faults,
calibrated_ft, false_done_all, frozen_long, knocky_tower, realistic, realistic_faults, refusal_storm and
slow_everything; those 13 were defined in a local driver (`results/stress/final_20261108/jobs.txt` runs
`--tree <Windows scratch worktree>`), **not in any committed file** (§13). The `calibrated_*` scenarios use base
Dream-1's real unplug rate (~1 try in 6) and are the realistic rows.

CLI: `-s/--scenario` (repeatable), `--seeds`, `--seed0`, `-j/--jobs`, `--out FILE.jsonl`, `--list`, `-v`,
`--latency S|LO-HI`, `--plan`. `experiments/live_stress.py -s NAME --speed 10` replays one match in the browser
(port 8765); `experiments/sweep_variants.py` A/Bs plan variants over every scenario at a given latency.

Results are in §7.2.

## 6.2 The MuJoCo station stand-in (`sim_station.py`)

A drop-in replacement for Dream's station agent: the same HTTP routes on 8091 and the same debug view on 8093, on
**Dream's MuJoCo YAM example** (`dream_examples/yam_mujoco` from `pip install dreamlabs`), using Dream's own control
loop (camera windows, `get_action`, `validate_chunk`, 20 Hz stepping) and the **live Dream-1 API**. The harness cannot
tell it from the station. Extra routes: `/objects` (ground truth), `/joints` (cloud-sims), `/live` (three policy
cameras, a ground-truth top-down map, the referee, station state and round trips).

Modes: `--policy dream [--checkpoint ...]`, `--policy scripted` (small sinusoidal chunks, no key, no network),
`--scene hill_climb`, `--no-realtime`, `--viewer` (MuJoCo's 3D window; `mjpython` on macOS), `--open`. Headless:
`MUJOCO_GL=egl` (GPU) or `osmesa` (software).

`experiments/sim_match.py` runs the unchanged harness and a plan against it with an **oracle verifier** (reads
`/objects`), reports "not met yet" as can't-tell like the real monitor, and scores with the referee. `--clock sim`
(77dce31, on `prompt-engine`, default there) runs the harness on simulation time, because the cluster's Inspiron ran
the sim at ~0.28× real time: a 295 s wall-clock match gave the robot 79–87 s of motion, so every timeout was ~3.5×
too short. `--filler oracle|default|plan` picks the prompt engine's slot filler.

### 6.2.1 Table v1 (7 Nov; main)

`sim_scenes/hill_climb_table.py` on main: a stand-in. A silver four-port hub (two plugged, two loose cables), ten
plain blocks, six **white golf balls counted as legal** and black 8-balls illegal, a wire bin; the hub placed **in
front of the right arm** "as the plan's prompts assume". It got the rules right and the scene wrong. Audit A17:
"main's MuJoCo table treats white balls as legal". The cloud runner translated ball colours in the plans in memory
for this table (`cloud/run.py`).

### 6.2.2 The rebuilt realistic table (8 Nov; `cloud-sims` only)

Commits 0f47c0d (11:45 UTC) and **9c89ae3** (12:00 UTC), modelled on station photos:

| Object | Model |
|---|---|
| Power strip | ALESTOR 12-outlet 4-USB surge protector at catalogue size 25.7 × 9.7 × 3.0 cm: black body, two raised 6-outlet panels, red switch, lights, cord; **USB-C + 3 USB-A** slots in a row at the robot end; plugs go in vertically |
| Cables | USB-A plug, orange shells, black reliefs, a 7-link ball-jointed orange cord, USB-C plug with a tip site; the referee seats USB-A in a free A port or the USB-C tip in the C port |
| Blocks | 6 cm cubes with a 2.8 cm through hole (blue, magenta, blue); yellow 2 × 2 × 10 cm square stick; magenta 2.5 × 11 cm round stick; tower height over every geom of a block |
| Basket | white, slotted, ~26 × 18 × 9 cm |
| Balls | green, yellow, orange legal; **white illegal** (`eight_0`), a black 8-ball (`eight_1`) illegal; `SIM_BLACK_8BALLS=N` adds more |
| Table | light laminate, tuned so the white ball stays detectable; `plans/sim_table_colors.yaml` measured from a segmentation render |

Rendering: three 4096-px shadow maps and 4× MSAA cost ~320 ms per three-camera capture under software rendering, so
physics ran at 0.55× the match clock; `apply_render_quality` drops shadows and MSAA under software rendering
(~50 ms per capture, real time); full quality with a GPU (EGL on cluster nodes). `tools/render_bench.py` measures it.
`paper/render_sim_table.py` (task_simulation `paper`) renders this table with no policy and no network.

### 6.2.3 Which results came from which table

| Result | Table | Branch at run time |
|---|---|---|
| 25 cluster matches, 7–8 Nov (`results/cluster_mujoco/`) | **v1** hub table | main / codex sim-table line |
| Sim-time template-engine matches (six matches 42, 42, 41, 15, 15, 15) and the relay-order batch (12/12 first unplug) | **v1** (they branched from main) | `prompt-engine` |
| Stage curves in `plans/stage_curves_sim.json` | v1 (Dream's YAM sim runs before 9c89ae3) | cloud-sims |
| Cloud round 1 (r1-a/b/c/E) | **both**: it ran while the table was being rebuilt; its saved frames show both versions | cloud-sims images `cloud-v*` |
| Paper teaser figure (b, c) | rebuilt | `paper/render_sim_table.py` |

No clean comparison runs exist on the rebuilt table, because the Dream credits ran out (§10).

## 6.3 The rubric referee

`sim_scenes/hill_climb_table.py` scores rubric v4 from object poses each tick: `unplugged`, `seated`, `tower_in`
(height in inches), `stage1_at`/`stage2_at` (pass-bar times), `golf_in_bin`, `eight_in_bin`, `first_touch` per object
(for DQs: e.g. `{"plugged_1": 51.95, "loose_0": 161.05, "world": 204.6}`), `dq`, `loses`, `points`. Tests:
`tests/test_hill_climb_scene.py` (places objects by hand and checks scoring; needs `dream_examples`). Audit A15/A18:
the simulated tower gate does not establish a supported, stable tower, and the bin check counts objects below the
table and does not require rest/release (sim-only issues, not re-verified).

## 6.4 The home cluster runner (`cluster/`)

`cluster/Dockerfile` + `cluster/run_matches.sh` (main; `run_stress.sh` in task_simulation). The head node (an
Inspiron: 8 threads, Intel iGPU, no CUDA) holds the image and a shared folder. `run_matches.sh W M LABEL` gives each
of W workers its own container (own ports), mounts the Dream key read-only from `~/.dreamlabs/credentials`, and
staggers start-up by 20 s; about 3 MuJoCo workers fit. `cluster/run_stress.sh N J LABEL [scenarios]` runs the
offline sweep over N seeds on J cores; `SEED0=` splits seed ranges across nodes. On cloud-sims, `cloud.cluster`
(c2fe683, 77fa186) runs the **same image and entrypoint** as GCP on the home cluster (push, keys, run, status, pull,
stop), running containers as the node user with readable sources and keys via gcloud without a shell.

## 6.5 The Google Cloud runners (`cloud/`, branch `cloud-sims`)

`cloud/README.md`, `COST.md`, `RESEARCH.md`, `docs/GCP_CONSOLE_GUIDE.md`.

- `run.py` wires Runner, station, light frames, detector, live guard, start-pose gate and read-only lessons together
  without an operator console; records the effective plan, its hash, the original plan hash, versions, skips and the
  colour adaptation in the manifest.
- `judge.py`: `--judge oracle` (the monitor's two-call async scheduler with referee answers captured at request time
  and uniform 6.5–13 s delays; unknown questions stay unknown) or `--judge api` (the real Claude monitor on the API,
  never the subscription CLI) or OpenRouter judges (race/alternate, hedged). Oracle and API differ in perception and
  rewrites, so their scores are not interchangeable.
- `entrypoint.py`: a fresh process and sim per match, JSON and logs under `/out`, upload, then a completion marker;
  each Spot attempt has a unique prefix; only complete attempts are collected. One experiment arm per job
  (`--config`, `--seeds`, `--seed-start`, `--run-args`).
- `setup.sh`, `build.sh`, `submit.sh` → `control.py` (default **dry-run**; `--execute` to change anything);
  `collect.py` (dedups Spot retries by job/config/seed). Cloud Build runs render/API/detector checks before
  publishing an image.
- Safety rails added after round 1 started: **never retry a paid match** (c80189c), mark infrastructure-broken
  matches **invalid** (`invalid_because`), preflight upload before any paid match, upload errors logged, live
  snapshots (`live/scene.jpg`, `live/status.json`) every 15 s uploaded every 30 s.
- Cost and quota (report-inputs Codex reports): one match per `e2-highcpu-2` Spot VM; a smoke match took 301 s wall,
  137 CPU-seconds, 689 MiB peak RAM, and advanced physics 194 s; at an **assumed** $0.030/VM-hour, 1,000 matches ≈
  $2.50 of compute, plus Dream usage (~5,000 min for 1,000 matches), API judges, storage and builds. The project
  budget was $50 with 50/90/100 % alerts. **Both CPU quota increase requests (to 200) were denied**, leaving 32
  (all regions) and 24 (us-central1 E2) vCPUs; Spot obtainability for 100 VMs was 0.9 in `us-central1-f`. The
  `pilot2-*` jobs ended CANCELLED with upload failures and no `match.json`.
- The default matrix was 1,000 matches (500 seeds each for `hill_climb_v4` and `hybrid_v4 --skip cable_extra`); round 1
  used 8 seeds per arm.
- Secrets: the Dream key goes from `~/.dreamlabs/credentials` to Secret Manager via gcloud's stdin and is never
  printed; the Anthropic key is uploaded by the user; worker identities can read only those two secrets and cannot
  delete results.

The `GOAL.md` targets (Darsh, 8 Nov) that these runs were meant to hill-climb:

| # | Target | Done when |
|---|---|---|
| 1 | Dream calls at 2 Hz, arms moving during inference | median ≥ 2.0 Hz in valid matches, sim/wall ≥ 0.95 |
| 2 | Every pass-bar stage succeeds | 100 % over the latest 100 valid matches |
| 3 | Zero losses (DQ, illegal ball) | 0 in the latest 100 valid matches |
| 4 | Points as high as possible | each accepted change raises the mean with the 95 % bootstrap CI lower bound above the previous mean; ≥ 20 valid paired matches per arm |
| 5 | Infrastructure works | 100 % valid, 0 paid re-runs |

`HILL_CLIMB_LOG.md` round 1 (arms A baseline, B async 2 Hz + notes, C B + real judge, D B at 1 Hz) is **empty**:
no arm produced comparable valid matches (§7.7).
