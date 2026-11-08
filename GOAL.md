# Goal: hill-climb the harness to the highest consistent score

Set by Darsh, 8 Nov 2026. Every change and every cloud/cluster run is judged against these targets.

## Targets

| # | Target | Measured as | Done when |
|---|---|---|---|
| 1 | **Dream calls at 2 Hz** (normal action frequency) | `get_action` requests per second of match, from the sim's trip log | median >= 2.0 Hz in valid matches, arms keep moving during inference (sim time / wall time >= 0.95) |
| 2 | **Every task succeeds** | share of valid matches where each pass-bar stage passes (unplug, stack_2, stack_3, tower_pass) | 100% over the latest 100 valid matches |
| 3 | **Zero losses** | DQs and 8-ball (illegal ball) losses | 0 in the latest 100 valid matches |
| 4 | **Points as high as possible** | mean points per valid match, 95% bootstrap CI | each accepted change raises the mean with the CI lower bound above the previous mean |
| 5 | **Infrastructure works** | valid-match rate (`match.json` `valid`), paid retries | 100% valid, 0 paid re-runs |

## Rules for the hill climb

- One change per arm. Same seeds across arms (paired comparison). At least 20 valid matches per arm before a
  decision, more when the difference is small.
- Keep a change only if it improves target 4 without hurting 2 or 3. Record every arm in `HILL_CLIMB_LOG.md`
  (arm, change, n, mean points with CI, pass-bar rate, losses, Dream Hz, cost).
- Darsh's operator decisions (`docs/decisions.pdf`, `plans/operator_notes.yaml`) are the starting policy:
  technique per stage, error detection, 2 tries (3 for high points), push the white ball off first.
- Fine-tuned Dream-1 gets its recorded texts byte for byte. Free-text prompting only on base Dream-1.
- Review running jobs with their live snapshots (`live/scene.jpg`, `live/status.json`) and the console,
  with screenshots, at least every 20 minutes while jobs run. Cancel a job the moment its matches look broken.
- Never print or commit keys. Never let Batch re-run a paid match.

## Levers, in order of expected value

1. Correct sim setup: Dream's async pacing (`execution_horizon` 10 at 20 Hz = 2 Hz calls, physics keeps
   running during inference, seam smoothing via `commanded`), as the Dream SDK documents (`dream/pacing.py`).
2. Judge per decision: fastest accurate model for "is the stage done" vs "is a gripper holding an illegal
   ball" (benchmarks in `hedged-calls/runs/bench_fair`), reasoning level per decision, hedged calls.
3. Prompting: operator technique on/off, prompt levels 0-5, per stage.
4. Plan: retry counts, stage order, white-ball push, stopping rules.
5. Scale: cloud Batch (12 VMs, quota-limited) + home cluster (Inspiron, 4 containers).
