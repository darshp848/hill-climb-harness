# 8. Teleoperation and data collection

[Back to the index](../TECHNICAL_REPORT.md)

## 8.1 Recorders (`darshp848/follower_code`)

| Recorder | Arm | How | Writer | Status |
|---|---|---|---|---|
| `leader_follower.py` | i2rt **YAM** | one arm in i2rt zero-gravity mode is the leader, the other copies joints (rate-limited 2 rad/s); stops if the follower lags > 0.35 rad for 0.5 s | `runs_teleop/<time>/episode_NNN/` (`data.npz`, `meta.json`, per-camera mp4) | **refuses to run unless `~/.dreamlabs/station.yaml` says `body: yam`** (047a27e): the hackathon stations are Metal |
| `metal/leader_collect.py` | MakerMods **Metal** | one arm runs MakerMods' gravity-compensated `metal_leader` (kp = 0, Pinocchio gravity + friction feed-forward), moved by hand; the other follows | the kit's `dhk-collect` (dashboard, cameras, device checks, station lock, homing, lag check) and its **LeRobot v2.1** writer | used for the 97 leader episodes |
| `quest/quest_collect.py` | Metal, Meta Quest headset | the kit's `dhk-collect` with four pieces swapped in (below) | same v2.1 writer | written after the Quest data in the fine-tune was recorded |
| The kit's own Quest teleop (`dhk-collect`) | Metal | Quest controller → IK → the kit's `MetalFollower` | v2.1 | used for the **73 Quest episodes**; sends **zero torque**, so the arm sags (§9) |

### 8.1.1 Metal leader/follower details (`metal/leader_collect.py`)

- Keys (type, then Enter): Enter opens the arms, then starts or saves an episode; `d` discards; `h` sends the follower
  home; Ctrl+C walks both arms back to their start pose and releases them.
- `--leader left` (default). By default the leader's side records its **parked** pose (at deploy time that arm stays
  parked); `--record-leader` records its real motion. The left wrist camera still sees the leader and the hand, which
  is why the `-frozen` dataset variants replace that camera with a still (`tools/freeze_camera.py` in the harness).
- Follower: closes the gap at 0.4 rad/s for ≤ 3 s, then follows at `--follow-speed` 4 rad/s (MakerMods' 280°/s cap
  still applies); re-syncs if > 0.35 rad behind.
- **Gravity feed-forward (`FollowerGravity`, on by default, fdf4bf8):** without it every joint sits under its command by
  gravity torque / kp. The first real episodes showed shoulder_lift +4 to +6° and elbow_flex +2.5 to +4.3° on average,
  and the old 1.5 rad/s cap was hit on every joint. With MakerMods' own Metal gravity model in the follower's MIT
  frames, shoulder-lift tracking went from 4–6° low to **+0.2°**. `--no-gravity-ff` turns it off.
- `metal/tools/tracking_report.py <dataset>` prints per-joint tracking error of any dataset.
- Tests: `cd ~/physical-ai-hacks && .venv/bin/python -m pytest -q ~/follower_code/metal/tests` runs the kit's real
  Collector and writer over fake Metal devices, then `tools/add_eef.py` (Dream's dataset checks). "All 10 pass on the
  hackathon NUC" [D]. They need the kit and cannot run outside it.

### 8.1.2 Quest teleop and the continuity fixes

`quest/quest_collect.py` on `main` (6bc0576, e87a17a):

| Problem | Fix |
|---|---|
| Wrong direction | heading calibration: press **Y**, move the right controller ~20 cm forward, press **Y**; saved to `~/.cache/follower_code/quest_heading.json` |
| Jitter | a **One Euro filter** per controller pose, restarted at every engage; IK damping 1e-3 → 1e-2 |
| Lag | loop at 60 Hz instead of 30; joint cap 2.5 rad/s instead of 1.5 |
| Arm sits low | gravity feed-forward on by default (Metal), the same MakerMods model (e87a17a, 8 Nov 01:19 UTC) |
| Wrist goes wild | 2° rotation dead band, `--rotation-scale`, **X** locks/unlocks the wrist |

Offline on the kit's test arm with 2 mm tracking noise: tool jitter 4.7× lower than the kit's (0.47 vs 2.2 mm) at
~17 ms more lag on a 5 cm move [D]. Tests: 14 pass over the kit's fakes [D].

`quest-continuity` (not merged) adds `quest/continuity.py`:

- **`WristFlipGuard`** (line 26): stops big joint swings near the joint-5 limit. In a replay of 40 fast wrist events,
  7 swung a wrist joint > 90° with the kit's IK and 2 with the guard, tool position held within 8 mm [D].
- **`TrackingJumpGuard`** (line 74): a controller jump of 8 cm or 45° in one reading is a glitch; the arm holds until
  the clutch re-engages.
- **`per_joint_caps`** (line 102): wrist joints 4 rad/s instead of 2.5, plus a faster rotation filter: wrist lag
  105 → 27 ms in the offline replay [D].
- Homing: a low tool lifts straight up at 0.1 m/s before the joint-space move home; pressing home twice within 2 s
  discards the episode and homes.
- `quest/bench/` (`bench_ik.py`, `resp.py`, `final.py`, `ratio.py`, `guard.py`, `metal.urdf`): the replay bench behind
  these numbers; needs numpy, pandas, pyarrow ≥ 21, mujoco and mink 1.1.0 and the datasets copied off the NUC.
- **Never run on the robot**: the NUC dry run was held at 02:49 UTC 8 Nov while the team was recording; the 5 new
  tests have not run on the NUC either. An uncommitted NUC edit to `metal/leader_collect.py` on this branch is saved as
  `nuc/follower_code_quest-continuity_uncommitted.diff` on `station-tuning` (dashboard grip override with ramp,
  kinesthetic mode, read retry per Kaushik's notes).

## 8.2 Dataset format: LeRobot v2.1 vs v3.0, and `add_eef`

- Dream's fine-tune takes **LeRobot v2.1** (one parquet and one mp4 per camera **per episode**). The kit's
  `dhk-collect` writes v2.1, so no conversion was needed.
- MakerMods' own recorder writes **LeRobot v3.0** (many episodes per parquet and one mp4 per camera). Kaushik wrote
  `tools/v3_to_v21.py` (on `kaushik/day1-extras`) as a backup; on a 20-episode MakerMods dataset (13,411 frames) it
  passes Dream's validator [D: `extras/kaushik/PREP.md`]. "Format risk resolved 2026-11-07: the kit's `dhk-collect`
  writes v2.1."
- **`tools/add_eef.py`** (the kit's, run with `~/dream-env/bin/python tools/add_eef.py DATASET --arm metal [--replace]`)
  adds end-effector columns by FK and runs Dream's dataset checks. Metal fine-tunes require `use_eef=True`. The FT
  diagnosis checked `add_eef`'s FK against the URDF with a 0.135 m tool: < 0.001 mm.
- Dataset tools in the harness: `tools/split_episodes.py` (cut long takes into task episodes, each with its own text),
  `tools/auto_split.py` (picks the working arm and labels pick-and-place), `tools/freeze_camera.py` (replace one
  camera with a still "parked" view), `tools/check_dataset.py` (deep checks beyond Dream's `validate_dataset`: meta vs
  tables, index and timestamp continuity, NaN, ranges, one working arm with the other parked, frame counts, frozen
  leader cameras, duplicates, how each task ends), `tools/label_rollout.py` (label Dream's own rollouts with harness
  outcomes), `tools/review_page.py` (`tools/episode-review-page`, David's keep/drop review page).

## 8.3 What was recorded (TELEOP_PLAN.md)

Decision at 17:50 on 7 Nov: **one fine-tune on UNPLUG and PUT-DOWN** ("Unplug is everything": Stage 1's pass bar;
base Dream-1 unplugs ~1 try in 6). Stacking and balls stay on base skill; the fine-tune mixes in Dream's data
(`base_share` 0.5).

Task texts (set in the dashboard before every take): **T1** `unplug the USB cable at the end of the hub nearest the
right arm`; **T2** `put the held cable down on the table away from the hub`; plus T3 plug-in, T4 block, T5 ball, T7
cylinder in the hole.

`~/hackathon_data/ft-final`, built 22:30 from 42 takes (13:20–21:07), unplugs re-cut to the top of the pull, David's
review applied:

| Folder | Source | Episodes |
|---|---|---|
| `quest-split-v4-final-eef` | Quest 18:12–18:31, David's 9 drops | 65 |
| `unplug-split-v1-final-eef` | Quest | 5 |
| `unplug-v1-split-eef` | Quest | 3 |
| `leader-split-v4-final-frozen-eef` | leader takes 25–35 | 28 |
| `leader-split-v6-frozen-eef` | leader takes 39–40 | 21 |
| `leftarm-split-v5-frozen-eef` | left arm 36–38, 41 (balls, cylinders, blocks) | 16 |
| `leader-split-v1-frozen-eef` | leader | 14 |
| `plugin-split-v1-final-frozen-eef` | leader | 11 |
| `stack-split-v1-frozen-eef` | leader-right | 4 |
| `leader-split-v2-frozen-eef` | leader | 3 |
| **Total** | **73 Quest + 97 leader** | **170** |

Per task: 69 unplug, 55 put down, 19 plug-in, 12 ball, 10 cylinder in a block's hole, 4 block, 1 cylinder on top;
26.9 min; **48,413 frames** at 30 fps (Dream's check) — the kit's check said 48,428. State 34 wide, action 32. Final
checks at 23:00 all passed (`check_dataset.py --videos`: 0 errors, 0 warnings; laptop copies byte-identical).

Human teleop speeds: unplug median 8.5 s (15 demos), put-down 4.8 s (9 demos) [D].

## 8.4 Data still missing at the end

From `docs/LAST_NIGHT_2026-11-08.md`: more stack demos (any colour order), golf-ball demos (had 1), watcher-arm demos
(none), harness runs with per-stage success, real motor torque while pulling (`candump` not on the NUC;
`harness/can_live.py` receive-only listener on cloud-sims), grip at 2.0 N·m (later done), station frames for the
detector (later done on `fix/classifiers-8nov`), and unplug demos **from the home pose** (§9).
