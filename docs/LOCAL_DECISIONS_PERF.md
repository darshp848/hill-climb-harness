# Making the local decision code fast (notes for whoever sets up the match laptop)

The match runs on an Apple Silicon MacBook Air: 4 performance + 4 efficiency cores, no fan. Everything below runs on
that laptop, every frame or every tick, with no API call. The remote calls (Dream-1, the Claude/OpenRouter monitor)
are network-bound and not covered here.

## What runs locally, and how often

| Piece | File | Rate in a match | Work per call |
|---|---|---|---|
| Live guard | `harness/live_guard.py` | 10 Hz x 2 wrist cameras (`--live-guard`) | `TableDetector.guard` on each new wrist JPEG |
| Fast loop (if `fast:` in the plan; no shipped plan has it) | `harness/fastloop.py` | 20 Hz x 2 wrists | the same guard + observer + state-space step |
| Per-poll guard | `runner.fast_guard` | every `check_every_s` (3 s in v4) | the same guard on the poll's frames |
| Monitor request | `MonitorVerifier._build` | each check (~every 3-6 s, `parallel: 2`) | boxes on 3 frames (`annotate_frames`), stitched history grid, base64, stage text |
| Decision model | `harness/statespace.py` | each fast-loop tick | `Observer.fast` + `StateSpace.step`: tens of microseconds |
| Shadow decider | `harness/decide.py` | each runner look | pure Python on a few numbers; negligible |
| Prompt layers | `harness/promptgen.py` | each prompt | string joins; ~2 us |

So the hot code is the colour detector (`harness/detect.py`), first in the guard and second in the monitor's
annotation. The decision maths is not the bottleneck: keep it simple and exact.

## Measured (tools/mac_bench.py, recorded frames, no network)

Windows 11 x86-64 dev laptop (Darsh's, on AC), 8 Nov, same frames, before (f756095) -> after (this branch):
`bench_results/mac_bench_LAPTOP-3VMSCT0C_{before,after}.json`. No Mac was available: run the bench on the Air
(HARNESS_GUIDE.md section 2b) and add its numbers here.

| Component | before p50 / p95 ms | after p50 / p95 ms | p50 speedup |
|---|---|---|---|
| guard, one wrist frame | 19.2 / 21.7 | 5.3 / 7.4 | 3.6x |
| fast-loop tick (2 new frames) | 38.3 / 42.5 | 11.2 / 13.9 | 3.4x |
| monitor annotate (3 frames) | 137 / 139 | 27.0 / 29.2 | 5.1x |
| whole monitor request build | 152 / 158 | 32.9 / 36.1 | 4.6x |
| stitch, one check | 23.3 / 24.5 | 6.2 / 6.8 | 3.8x |
| state-space step (K = 1) | 0.022 / 0.025 | 0.020 / 0.022 | same (K = 1000: 246 -> 46 us) |
| prompt composition | 0.002 | 0.002 | same |

GitHub `macos-15` runner (Apple Silicon VM, shared, has a fan), same bench in CI
(`bench_results/mac_bench_gha_macos15.json`): guard 34.1 -> 5.7 ms p50 (6.0x, both measured in the same run),
fast-loop tick 12.5 ms, annotate 31.9 ms, monitor request build 35.7 ms, stitch 7.7 ms. Its sustained rows managed
only ~28 guards/s of ~50-60 at low CPU: the VM stretches short sleeps (a timing test also saw 50 ms waits take ~100 ms).
Check `guards/s` on the Air; if it is low there too, the loops' waits need a look before the match.

5 minutes of match load (live guard 10 Hz + fast loop 20 Hz on both wrists + a monitor request every 3 s), per
30 s window: guard p50 20.6 -> 5.0 ms, fast-loop tick p95 47 -> 13.7 ms (period 50 ms), overruns 1-12 -> 0,
process CPU 107% -> 26% of one core. Flat across the 5 minutes on this laptop (it has a fan); the Air is the test
that matters.

## Rules for any further optimisation

1. **Exact or not at all.** A guard verdict that changes is a behaviour change in a safety stop. Every change to
   `detect.py` must keep `tests/test_guard_equivalence.py` green: it compares the guard verdict, every box and every
   annotated JPEG with `tests/_detect_reference.py` (the detector the match was tuned with) on
   `tests/data/guard_frames`. Run it on more frames with
   `HARNESS_GUARD_FRAMES=<folder> python -m pytest tests/test_guard_equivalence.py`.
   If you do want different behaviour (a smaller frame, a crop), that is a re-tune: change
   `plans/table_colors.yaml` or the reference on purpose, re-run the table photos, and say so in the commit.
2. **Measure on the Mac, sustained.** A fanless Air is fast for 30 s and slower after a few minutes.
   `python tools/mac_bench.py --sustained 300` shows p95 per 30 s window and `pmset -g therm`.
3. **Vectorise and do less, don't add threads.** numpy and Pillow release the GIL in their C loops, but there are
   only 4 performance cores and the guard, the fast loop, the monitor build and the HTTP threads already share them.
   `harness/perf.py` caps BLAS/OpenMP to one thread; keep it that way.
4. **Never touch what Dream-1 sees or is told.** The detector only reads frames; prompts stay byte for byte.

## What was done (8 Nov, branch mac-arm64)

All exact (equivalence test above, plus 201 more real and sim frames):

- **Morphology without scipy's iterated loops.** `binary_closing(iterations=2)` is four whole-array shifts each
  way (`_close2`); `binary_fill_holes` is one labelling of the background (`_fill_holes`). Both are checked against
  scipy on random masks of every shape in `test_close_and_fill_are_scipys`.
- **Crop before labelling.** Fill and label run on the box around the mask plus one pixel (`_crop`); a colour with
  a handful of pixels costs microseconds instead of a full-frame label.
- **No per-blob full-image scans.** The old loop did `np.nonzero(lab == i)` over the whole image for every blob; now
  each blob is read inside its `find_objects` box, and a box too small to pass `min_frac` is skipped unread.
- **The guard asks only its own question.** `TableDetector._held` runs only the colours that can trip it, skips
  blobs too small to be held or centred outside `grip_roi` before the shape test, and stops at the first held ball.
- **uint8 HSV planes.** No int16 copy; bounds every pixel passes (black's hue and saturation) build no array.
- **Stitch tile cache.** The stage-start and history frames come back in every check; resized tiles are cached by
  digest (32 tiles, ~8 MB), so a check decodes and resizes only its new frame.
- **State-space thresholds built once.** `StateSpace.decide` rebuilt five arrays from Python lists every tick;
  with K = 1000 weight sets (offline hill-climb sweeps) a step went from 246 us to 46 us, decisions identical.

## What is left, best first

1. **Share frames and verdicts between the live guard and the fast loop.** With both on, each has its own
   `LiveFrames` and runs the detector on its own fetch of the same cameras, so the detector work doubles. One
   frame source feeding both, plus a verdict memo keyed by the frame's SHA-1 (both loops already hash each frame),
   halves it. Behaviour note: the fast loop would then see the guard's frames, not its own fetch 0-50 ms later.
2. **Ask the station for 640 px frames in the live guard** (`/frame/<cam>.jpg?w=640`, as `FrameSource` already does).
   Fewer bytes over Wi-Fi and a smaller decode. The pixels differ slightly from a draft-mode decode of the full
   frame, so this is a re-tune: re-check `plans/table_colors.yaml` on real wrist frames first.
3. **HSV conversion** (~2 ms of the ~5 ms guard on x86; decode ~1 ms, the rest is per-colour masks and labels). Pillow's RGB->HSV is a per-pixel float routine. A
   256^3 lookup table built once from Pillow itself would be exact by construction (50 MB) but may not beat Pillow
   on memory bandwidth; measure on the Air. Do not re-derive the HSV formula in numpy: rounding differs.
4. **Do the guard only where a held ball can be.** A held ball's blob must be centred in `grip_roi`, but it can
   extend outside it, so cropping the image before closing is not exact. Only as a re-tune.
5. **Fast loop at 10 Hz instead of 20** when it is in shadow mode: it logs only, so half the rate halves its cost.
   That is a plan setting (`fast.rate_hz`), not code.
6. **State-space model.** Already microseconds a tick; `Observer._update` loops over a dict in Python, which is
   fine at K = 1. For offline sweeps over many weight sets, batch over K (the `StateSpace` already is) rather than
   over runs in Python.

## How to check a change

```bash
python -m pytest -q tests/test_guard_equivalence.py tests/test_detect.py tests/test_statespace.py tests/test_fast_additions.py
python tools/mac_bench.py --seconds 2                  # guard vs guard_ref is the before/after on the same box
python tools/mac_bench.py --sustained 300              # on the Air: p95 must not climb window after window
```
