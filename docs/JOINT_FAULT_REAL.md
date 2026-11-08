# Joint fault detector on real Metal-arm runs

`tools/joint_fault_real.py` · the torque-free check of the learned detector (`docs/JOINT_FAULT_NET.md`) on Dream's
real rollouts. Laptop only: it reads copies of the NUC's files and never runs anything on the NUC.

## Run it

```bash
rsync -a --include='*/' --include='last-run.csv' --exclude='*' pah@nuck-5:rollouts/ ../rollouts/
uv venv -p 3.12 ../jf-venv && uv pip install -p ../jf-venv/bin/python torch numpy
../jf-venv/bin/python tools/joint_fault_real.py --rollouts ../rollouts --quick          # ~2 min, 3 models: add --only
../jf-venv/bin/python tools/joint_fault_real.py --rollouts ../rollouts --out real.json  # all candidates, 3000 steps
```

To compare a new design (the 4-output encoder, say), add it to `EXTRA_CANDIDATES` at the top of the tool as
`(family, config)`, and pass `--only "<its name>,tcn c32 L4,enc d32 L2 h4"` to run it next to the current best two.

## What it measures

- Base motion: `last-run.csv` from each rollout (20 Hz measured and commanded joint angles, both arms), split by run
  into train / val / test. 22 of the 52 runs on the NUC on 8 Nov had enough ticks: 6.9 joint-hours.
- Train and val clips get the injected faults from `tools/joint_fault_data.py`, without overload (it needs torque). The
  torque features are zeroed: the trace has no torque.
- **Untouched test runs:** each model and the rule observer run over the held-out runs with nothing injected.
  `real_alarms` in the JSON lists every alarm with its run, arm, joint and time. These are policy rollouts, so check an
  alarm against the run before calling it false.

## First result (8 Nov, `--quick`: 300 steps, so the models are undertrained)

| | val timely recall | alarms / joint-hour, untouched real runs |
|---|---:|---:|
| logistic | 0.00 | 0.0 |
| TCN 32x4 | 0.82 | 3.5 |
| encoder d32 x2 | 0.63 | 2.4 |
| rule observer | | 99.6 (limit_approach 94.3, tracking_divergence 5.9) |

## Encoder size sweep (8 Nov, 3000 steps, 1 seed)

| model | weights | val timely | blocked | overspeed | alarms / joint-hour, untouched real runs |
|---|---:|---:|---:|---:|---:|
| TCN 32x4 (the sim winner) | 10,530 | 0.89 | 0.61 | 1.00 | 1.2 |
| encoder d16 x1 | 2,690 | 0.74 | 0.44 | 0.55 | 0.0 |
| encoder d32 x1 | 9,474 | 0.92 | 0.73 | 0.98 | 0.6 |
| **encoder d32 x2** | **18,018** | **0.96** | **0.87** | **0.99** | **1.8** |
| encoder d64 x2 | 68,802 | 0.96 | 0.87 | 0.99 | 2.4 |
| encoder d64 x4 | 135,746 | 0.97 | 0.92 | 0.99 | 1.8 |

Limit run and oscillation are 0.96-1.00 for every model. Without torque the TCN loses most on blocked joints, and
the encoder d32 x2 beats it there. d16 is too small; 4x to 7x more weights than d32 x2 buys 0.01. The real-run alarm
rates are 0 to 4 alarms in 1.7 joint-hours, too few to rank models on; all are near the 2 per joint-hour budget. One
seed, and val and test are 3 runs each, so retrain with more seeds and the teleop data before choosing.

The rule observer's `limit_approach` fires on real arms as it did in sim: `shoulder_lift` and `elbow_flex` rest at
their 0 deg soft limit at home, so every move back home projects past it. Not in the 8 Nov demo build (the NUC runs
`ee-pull-force`, which has no joint telemetry), but fix the margin or the home pose before turning the rules on.

Not covered: overload and anything torque-only; the 30 Hz LeRobot teleop datasets in `~/hackathon_data` (more motion,
needs resampling to 20 Hz and a parquet reader).

## Encoder weights (8 Nov evening, branch `joint-fault-encoder`)

The sweep above only reported numbers; no encoder weights were kept. `--export DIR` now saves every trained model in
the runtime format (`harness.joint_fault_net.FaultModel`), with `torque_free: true` so the runtime zeroes the torque
features even when the live source sends torque. Rerun on all 79 rollouts on the NUC at the time (31 with enough
ticks: train 21 runs / 5.07 joint-hours, val 5 / 1.38, test 5 / 0.83), 3000 steps, seed 0:

```bash
../.train-venv/Scripts/python.exe tools/joint_fault_real.py --rollouts ../nuc_data/jf_rollouts_20261108 \
  --only "enc d16 L1 h2,enc d32 L1 h4,enc d32 L2 h4,enc d64 L2 h4,enc d64 L4 h4,tcn c32 L4" \
  --out harness/models/encoder/sweep_real.json --export harness/models/encoder
```

| model | weights | val timely | blocked | overspeed | val FA / joint-hour | alarms / joint-hour, untouched test runs |
|---|---:|---:|---:|---:|---:|---:|
| TCN 32x4 | 10,530 | 0.75 | 0.20 | 1.00 | 1.9 | 0.0 |
| encoder d16 x1 | 2,690 | 0.67 | 0.42 | 0.36 | 1.9 | 0.0 |
| encoder d32 x1 | 9,474 | 0.86 | 0.67 | 0.90 | 1.7 | 0.0 |
| encoder d32 x2 | 18,018 | 0.87 | 0.78 | 0.84 | 1.2 | 0.0 |
| encoder d64 x2 | 68,802 | 0.91 | 0.79 | 0.91 | 1.9 | 0.0 |
| encoder d64 x4 | 135,746 | 0.92 | 0.85 | 0.90 | 1.9 | 0.0 |

Files: `harness/models/encoder/joint_fault_<name>.json` (weights, normalisation, threshold, split, val metrics),
`sweep_real.json` (every number, the split, the rule observer's alarms) and `sweep_real.log`. The default model
(`harness/models/joint_fault_net.json`) is unchanged: still the sim-trained TCN with torque. To try one, load it with
`FaultModel.load("harness/models/encoder/joint_fault_enc_d32_L2_h4.json")`.

Same order as the afternoon sweep (the encoders beat the TCN on blocked joints without torque), with lower numbers on
this bigger, different split. Encoders d64 buy about 0.05 val timely for 4x to 7x the weights of d32 x2. No model
alarmed on the 0.83 untouched test joint-hours, so that column cannot rank them. One seed; retrain with more seeds
and the teleop data before choosing a default.
