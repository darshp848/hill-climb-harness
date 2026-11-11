# Claude's review of the Codex audit (8 Nov 2026)

Codex (`gpt-6.1-sol`, high reasoning, Codex CLI 0.161.0) ran on Darsh's laptop in a throwaway clone. It could
write only to its own `out/` folder, and it made no robot, Dream or network calls. Its report is
[AUDIT_FINDINGS.md](AUDIT_FINDINGS.md) and [findings.json](findings.json), with raw evidence in [raw/](raw/). Heads
audited: main `b1f6e4b`, cloud-sims `c6a3255`, task_simulation `60d6846`, plus the other 12 branches by diff.

I re-read the code for each finding in a separate clone of the same commits. Verdicts:

- **Confirmed**: I read the code path and it does what Codex says.
- **Partly**: the core claim holds, but part of it is wrong or overstated.
- **Plausible**: the reasoning is sound, but I did not trace it end to end, or it depends on unmeasured hardware facts.
- **Not checked**: sim-only or test-hygiene items I did not re-verify.

| id | Codex severity | My verdict | My severity | Note |
|---|---|---|---|---|
| A01 | Critical | **Confirmed, and broader** | Critical | `Runner.run()` catches only named exceptions, and `cli.py` catches only `StationError`. Any other Python error (OSError from logging, KeyError, AttributeError, StopIteration...) escapes without `_safe_stop()`, so the robot keeps running its last prompt. The fix is one `except BaseException: self._safe_stop(...); raise` in `run()`. |
| A02 | Critical | Confirmed | High | `tower_pass` has `skip_if_done: true`, and `_entry_check` passes on one synchronous look at confidence ≥ 0.8, with no 3 s free-standing dwell. stack_3 has already been confirmed by 2 looks, which limits the risk. |
| A03 | High | Confirmed | High | `recent` (two dones within `DONE_WINDOW_S` = 15 s) passes even after `consec_done` was reset, and a "failing" verdict within 15 s of a done is ignored. A tower that falls and is re-placed inside 15 s can pass. On cloud-sims, `confirm_gap_s` defaults to 0. |
| A04 | High | Confirmed | High | The monitor's "stage start" image (`verifiers.py` `_first`) is the first frame it is asked about, after `min_run_s` plus a poll. On continuous same-prompt stages (stack_2 → stack_3 → tower_pass → higher_*) that frame can already show the success. The higher_* success text ("one more block than in the first image") is the worst case. |
| A05 | High | **Partly** | Medium | Wrong that main has no fast guard: `--detect` installs `runner.fast_guard` (`cli.py:118-121`), which runs on every poll. Right that it runs only every `check_every_s` (3 s), not at the 10 Hz of cloud-sims' live guard. |
| A06 | High | Confirmed | High | `TableDetector.guard` returns False ("none held") if any readable wrist view shows nothing. A missing or frozen right-wrist frame is masked by the left wrist. |
| A07 | High | Confirmed (thresholds) | High | White needs S ≥ 22 and hue 135–168. It was tuned on a phone photo, not on the station's wrist cameras. Neutral or warm-balanced white falls outside that range. Prevalence on the real cameras is unmeasured. |
| A08 | High | **Confirmed (I found it independently)** | **Critical for today** | Both branches now default to the fine-tune, but they still send text it was never trained on. main `hill_climb_v4.yaml` keeps `tower_prompts`, so the stick stage sends "stand a stick upright on top of the stack", which is not a recorded text. TELEOP_PLAN step 4 says to delete `tower_prompts` for a fine-tune. cloud-sims `hybrid_v4.yaml` keeps `rewrite_prompts`, `prompt_technique`, `tower_prompts`, `prompt_prefix` and the long zero-shot unplug texts, all of which its own FINE-TUNE NOTE says to turn off. `--no-rewrite` turns off only the rewrites. TELEOP_PLAN's A/B on Stage 1 (5 runs each model) was meant to decide the default and has not been run. |
| A09 | High | Confirmed | High | `StationClient` timeout is 70 s, and the monitor transport timeout is 60–90 s. Entry checks, preconditions and the tower look are synchronous, so one slow call can run the robot well past the 295 s budget before `_check_global` runs again. |
| A10 | High | Confirmed | Medium | `match_problems` rejects only `--budget` and manual verifiers. `--verifier time` and `--skip` of pass-bar stages pass. (`--skip cable_extra` is legitimately needed, so an allow-list is the right fix, not a ban.) |
| A11 | High | Confirmed | High | `_poll_health` restarts via `_start_run`, not `_issue`. That skips `notify_prompt_change`, so the verifier generation is not bumped, and `last_done_t` survives in `_watch`. On cloud-sims, `_maybe_reset` also runs `/reset` (both arms move) before the restart, and the old done still counts. |
| A12 | High | Confirmed | Medium | After 2 refusals each retry resends the same prompt once (3 s nap). With `max_attempts: 99` on the unplug pass bar, this spends the clock, but no other stage could score anyway. |
| A13 | High | Plausible | Medium | The put-down and clear stages are `on_fail: skip`, and nothing checks for an empty gripper before stack_2. |
| A14 | High | Plausible | Medium | Depends on real block height. If 2 blocks are over 10 cm, stack_3 (99 attempts) is wasted risk. Measure one block. |
| A15 | High | Not checked | (sim) | task_simulation tower oracle. |
| A16 | Medium | Confirmed | Low | Cumulative ball targets only mislabel stages: every ball stage sends the same prompt, and the referee counts the balls. |
| A17 | Medium | Confirmed earlier | Medium (sim) | Already in the hill-climb-harness memory notes: main's MuJoCo table treats white balls as legal. |
| A18 | Medium | Not checked | (sim) | |
| A19 | Medium | Confirmed | Low | Real, but latent: `claude.hedge` defaults to 1 and no plan sets it. |
| A20–A26 | Medium/Low | Not checked | — | Oracle, transport, add-on resize and test hygiene. A21 is real (subscription transport sends only `CLI_SYSTEM_PROMPT`) but matters only with `HARNESS_CLAUDE_BACKEND=subscription`. |

## What to do before 15:00 PT, in order

1. **Choose the model consciously (A08).** For a fine-tune match on main, delete `tower_prompts` from
   `hill_climb_v4.yaml` (or override it), and run `python -m harness.tasks` on the plan. On cloud-sims, run the
   fine-tune only with `hill_climb_v4.yaml` and `--no-rewrite`, never `hybrid_v4`. Use hybrid_v4 only with
   `--var model=dreamlabs/dream-1-beta`. If there is time, do the 5+5 Stage 1 A/B from TELEOP_PLAN.
2. **Always stop on any exception (A01).** This is a two-line change in `Runner.run()`.
3. **Keep a hand on the e-stop for every run anyway**: A09 (deadline overrun on a slow call) and A06/A07 (guard
   blind spots) are not fixable safely in the time left.
4. If code changes are still going in: clear done evidence on a station restart (A11), and drop
   `skip_if_done` from tower_pass or require two looks (A02).
