# State-space form of the harness decisions (`harness/statespace.py`)

Every quantity below is what the code computes. `K` = number of weight sets evaluated at once (1 in a match,
thousands when hill-climbing over a logged run). Rows of `X` are weight sets; columns are state variables.

## 1. Decision state, inputs, sizes

State `x` (n = 9), one row per weight set, `X ∈ R^(K×9)`:

| i | name | meaning |
|---|---|---|
| 0 | `done` | done evidence (counted "done" verdicts) |
| 1 | `fail` | failing evidence (failing verdicts in a row) |
| 2 | `stalled` | "stalled" verdicts in a row |
| 3 | `guard_m` | guard "yes" from the monitor, in a row |
| 4 | `guard_l` | guard "yes" from the local detector on new live frames, in a row |
| 5 | `pmax` | best progress this attempt |
| 6 | `since_rise` | seconds since progress last rose by 0.05 |
| 7 | `t` | seconds into the attempt |
| 8 | `since_done` | seconds since the last counted "done" (10^6 = none yet) |

Input `u` (m = 12), one row vector per tick, `u ∈ R^(1×12)`:

| j | name | value |
|---|---|---|
| 0 | `dt` | seconds since the last tick (0.05 at 20 Hz) |
| 1 | `v_new` | 1 on the tick a real monitor verdict lands |
| 2 | `v_done` | verdict says done |
| 3 | `v_same` | ...on the same frames as the last done |
| 4 | `v_failing` | verdict says no, or status failing, or progress fell 0.3 below its best |
| 5 | `v_stalled` | status stalled |
| 6 | `v_rise` | progress rose ≥ 0.05 above its best |
| 7 | `v_guard_yes` | monitor guard yes |
| 8 | `v_guard_no` | monitor guard no |
| 9 | `l_guard_yes` | local detector: illegal ball between the fingers, new frame |
| 10 | `l_guard_no` | local detector: clear, new frame |
| 11 | `obs_release` | observer: gripper released at the goal (weight 0 by default) |

## 2. The linear part (every tick)

```
X_{k+1} = (1_K ⊗ (1 − clip(u_k R, 0, 1))) ⊙ (X_k Aᵀ) + 1_K (u_k B)
```

`⊙` is element-wise, `1_K` a K×1 column of ones (the same input reaches every weight set).

`A = I_9`: every state holds its value unless something resets it.

`B ∈ R^(12×9)`: what each input adds (all other entries 0):

```
             done fail stalled guard_m guard_l pmax since_rise  t  since_done
dt             0    0     0       0       0     0       1       1      1
v_guard_yes    0    0     0       1       0     0       0       0      0
l_guard_yes    0    0     0       0       1     0       0       0      0
```

`R ∈ R^(12×9)`: which states an input resets to 0 (all other entries 0):

```
             done fail stalled guard_m guard_l pmax since_rise  t  since_done
v_rise         0    0     0       0       0     0       1       0      0
v_guard_no     0    0     0       1       0     0       0       0      0
l_guard_no     0    0     0       0       1     0       0       0      0
```

So the clocks integrate `dt`, and each guard counter counts "yes" answers in a row and resets on a "no".

## 3. The verdict jump map (only on ticks with `v_new = 1`)

The counters for "done" and "failing" switch on which kind of verdict landed, so this part is a switched
(hybrid) system: one masked update per weight set. Per-weight-set vectors: confirm `c ∈ R^K`, done window
`w ∈ R^K` (15 s), minimum gap `g ∈ R^K` (3 s). Let `s = X[:, since_done]` before the update.

```
stalled ← (stalled_prev + 1) · v_stalled            (a non-stalled verdict resets it)

if v_same:                   nothing                (the same frames judged twice are one look)
elif v_done:
    ok     = s ≥ g                                   (closer than g: the same look, not a second one)
    rows ok:  fail ← 0,  done ← done + 1,  since_done ← 0
    pass   = ok ∧ ( s ≤ w  ∨  done ≥ c )             (a second done inside the window, or c in a row)
elif v_failing:
    out    = s > w                                   (one flicker right after a done does not undo it)
    rows out: done ← 0,  fail ← fail + 1
else:                                                (a verdict with no answer)
    done ← 0,  fail ← 0
```

`pmax` and `v_rise` come from the feature step: `v_rise = 1[p ≥ pmax + 0.05]`, `pmax ← max(pmax, p)`.

## 4. Observer coupling (off by default)

```
done ← done + w_goal · 1[ẑ_at_goal ≥ γ] · dt + w_release · obs_release
pass ← pass ∨ (done ≥ c)
```

Defaults `w_goal = w_release = 0`, so the local perception cannot pass a stage until the hill-climb shows that it
should.

## 5. Decisions (read out every tick, per weight set)

```
pass    = from §3 / §4
fail    = (fail ≥ c_f) ∧ (t ≥ φ·T  ∨  ¬monitor)               c_f = 2, φ = 0.5, T = stage timeout
stall   = no_done ∧ ( stalled ≥ c_s  ∨  (since_rise ≥ T_s ∧ 0 < pmax < 0.95) )      c_s = 2, only if the stage has stall_s
guard   = (guard_m ≥ c_gm) ∨ (guard_l ≥ c_gl)                c_gm = plan guard confirm (1), c_gl = 3 live frames
timeout = t ≥ T
```

`no_done` = no counted done this attempt (`since_done = 10^6`).

## 6. Observer (state estimate)

Estimated task state `ẑ ∈ [0,1]^5` = (progress, holding, at_goal, illegal_grip, cable_out).

```
predict (every tick):   ẑ ← ½·1 + ρ (ẑ − ½·1)          (progress is held, not relaxed)          ρ = 0.98
fast update (local):    ẑ ← ẑ + L_f · H_f (y_f − ẑ)                                              L_f = 0.25
slow update (Claude):   ẑ ← ẑ + L_s · H_s (y_s − ẑ)                                              L_s = 0.80
```

`H` is a 0/1 diagonal mask of the components that were measured on that tick. Written the usual way:
`ẑ⁺ = (I − L H) ẑ⁻ + L H y`, a fixed-gain (steady-state Kalman / Luenberger) observer. The fast path pulls the
estimate a quarter of the way to each local measurement; a Claude verdict pulls it 80% of the way, so the slower
but more reliable source wins when they disagree. All values are clipped to [0, 1].

## 7. As a neural network

Sections 2–3 are one gated recurrent cell:

```
h_{k+1} = (1 − σ_hard(u_k R)) ⊙ (h_k Aᵀ) + u_k B + J(h_k, u_k)
```

with `σ_hard = clip(·, 0, 1)` and `J` the jump map of §3 (piecewise-linear, so it is exactly expressible with ReLU
and step units). `StateSpace.export(path)` saves `A`, `B`, `R` and the per-weight-set thresholds; with logged sim
runs, the thresholds and gains can be fitted instead of hand-set.

## 8. Defaults (= today's runner)

| weight | value | runner constant |
|---|---|---|
| `confirm` c | 2 | plan `confirm` |
| `window_s` w | 15 s | `DONE_WINDOW_S` |
| `min_gap_s` g | 3 s | plan `confirm_gap_s` (new) |
| `fail_confirm` c_f | 2 | plan `confirm` |
| `fail_min_fraction` φ | 0.5 | `FAIL_MIN_FRACTION` |
| `stall_confirm` c_s | 2 | plan `confirm` |
| `guard_confirm` c_gm | 1 | plan guard `confirm` |
| `live_guard_frames` c_gl | 3 | `--live-guard-frames` |
| `l_fast`, `l_slow`, `decay` | 0.25, 0.8, 0.98 | new (observer) |
| `w_goal`, `w_release`, `goal_conf` γ | 0, 0, 0.9 | new (off) |

With these defaults the model makes the same pass decision as `harness/decide.py`'s reference rule on 3,000
random verdict sequences (`tests/test_statespace.py`), and one tick over 1,000 weight sets costs well under 5 ms.
