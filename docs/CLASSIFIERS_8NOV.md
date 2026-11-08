# Ball, block and cable-end classifiers on the 8 Nov station frames

Checked on Dream's own rollout frames from the NUC (7-8 Nov, 640x360, scene + both wrists), against hand labels.
Before = `fix/guard-real-frames` 86121b9. The labels and the scoring scripts are kept with the frames, outside the
repo (the frames show people): `nuc_analysis_20261108/classifiers/gt/` and `usbc/real/wrist_*`.

## Golf balls and foam blocks (`harness/detect.py`, `plans/table_colors.yaml`)

Overhead camera, object found when a box of the right colour and shape covers its labelled centre.

| | tuning frames (14) before | after | held-out frames (12) before | after |
|---|---:|---:|---:|---:|
| balls found | 24 / 46 | 42 / 46 | 20 / 27 | 26 / 27 |
| ball boxes that are balls | 77% | 100% | 83% | 100% |
| blocks found | 32 / 40 | 39 / 40 | 16 / 23 | 23 / 23 |
| block boxes that are blocks | 86% | 95% | 80% | 92% |

All 2,010 overhead frames of 8 Nov: the white bin boxed as a red WHITE BALL in 1,286 frames before, 0 after; merged
"blue stick" boxes 0.36 per frame before, 0.11 after. The wrist guard tripped on none of 2,145 wrist frames before or
after (none of them shows a held white ball). Detection time is within this laptop's run-to-run noise (~±15%).

What changed:
- Yellow runs H 30-70 from S 50: the pale yellow-green ball (H 56-65) was in neither yellow nor green.
- Yellow, like white, green and orange, has no cubes here: a compact blob of it is a ball; one centred on a cube is a
  stick standing in its hole.
- A small blob of a ball colour with at least 50 px, elongation <= 2.1 and roundness >= 0.28 is a ball: half-lit
  balls read as crescents and were dropped. A cube's pale rim stays out (roundness <= 0.15).
- Overhead, a ball-colour blob over 0.6% of the image (the bin) is never a ball.
- Overhead, a cube colour's blob at least 14 px wide is split at a dip in its distance ridge: two touching cubes. A
  lying stick (~9 px wide, no dip) stays whole. Cut pieces and corner-on cubes of a colour with no balls are cubes.

Still missed: balls inside the bin (1 of 4 labelled), and a white ball pressed against the bin (it merges with it;
looking for round lobes on the bin also fired on the corners of a bin seen at an angle). Hole direction was not
checked against truth. Regression frames: `tests/data/table_real/`, `tests/test_detect_real_table.py`.

## Cable ends (`harness/cable_end.py`, `models/cable_end.onnx`)

The 7 Nov model was trained on 891 crops, mostly from the overhead videos. It labelled the USB-C end held upright
right, but a USB-A end lying flat (wide tip, two holes) "USB-C" at 0.71-0.95, and missed many USB-A ends. 700 wrist
crops from 89 runs were hand-labelled (478 usable: 115 USB-A, 82 USB-C, 281 neither; 222 unsure left out). Held out
by run (25 runs, 137 crops), at the 0.7 confidence the harness reports at:

| | USB-C precision | USB-C recall | USB-A precision | USB-A recall | accuracy |
|---|---:|---:|---:|---:|---:|
| 7 Nov model | 0.83 | 0.91 | 1.00 | 0.68 | 0.91 |
| 7 Nov data + wrist train | 0.92 | 1.00 | 0.95 | 0.84 | 0.96 |

The shipped model is retrained on all 1,369 crops (15 epochs, one seed). Hand check of every box on held-out run
103905: about 62 of 64 right; the old model missed about 13 flat USB-A ends there. One new false box: the power
strip's red switch, called USB-A. Neither model finds ends on the overhead camera (ferrules are a few pixels wide).
