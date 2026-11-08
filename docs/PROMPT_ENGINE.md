# Prompt engine (prototype, 8 Nov)

Every instruction Dream-1 gets comes from a tested template with its slots filled from the scene. No model writes
free text to the robot. Code: `harness/prompt_engine.py`. Sim plan: `plans/engine_v4_sim.yaml`.

| Layer | What | Where |
| --- | --- | --- |
| L0 library | `prompt_engine.templates` (wordings that worked on the bot, `docs/BOT_PROMPTS_2026-11-08.md`) and `vocab` (one option list per slot). Every option is linted at load: word cap, no negations, no arm named in an `arm: none` template, no `forbid` phrase, no unfilled `<slot>`. | plan YAML |
| L1 filler | Picks one option per slot. `default`: the stage's `slots` (no model). `claude`: one cheap call (`filler_model`) with the frames and numbered options, reply `<slots>{"piece": 1}</slots>`. Sim: `OracleFiller` from ground truth. A bad reply keeps the stage defaults; an over-long combination falls back to them. | `prompt_engine.py`, `experiments/sim_match.py` |
| L2 policy | The runner composes at stage start and retry. With `rewrite_prompts`, a stalled or failing monitor verdict re-fills the slots with the monitor's reason as a hint; the monitor's `next_prompt` text is logged (`rewrite_suggested`) and never sent; an unchanged refill is not re-sent. On the first done verdict of a stage, the next stage's slots are filled in the background (`prompt_prefetch`) and used at the boundary if younger than `prefetch_ttl_s`. | `runner.py` |
| L3 skills | Not in this prototype: regrip / squeeze / pull-up from `tools/try_prompt.py`. The sim has no `/move`, and harness-driven arm motion in a match needs a rubric check first. | — |

`mode: verbatim` is for a fine-tune: stage prompts byte for byte. The plan loader refuses `rewrite_prompts`,
`write_prompts`, `tower_prompts` or stage templates with it, and refuses `write_prompts` next to `compose`.

Slot holes are `<name>` (plan `{vars}` are substituted before parsing).

Events: `prompt_composed` (template, slots, prompt, source = filler | default | fallback | prefetch | verbatim,
latency, lint issues), `prompt_prefetch`, `rewrite_suggested`, `prompt_rewrite` with `how: refill`. A run's
(template, slots) pairs are the labels for a later fine-tune and the rows for comparing wordings.

Sim: `python experiments/sim_match.py --plan plans/engine_v4_sim.yaml --filler oracle|default|plan`.
