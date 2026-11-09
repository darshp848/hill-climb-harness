# Kaushik's loose hackathon code (7–8 Nov 2026)

Code that lived only on Kaushik's Mac or the station, collected so the team has it. None of it is wired into the
harness on `main`; the harness work itself is on its own branches (see the bottom).

| Folder | What it is |
|---|---|
| `first-harness/` | The first harness: `harness.py` driver, `referee.py` judge, sweep plans (`sweep/unplug-4.json`, `unplug-pinch.json`), referee test cases and eval log. |
| `conductor/` | Claude conductor loop (`conductor.py`) and `grasp.py`: Jev-box servoing for the cable grasp (`calibrate`, `center`, `roll`, `advance` with no overshoot). |
| `station_addons/move_addon.py` | The station's `/move` and `/pose` routes (IK moves, sag correction). `sitecustomize.py` (frames, `/arms`) is already in `station_addons/` at the repo root. |
| `teleop/` | Teleop dashboard additions for the organizers' kit, as patch scripts: mode buttons and A/B/home presses (`patch_dashboard.py`), follower gripper slider (`patch_grip.py`), the mode supervisor (`teleop_supervisor.sh`) and the zero-shot run archiver (`archive_runs.sh`, copies `~/.dreamlabs/last-run*` to `~/rollouts/` after each run). |
| `kbd_teleop/` | Keyboard teleop client and collector. |
| `tools/` | Dataset tools: `trim_episodes.py`, `v3_to_v21.py`. |
| `PREP.md` | Pre-hackathon prep notes. |

**Only on the station, not captured here** (the station was offline when this was collected): the Quest joystick
extras in the kit's `collect.py`/`quest.py` (stick holds the gripper, left stick steps the scale), and the
`leader_collect.py` edits in `~/follower_code` (dashboard grip override with ramp, kinesthetic mode, read retry).

**Data is not in git:** datasets in `~/hackathon_data` (ft-final-v2 = the 170 clips the fine-tune trained on) and
79 zero-shot runs in `~/rollouts/` on the station; `data/` (629 MB) on Kaushik's Mac.

**Harness branches on this repo:** `claude/harness-stitch` (PR #5, the final match harness), `claude/bot-prompt-writer`,
`hybrid-harness`, `claude/engine-bot-local`, `combined`, `kaushik-fixes`, `sim-and-move-fix`, `light-frames-traces`,
`kaushik/cloud-sims-bot-plan`, and `claude/sim-hillclimb` (MuJoCo hill-climb).
