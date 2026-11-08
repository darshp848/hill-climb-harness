"""The prompt engine: every instruction Dream-1 gets is a tested template with its slots filled from what the cameras
see, so no model ever writes free text to the robot.

Layers (docs/PROMPT_ENGINE.md):
  L0 library   plan `prompt_engine.templates` + `vocab`: wordings that worked on the bot (docs/BOT_PROMPTS_2026-11-08.md),
               each slot an option list. Every option is linted at load (word cap, no negations, no arm named in an
               `arm: none` template, no forbidden target), so whatever a filler picks is already clean.
  L1 filler    picks one option per slot from the frames. `default` (stage defaults, no model), `claude` (one cheap
               call, JSON reply of option numbers), or any object with .fill() (the sim's ground-truth oracle).
  L2 policy    the runner: compose at stage start and retry, re-fill (not free text) when the monitor says the robot
               is stuck, prefetch the next stage's slots during the confirm look so a stage boundary waits for no model.

`mode: verbatim` is the fine-tune: stage.prompt byte for byte, nothing composed, no rewrites.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

MODES = ("compose", "verbatim")
FILLERS = ("default", "claude")
ARMS = ("left", "right", "none", "any")
_ENGINE_KEYS = {"mode", "filler", "filler_model", "prefetch_ttl_s", "max_words", "prefix", "forbid", "vocab",
                "templates"}
_TEMPLATE_KEYS = {"text", "prefix", "arm", "slots", "note"}
# Hand tests 8 Nov: "not the white ball" put the white ball in play. Name the right object instead.
NEGATION = re.compile(r"\b(not|no|never|don't|do not|without|except|avoid|instead of|other than)\b", re.I)
ARM_WORDS = re.compile(r"\b(left|right) (arm|gripper|hand)\b", re.I)
SLOT = re.compile(r"<(\w+)>")


@dataclass
class Template:
    id: str
    text: str                        # with <slot> holes; the safety prefix is not part of it
    slots: dict[str, str]            # slot -> vocab list name
    prefix: bool = False             # put plan prompt_engine.prefix in front (cable work)
    arm: str = "any"                 # none: the body may not name an arm (stacking: Dream picks the near arm)
    note: str = ""                   # for the filler: what the template is for


@dataclass
class Composed:
    text: str
    template: str | None
    slots: dict[str, str]
    source: str                      # verbatim | default | filler | prefetch | fallback
    why: str = ""
    latency_s: float | None = None
    issues: list[str] = field(default_factory=list)


@dataclass
class Library:
    mode: str = "compose"
    filler: str = "default"
    filler_model: str = "claude-haiku-5-5"
    prefetch_ttl_s: float = 20.0
    max_words: int = 30
    prefix: str = ""
    forbid: list[str] = field(default_factory=list)
    vocab: dict[str, list[str]] = field(default_factory=dict)
    templates: dict[str, Template] = field(default_factory=dict)

    def render(self, tpl: Template, slots: dict[str, str]) -> tuple[str, str]:
        """(full text, body). Unknown holes stay visible so lint catches them."""
        body = SLOT.sub(lambda m: slots.get(m.group(1), m.group(0)), tpl.text)
        body = " ".join(body.split())
        body = re.sub(r"\s+([,.;])", r"\1", body)
        full = f"{self.prefix} {body}" if tpl.prefix and self.prefix else body
        return full, body

    def lint(self, tpl: Template, body: str) -> list[str]:
        issues = []
        n = len(body.split())
        if n > self.max_words:
            issues.append(f"{n} words > {self.max_words}")
        if m := NEGATION.search(body):
            issues.append(f"negation {m.group(0)!r}")
        if tpl.arm == "none" and (m := ARM_WORDS.search(body)):
            issues.append(f"names an arm ({m.group(0)!r}) in an arm: none template")
        low = body.lower()
        issues += [f"forbidden target {f!r}" for f in self.forbid if f.lower() in low]
        if m := SLOT.search(body):
            issues.append(f"unfilled slot {m.group(0)}")
        return issues

    def defaults(self, tpl: Template, given: dict[str, str] | None = None) -> dict[str, str]:
        given = given or {}
        return {s: given.get(s, self.vocab[v][0]) for s, v in tpl.slots.items()}


def _strs(raw: Any, where: str) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValueError(f"{where}: must be a list of strings")
    return [" ".join(x.split()) for x in raw]


def parse_library(raw: Any, stages: list[Any]) -> Library | None:
    """plan.prompt_engine -> Library, linted. Stage `template`/`slots` are checked against it. None when absent."""
    if raw is None:
        if any(getattr(s, "template", None) for s in stages):
            raise ValueError("stage.template: needs plan.prompt_engine")
        return None
    if not isinstance(raw, dict) or set(raw) - _ENGINE_KEYS:
        raise ValueError(f"plan.prompt_engine: unknown field(s) {sorted(set(raw) - _ENGINE_KEYS) if isinstance(raw, dict) else raw!r}")
    lib = Library(mode=raw.get("mode", "compose"), filler=raw.get("filler", "default"),
                  filler_model=str(raw.get("filler_model", "claude-haiku-5-5")),
                  prefetch_ttl_s=float(raw.get("prefetch_ttl_s", 20.0)), max_words=int(raw.get("max_words", 30)),
                  prefix=" ".join(str(raw.get("prefix", "")).split()),
                  forbid=_strs(raw.get("forbid", []), "plan.prompt_engine.forbid"))
    if lib.mode not in MODES:
        raise ValueError(f"plan.prompt_engine.mode: one of {MODES}")
    if lib.filler not in FILLERS:
        raise ValueError(f"plan.prompt_engine.filler: one of {FILLERS}")
    if lib.mode == "verbatim":
        if any(getattr(s, "template", None) for s in stages):
            raise ValueError("plan.prompt_engine: mode verbatim (fine-tune) sends stage prompts byte for byte; "
                             "remove stage templates")
        return lib
    for name, opts in (raw.get("vocab") or {}).items():
        opts = _strs(opts, f"plan.prompt_engine.vocab.{name}")
        if not opts:
            raise ValueError(f"plan.prompt_engine.vocab.{name}: needs at least one option")
        lib.vocab[name] = opts
    for tid, t in (raw.get("templates") or {}).items():
        where = f"plan.prompt_engine.templates.{tid}"
        if not isinstance(t, dict) or set(t) - _TEMPLATE_KEYS or not isinstance(t.get("text"), str):
            raise ValueError(f"{where}: a mapping with text[, prefix, arm, slots, note]")
        slots = t.get("slots") or {}
        holes = set(SLOT.findall(t["text"]))
        if holes != set(slots):
            raise ValueError(f"{where}: holes {sorted(holes)} must match slots {sorted(slots)}")
        for s, v in slots.items():
            if v not in lib.vocab:
                raise ValueError(f"{where}.slots.{s}: no vocab list {v!r}")
        tpl = Template(id=tid, text=" ".join(t["text"].split()), slots=dict(slots), prefix=bool(t.get("prefix")),
                       arm=t.get("arm", "any"), note=str(t.get("note", "")))
        if tpl.arm not in ARMS:
            raise ValueError(f"{where}.arm: one of {ARMS}")
        if tpl.prefix and not lib.prefix:
            raise ValueError(f"{where}: prefix: true needs plan.prompt_engine.prefix")
        # every option, one slot at a time, must render clean (combinations are linted again at compose time)
        base = lib.defaults(tpl)
        for s, v in [(None, None)] + [(s, o) for s, vn in tpl.slots.items() for o in lib.vocab[vn]]:
            trial = dict(base, **({s: v} if s else {}))
            issues = lib.lint(tpl, lib.render(tpl, trial)[1])
            if issues:
                raise ValueError(f"{where}: {lib.render(tpl, trial)[1]!r}: {'; '.join(issues)}")
        lib.templates[tid] = tpl
    for st in stages:
        tid = getattr(st, "template", None)
        if not tid:
            continue
        if tid not in lib.templates:
            raise ValueError(f"stage {st.name!r}.template: no template {tid!r}")
        ids = [tid, *getattr(st, "variants", [])]
        for vid in ids[1:]:
            if vid not in lib.templates:
                raise ValueError(f"stage {st.name!r}.variants: no template {vid!r}")
        tpls = [lib.templates[i] for i in ids]
        for s, v in (st.slots or {}).items():   # a default must fit every template of the stage that has that slot
            having = [t for t in tpls if s in t.slots]
            if not having or any(v not in lib.vocab[t.slots[s]] for t in having):
                raise ValueError(f"stage {st.name!r}.slots.{s}: {v!r} is not an option of "
                                 f"{sorted({t.slots[s] for t in having}) or s!r}")
    return lib


# ------------------------------------------------------------------------------------------------------ fillers
class Filler(Protocol):
    def fill(self, tpl: Template, lib: Library, stage: Any, frames: dict[str, bytes], ctx: dict[str, Any],
             hint: str | None) -> tuple[dict[str, str] | None, str]: ...


class DefaultFiller:
    """No model: the stage's own slots (or each list's first option)."""

    def fill(self, tpl, lib, stage, frames, ctx, hint):
        return lib.defaults(tpl, stage.slots), "stage defaults"


FILL_RULE = ("Fill the robot's instruction template for THIS stage by choosing one numbered option per slot, from "
             "what the images show now. Pick the object that is really there and easiest to reach; where there are "
             "look-alikes pick the option that tells them apart. Reply only with <slots>{\"slot\": option number, "
             "...}</slots>.")


class ClaudeFiller:
    """One call to a cheap model: images + template + numbered options in, option numbers out."""

    def __init__(self, transport: Callable[[dict[str, Any]], dict[str, Any]], model: str,
                 system_prompt: str = "") -> None:
        self.transport, self.model, self.system_prompt = transport, model, system_prompt

    def request(self, tpl: Template, lib: Library, stage: Any, frames: dict[str, bytes], ctx: dict[str, Any],
                hint: str | None) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for role, jpeg in sorted(frames.items()):
            content.append({"type": "text", "text": f"NOW: {role}"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                        "data": base64.b64encode(jpeg).decode("ascii")}})
        lines = [f"Whole task: {ctx.get('task', '')}", f"This stage: {stage.name}. Passes when: {stage.success}",
                 f"Template: {tpl.text}" + (f" ({tpl.note})" if tpl.note else "")]
        for k in ("done", "tower", "grip"):
            if ctx.get(k):
                lines.append(f"{k}: {ctx[k]}")
        if hint:
            lines.append(f"The robot is stuck with the current instruction ({ctx.get('current', '')!r}): {hint}. "
                         "Choose options that fix that.")
        for s, vn in tpl.slots.items():
            lines.append(f"Slot {s}: " + "; ".join(f"{i}) {o or '(nothing)'}" for i, o in enumerate(lib.vocab[vn])))
        lines.append(FILL_RULE)
        content.append({"type": "text", "text": "\n".join(lines)})
        payload: dict[str, Any] = {"model": self.model, "max_tokens": 1500, "output_config": {"effort": "low"},
                                   "messages": [{"role": "user", "content": content}]}
        if self.system_prompt:
            payload["system"] = self.system_prompt
        return payload

    def fill(self, tpl, lib, stage, frames, ctx, hint):
        if not frames:
            return None, "no frames"
        try:
            reply = self.transport(self.request(tpl, lib, stage, frames, ctx, hint))
        except Exception as exc:  # noqa: BLE001 - the engine falls back to the stage defaults
            return None, f"filler error: {type(exc).__name__}: {str(exc)[:160]}"
        text = "".join(b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text")
        return parse_slots(text, tpl, lib, stage.slots)


def parse_slots(text: str, tpl: Template, lib: Library, given: dict[str, str] | None) -> tuple[dict[str, str] | None, str]:
    """<slots>{"piece": 2}</slots> -> option strings. A bad or missing slot keeps its default; no JSON -> None."""
    m = re.findall(r"<slots>(.*?)</slots>", text, re.S)
    try:
        picked = json.loads(m[-1]) if m else None
    except json.JSONDecodeError:
        picked = None
    if not isinstance(picked, dict):
        return None, f"no slots in reply: {text[-160:]!r}"
    out, bad = lib.defaults(tpl, given), []
    for s, vn in tpl.slots.items():
        v, opts = picked.get(s), lib.vocab[vn]
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < len(opts):
            out[s] = opts[v]
        elif isinstance(v, str) and " ".join(v.split()) in opts:
            out[s] = " ".join(v.split())
        else:
            bad.append(s)
    return out, ("filled" if not bad else f"filled; kept defaults for {bad}")


# ------------------------------------------------------------------------------------------------------- engine
class PromptEngine:
    def __init__(self, lib: Library, filler: Filler | None = None) -> None:
        self.lib = lib
        self.filler: Filler = filler or DefaultFiller()
        self._lock = threading.Lock()
        self._pre: dict[str, tuple[float, Composed]] = {}
        self._threads: dict[str, threading.Thread] = {}

    def applies(self, stage: Any) -> bool:
        return self.lib.mode == "compose" and bool(getattr(stage, "template", None))

    def compose(self, stage: Any, frames: dict[str, bytes], ctx: dict[str, Any], hint: str | None = None) -> Composed:
        if not self.applies(stage):
            return Composed(stage.prompt, None, {}, "verbatim")
        tpl = self.lib.templates[stage.template]
        t0 = time.monotonic()
        slots, why = self.filler.fill(tpl, self.lib, stage, frames, ctx, hint)
        latency = round(time.monotonic() - t0, 2)
        source = "filler" if slots is not None and not isinstance(self.filler, DefaultFiller) else "default"
        if slots is None:
            slots, source = self.lib.defaults(tpl, stage.slots), "fallback"
        text, body = self.lib.render(tpl, slots)
        issues = self.lib.lint(tpl, body)
        if issues:   # an over-long combination: the stage's own slots, which load-time lint passed
            slots, source = self.lib.defaults(tpl, stage.slots), "fallback"
            text, body = self.lib.render(tpl, slots)
        return Composed(text, tpl.id, slots, source, why, latency, issues)

    # L2: fill the next stage while this one is being confirmed --------------------------------------------------
    def prefetch(self, stage: Any, frames_fn: Callable[[], dict[str, bytes]], ctx: dict[str, Any]) -> bool:
        if not self.applies(stage) or self.lib.prefetch_ttl_s <= 0:
            return False
        with self._lock:
            if stage.name in self._threads or stage.name in self._pre:
                return False

            def work() -> None:
                c = self.compose(stage, frames_fn(), ctx)
                with self._lock:
                    self._pre[stage.name] = (time.monotonic(), c)
                    self._threads.pop(stage.name, None)

            th = threading.Thread(target=work, daemon=True, name=f"prefetch-{stage.name}")
            self._threads[stage.name] = th
        th.start()
        return True

    def take(self, stage: Any, wait_s: float = 0.0) -> Composed | None:
        """A prefetched prompt for `stage` if one is ready (or finishes within wait_s) and still fresh."""
        with self._lock:
            th = self._threads.get(stage.name)
        if th is not None and wait_s > 0:
            th.join(wait_s)
        with self._lock:
            got = self._pre.pop(stage.name, None)
        if got is None:
            return None
        t, c = got
        if time.monotonic() - t > self.lib.prefetch_ttl_s:
            return None
        c.source, c.why = "prefetch", f"prefetched {time.monotonic() - t:.1f} s ago; {c.why}"
        return c

    def drop_prefetch(self) -> None:
        with self._lock:
            self._pre.clear()
