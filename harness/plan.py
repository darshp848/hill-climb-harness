"""Plan files: YAML in, validated dataclasses out."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from pathlib import Path
from typing import Any

import yaml

VERIFIERS = ("manual", "claude", "time", "claude+manual", "monitor", "monitor+manual")
RECOVERY_KINDS = ("retry", "reset")
# retry_then_skip: retry in place (recovery menu as usual) up to max_attempts, then move on instead of ending the run.
# Darsh's rule (decisions.pdf): "if a task fails more than twice, move on; three times if it is worth a lot".
ON_FAIL = ("retry_in_place", "home_and_retry", "skip", "abort", "retry_then_skip")
MAX_PROMPT_CHARS = 512

_PLAN_KEYS = {"task", "checkpoint", "time_budget_s", "check_every_s", "verifier", "claude", "cameras_for_check", "stages", "confirm", "done_message", "vars", "guard", "tower_target", "tower_prompts",
              "system_prompt", "rewrite_prompts", "prompt_prefix", "reset_first",
              "write_prompts", "rewrite_max", "rewrite_cooldown_s", "stop_on_pass", "hands_off", "lock_prompt",
              "prompt_engine"}
TOWER_STAGES = ("stack", "tower", "higher")   # stage-name prefixes the tower target applies to
_STAGE_KEYS = {"name", "prompt", "success", "timeout_s", "max_attempts", "min_run_s", "verifier", "recovery", "on_fail",
               "stall_s", "settle_s", "skip_if_done", "skip_unless", "confirm", "expect",
               "cameras", "camera_notes", "history_cameras", "watch", "requires_stage", "needs_grip", "confirm_check",
               "grip_assist", "write_prompt", "retry_prompt", "writer_notes", "template", "slots", "variants",
               "not_after_s"}
_RECOVERY_KEYS = {"name", "prompt", "when", "kind", "settle_s", "max_uses"}
_GRIP_KEYS = {"arm", "regrip", "regrip_dz", "pull_up_m", "pull_s", "seat_dz", "repull", "put_down_prompt",
              "put_down_s", "hold", "open_above", "poll_s", "min_z", "pass_on_pull"}
MAX_MOVE_M = 0.12   # the station's /move takes at most 12 cm per call


@dataclass
class Recovery:
    """One entry of a stage's recovery menu. A v1 plain-string item has legacy=True (hold, then re-issue)."""

    name: str
    prompt: str = ""
    when: str = ""
    kind: str = "reset"  # retry: re-issue the stage prompt; reset: send `prompt`, check the scene, then re-issue
    settle_s: float = 8.0
    max_uses: int = 1
    legacy: bool = False


@dataclass
class GripAssist:
    """Dream grips, the harness fixes the grip and pulls (tools/try_prompt.py, 8 Nov: 2/2 plugs held through the pull).

    While Dream runs the stage prompt the runner reads GET /arms every poll_s. When `arm`'s gripper was open
    (> open_above) and now reads shut on nothing (<= hold[0]) twice: /stop, open in place, lower regrip_dz, close (at
    most `regrip` times; no hold -> Dream starts again). When it holds (hold[0] < g < hold[1]) twice: /stop, (seat:
    open a little, lower seat_dz, ) squeeze in place, re-read, then lift straight up by pull_up_m steps of pull_s
    each, grip shut. A slip on the way up goes back down and re-grips, at most `repull` times. Then Dream gets
    put_down_prompt for up to put_down_s. Every /move is sent with no Dream run going, <= 12 cm each."""

    arm: str = "right"
    regrip: int = 2
    regrip_dz: float = -0.015
    pull_up_m: list[float] = field(default_factory=lambda: [0.06, 0.04])
    pull_s: float = 3.0
    seat_dz: float = 0.0
    repull: int = 2
    put_down_prompt: str | None = None
    put_down_s: float = 15.0
    # Reading of a holding gripper. A USB plug is thin: 0.05-0.15 (8 Nov: a pull from 0.27 lost the plug, so <0.20).
    # A held foam cube reads ~0.33-0.51: a stage gripping cubes would use [0.25, 0.6].
    hold: tuple[float, float] = (0.045, 0.20)
    open_above: float = 0.5
    poll_s: float = 1.0
    # No pull when the hand is lower than this (GET /pose z, m, arm base frame): a plug in the strip is gripped at
    # ~0.08, a cable lying on the table at ~0.02 (8 Nov live run: the retry "unplugged" a loose cable off the table).
    min_z: float | None = None
    # After a pull that held and the put-down: ask the stage's confirm_check at once and pass on a yes, instead of
    # waiting for the fast monitor (8 Nov live run: a real unplug at 40 s was never called done, and the retry went
    # for the loose cable).
    pass_on_pull: bool = True


@dataclass
class Stage:
    name: str
    prompt: str
    success: str
    timeout_s: float = 60.0
    max_attempts: int = 3
    min_run_s: float = 4.0
    verifier: str = "manual"
    recovery: list[str | Recovery] = field(default_factory=list)
    on_fail: str = "retry_in_place"
    stall_s: float | None = None
    settle_s: float | None = None  # None = min_run_s
    skip_if_done: bool = False
    # A yes/no question asked once before the prompt is sent; the stage is skipped unless the answer is a confident
    # yes. E.g. plug-in stages: "are both grippers empty?", so a still-held UNPLUGGED cable is never pushed back in.
    skip_unless: str | None = None
    confirm: int | None = None    # None = the plan's confirm; 1 for stages where a false "done" costs nothing
    expect: str | None = None
    cameras: list[str] = field(default_factory=list)          # roles this stage's checks see; empty = plan's
    camera_notes: dict[str, str] = field(default_factory=dict)  # role substring -> what that view shows
    history_cameras: list[str] = field(default_factory=list)  # extra roles whose frame history the monitor keeps
    watch: Watch | None = None
    requires_stage: str | None = None   # skip this stage at once unless the named earlier stage passed
    # A "done" counts only after a gripper was felt holding something in this stage (/arms: commanded shut, stopped
    # partway). An unplug cannot happen without a grip; in the sim the monitor passed cable stages on loose cord ends.
    needs_grip: bool = False
    # A pointed yes/no question a stronger model (plan claude.confirm_model) answers from the stage-start and newest
    # frames before the stage may pass. For stages where a false pass costs the match (an unplug: blocks touched
    # before a cable is out are a DQ).
    confirm_check: str | None = None
    grip_assist: GripAssist | None = None
    # False: the monitor neither writes this stage's prompt (plan.write_prompts) nor rewrites it mid-stage
    # (plan.rewrite_prompts); stage.prompt goes to Dream as written. 8 Nov: the writer's detail hurt stacking.
    write_prompt: bool = True
    retry_prompt: str | None = None   # sent instead of `prompt` on every retry of the stage
    # Extra rules for the stage-prompt writer on this stage (plan.write_prompts). It may answer SKIP (nothing it can
    # do here, e.g. no legal ball within reach): the stage is then skipped.
    writer_notes: str | None = None
    # plan.prompt_engine: the template this stage's instruction is composed from, and its default slot options
    # (also the fallback). `prompt` stays the intent the monitor reads and the text a verbatim run sends.
    template: str | None = None
    slots: dict[str, str] = field(default_factory=dict)
    # More templates for later tries of the same stage: try n uses ([template] + variants)[(n - 1) % len].
    variants: list[str] = field(default_factory=list)
    # Match clock (s since run start) after which this optional stage is not started or retried: leaves time for the
    # stages after it. Only on skip / retry_then_skip stages (a pass bar can't be left).
    not_after_s: float | None = None

    def note_for(self, role: str) -> str:
        return next((n for k, n in self.camera_notes.items() if k in role), "")

    def menu(self) -> list[Recovery]:
        """The recovery list with v1 string items wrapped as legacy Recovery entries."""
        return [r if isinstance(r, Recovery) else Recovery(name=f"recovery{i + 1}", prompt=r, legacy=True)
                for i, r in enumerate(self.recovery)]

    def effective_settle(self) -> float:
        return self.min_run_s if self.settle_s is None else self.settle_s


_GUARD_KEYS = {"question", "stages", "confirm", "message", "max_blind_s"}


@dataclass
class Guard:
    """A yes/no safety question the monitor answers on every check of the stages it covers. `confirm` yes answers
    in a row stop the run at once (POST /stop: the arms hold where they are, so a held object stays held)."""

    question: str
    stages: list[str] = field(default_factory=list)   # stage-name prefixes it covers; empty = every stage
    confirm: int = 1
    message: str = "GUARD TRIPPED: RUN STOPPED. CALL STOP NOW"
    # Stop as if tripped when a covered stage has had no yes/no answer for this long (monitor down, bad frames):
    # a robot binning balls that nobody is watching for a black one. None = never.
    max_blind_s: float | None = None

    def covers(self, stage_name: str) -> bool:
        return not self.stages or any(stage_name.startswith(p) for p in self.stages)


@dataclass
class Watch:
    """A watcher arm whose wrist camera must keep looking at the work while another arm does the stage.

    Each monitor check also answers `check`; after `confirm` "no" answers in a row the runner sends `prompt`
    (put the watcher back), holds `settle_s`, then re-sends the stage prompt. At most `max_fixes` per attempt;
    a fix does not count as a failed attempt."""

    camera: str
    check: str
    prompt: str
    settle_s: float = 5.0
    max_fixes: int = 2


@dataclass
class Plan:
    task: str
    stages: list[Stage]
    checkpoint: str | None = None
    time_budget_s: float = 900.0
    check_every_s: float = 3.0
    verifier: str = "manual"
    claude: dict[str, Any] = field(default_factory=lambda: {"model": "claude-haiku-5-5", "max_tokens": 200})
    cameras_for_check: list[str] = field(default_factory=list)  # empty = all cameras
    confirm: int = 1
    claude_user: dict[str, Any] = field(default_factory=dict)  # only the keys the plan file set
    done_before: list[str] = field(default_factory=list)       # stages dropped by --skip: done before this run
    done_message: str = ""  # printed large (with a bell) when every stage passed, e.g. "CALL STOP NOW"
    guard: Guard | None = None
    # The tower to build, bottom to top, one piece per entry ("blue cube, hole facing sideways"). The monitor gets it
    # as the expected result on stacking stages and reports the tower it sees; the runner logs whether the order
    # matches. It does not gate a stage: the stack prompt is one recorded text, so Dream can't be told which piece.
    tower_target: list[str] = field(default_factory=list)
    # Optional: name the next piece in the prompt instead of the stage's generic one. Shape word -> template with
    # <piece>, e.g. {"cube": "put the <piece> on top of the stack"}. The piece is the target's next entry after the
    # tower the monitor last reported; an optional <top> names the piece it goes on. Off (empty) for a fine-tune
    # that only knows its recorded texts.
    tower_prompts: dict[str, str] = field(default_factory=dict)
    system_prompt: str = ""        # the monitor's system prompt: station rules and lessons from testing
    # Let the monitor write the robot's next prompt (its `next_prompt`), with a cooldown and a cap per attempt.
    # Base Dream-1 only: a fine-tune follows only its recorded task texts.
    rewrite_prompts: bool = False
    rewrite_max: int = 3               # monitor-written prompts per attempt
    rewrite_cooldown_s: float = 15.0   # no monitor-written prompt sooner than this after any prompt change
    # The monitor writes each stage's prompt at stage start (and at each retry) from what it sees: one short
    # instruction naming the object and where it goes. stage.prompt is the intent it works from and the fallback.
    # Base Dream-1 only (8 Nov, bot: "the pink cube onto the blue cube" stacked; "a cube onto the cube" hit a ball).
    write_prompts: bool = False
    # A sentence a rewritten prompt must open with when the stage's own prompt opens with it (Dream refuses cable
    # work without the "no power" sentence); added when the monitor drops it.
    prompt_prefix: str = ""
    reset_first: bool = False      # POST /reset and wait for idle before the first stage: both grippers start open
    # Lock a success: stop Dream at the first look that would pass the stage (before the confirm_check), so it can't
    # undo it (8 Nov: "stack ... as high as possible" built blue on blue, then took it apart). A confirm that says
    # no starts Dream again on the same prompt.
    stop_on_pass: bool = False
    # With stop_on_pass: send this prompt instead of /stop (e.g. "open both grippers and move the arms up"), so Dream's
    # run stays warm and the next stage only changes the prompt: a fresh /start costs 7-12 s of first answers.
    lock_prompt: str = ""
    # Leave Dream alone: an attempt ends only on a pass, a guard or the stage timeout. No regression / "failing" /
    # "stalled" exits, no recovery prompts, and a retry does not resend the prompt Dream is already running. 8 Nov
    # live: a noisy fast monitor ended 7 stacking attempts in 4 min, each with "open both grippers ...", which made
    # Dream drop the cube it held.
    hands_off: bool = False
    prompts: Any = None            # harness.prompt_engine.Library from plan.prompt_engine, or None


def _roles(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(c, str) and c.strip() for c in value):
        raise ValueError(f"{where}: must be a list of camera role names")
    return [c.strip() for c in value]


def _cooldown(value: Any) -> float:
    """plan.rewrite_cooldown_s: a number of seconds, 0 allowed."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"plan.rewrite_cooldown_s: must be a number >= 0, got {value!r}")
    return float(value)


def _num(value: Any, where: str, *, integer: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and not isinstance(value, int)):
        raise ValueError(f"{where}: must be {'an integer' if integer else 'a number'}, got {value!r}")
    if value <= 0:
        raise ValueError(f"{where}: must be positive, got {value!r}")
    return value


def _prompt(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: must be a non-empty string")
    value = value.strip()
    if "\n" in value or "\r" in value:
        raise ValueError(f"{where}: must be a single line")
    if len(value) > MAX_PROMPT_CHARS:
        raise ValueError(f"{where}: {len(value)} chars, the limit is {MAX_PROMPT_CHARS}")
    return value


def _enum(value: Any, allowed: tuple[str, ...], where: str) -> str:
    if value not in allowed:
        raise ValueError(f"{where}: must be one of {', '.join(allowed)}, got {value!r}")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{where}: must be true or false, got {value!r}")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: must be a non-empty string")
    return value.strip()


def _recovery_item(raw: Any, j: int, label: str, seen: set[str]) -> str | Recovery:
    """A v1 string item stays a plain str in Stage.recovery (Stage.menu() wraps it)."""
    where = f"{label}.recovery[{j}]"
    if isinstance(raw, str):
        item = Recovery(name=f"recovery{j + 1}", prompt=_prompt(raw, where), legacy=True)
        if item.name in seen:
            raise ValueError(f"{where}: name clash with another recovery item")
        seen.add(item.name)
        return item.prompt
    elif isinstance(raw, dict):
        unknown = set(raw) - _RECOVERY_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown field(s) {sorted(unknown)}")
        has_prompt = raw.get("prompt") is not None
        kind = _enum(raw.get("kind", "reset" if has_prompt else "retry"), RECOVERY_KINDS, f"{where}.kind")
        if kind == "reset" and not has_prompt:
            raise ValueError(f"{where}.prompt: required for kind reset")
        if kind == "retry" and has_prompt:
            raise ValueError(f"{where}.prompt: kind retry re-issues the stage prompt; remove prompt or use kind reset")
        name = raw.get("name", f"recovery{j + 1}")
        item = Recovery(
            name=_text(name, f"{where}.name"),
            prompt=_prompt(raw["prompt"], f"{where}.prompt") if has_prompt else "",
            when=_text(raw["when"], f"{where}.when") if raw.get("when") is not None else "",
            kind=kind,
            settle_s=float(_num(raw.get("settle_s", 8.0), f"{where}.settle_s")),
            max_uses=int(_num(raw.get("max_uses", 1), f"{where}.max_uses", integer=True)),
        )
    else:
        raise ValueError(f"{where}: must be a prompt string or a mapping")
    if item.name in seen:
        raise ValueError(f"{where}.name: duplicate recovery name {item.name!r}")
    seen.add(item.name)
    return item


def _variants(raw: Any, template: Any, label: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(v, str) and v.strip() for v in raw):
        raise ValueError(f"{label}.variants: must be a list of template ids")
    if not template:
        raise ValueError(f"{label}.variants: needs a stage template")
    return [v.strip() for v in raw]


def _not_after(raw: dict, label: str) -> float | None:
    if raw.get("not_after_s") is None:
        return None
    if raw.get("on_fail") not in ("skip", "retry_then_skip"):
        raise ValueError(f"{label}.not_after_s: only on on_fail skip or retry_then_skip stages (a pass bar can't be left)")
    return float(_num(raw["not_after_s"], f"{label}.not_after_s"))


def parse_plan(data: Any) -> Plan:
    """Validates an already-loaded mapping."""
    if not isinstance(data, dict):
        raise ValueError("plan: top level must be a mapping")
    unknown = set(data) - _PLAN_KEYS
    if unknown:
        raise ValueError(f"plan: unknown field(s) {sorted(unknown)}")
    default_verifier = _enum(data.get("verifier", "manual"), VERIFIERS, "plan.verifier")
    raw_stages = data.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise ValueError("plan.stages: must be a non-empty list")

    stages: list[Stage] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_stages):
        label = f"stage #{i + 1}"
        if not isinstance(raw, dict):
            raise ValueError(f"{label}: must be a mapping")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{label}.name: must be a non-empty string")
        label = f"stage {name!r}"
        if name in seen:
            raise ValueError(f"{label}.name: duplicate stage name")
        prerequisite = raw.get("requires_stage")
        if prerequisite is not None and (not isinstance(prerequisite, str) or prerequisite not in seen):
            raise ValueError(f"{label}.requires_stage: must name an earlier stage")
        seen.add(name)
        unknown = set(raw) - _STAGE_KEYS
        if unknown:
            raise ValueError(f"{label}: unknown field(s) {sorted(unknown)}")
        success = raw.get("success", f"Has the stage '{name}' been completed?")
        if not isinstance(success, str) or not success.strip():
            raise ValueError(f"{label}.success: must be a non-empty string")
        recovery = raw.get("recovery") or []
        if not isinstance(recovery, list):
            raise ValueError(f"{label}.recovery: must be a list of prompts or mappings")
        rec_seen: set[str] = set()
        recs = [_recovery_item(r, j, label, rec_seen) for j, r in enumerate(recovery)]
        stall = raw.get("stall_s")
        settle = raw.get("settle_s")
        expect = raw.get("expect")
        if expect is not None:
            expect = _text(expect, f"{label}.expect")
        cams = _roles(raw.get("cameras"), f"{label}.cameras")
        hist = _roles(raw.get("history_cameras"), f"{label}.history_cameras")
        notes = raw.get("camera_notes") or {}
        if not isinstance(notes, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                                  for k, v in notes.items()):
            raise ValueError(f"{label}.camera_notes: must map camera role names to non-empty text")
        watch = None
        if raw.get("watch") is not None:
            w = raw["watch"]
            if not isinstance(w, dict) or set(w) - {"camera", "check", "prompt", "settle_s", "max_fixes"}:
                raise ValueError(f"{label}.watch: must be a mapping of camera, check, prompt[, settle_s, max_fixes]")
            watch = Watch(camera=_text(w.get("camera"), f"{label}.watch.camera"),
                          check=_text(w.get("check"), f"{label}.watch.check"),
                          prompt=_prompt(w.get("prompt"), f"{label}.watch.prompt"),
                          settle_s=float(_num(w.get("settle_s", 5.0), f"{label}.watch.settle_s")),
                          max_fixes=int(_num(w.get("max_fixes", 2), f"{label}.watch.max_fixes", integer=True)))
            if not any(watch.camera in h for h in hist):
                hist.append(watch.camera)   # the monitor needs the watcher's frame history to judge it
        stages.append(
            Stage(
                name=name,
                prompt=_prompt(raw.get("prompt"), f"{label}.prompt"),
                success=success.strip(),
                timeout_s=float(_num(raw.get("timeout_s", 60.0), f"{label}.timeout_s")),
                max_attempts=int(_num(raw.get("max_attempts", 3), f"{label}.max_attempts", integer=True)),
                min_run_s=float(_num(raw.get("min_run_s", 4.0), f"{label}.min_run_s")),
                verifier=_enum(raw.get("verifier", default_verifier), VERIFIERS, f"{label}.verifier"),
                recovery=recs,
                on_fail=_enum(raw.get("on_fail", "retry_in_place"), ON_FAIL, f"{label}.on_fail"),
                stall_s=None if stall is None else float(_num(stall, f"{label}.stall_s")),
                settle_s=None if settle is None else float(_num(settle, f"{label}.settle_s")),
                skip_if_done=_bool(raw.get("skip_if_done", False), f"{label}.skip_if_done"),
                skip_unless=None if raw.get("skip_unless") is None else _text(raw["skip_unless"], f"{label}.skip_unless"),
                confirm=None if raw.get("confirm") is None else int(_num(raw["confirm"], f"{label}.confirm", integer=True)),
                expect=expect,
                cameras=cams,
                camera_notes={k: v.strip() for k, v in notes.items()},
                history_cameras=hist,
                watch=watch,
                requires_stage=prerequisite,
                needs_grip=_bool(raw.get("needs_grip", False), f"{label}.needs_grip"),
                confirm_check=None if raw.get("confirm_check") is None else _text(raw["confirm_check"], f"{label}.confirm_check"),
                grip_assist=_grip_assist(raw.get("grip_assist"), f"{label}.grip_assist"),
                write_prompt=_bool(raw.get("write_prompt", True), f"{label}.write_prompt"),
                retry_prompt=None if raw.get("retry_prompt") is None else _prompt(raw["retry_prompt"], f"{label}.retry_prompt"),
                writer_notes=None if raw.get("writer_notes") is None else _text(raw["writer_notes"], f"{label}.writer_notes"),
                template=None if raw.get("template") is None else _text(raw["template"], f"{label}.template"),
                slots=_slot_defaults(raw.get("slots"), label),
                variants=_variants(raw.get("variants"), raw.get("template"), label),
                not_after_s=_not_after(raw, label),
            )
        )

    checkpoint = data.get("checkpoint")
    if checkpoint is not None and (not isinstance(checkpoint, str) or not checkpoint.strip()):
        raise ValueError("plan.checkpoint: must be a string or null")
    claude = {"model": "claude-haiku-5-5", "max_tokens": 200}
    claude_user: dict[str, Any] = {}
    if data.get("claude") is not None:
        if not isinstance(data["claude"], dict):
            raise ValueError("plan.claude: must be a mapping")
        claude.update(data["claude"])
        claude_user = dict(data["claude"])
    cams = data.get("cameras_for_check") or []
    if not isinstance(cams, list) or not all(isinstance(c, str) for c in cams):
        raise ValueError("plan.cameras_for_check: must be a list of role names")
    task = data.get("task", "")
    if not isinstance(task, str):
        raise ValueError("plan.task: must be a string")
    done_message = data.get("done_message", "") or ""
    if not isinstance(done_message, str):
        raise ValueError("plan.done_message: must be a string")
    guard = _guard(data.get("guard"))
    if guard is not None and default_verifier not in ("monitor", "monitor+manual"):
        raise ValueError("plan.guard: needs verifier monitor or monitor+manual (only the monitor answers it)")
    if guard is not None:
        names = [st.name for st in stages]
        for prefix in guard.stages:
            if not any(n.startswith(prefix) for n in names):
                raise ValueError(f"plan.guard.stages: {prefix!r} matches no stage name")
    tower = data.get("tower_target") or []
    if not isinstance(tower, list) or not all(isinstance(x, str) and x.strip() for x in tower):
        raise ValueError("plan.tower_target: must be a list of pieces, bottom to top")
    system_prompt = data.get("system_prompt", "") or ""
    if not isinstance(system_prompt, str):
        raise ValueError("plan.system_prompt: must be a string")
    prefix = data.get("prompt_prefix", "") or ""
    if not isinstance(prefix, str):
        raise ValueError("plan.prompt_prefix: must be a string")
    from harness.prompt_engine import parse_library   # here: prompt_engine imports nothing from plan
    prompts = parse_library(data.get("prompt_engine"), stages)
    if prompts is not None and prompts.mode == "verbatim":
        on = [k for k in ("rewrite_prompts", "write_prompts") if data.get(k)] + (["tower_prompts"] if data.get("tower_prompts") else [])
        if on:
            raise ValueError(f"plan.prompt_engine: mode verbatim (fine-tune) sends recorded texts only; turn off {on}")
    if prompts is not None and prompts.mode == "compose" and data.get("write_prompts"):
        raise ValueError("plan: write_prompts (free text) and prompt_engine compose are two prompt writers; pick one")
    return Plan(
        prompts=prompts,
        task=task,
        guard=guard,
        system_prompt=system_prompt.strip(),
        rewrite_prompts=_bool(data.get("rewrite_prompts", False), "plan.rewrite_prompts"),
        rewrite_max=int(_num(data.get("rewrite_max", 3), "plan.rewrite_max", integer=True)),
        rewrite_cooldown_s=_cooldown(data.get("rewrite_cooldown_s", 15.0)),
        write_prompts=_bool(data.get("write_prompts", False), "plan.write_prompts"),
        prompt_prefix=prefix.strip(),
        reset_first=_bool(data.get("reset_first", False), "plan.reset_first"),
        stop_on_pass=_bool(data.get("stop_on_pass", False), "plan.stop_on_pass"),
        lock_prompt=_prompt(data["lock_prompt"], "plan.lock_prompt") if data.get("lock_prompt") else "",
        hands_off=_bool(data.get("hands_off", False), "plan.hands_off"),
        tower_target=[x.strip() for x in tower],
        tower_prompts=_tower_prompts(data.get("tower_prompts"), [x.strip() for x in tower]),
        done_message=done_message,
        stages=stages,
        checkpoint=checkpoint,
        time_budget_s=float(_num(data.get("time_budget_s", 900.0), "plan.time_budget_s")),
        check_every_s=float(_num(data.get("check_every_s", 3.0), "plan.check_every_s")),
        verifier=default_verifier,
        claude=claude,
        cameras_for_check=list(cams),
        confirm=int(_num(data.get("confirm", 1), "plan.confirm", integer=True)),
        claude_user=claude_user,
    )


def _slot_defaults(raw: Any, label: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
        raise ValueError(f"{label}.slots: must map slot names to option strings")
    return {k: " ".join(v.split()) for k, v in raw.items()}


def _tower_prompts(raw: Any, target: list[str]) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                            for k, v in raw.items()):
        raise ValueError("plan.tower_prompts: must map a shape word to a prompt")
    if not target:
        raise ValueError("plan.tower_prompts: needs tower_target")
    for piece in target:
        name = piece.split(",")[0].strip()
        if not any(name.endswith(k) for k in raw):
            raise ValueError(f"plan.tower_prompts: no template for {name!r}")
        prompt = next(v for k, v in raw.items() if name.endswith(k)).replace("<piece>", name).replace("<top>", name)
        _prompt(prompt, "plan.tower_prompts")
    return {k: v.strip() for k, v in raw.items()}


def _grip_assist(raw: Any, where: str) -> GripAssist | None:
    if raw is None or raw is False:
        return None
    if raw is True:
        return GripAssist()
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: must be a mapping (or true for the defaults)")
    unknown = set(raw) - _GRIP_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown field(s) {sorted(unknown)}")
    d = GripAssist()
    arm = raw.get("arm", d.arm)
    if arm not in ("left", "right"):
        raise ValueError(f"{where}.arm: must be left or right, got {arm!r}")
    steps = raw.get("pull_up_m", d.pull_up_m)
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"{where}.pull_up_m: must be a non-empty list of lift steps in metres")
    steps = [float(_num(x, f"{where}.pull_up_m")) for x in steps]
    if any(x > MAX_MOVE_M for x in steps):
        raise ValueError(f"{where}.pull_up_m: each step must be <= {MAX_MOVE_M} m (the station's /move limit)")
    hold = raw.get("hold", list(d.hold))
    if (not isinstance(hold, list) or len(hold) != 2 or not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                                                for x in hold) or not 0 <= hold[0] < hold[1] <= 1):
        raise ValueError(f"{where}.hold: must be [low, high] gripper readings with 0 <= low < high <= 1")
    regrip_dz = raw.get("regrip_dz", d.regrip_dz)
    if isinstance(regrip_dz, bool) or not isinstance(regrip_dz, (int, float)) or abs(regrip_dz) > MAX_MOVE_M:
        raise ValueError(f"{where}.regrip_dz: must be a number of metres within +-{MAX_MOVE_M}")
    seat = raw.get("seat_dz", 0.0)
    if isinstance(seat, bool) or not isinstance(seat, (int, float)) or not 0 <= seat <= MAX_MOVE_M:
        raise ValueError(f"{where}.seat_dz: must be 0..{MAX_MOVE_M} m (lowered before the pull; 0 = no seat)")
    counts = {}
    for k in ("regrip", "repull"):
        v = raw.get(k, getattr(d, k))
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(f"{where}.{k}: must be an integer >= 0, got {v!r}")
        counts[k] = v
    put = raw.get("put_down_prompt")
    return GripAssist(arm=arm, regrip=counts["regrip"], regrip_dz=float(regrip_dz), pull_up_m=steps,
                      pull_s=float(_num(raw.get("pull_s", d.pull_s), f"{where}.pull_s")), seat_dz=float(seat),
                      repull=counts["repull"], put_down_prompt=None if put is None else _prompt(put, f"{where}.put_down_prompt"),
                      put_down_s=float(_num(raw.get("put_down_s", d.put_down_s), f"{where}.put_down_s")),
                      hold=(float(hold[0]), float(hold[1])),
                      open_above=float(_num(raw.get("open_above", d.open_above), f"{where}.open_above")),
                      poll_s=float(_num(raw.get("poll_s", d.poll_s), f"{where}.poll_s")),
                      min_z=None if raw.get("min_z") is None else float(_num(raw["min_z"], f"{where}.min_z")),
                      pass_on_pull=_bool(raw.get("pass_on_pull", True), f"{where}.pass_on_pull"))


def _guard(raw: Any) -> Guard | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("plan.guard: must be a mapping")
    extra = set(raw) - _GUARD_KEYS
    if extra:
        raise ValueError(f"plan.guard: unknown key(s): {', '.join(sorted(extra))}")
    question = raw.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("plan.guard.question: must be a non-empty yes/no question")
    stages = raw.get("stages") or []
    if not isinstance(stages, list) or not all(isinstance(x, str) and x.strip() for x in stages):
        raise ValueError("plan.guard.stages: must be a list of stage-name prefixes")
    message = raw.get("message", Guard.message)
    if not isinstance(message, str) or not message.strip():
        raise ValueError("plan.guard.message: must be non-empty text")
    blind = raw.get("max_blind_s")
    return Guard(question=question.strip(), stages=[x.strip() for x in stages],
                 confirm=int(_num(raw.get("confirm", 1), "plan.guard.confirm", integer=True)), message=message.strip(),
                 max_blind_s=None if blind is None else float(_num(blind, "plan.guard.max_blind_s")))


_VAR = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def substitute(text: str, variables: dict[str, str] | None) -> str:
    """Replaces {name} placeholders (no spaces inside the braces) with --var values.

    Raises ValueError for a placeholder with no value, so a plan never reaches the robot with "{color}" in it.
    YAML flow mappings such as {name: x} are untouched (they contain a colon and a space)."""
    variables = variables or {}
    missing = sorted({m for m in _VAR.findall(text) if m not in variables})
    if missing:
        raise ValueError(f"plan needs --var for: {', '.join(missing)}")
    return _VAR.sub(lambda m: variables[m.group(1)], text)


def load_plan(path: str | Path, variables: dict[str, str] | None = None) -> Plan:
    """Reads and validates a plan YAML file. Raises ValueError (or OSError if unreadable).

    {name} placeholders are filled from the plan's own `vars:` defaults, then from `variables` (--var wins)."""
    text = Path(path).read_text(encoding="utf-8")
    merged: dict[str, str] = {}
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        raw = None  # placeholders may sit where YAML can't parse them; the substituted text is checked below
    if isinstance(raw, dict) and raw.get("vars") is not None:
        if not isinstance(raw["vars"], dict) or not all(isinstance(k, str) for k in raw["vars"]):
            raise ValueError("plan.vars: must map names to values")
        merged.update({k: str(v) for k, v in raw["vars"].items()})
    merged.update(variables or {})
    text = substitute(text, merged)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from None
    return parse_plan(data)
