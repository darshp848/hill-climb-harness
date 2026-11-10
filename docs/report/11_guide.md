# 11. Practical guide

[Back to the index](../TECHNICAL_REPORT.md)

Every command below was either run for this report (marked [ran]) or is copied from an in-repo guide with its source
named. Secrets are referred to by name only. Commands that move the arms are for a person at the robot with a hand on
the e-stop.

## 11.1 Install

Prerequisites: Python 3.12 or 3.13, git. The core harness needs only the standard library + PyYAML; the detector
needs numpy and Pillow; the cable-end classifier needs OpenCV; the MuJoCo sim needs `mujoco` and Dream's
`dreamlabs==1.0.37` (for `dream_examples.yam_mujoco`); headless rendering needs OSMesa or EGL.

Clone the three repos next to each other:

```bash
git clone https://github.com/darshp848/hill-climb-harness.git
```

```bash
git clone https://github.com/darshp848/task_simulation.git
```

```bash
git clone https://github.com/darshp848/follower_code.git
```

Create an environment and install the test dependencies ([ran] on Linux, Python 3.13):

```bash
python3 -m venv .venv && . .venv/bin/activate
```

```bash
pip install pytest pyyaml numpy pillow scipy opencv-python-headless mujoco pyarrow pandas psutil pyjwt
```

Headless Linux rendering for the MuJoCo tests ([ran] on Debian/Ubuntu):

```bash
sudo apt-get install -y libosmesa6 libegl1
```

```bash
export MUJOCO_GL=osmesa PYOPENGL_PLATFORM=osmesa
```

Optional extras: `pip install "dreamlabs==1.0.37"` (Dream SDK, needed for `sim_station.py --policy dream` and the
scene tests), `pip install livekit` (observer/viewer), `pip install dimos` (DimOS skills). The match laptop pins its
environment in `requirements-match.txt` on `mac-arm64`.

## 11.2 Run the tests

On main ([ran] 9 Nov: **334 passed, 1 failed, 10 skipped** with `tests/test_hill_climb_scene.py` ignored; see §13.1):

```bash
cd hill-climb-harness
```

```bash
python -m pytest -q --ignore=tests/test_hill_climb_scene.py
```

`tests/test_hill_climb_scene.py` fails at **collection** without Dream's SDK, because
`importlib.util.find_spec("dream_examples.yam_mujoco")` raises `ModuleNotFoundError` when the parent package is
missing instead of returning `None`. Either install `dreamlabs` or ignore that file. The one failing test,
`tests/test_grip_assist.py::test_bot_v1_plan`, asserts that the cable prompt contains no "toss"; 716c4d0 added
"toss the cable to the far right" back to `plans/bot_v1.yaml`.

A subset that needs nothing beyond PyYAML and numpy:

```bash
python -m pytest -q tests/test_runner.py tests/test_stress.py tests/test_prompt_engine.py tests/test_grip_assist.py tests/test_guard.py
```

On another branch, use a worktree so main stays untouched:

```bash
git worktree add ../wt-cloud origin/cloud-sims
```

```bash
cd ../wt-cloud && python -m pytest -q --continue-on-collection-errors
```

## 11.3 Run the stress simulator (no keys, no network)

List the scenarios ([ran] 24 on every branch):

```bash
python experiments/stress_match.py --list
```

All scenarios, 5 seeds, 8 processes, one JSON line per match ([ran] ~1–3 min on 4 cores):

```bash
python experiments/stress_match.py --seeds 5 -j 8 --out runs_stress.jsonl
```

One match, event by event:

```bash
python experiments/stress_match.py -s no_release --seeds 1 -v
```

At the slower judge-latency input (6.5–13 s, from a Haiku 5.5 check), against a chosen plan:

```bash
python experiments/stress_match.py --latency 6.5-13 --plan plans/hybrid_v4.yaml --seeds 5 -j 4
```

Watch one match in the browser at 10× (port 8765):

```bash
python experiments/live_stress.py -s black_ball --speed 10
```

A/B plan variants over every scenario:

```bash
python experiments/sweep_variants.py --latency 6.5-13
```

Summarise a JSONL file (mean points, losses):

```bash
python -c "import json,sys; r=[json.loads(l) for l in open(sys.argv[1])]; print(len(r), sum(x['points'] for x in r)/len(r), sum(x['lost'] for x in r))" runs_stress.jsonl
```

To aggregate the published 185-match sweep ([ran]):

```bash
cd ../task_simulation/results/stress/final_20261108 && python analyze.py
```

**Add a scenario**: append a `Scenario(...)` to `SCENARIOS` at the bottom of `experiments/stress_match.py`
(behaviour switches, `Fault(at, kind)`s, outages, camera spans, latency, or another `plan=`). If it guards a fix, add
an assertion to `tests/test_stress.py`.

## 11.4 Run a MuJoCo sim match

Prerequisites: `dreamlabs==1.0.37`, `mujoco`, a Dream key with **credit** in `~/.dreamlabs/credentials`
(`DREAM_API_KEY=...`). Probe the policy with one call first: a disabled key (DA-1000) fails every match.

Offline smoke test with the scripted policy (no key):

```bash
python sim_station.py --policy scripted --scene hill_climb
```

Terminal 1, the simulated station with real Dream-1 (README):

```bash
python sim_station.py --policy dream --scene hill_climb --open
```

Terminal 2, the harness against it:

```bash
python -m harness plans/sim_fruit_bowl.yaml --live --station http://127.0.0.1:8091 --view http://127.0.0.1:8093 --verifier time
```

A scored relay match with the oracle verifier and referee (`experiments/sim_match.py`; on `prompt-engine` add
`--clock sim` so timeouts mean sim seconds, and `--filler oracle|default|plan` for the prompt engine):

```bash
python experiments/sim_match.py --plan plans/engine_v4_sim.yaml --filler oracle
```

For the **rebuilt realistic table**, check out `cloud-sims` first (`sim_scenes/hill_climb_table.py` at 9c89ae3+). Render
it without a policy or network (task_simulation `paper` branch):

```bash
cd ../task_simulation && git checkout paper && python paper/render_sim_table.py
```

On macOS the 3D viewer needs `mjpython`:

```bash
.venv/bin/mjpython sim_station.py --policy scripted --scene hill_climb --realtime --viewer
```

## 11.5 Cluster and cloud batches

Home cluster (task_simulation README):

```bash
docker build -t hill-climb-sim -f cluster/Dockerfile cluster
```

```bash
PLAN=plans/hill_climb_v4.yaml cluster/run_matches.sh 3 2 my_label
```

Offline sweep on the cluster (task_simulation):

```bash
cluster/run_stress.sh 50 7 sweep1
```

```bash
SEED0=51 cluster/run_stress.sh 50 2 sweep1 happy black_ball
```

Google Cloud Batch (branch `cloud-sims`, `cloud/README.md`). Every script defaults to a dry run; `--execute` spends
money. Prerequisites: gcloud logged in to the billing account's owner, project `hill-climb-sims` with billing linked,
APIs enabled, the Dream key in `~/.dreamlabs/credentials` (setup pipes only `DREAM_API_KEY` to Secret Manager via
stdin), and for API judges an `anthropic-api-key` secret uploaded by you.

```bash
bash cloud/setup.sh --dry-run
```

```bash
bash cloud/build.sh --tag cloud-v1 --dry-run
```

```bash
bash cloud/submit.sh --tag cloud-v1 --job hill-climb-pilot-v1 --pilot --parallelism 8 --dry-run
```

After a successful pilot (check peak RSS against the 1,280 MiB request, camera freshness < 2 s, sim/wall rate, and
that Dream accepts the concurrency — **it did not at ~28 sessions on 8 Nov**), collect:

```bash
python -m cloud.collect --prefix gs://hill-climb-sims-results/runs/hill-climb-pilot-v1 --execute
```

Rules from `GOAL.md`: one change per arm, same seeds across arms, ≥ 20 valid matches per arm, accept a change only
if the 95 % bootstrap CI lower bound beats the previous mean without more losses; log every arm in
`HILL_CLIMB_LOG.md`; review live snapshots every 20 minutes; never let Batch re-run a paid match.

## 11.6 Run the harness live on the station

Start the station agent with the add-on (on the NUC, in tmux; `docs/HOW_THE_HARNESS_WORKS.md`, TELEOP_PLAN):

```bash
source ~/dream-env/bin/activate
```

```bash
PYTHONPATH=~/hill-climb-harness/station_addons dreamcli robot up --view --view-port 8093
```

With station-side hedged Dream calls (`plans/bot_v1.yaml:17-18`):

```bash
PAH_HEDGE_N=2 PAH_HEDGE_CUTOFF_S=1.5 PAH_HEDGE_MAX_CALLS=3 PYTHONPATH=~/hill-climb-harness/station_addons dreamcli robot up --view --view-port 8093
```

Check the add-on's light frames before anything moves:

```bash
curl -s -o /dev/null -w "%{http_code} %{size_download}\n" http://127.0.0.1:8091/frame/wrist_r.jpg?w=640
```

Dry run (reads status and frames, prints what it would POST):

```bash
python -m harness plans/bot_v1.yaml --dry-run --detect plans/table_colors.yaml --live-guard
```

The match command (keys injected by your secret manager, never on the command line; `--live` refuses without the
live guard when the plan has a guard). This is the command in `plans/bot_v1.yaml:24-26`, with the project-specific
secret-manager prefix left out:

```bash
python -m harness plans/bot_v1.yaml --live --no-operator --monitor-model google/gemini-3.1-flash-lite --detect plans/table_colors.yaml --live-guard
```

**Before you do:** main's `plans/table_colors.yaml` still has the phone-photo white range, which missed both
held-white test frames in this report's check (§5.2.4). Use the station-retuned colours from `fix/classifiers-8nov` (or
`fix/guard-real-frames`) and re-check on your own wrist frames:

```bash
git show origin/fix/classifiers-8nov:plans/table_colors.yaml > plans/table_colors_station.yaml
```

```bash
python -m harness.detect plans/table_colors_station.yaml frame.jpg --out boxed.jpg
```

Practice variants: `--skip cable_` (stack + balls only), `--skip cable_ --skip stack --skip tower` (balls only),
`--var model=dream-1-ft:...` (another checkpoint), `python -m harness.panel` (operator panel, practice only),
`--verifier monitor+manual` (your `y`/`n` overrules the judge).

Read-only tools for the guard on live frames and recorded video (`fix/guard-real-frames`): `tools/guard_watch.py`,
`tools/guard_replay.py`. LiveKit viewer for the referee (+10):

```bash
LIVEKIT_URL=wss://<your-project>.livekit.cloud LIVEKIT_ROOM=<room> python -m harness.livekit_view --port 8098
```

(with `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` or `LIVEKIT_TOKEN` in the environment). DimOS skills (+5):

```bash
python -m integrations.dimos_skills plans/hill_climb_v4.yaml --mcp-port 9990
```

At the end:

```bash
dreamcli robot down
```

Logs are in `runs/<UTC timestamp>/`. Summarise them with `python -m harness.report runs/ --by-checkpoint`
(`--last N` for the newest N); `tools/bot_report.py [--open]` builds an HTML page over the run folders hard-coded in
its `RUN_DIRS`.

## 11.7 Collect teleop data (Metal)

Leader/follower on the station, inside tmux, in the kit's venv (follower_code README). The kit's venv needs
Pinocchio first:

```bash
cd ~/physical-ai-hacks && uv pip install "pin>=3.1,<4" "cmeel-urdfdom>=4,<5" "cmeel-tinyxml2<11" numpy==2.2.6
```

```bash
.venv/bin/python ~/follower_code/metal/leader_collect.py --config configs/station.yaml --dry-run
```

```bash
.venv/bin/python ~/follower_code/metal/leader_collect.py --config configs/station.yaml --task "unplug the USB cable at the end of the hub nearest the right arm"
```

Quest teleop (gravity feed-forward on by default since e87a17a):

```bash
.venv/bin/python ~/follower_code/quest/quest_collect.py --config configs/station.yaml --dataset-dir ~/hackathon_data/my-task
```

After recording: split long takes, freeze the leader's wrist camera, add EEF columns, deep-check:

```bash
python tools/split_episodes.py --help
```

```bash
~/dream-env/bin/python tools/add_eef.py ~/hackathon_data/my-task --arm metal --replace
```

```bash
python tools/check_dataset.py ~/hackathon_data/my-task --videos
```

Check tracking per joint, and — new rule from §9 — **screen every folder for steady-state action − state offsets
above ~1 cm** before training:

```bash
.venv/bin/python ~/follower_code/metal/tools/tracking_report.py ~/hackathon_data/my-task
```

Check that a plan's prompts all have demos:

```bash
python -m harness.tasks plans/hill_climb_v4.yaml ~/hackathon_data/unplug-v1 ~/hackathon_data/relay-v1
```

## 11.8 Fine-tune

From TELEOP_PLAN.md. In a shell that holds the fine-tune account's key (`read -rs DREAM_API_KEY; export DREAM_API_KEY`):

```bash
tmux new -A -s finetune
```

```bash
~/dream-env/bin/python ~/hill-climb-harness/tools/finetune_watch.py start ~/hackathon_data/ft-final --name relay-final --suffix relay-final --max-requeues 1 --i-accept-charges
```

```bash
cat ~/finetune_watch/STATUS.txt
```

Before using a new checkpoint: Dream's own loop/model check (moves the arms in small circles; 8 billed calls):

```bash
~/dream-env/bin/python -m dream.check --recording ~/hackathon_data/ft-final/quest-split-v4-final-eef -- ~/dream-env/bin/dreamcli robot run --checkpoint dream-1-ft:<account>-<suffix> --task "unplug the USB cable at the end of the hub nearest the right arm"
```

Then a 5 + 5 A/B on Stage 1 against base, alternating run by run:

```bash
python -m harness plans/bench_cable.yaml --live --verifier monitor+manual --runs-dir runs/bench --var model=dream-1-ft:<account>-<suffix>
```

```bash
python -m harness.report runs/bench --by-checkpoint
```

For a fine-tune, prompts go byte for byte (`prompt_engine.mode: verbatim`, `--no-rewrite`, no `tower_prompts`).
To correct the Quest gravity sag, follow §9.6 (relabel actions as measured joints ~3 frames later, then
`add_eef --replace`). Budget ~5 h and ~$100 for the 500-step minimum.

## 11.9 Add a classifier

1. Put the model in `harness/<name>.py` with a pure function `frames → result` and no network. Keep runtime deps to
   numpy/OpenCV (the cable-end model shows the pattern: ONNX via `cv2.dnn`, fixed batch).
2. Decide where it plugs in: **(a)** boxes on the monitor's frames (like `TableDetector.annotate_frames` and
   `CableEndClassifier`), **(b)** a fast guard (`runner.fast_guard` / `LiveGuard(judge=...)` must return
   `(True|False|None, why)`), **(c)** a decision feature (`decide.DoneWeights.w_*` / `statespace` inputs, weight 0
   until a sim sweep says otherwise), or **(d)** a gripper-style sensor note (`runner._grip_note`).
3. Add a CLI flag in `harness/cli.py` next to `--detect` and wire it in `main()`.
4. Tests: a unit test on committed frames (people-free crops only), and if it can stop the robot, an equivalence or
   regression test like `tests/test_guard_real_frames.py`. If it can change a guard verdict, add a stress scenario.
5. Evaluate on **station frames held out by run**, report precision/recall at the operating threshold, and say which
   frames were used (keep frames with people out of the repo).

## 11.10 Add a plan stage or prompt

A stage (see `plans/bot_v1.yaml` and `harness/plan.py` for every key):

```yaml
  - name: ball_extra                   # unique; prefixes matter: cable_, stack/tower/higher, ball_
    prompt: "With the right arm, pick up the orange golf ball on the right side of the table and drop it into the white basket."
    success: "Compared with the stage-start images, is there one more legal golf ball inside the bin?"
    confirm: 1                         # 2 for any pass bar
    confirm_check: "Count the legal balls in the bin at STAGE START and NOW. Is NOW higher, with no white or black ball in the bin?"
    timeout_s: 60
    max_attempts: 2
    on_fail: skip                      # never skip a pass bar: that is a DQ
    requires_stage: tower              # gates: balls only after the tower passed
```

Rules learned on the station: one short action naming one object by colour and position; no negations; no arm named
for stacking; the safety sentence (`prompt_prefix: "{safe}"`) on every cable prompt, and only while it is true; never
re-prompt while a gripper holds the target; lock a pass with `stop_on_pass` + `lock_prompt`; give pick-and-place
steps at least 40–60 s.

A template in the prompt engine (`plans/engine_bot.yaml`):

```yaml
prompt_engine:
  templates:
    ball:
      text: "<arm> pick up <ball> <where> and drop it into the white basket"
      slots: {arm: arm, ball: ball, where: where}
      note: "one named ball near the right arm"
```

Then reference it with `template: ball` (and `variants: [ball_goal]` for second tries) in the stage. `load_plan`
lints every vocabulary option at load and refuses the plan on a negation, an arm in an `arm: none` template, a
`forbid` phrase or an unfilled slot. Run the plan tests and the stress sim after any change:

```bash
python -m pytest -q tests/test_match_plans.py tests/test_prompt_engine.py
```

```bash
python experiments/stress_match.py --plan plans/engine_bot.yaml --seeds 3 -j 4
```
