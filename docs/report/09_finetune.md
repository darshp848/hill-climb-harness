# 9. Fine-tuning: why `relay-final` aimed high

[Back to the index](../TECHNICAL_REPORT.md)

Primary source: `docs/ft_diagnosis/FT_DIAGNOSIS_2026-11-08.md` on branch `ft-diagnosis` (PR 3). Everything there was
read from the NUC over SSH (no robot commands, no acting station calls, no Dream calls). Marks as in §7.

## 9.1 The job

| Item | Value |
|---|---|
| Model | **`dream-1-ft:bb4b9e5f-relay-final`**, ready 07:02, warm 07:14 (8 Nov) |
| Job / dataset | `ftj_fdff469db8e2b643` / `ds_5ae1c3b4e8739af1` (`relay-final`), queued 22:54–23:00 7 Nov from tmux `finetune` |
| Data | 170 episodes, 48,413 frames (§8.3) |
| Hyperparameters | 1,000 steps, lr 1e-5, batch 32, `use_eef: true`, constant schedule, `base_share` 0.5; final loss 0.0034 |
| Training time | queued 23:00, training 23:20 → 05:56 (0.4 min/step), saving to 07:01 |
| Cost | ~$200 for 1,000 steps (plan estimate); a 500-step minimum retrain ≈ 5 h and ~$100 |
| Tooling | `tools/finetune_watch.py start … --max-requeues 1 --i-accept-charges` (retries, renames, one re-queue, warm-up, `MODEL.txt`); `tools/start_finetune.sh` |

Only the fine-tune account's key runs the model; the station's saved key is a different account. Main defaulted the
match plans to the fine-tune at b1f6e4b (8 Nov 18:09 UTC) and the final `bot_v1.yaml` went back to **base
`dreamlabs/dream-1-beta`** ("the 8 Nov fine-tune was worse on every task", `plans/bot_v1.yaml:36`).
`plans/bot_v1_ft.yaml` keeps the bot harness on the fine-tune with trained texts only.

## 9.2 Symptom

On the station the fine-tune's tool stayed **8–13 cm above the table** while reaching and closed at **11–16 cm**; the
demonstrations grasp at **4–6 cm**; base Dream-1 reached 0–6 cm the same day.

| Run | Model | Min z (m) | Median z (m) | Right gripper min |
|---|---|---|---|---|
| 095731 | base | 0.060 | 0.064 | 0.03 |
| 095854 | fine-tune | 0.083 | 0.157 | 0.03 |
| 100247 | fine-tune | 0.099 | 0.130 | 0.15 |
| 100709 | fine-tune | 0.121 | 0.162 | 0.33 |
| 101053 | fine-tune | 0.126 | 0.161 | 0.37 |
| 101617 | fine-tune | 0.128 | 0.152 | 0.95 |
| 101716 | fine-tune | 0.132 | 0.166 | 0.41 |
| 101840 | fine-tune | 0.115 | 0.150 | 0.22 |
| 102040 (long no-power prompt) | fine-tune | 0.102 | 0.137 | 0.18 |

Its y was on target (0.13–0.22 m): the approach direction was right and the height wrong. Prompt study: 0 cable grips
in ~10 min (§7.6).

## 9.3 Cause 1 (strong): gravity sag in the Quest actions

Mean action − state, right arm, **slow frames only** (joint speed < 0.15 rad/s, so steady state, not lag):

| Folder | Source | Frames | Joint 2 (rad) | Joint 3 | Joint 4 | Tool offset x / y / z (mm) | Median norm (mm) |
|---|---|---|---|---|---|---|---|
| quest-split-v4-final-eef | Quest | 2,137 | **+0.078** | **+0.047** | **+0.043** | +18 / +3 / **+54** | **55.9** |
| unplug-split-v1-final-eef | Quest | 96 | +0.093 | +0.030 | +0.004 | +9 / 0 / +37 | 37.2 |
| unplug-v1-split-eef | Quest | 152 | +0.086 | +0.039 | +0.033 | +16 / +3 / +48 | 50.9 |
| leader-split-v1-frozen-eef | Leader | 1,121 | +0.007 | +0.008 | +0.002 | +3 / +1 / +6 | 8.0 |
| leader-split-v2-frozen-eef | Leader | 467 | +0.010 | +0.010 | +0.013 | +4 / 0 / +7 | 6.1 |
| leader-split-v4-final-frozen-eef | Leader | 4,015 | −0.002 | +0.005 | +0.001 | +1 / 0 / +1 | 5.5 |
| leader-split-v6-frozen-eef | Leader | 2,310 | −0.006 | +0.003 | +0.012 | +1 / +1 / +1 | 7.8 |
| plugin-split-v1-final-frozen-eef | Leader | 2,987 | −0.014 | +0.003 | −0.002 | −2 / +3 / −5 | 7.6 |

Mechanism: the kit's `MetalFollower` sends zero torque, so each joint sits below its target by gravity over kp; the
leader path adds `FollowerGravity`; Dream's station driver also sends gravity torque (live command − measured on slow
frames ≤ 0.016 rad). In a Quest episode the model learns that holding the arm at height z takes a command at
**z + 5.5 cm**. Live, that command is reached, so the arm rises and is asked to rise again. Leader episodes say the
opposite for the same scene and state: **two incompatible state → action maps**. Quest datasets also show 9–10°
median rotation error between action and state (leader 2–3°).

Where demos grasp (first frame the right gripper drops below 0.35): leader state z 0.043 / action z 0.049 (n = 28);
Quest state z 0.059 / action z **0.110** (n = 32). Mix: unplug 36 Quest / 33 leader; put-down 37 / 18; plug-in, stack,
balls, cylinder 0 / 46.

The effect on behaviour is an inference from these numbers; the policy was not run on held-out frames.

## 9.4 Other causes, ranked

| # | Cause | Evidence | Fix without retraining? |
|---|---|---|---|
| 2 | Almost no demos start from home: **9 of 69 unplugs** start within 0.3 rad of it (median start distance 2.14 rad); put-down 0 of 55; plug-in 0 of 19 | solid as a fact, effect unproven | no (the start-pose gate logs it) |
| 3 | Stack and balls thin and left-armed (4 block demos, all left arm; 12 balls); "open both grippers and move the arms up" and `tower_prompts` texts are not in the trained set | solid | yes: base Dream-1 for those stages |
| 4 | Left arm parked elsewhere in 17 Quest episodes (4.4 / 2.1 / 2.5–2.7 rad from live home) | moderate | no |
| 5 | 20 Hz playback of a 30 fps-trained model; live command→measured lag 160–200 ms vs 83–117 ms in the recordings (~80 ms extra) | real but ~1 cm at < 0.1 m/s | optional A/B (`control_hz: 30`, `PAH_FF_TAU_S=0.15`) — not together with a data fix |
| 6 | One 257 mm action jump (Quest ep 36, frame 245); ~20 Quest episodes with 30–52 mm steps | minor | drop ep 36 |

Ruled out: camera keys, swaps, rotation, colour, resolution (640×360 from 1280×720), overhead camera moved (< 2 px),
schema (one signature across 10 folders), joint order and sign, units (rad; gripper 0–100 in state, 0–1 in `grip`),
TCP (FK vs URDF < 0.001 mm), action space. Not fully ruled out: table clutter (headset, jar, cards near the hub).

## 9.5 Offline fixes tried on a fitted Metal model (`station-tuning`) [D]

`station_addons/bench_replay.py`, `metal_plant.py`, `sweep_replay.py`: a MuJoCo model of one Metal arm (Dream's link
table, kp/kd, 30 Hz bus, gravity and velocity feed-forward as in `robots/damiao.py`) replaying the saved `commanded_r`
rows of the 7 fine-tune unplug runs. Calibration: effective load 1.18 / 1.30 / 1.48 × URDF on joints 2/3/4; replay
of Quest actions without feed-forward reproduces recorded tool height within 10 mm RMS (+1.7 mm mean, 9 episodes);
live with feed-forward 5–6 mm RMS (4 runs).

| Fix | Needs | Grasp z (cm) | Reach median z | Reach min z, worst run | Passes 6 cm bar | Hits table |
|---|---|---|---|---|---|---|
| Baseline | — | 13.1 | 15.2 | 8.6 | no | no |
| Gravity × 0.5 / × 0 | env var | 10.8 / 8.5 | 13.3 / 11.5 | 6.4 / 4.3 | no | no |
| Joint offset × 1 (full Quest sag) | code | 7.9 | 10.2 | 3.9 | no | no |
| **Joint offset × 1.5 (live-implied), right arm, faded in by reach** | code | **5.5** | 7.9 | 1.8 | **yes** | no (1.8 cm margin) |
| z −5.5 cm | code | 7.8 | 9.3 | −3.1 | no | yes |
| z −8 cm | code | 5.2 | 4.8 | −7.3 | yes | yes |
| gravity 0 + z −2.4 cm | both | 6.4 | 9.1 | 2.1 | no (close) | no |

**No env-var-only fix passes.** The only fix that passes without going through the table is a right-arm joint offset
of ~1.5× the Quest sag, faded in over tool x 0.20–0.25 m: ~20 lines in `station_addons/pah_tuning.py` (a goal offset
in `DamiaoChain.send_action`, opt-in by env var). **Not built.** Fidelity limits: open loop, one offset for two kinds
of output (leader-style rows would go ~8 cm too low), no contact, and the bar is a height check, not an unplug.

### 9.5.1 The EEF override that was built (d5e9948, `station-tuning`)

Dream's answer for a Metal fine-tune is tool poses per arm (`action.<side>_eef_xyz`, `_eef_rot6`, `_grip`, 48 rows);
the client turns them into joints in `dream.client_ik.ClientIK.solve` on the station. `pah_tuning.py` wraps that call:

| Variable | Effect |
|---|---|
| `PAH_EEF_OFFSET_M` | shift targets per arm before the IK, base frame, metres, capped at 5 cm (e.g. `0,0,-0.02`; `r:0.01,0,-0.03;l:0,0,0` moves only the right arm) |
| `PAH_EEF_Z_MIN_M` | floor on each arm's tool height |
| `PAH_EEF_HOOK` | `module:function(side, xyz, rot6, grip, obs) -> (xyz, rot6, grip)` |
| `PAH_EEF_LOG` | JSONL per chunk: Dream's targets, what went to the IK, solved joints |

Checked in sim on run-3 chunks with Dream's real ClientIK (`station_addons/bench_eef_override.py`): no override →
solved path within 0.1 cm (right) / 0.3 cm (left); `0,0,-0.02` → right hand −1.97 cm, left −1.57 cm. This is the
"ft-joint-offset / EEF override" of the brief. It was never tried on the arm with the fine-tune; the station ran base
Dream-1. (The Cartesian z-offset variants hit joint limits on 971–4,138 of 13,595 ticks in the replay, which is why
the diagnosis preferred a joint-space offset.)

## 9.6 The recipe for a corrected fine-tune (not run)

1. For the three Quest folders, **recompute the arm action as the measured joints ~3 frames later** (keep the
   commanded gripper), then rerun `tools/add_eef.py --replace`.
2. Drop quest-split-v4-final episode 36; consider dropping episodes 4–17 and unplug-v1-split 0–2 (left arm parked
   elsewhere).
3. Add unplug demos **from the home pose**.
4. Screen every folder for steady-state action − state offsets above ~1 cm before training.
5. Consider `lr_schedule: cosine`.

A 500-step job takes ~5 h from queueing; `resume_from` also has a 500-step minimum and starts from the biased
weights. **A fair sim A/B cannot run with our sims**: `sim_station.py` drives Dream's YAM simulator and the fine-tune
is a Metal embodiment.

## 9.7 Base vs fine-tune from the station logs [D]

| | FT (trained text) | FT (long text) | Base 8 Nov | Base 7 Nov |
|---|---|---|---|---|
| Runs | 7 | 1 | 3 | 5 |
| Tool min z on cable prompt, median (range) | 11.7 cm (8.3–12.8) | 10.0 | 4.0 (0–6.0) | 2.1 (0.8–5.6) |
| First close while forward, z median | 15.8 cm (n = 5) | 12.8 | 28.6 (early closes) | 7.8 |
| Time to first close, median | 41 s | 17 s | 6 s | 13 s |
| Dream RTT p50 / p90 | 700 / 857 ms | 700 / 917 | 750 / 1,603 | 890 / 2,135 |
