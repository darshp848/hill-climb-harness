# Metal station tuning: joint-state rate, torques, IK (8 Nov)

Everything here is opt-in through `PAH_*` environment variables read by `station_addons/pah_tuning.py`, which the
existing `station_addons/sitecustomize.py` loads. With no `PAH_*` variable set the station runs Dream's code
unchanged. Dream's installed files are never edited, and every patch refuses to load on any dreamlabs other than
1.0.37 (the NUC's version, verified unpatched against its wheel RECORD on 8 Nov).

## What the station actually does (dreamlabs 1.0.37, read on the NUC)

| Layer | Rate | Where |
|---|---|---|
| Policy rows (Dream chunks of 48 rows) | `control_hz` 20 (fine-tune trained at 30 fps) | `robots/config.py` `METAL_CONTROL_HZ` |
| Command thread, interpolates rows | 250 Hz (240 measured) | `command_hz` |
| **Metal bus thread: reads every motor's state and sends the MIT frames** | **30 Hz, hard-coded** | `robots/metal.py` `ARM.tick_hz` |

So the joint state is read and published at 30 Hz, and the motors only get a new setpoint every 33 ms: 7 of every 8
interpolated commands never reach the arm. What bounds the bus tick is the serial protocol per tick, not the bus: six
state queries one after another, two to three gripper queries (the gripper often holds its reply until the next frame,
so each can wait for a 3 ms flush), and the MIT batch's 5 ms reply window. CAN is 1 Mbit/s with 0 bus errors or
drops; a tick is about 30 frames, roughly 4 ms of bus time.

Measured on the station (run 3, fine-tune `relay-final`): the arm trails the command by 150-250 ms, raw tracking RMS
up to 0.034 rad (r3), most of it lag. One cause is in Dream's code: the velocity feed-forward is filtered with
`FF_ALPHA = 0.04` per bus tick, a 0.8 s time constant at 30 Hz, so it lags any motion shorter than a second.

## Knobs

| Variable | Effect | Suggested |
|---|---|---|
| `PAH_FF_TAU_S` | velocity feed-forward filter time constant (Dream: 0.8 s) | `0.15` |
| `PAH_METAL_TICK_HZ` | bus tick = joint-state read rate and MIT setpoint rate (Dream: 30) | `60` |
| `PAH_IK_TRAJ` | eef traj-solve weights vel,acc,jerk,history (Dream: 5,50,200,5, YAM-tuned) | `20,100,200,5` (Dream's own Metal note) |
| `PAH_TORQUE_CAP_NM` | max push per joint above gravity, by clamping the goal (`kp*|err| <= cap`) | off for the demo; `6,10,6,4,2,2` to try |
| `PAH_GRAVITY_SCALE` | multiplies Dream's gravity feed-forward | unset |
| `PAH_EEF_OFFSET_M` | shift Dream's end-effector targets per arm before its IK, base frame, m (cap 5 cm) | e.g. `0,0,-0.02` if the fine-tune aims high |
| `PAH_EEF_Z_MIN_M` | floor on each arm's tool height before the IK | off |
| `PAH_EEF_HOOK` | `module:function(side, xyz, rot6, grip, obs) -> (xyz, rot6, grip)`: your own override of each chunk | off |
| `PAH_EEF_LOG` | JSONL per chunk: Dream's targets, what went to the IK, solved joints | `~/.dreamlabs/eef-log.jsonl` |
| `PAH_TICK_LOG` | CSV per bus tick: tick time, joints, measured motor torque (from MIT replies) | `~/.dreamlabs/tick-log.csv` |

Every 10 s the station log gets `pah_tuning: tick right: 52.0 Hz, tick p50 ... ms, ...` when any knob is on.

Start (same as today plus the variables), only in a slot the team gives, with someone at the e-stop:

```bash
PAH_TICK_LOG=~/.dreamlabs/tick-log.csv PAH_FF_TAU_S=0.15 PAH_METAL_TICK_HZ=60 PAH_IK_TRAJ=20,100,200,5 \
PYTHONPATH=~/hill-climb-harness/station_addons dreamcli robot up --view --view-port 8093
```

Revert: `dreamcli robot down`, then start without the `PAH_*` variables.

## Offline bench (fake Damiao bus, `station_addons/bench_metal_tick.py`)

Dream's own driver on a simulated arm, 20 Hz rows interpolated at 250 Hz, 0.4 Hz sweep, gripper late half the time.
The fake bus is slower than the NUC's Python (more overhead per frame), so treat tick rates as a floor.

| Setting | Bus tick achieved | Tracking RMS, deg (j0..j5) |
|---|---|---|
| Dream as is (30 Hz, ff 0.8 s) | 30.0 | 2.24 2.89 3.64 4.46 1.73 1.74 |
| 60 Hz | 47-52 | 2.16 2.80 3.53 4.44 1.63 1.63 |
| 30 Hz, ff 0.15 s | 29.8 | 1.37 1.73 2.19 2.31 1.18 1.19 |
| 60 Hz, ff 0.15 s | 50.4 | 1.27 1.60 2.03 2.27 1.05 1.05 |

Reading: the feed-forward filter is the lever for tracking (about -40 to -50% RMS); the tick rate alone barely moves
it, and with the gripper late every tick a 100 Hz request only reaches about 45-60 Hz. The tick rate's value is
fresher joint state and smaller setpoint steps.

## Not built, and why

- Per-row inverse-dynamics torques from Dream's chunk: the arm moves slowly (hand under 0.36 m/s), and the P term
  peaked about 1.5 N.m in run 3, so inertia torques are small next to gravity, which Dream already feeds forward.
- `control_hz: 30` (plays the fine-tune at its trained speed, 1.5x faster) is a station.yaml change, not code. On
  today's chunks it would leave 4.9% of chunks too late to arrive in time against 0.5% at 20 Hz: try it in the same
  A/B slot, fine-tune only.
- `action_space: joint` for the fine-tune (skips the IK, runs the joint head trained on the teleop joints): allowed
  for a fine-tune of this robot. Worth one A/B against `eef`; Dream notes the eef head is noisier than the joint head.

## Intercepting Dream's end-effector commands

Dream's answer for a Metal fine-tune is tool poses per arm (`action.<side>_eef_xyz`, `_eef_rot6`, `_grip`, 48 rows).
The client turns them into joints in `dream.client_ik.ClientIK.solve` on the station, so that is the hook:
`PAH_EEF_*` wraps it, edits the poses, and hands them to Dream's own IK unchanged otherwise. Checked in sim
(`station_addons/bench_eef_override.py`, Dream's real ClientIK on run-3 chunks): with no override the solved hand
path matches the target within 0.1 cm (right) / 0.3 cm (left); with `0,0,-0.02` the right hand moves -1.97 cm and the
left -1.57 cm (left runs into its joint margin on some rows); `r:0.01,0,-0.03;l:0,0,0` moves only the right arm.
The joint-space hooks (feed-forward, torque cap, gravity scale) sit below this, in the Metal driver.
