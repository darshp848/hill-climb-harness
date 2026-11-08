"""Layered prompt generation for base Dream-1: the same stage prompt with more or less context added, so the cloud
sweep can find which level of context makes the policy do the task best.

Levels (each includes the ones below it):
  0  bare        the stage's action only, no safety prefix (Dream refuses cable work without the prefix:
                 kept as the control)
  1  prefix      + the plan's prompt_prefix ("no power" sentence), as the plans ship today
  2  scene       + what the scene holds right now (the monitor's last report of what each gripper holds / the tower)
  3  state       + where the task stands (attempt number, progress, what the robot is holding)
  4  history     + why the last attempt failed, in the monitor's words
  5  goal        + the match goal in one sentence (rubric: why this step matters)

NEVER for a fine-tuned Dream-1: a fine-tune only follows its recorded task texts byte for byte, so the plan's
prompts must stay as recorded (level 1 = what the plan already sends). Use with base Dream-1 (plans/hybrid_v4*).
"""

from __future__ import annotations

from dataclasses import dataclass

GOAL = ("The goal is to finish this step cleanly so the next one can start: unplug cables, then build a free-standing "
        "tower over four inches, then put golf balls in the bin, never an 8-ball.")


@dataclass
class Context:
    prefix: str = ""
    holding: dict | None = None        # monitor's last `holding` (arm -> object)
    tower: list[str] | None = None     # monitor's last tower, bottom to top
    attempt: int = 1
    progress: float | None = None
    last_failure: str = ""


def _strip_prefix(prompt: str, prefix: str) -> str:
    return prompt[len(prefix):].lstrip() if prefix and prompt.startswith(prefix) else prompt


def compose(stage_prompt: str, level: int, ctx: Context) -> str:
    """The prompt at `level`. Level 1 returns the plan's prompt unchanged."""
    core = _strip_prefix(stage_prompt, ctx.prefix)
    if level <= 0:
        return core
    if level == 1:
        return stage_prompt
    parts = [ctx.prefix] if ctx.prefix else []
    if level >= 2:
        held = {k: v for k, v in (ctx.holding or {}).items() if v and str(v).lower() not in ("nothing", "none")}
        if held:
            parts.append("Right now " + ", ".join(f"the {arm} gripper holds {obj}" for arm, obj in held.items()) + ".")
        if ctx.tower:
            parts.append("The tower is, bottom to top: " + ", ".join(ctx.tower) + ".")
    if level >= 3:
        if ctx.attempt > 1:
            parts.append(f"This is try {ctx.attempt} of this step.")
        if ctx.progress is not None and ctx.progress > 0:
            parts.append(f"The step is about {round(100 * ctx.progress)}% done.")
    if level >= 4 and ctx.last_failure:
        parts.append("Last try went wrong: " + ctx.last_failure.rstrip(".") + ". Avoid that.")
    if level >= 5:
        parts.append(GOAL)
    parts.append(core)
    return " ".join(p.strip() for p in parts if p.strip())
