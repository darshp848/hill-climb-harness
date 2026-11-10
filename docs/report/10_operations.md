# 10. Operations and failure modes

[Back to the index](../TECHNICAL_REPORT.md)

## 10.1 Dream errors seen, and what the harness does

### 10.1.1 On the station [V]

From `robots-inference.jsonl` (`kind: "error"`):

| Error | 7 Nov log (19 runs, to 00:02 UTC) | Full log (52 runs, to 05:22 UTC 8 Nov) | Text | Harness action (`runner.py:79-98`) |
|---|---|---|---|---|
| Safety refusal | 4 | 4 (19:55, 20:33, 23:01, 23:30) | `DreamBadRequest DA-4000 BAD_REQUEST (malformed): unsafe instruction, refused: …` | restart once, then fail the attempt (match goes on) |
| IK failure | 3 | 4 (23:03, 23:34, 23:50, 03:11) | `LinAlgError: SVD did not converge` | `/reset` + restart (≤ 2 auto-resets per run) |
| Action buffer empty | 2 | 4 (19:57, 23:37, 03:10, 03:12) | `ControlLoopError: action buffer empty for 241 consecutive ticks (12.05 s)` | restart (≤ 5 per run) |
| Session replaced | 1 | 4 (23:36, 03:11 ×3) | `DreamSessionExpired DA-0001 … was replaced by …` | restart |
| **Total** | **10** | **16** | | |

The digest's "DA-4000 ×4, SVD ×3, buffer-empty ×2, session expired ×1" is the 7 Nov log; the paper's "4 + 4 + 4 + 4"
is the full log. Both are right. No HTTP 402 or 429 appears anywhere.

Refusal reasons Dream gave (`LAST_NIGHT_2026-11-08.md`): "inserting metal or objects into a power strip", **"a
person's hand is holding the plug"** (a real scene problem), "unplug cables from a live power strip", "unplug live
bare wires". Another refusal on 8 Nov 13:54 was triggered by a person's arm over the green ball. Remedies: a **true**
safety sentence in every cable prompt (the strip is unplugged; insulated low-voltage USB cords), and hands out of the
camera view during runs. Plug-in prompts were still refused in the sim ("Plugging cords into power strips carries
electrical/equipment…").

### 10.1.2 In sim and cloud runs (8 Nov)

| Error | When | Meaning |
|---|---|---|
| `DA-1000 UNAUTHORIZED (disabled)` | 12:04:48 UTC to the end of the runs, 83 log lines | **Out of inference credit.** Reads like a revoked key while account calls (listing models) keep working. ~28 concurrent sessions were open; happened twice on 8 Nov. |
| DA-4000 (plug-in prompt) | round 1, 6 lines | safety refusal |
| `DA-6000 GATEWAY_FAULT (connection_timed_out)` | night of 8 Nov, 2 mentions | transient; restart |

Operational rule from this: **probe the policy with one call before every batch and stop on DA-1000** (never built;
`LAST_NIGHT` to-do #3), never run ~28 sessions at once without checking credit, and never let Batch retry a paid
match (c80189c).

### 10.1.3 Other station failure modes

| Failure | Cause | Mitigation |
|---|---|---|
| Arm has no queued motion for about a third of each stop-and-wait cycle (computed) | median RTT above the 900 ms pacer threshold → sync | light frames; wired network; station-side hedging; keep the harness's own traffic small |
| Calls of 2–6 s | the harness pulled ~2 MB debug-view trips (one note, no controlled comparison) | `/frame/<cam>.jpg?w=640` (~35 KB) |
| 7–12 s discarded first answers after `/start`; fine-tune cold start 3–15 min | session warm-up | never `/start` mid-match; lock with a prompt instead of `/stop` |
| Lost run record | Dream overwrites `last-run.csv` on every `/start`; the archiver polls every 3 s | `ARCHIVE_WAIT_S = 4` before a restart |
| Restart from 10+ cm up → SVD error → full `/reset` | IK seeded far from Dream's last state | the pull skill lowers back before handing back to Dream (`runner.py:970-973`, e8e1489) |
| Dream went for its own black arm cable | "the USB cable" was ambiguous | "the orange USB cable" (59f439a, **not on main**) |
| Dream went for the balls mid-unplug | "toss it" | removed "toss" (bec9975), but the 13:57 replay's only confirmed unplug used the "toss" wording, and 716c4d0 put "toss … to the far right" back — this is why `test_bot_v1_plan` fails on main |
| Monitor blind | OpenRouter key over its limit (every look HTTP 403) | switch the panel's monitor to Haiku on the Anthropic API (8ec93b1, a516488); `max_blind_s` stops the run if the guard gets no answer for 20 s |
| Gripper too weak | hold torque 0.5 N·m originally | 1.5 → 2.0 N·m (Dream's max) at 19:14 UTC 8 Nov; watch gripper temperature (latches off at 55 °C) |

## 10.2 The match laptop: MacBook Air (Apple Silicon, fanless)

Constraints (`docs/LOCAL_DECISIONS_PERF.md`, `mac-arm64`):

- 4 performance + 4 efficiency cores, **no fan**: fast for 30 s, slower after a few minutes of sustained load. The live
  guard (10 Hz × 2 wrists), the per-poll guard, the monitor's request build (annotation + stitch), and HTTP threads
  share 4 P-cores. Rule: **vectorise and do less; do not add threads** (`harness/perf.py` caps BLAS/OpenMP to one
  thread).
- **Exact or not at all**: any change to `detect.py` must keep `tests/test_guard_equivalence.py` green (verdicts, boxes
  and annotated JPEGs identical to `tests/_detect_reference.py`).
- Measured speed-ups are in §5.2.5; on the GitHub `macos-15` M1 VM the sustained bench managed only ~28 guards/s of
  ~50–60 because the VM stretches short sleeps — check `guards/s` on the real Air.
- `requirements-match.txt` pins the match environment; `tools/mac_bench.py --sustained 300` shows p95 per 30 s window
  and `pmset -g therm`.
- macOS specifics elsewhere: `sim_station.py --viewer` must be started with `mjpython`; the fake Claude CLI tests had
  to be made portable (422e3b8 for Windows; two Windows-shebang failures documented in `OPENROUTER_RESEARCH.md`).
- **None of this was measured on the actual Air**, and `mac-arm64` was never merged, so the match laptop ran the
  slower detector from main.

## 10.3 Secrets and environment (names only)

| Secret / variable | Used by | Notes |
|---|---|---|
| `DREAM_API_KEY` / `~/.dreamlabs/credentials` | station agent, `sim_station.py`, cloud | the fine-tune runs only under the fine-tune account's key; kept in a tmux shell (`read -rs DREAM_API_KEY; export DREAM_API_KEY`) |
| `ANTHROPIC_API_KEY` | monitor, confirm, writer, filler on the API backend | never logged; stripped from `claude -p`'s environment on the subscription backend |
| `OPENROUTER_API_KEY` (via `--claude-backend openrouter` / `--monitor-model`) | fast monitor, judge bench | `docs/OPENROUTER_SETUP.md` |
| `HARNESS_CLAUDE_BACKEND` (`api`/`subscription`/`openrouter`), `HARNESS_ANTHROPIC_MODELS` | backend selection | |
| `LIVEKIT_URL`, `LIVEKIT_TOKEN` or `LIVEKIT_API_KEY` + `LIVEKIT_API_SECRET` + `LIVEKIT_ROOM`, `LIVEKIT_PUBLISHER`, `LIVEKIT_TRACKS` | LiveKit observer/viewer | minted tokens are subscribe-only |
| `PAH_HEDGE_N`, `PAH_HEDGE_CUTOFF_S`, `PAH_HEDGE_MAX_CALLS`, `PAH_HEDGE_MAX_IN_FLIGHT`, `PAH_HEDGE_LOG` | station-side hedged Dream calls | off unless `PAH_HEDGE_N ≥ 2` |
| `PAH_FF_TAU_S`, `PAH_METAL_TICK_HZ`, `PAH_IK_TRAJ`, `PAH_TORQUE_CAP_NM`, `PAH_GRAVITY_SCALE`, `PAH_EEF_*`, `PAH_TICK_LOG`, `PAH_PULL_FORCE_N`, `PAH_PULL_DIR` | station tuning (`station-tuning`, `ee-pull-force`) | opt-in; refuse to load on any `dreamlabs` ≠ 1.0.37 |
| Doppler (`doppler run -p … -c … --`) | how keys were injected for the match command (`plans/bot_v1.yaml:24-26`) | keys never on the command line |
| GCP Secret Manager `anthropic-api-key`, the Dream key secret | cloud runner | uploaded by the user; workers read only these two |
| `MUJOCO_GL` (`egl` / `osmesa`), `PYOPENGL_PLATFORM` | headless rendering | |

## 10.4 Safety posture

- The physical e-stop is the real safety. Every station guide says to keep a hand on it, people clear, and that only
  a person runs commands that move the arms.
- `--dry-run` sends no POST; `--live` asks you to type `yes` after the safety lines; `--no-operator` (and `--match` on
  final-harness) refuses human input after go.
- The harness never homes at the end of a run unless `reset_last` is set, and never after a guard trip (the gripper
  holds an illegal ball).
- Open safety items: audit A09 (blocking I/O can carry motion past the deadline: station client timeout 70 s, monitor
  transport 60–90 s); A06/A07 (guard blind spots; §5.2.4); A11 (a restart after a fault keeps old done evidence).
