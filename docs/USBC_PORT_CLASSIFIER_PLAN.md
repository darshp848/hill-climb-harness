# USB port finder for the plug-in stage: plan (not built)

Status: plan only, 8 Nov. Nothing trained, nothing committed.

## 0. What the photos and frames show (read this first)

- **The strip has no USB-C port.** It is a black 12-outlet surge strip (red rocker switch, two LEDs "Protected / Grounded", brand logo). The outlets are two columns of six. At the **end nearest the robots** there is a recessed bank of **four silver USB-A ports in one row** (printed "IO 5V/0-3A"). IMG_2703/2704 show three or four silver slots and no USB-C opening.
- The orange cables are **USB-A at one end, USB-C at the other** (IMG_2702). The USB-A end is what goes into the strip. `harness/cable_end.py` already says the same. So "USB-C port, first from the left" in DECISIONS.md almost certainly means "the first USB port in the bank, counted from the left, that takes the cable's plug". **Confirm with the operator (5 min) before labelling anything.** The rest of this plan calls them "bank ports" and detects USB-A openings; if a USB-C port ever appears, it is a new class, not a rewrite.
- Viewpoints. Overhead scene cam (640x360): strip is vertical, bank at the bottom edge, each port only ~5-6 px wide, enough for strip pose, not for ports. Wrist cams (640x360, 30 fps, forward-looking, fingers at the bottom of the frame): the strip is a diagonal or vertical dark slab; in the right wrist the bank is ~15-25 px per port and partly hidden by the fingers. Phone photos are 4032x3024 and not the same optics.
- Appearance: the ports are bright silver slots in a dark recess, so they stand out. The matt black plastic is the problem under glare. Orange ferrules and a golf ball sit near the strip and can be mistaken for it.
- Data on disk (`nuc_data/`): `hackathon_data/plug-unplug-v1` (42 episodes, 115k frames, left_wrist/right_wrist/scene), `plugin-split-v1-frozen` (12 plug-in episodes, task "plug the loose USB cable on the table into a free port on the hub"), the unplug sets, 1176 mp4 files, 4098 jpg (rollouts, `.dreamlabs/last-run_inputs`). All 640x360. Many frames show people's faces: **keep full frames local or on Drive; anything committed must be strip crops only.**
- Reality check on value: `plans/hill_climb_v4.yaml` has no plug-in stage (decided 7 Nov: base Dream-1 never seated a plug). So the first payoff is judge help and a seated check, not pushing a plug in. Build in that order.

## 1. What to detect and output

Per frame, per camera, one `PortReading`:

```
strip: {found, quad[4 pts], conf}            # dark slab, switch end vs USB end known
ports: [ {index 0..3, box, centre, state, conf, visible_frac} x4 ]
state:  empty | occupied | occluded | unknown
index 0 = port on the LEFT of the bank when you stand at the table edge by the robots, facing the strip (= image-left in the overhead photo).
free_leftmost: index of first state==empty port in 0..3, or null
```

- Classes: bank USB-A opening (the target) and, only as negatives, AC outlets, the switch, cable ferrules. AC outlets are never output.
- "First from the left" is decided from the **strip template, not from image x**: the strip is found at any rotation, the USB end is identified (bank of four silver blobs vs the switch/LED end), and index 0 is the end of the bank that is on the left when viewed from the USB end. This matters because in the wrist views the strip is diagonal and in IMG_2703 it is rotated about 60 degrees. The same logic runs on the scene cam (index agrees across cameras) and is the cross-check.
- Occupied = the opening is filled by the orange or metal plug shell (silver slot interior gone, orange or black blob covering it). A cable merely lying across the strip, as in IMG_2703/2704, must read as `occluded` or `empty`, not `occupied`.

## 2. Approaches compared

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| Template match / contour + aspect filters on the dark strip | Cheap, no data | Scale/rotation/glare fragile on its own | Use only as the strip finder |
| **Strip pose + fixed port offsets from a template** (homography or similarity from strip quad + 4 silver anchors + red switch) | 4 ports are rigid, so one fit gives all four and their indices; survives a gripper hiding 1-2 ports; tiny runtime | Needs strip measurements (a ruler, 5 min); strip quad poor when the strip leaves the frame | **Core** |
| Tiny learned detector (YOLOv8n or NanoDet, 320 px, ONNX via `cv2.dnn`) | Handles glare and odd poses without hand tuning | ~400 labelled frames; no index logic; 6-15 ms on M-series | Fallback if section 5 acceptance fails after step 5 |
| Small patch CNN on the 4 projected port patches (48x48, empty/occupied/occluded) | Needs only ~600 patches; mirrors `cable_end.py` | Needs the pose first | **Use for occupancy** |
| VLM only | Already there | ~1 s and cents per call, cannot say "leftmost of four" reliably at 15 px ports | Stays as the judge; detector feeds it |

**Recommendation:** classical strip finder (dark large quad, `minAreaRect`, aspect ~1:2.9, plus a row of 4 bright silver blobs at one short end to fix orientation) then template fit, then a patch CNN for occupancy. It mirrors the shipped cable-end design (numpy + OpenCV, ONNX through `cv2.dnn`, no torch), the geometry is the thing that does not change between runs, and labelling is mostly 5 clicks a frame. Do not use sim frames for training: the sim hub is a silver 4-port USB-A box, and the Blender-render result in HARNESS_GUIDE (synthetic did not help `cable_end`) says to skip it. If sim testing of the plumbing is wanted, add a black strip mesh to `sim_scenes/hill_climb_table.py` later, for pipeline tests only.

## 3. Data

- **Bootstrap (no station time):** extract every 10th frame from `plugin-split-v1-frozen`, `plug-unplug-v1`, `unplug-*` for wrist_l, wrist_r, scene; keep the ~600 that show the bank (auto-filter by silver-blob row). Add the 4 phone photos only for the strip-finder unit test, not for training (different optics, people in IMG_2698).
- **Collect at the station:** about 40 minutes. Wrist and scene frames from `GET /frame/<cam>.jpg` while an operator teleoperates or holds arms at 8 poses over the bank (approach from left, centre, right; heights from 30 cm down to contact), strip moved to 5 positions and 3 rotations, with 0, 1, 2, 4 cables seated in every combination, with and without the golf ball and loose cables on the strip, both overhead-light and window-light, and glare from a lamp. Target 1500 frames total, 300 of them with a cable seated in each port position.
- **Labels:** one small OpenCV click tool (no new service): 4 strip corners + 4 port centres + per-port state, saved as one JSON per frame (`{image, strip_quad, ports:[{xy,state,visible}]}`); COCO export script if the YOLO fallback is needed. ~8 s per frame, ~400 frames labelled by hand, the rest pre-labelled by the template fit and corrected.
- **Augmentation:** brightness/contrast/gamma, specular blobs on the black plastic, motion blur, JPEG quality, small rotations and scale, random finger-shaped occluders at the bottom edge. No flips (flipping swaps "left").
- **Splits:** by session/day and by camera, never by frame. Train = bootstrap episodes plus the first station session. Val = one held-out set of episodes. **Test = the last station session recorded at match-day lighting, plus the 4 phone photos.** Nothing from the test session is looked at while tuning.

## 4. Integration

New `harness/usb_port.py` (same shape as `cable_end.py`): `UsbPortFinder(template_yaml, model_path)` with `.read(bgr) -> PortReading`, `.annotate(jpeg) -> bytes`, `.legend()`. Template (measured strip size, port pitch) in `plans/usb_strip.yaml`; patch model in `models/usb_port_state.onnx`; training in `tools/usb_port/`.

- `harness/cli.py` near line 206: add `--usb-ports` flag, set `det.usb_ports`.
- `harness/detect.py` `annotate_frames` / `legend` (lines ~274-287): chain `usb_ports.annotate`, draw ports as numbered boxes (green = empty, red = occupied, grey = occluded, port 1 outlined) and add the legend line.
- `harness/verifiers.py` (monitor lines, ~626): optional one-line text hint from `PortReading`, e.g. "detector: port 1 empty, gripper aligned". Ship boxes first and A/B the text hint, since the judge could over-trust it.
- **Seated check (c):** a `plug_cable` stage's `success` is the VLM question AND detector `ports[target].state == occupied` for 3 consecutive frames, both required. This is the first thing to wire up. A false confirm is worse than a late one, so tune for low false-occupied.
- **Alignment gate (a):** the wrist camera is rigid on the wrist, so the plug tip is at a fixed pixel `(u*, v*)` and expected scale. Learn both from the teleop insertion moments in `plugin-split-v1-frozen` (port centre and port width at the frame just before seating). Gate: target port centre within 0.5 port-widths of `(u*, v*)` and port width within +-25% of expected, stable for 5 frames. The harness cannot command poses, so the gate does only two things: hold the "bring it vertically down" prompt until aligned, and otherwise re-prompt or retry with a nudge prompt ("move slightly left"). Dream-1 may not honour a nudge; log how often, do not assume it.
- **Port choice (d):** `free_leftmost`; the operator says port 1, so use it, and fall to the next free port only if port 1 is occupied. Respect the rule that an unplugged cable is not plugged back in.
- Live guard (`harness/live_guard.py`): leave alone, it is a stop-on-illegal-ball thread. A seated check can reuse `LiveFrames` later if polling is too slow.
- `harness/decide.py`: log the occupancy and alignment as shadow features before they drive decisions.

## 5. Evaluation and acceptance (held-out test)

| Metric | Threshold |
|---|---|
| Strip found when >=80% visible | recall >= 0.97 on wrist, >= 0.99 on scene |
| Port centre error (when state is not occluded) | median <= 0.25 port-width, 95th pct <= 0.5 |
| **Index correct** (port 1 is the true leftmost) | >= 0.98 of frames where all 4 ports are visible; a wrong index counts as a critical failure |
| Occupied vs empty | F1 >= 0.90; false-occupied rate <= 3% |
| Alignment gate | precision >= 0.90 against labels from teleop insertions (recall >= 0.6 is fine) |
| Latency (M-series MacBook Air, 640x360, one core) | <= 20 ms per frame all in; target ~8 ms (strip 3 ms, fit 1 ms, patch CNN 4 patches 3 ms). Measure on the Mac, not this PC. Run at <= 10 Hz on wrist cams so the fanless Air does not throttle. |

Also check on every frame set: percentage `unknown` (it must fall back to `unknown`, not guess), and the same numbers per lighting condition. If strip-pose acceptance fails on glare frames after step 5, switch to the YOLO-nano fallback and re-run the same test.

## 6. Risks and mitigations

- **Glare on black plastic:** the strip finder keys on large dark area plus the silver USB blobs, not on edges; add glare augmentation; use CLAHE on V before thresholding; fall back to `unknown`.
- **Gripper occludes ports:** the template infers hidden ports from visible ones (need >= 2 blobs or the quad); states are `occluded`, never `empty`, when under the fingers.
- **Strip moves** (arm bumps it, 2703 shows it rotated): re-fit every frame; no stored absolute pose; scene cam gives an independent pose check.
- **Camera calibration:** no intrinsics needed; the learned `(u*, v*)` absorbs wrist mounting error, but remeasure after any camera re-mount (10 min at the station).
- **Distractors:** orange ferrules, golf ball, a cable lying across the bank; negatives in the patch set; require silver slot geometry.
- **Mismatch with the real strip at the match** (different strip model): measure the real strip first; the template is one yaml.
- **Label ambiguity (USB-C vs USB-A):** settled in step 0.
- **Privacy:** people appear in scene and wrist frames and IMG_2698; train on crops, commit only crops and the ONNX.

## 7. Build order and time

| Step | Work | Time | Needs user |
|---|---|---|---|
| 0 | Confirm USB-A port is the target, measure strip and port pitch | 15 min | Yes (ruler, 5 min at the strip) |
| 1 | Frame mining + auto-filter from existing videos | 2 h | No |
| 2 | Click labeller, ~400 labels | 2 h | Light (label review) |
| 3 | Strip finder + template fit + indexing, unit-tested on the 4 photos and mined frames | 4 h | No |
| 4 | Patch CNN for occupied/empty (reuse `tools/cable_end/train.py`) | 2 h | No |
| 5 | `usb_port.py`, `--usb-ports`, annotate + legend; run on recorded rollouts | 3 h | No |
| 6 | **Station session** (collection above) | 40 min | **Yes: moves the strip, seats cables, teleops arms** |
| 7 | Retrain on station data, run the held-out test, measure on the Mac | 3 h | Mac access |
| 8 | Seated-check wiring into the stage + shadow logging; alignment gate calibration | 3 h | No |
| 9 | Match-day re-calibration at the venue and a smoke run | 20 min | Yes |

About 2 days of engineering plus about 1 hour of the user at the station. Steps 0-5 and 8 (seated check) can happen before any station time; the gate is the last and least certain piece because the policy cannot be steered finely.
