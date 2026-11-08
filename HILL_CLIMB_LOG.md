# Hill climb log

Rules in GOAL.md. One change per arm, same seeds across arms, results from `match.json` rows with `valid: true`
only. Points as mean with a 95% bootstrap CI; pass-bar = every pass-bar stage passed; Dream Hz = `dream.call_hz`.

## Round 1 (8 Nov 2026, night): does the corrected sim + Darsh's decisions help?

Plan `hybrid_v4` (base Dream-1, `--skip cable_extra`), seeds 0-7, image `cloud-v7`.

| arm | where | change | n valid | points (95% CI) | pass-bar | losses | Dream Hz | sim rate | notes |
|---|---|---|---|---|---|---|---|---|---|
| A baseline | cloud | `--execution-horizon 0 --operator-notes off`, oracle judge (the old harness) | | | | | | | |
| B async 2 Hz + notes | cloud | `--execution-horizon 10 --dream-pipeline 2`, notes on, oracle judge | | | | | | | |
| C B + real judge | cloud | B, `--judge openrouter` Haiku 5.5 reasoning low | | | | | | | |
| D B at 1 Hz | cluster | B with `--dream-pipeline 1` | | | | | | | |

Decision after round 1: keep B over A only if the points CI says so without more losses; C vs B is the judge gap.
