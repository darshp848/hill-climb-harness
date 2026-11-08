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

The rule observer's `limit_approach` fires on real arms as it did in sim: `shoulder_lift` and `elbow_flex` rest at
their 0 deg soft limit at home, so every move back home projects past it. Not in the 8 Nov demo build (the NUC runs
`ee-pull-force`, which has no joint telemetry), but fix the margin or the home pose before turning the rules on.

Not covered: overload and anything torque-only; the 30 Hz LeRobot teleop datasets in `~/hackathon_data` (more motion,
needs resampling to 20 Hz and a parquet reader).
