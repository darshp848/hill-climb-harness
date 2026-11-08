# Learned joint fault detector

`harness/joint_fault_net.py` · trained by `tools/train_joint_fault.py` · data from `tools/joint_fault_data.py` ·
weights `harness/models/joint_fault_net.json` · full search results `harness/models/joint_fault_search.json`.

## What it does

For each joint, at the harness telemetry rate (20 Hz), the last 16 readings (0.8 s) of 8 features go in:

| # | feature | meaning |
|---|---|---|
| 1 | pos_frac | where the joint is in its range, -1 to 1 |
| 2 | margin | distance to the nearer limit / 30 deg |
| 3 | v_toward | velocity toward that limit / 200 deg/s |
| 4 | track_err | command minus position / 10 deg |
| 5 | cmd_v_toward | command velocity toward the limit / 200 deg/s |
| 6 | tau | measured torque / torque limit |
| 7 | tau_cmd | commanded torque / torque limit |
| 8 | tau_diff | measured minus commanded / torque limit |

Two flags come out: **now** (the joint is in a fault state) and **soon** (now, or within 0.25 s). An alert is raised
when a probability stays over its threshold for 2 readings in a row. It runs in the harness on the MacBook next to the
rule observer (`harness/joint_observer.py`); the NUC keeps the rules (stdlib only). `JointTelemetry` loads the model
only when its file says it beat the rules on held-out traces (`beats_baseline`).

## Data

Real motion: the 6 sim matches of round 1 (Dream driving both YAM arms), 72 joint series, 413k samples. Each 6 s clip
is one joint, placed inside the YAM's or the Metal arm's limits, with noise at the Damiao CAN reply's resolution and an
independent torque-reading error (the sim's measured torque equals the command). One of these is injected; labels come
from the resulting signals, not from any rule:

normal · near-miss (fast move that stops 1-12 deg short: no fault) · limit run · blocked/crawling joint · oscillation
(1.5-6 Hz) · overload (torque to the limit) · overspeed (over half the motor's top speed).

Split by match file: 4 files train (16,000 clips), 1 validation (4,000), 1 test (4,000).

## Architecture search (8 Nov 2026)

Same windows, 3,000 steps each, AdamW + one-cycle, BCE with balanced positive weight. Threshold chosen on validation
for at most 2 false alarms per joint-hour; ranked by "timely recall" (alarm no later than 0.25 s after the fault
starts), mean over the 5 fault kinds.

| model | weights | val timely | AUROC soon |
|---|---:|---:|---:|
| logistic | 258 | 0.00 | 0.600 |
| MLP 32 | 4,194 | 0.86 | 0.975 |
| MLP 64-32 | 10,402 | 0.91 | 0.978 |
| MLP 128-64 | 24,898 | 0.96 | 0.979 |
| TCN 8 ch, 4 layers | 834 | 0.73 | 0.977 |
| TCN 16 ch, 3 layers | 2,178 | 0.86 | 0.979 |
| TCN 16 ch, 4 layers | 2,962 | 0.95 | 0.980 |
| **TCN 32 ch, 4 layers** | **10,530** | **0.98** | **0.980** |
| GRU 8 | 450 | 0.50 | 0.976 |
| GRU 16 | 1,282 | 0.68 | 0.977 |
| GRU 32 | 4,098 | 0.91 | 0.978 |
| GRU 32 x2 | 10,434 | 0.95 | 0.979 |
| Transformer d16, 1 layer | 2,690 | 0.81 | 0.979 |
| Transformer d32, 1 layer | 9,474 | 0.96 | 0.980 |
| Transformer d32, 2 layers | 18,018 | 0.98 | 0.980 |

Top 3 retrained 3 seeds x 6,000 steps: TCN 32x4 0.987 ± 0.001, transformer d32x2 0.985 ± 0.004, transformer d32x1
0.953 ± 0.013. **Winner: dilated causal TCN, 32 channels, 4 layers (dilations 1-2-4-8, kernel 3, residual), last-step +
mean pooling, 10.5k weights.** It ties the transformer at 58% of its weights and is the most stable across seeds.
Numpy inference: 0.83 ms for all 12 joints per reading on this laptop, about 2% of one core at 20 Hz.

## Operating point and comparison with the rules (TEST file, never seen in training or selection)

Thresholds picked on validation for at most 1 false alarm per joint-hour: soon 0.998, now 0.95, 2 readings in a row.

| | false alarms / joint-hour | timely recall | limit run (early) | blocked | oscillation | overload | overspeed |
|---|---:|---:|---|---:|---:|---:|---:|
| learned (soon) | 0.2 | 0.91 | 1.00 (90% before the breach) | 0.94 | 0.92 | 0.99 | 0.68 |
| rules (any alert) | 197 | 0.50 | 1.00 (99% before) | 0.01 | 0.22 | 0.34 | 0.93 |

The rules' false alarms are almost all `limit_approach` (198/joint-hour): its constant-acceleration projection fires on
every fast move toward a limit, including ones that stop short. A looser learned operating point (soon 0.97) reaches
0.99 timely recall at 3.8 false alarms/joint-hour.

## Limits

- All motion is simulated YAM motion and the faults are injected models of faults. Retrain on the NUC's CAN traces
  (`joints.jsonl` from `can_live` + `JointTelemetry(record_to=...)`) before trusting the numbers on the Metal arms.
- Overspeed bursts last 1-3 readings at 20 Hz, so the 2-in-a-row rule misses a third of them; the rules catch those.
  Keep both running.
- Temperature and motor status are not inputs; the rules cover overheat and motor faults.

Retrain: `python tools/train_joint_fault.py --traces <folder with joints.jsonl>` (needs torch; ~45 min on 20 threads).
