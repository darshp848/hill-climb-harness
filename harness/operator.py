"""The operator talks to the harness in plain words while it runs (testing and tuning only: any human input during a
match is a DQ).

Each typed line goes to Claude with the plan and the run so far. Claude answers it and says what to do about it:
end the current stage, skip later stages, send the robot a new instruction now, or keep a lasting note ("next time
approach the tower from the left") that the monitor follows for the rest of the run and that is saved as a lesson
for later runs. The runner applies the actions between checks; everything goes to events.jsonl.
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .plan import Plan
from .verifiers import valid_jpeg

MAX_CONTEXT_EVENTS = 40
SHOWN = ("stage_start", "prompt_change", "prompt_rewrite", "verdict", "attempt_end", "stage_skipped", "recovery",
         "retry", "auto_reset", "station_restart", "guard_yes", "operator_input", "operator_reply", "tower_prompt")

INSTRUCTION = """You are the operator's assistant inside a robot test harness. The operator is watching the robot and
typed the message below. Read it as plain speech: it may be a question ("why did it pick the pink block?"), an order
("skip the next step", "stop stacking, go to the balls"), or feedback on how to do things ("grip the plug lower next
time"). Reply to the operator in 1-3 short sentences, then output JSON only between <json> and </json>:
{"reply": str,
 "end_current_stage": bool,          // true: give up the current stage now and go on with the next one
 "skip_stages": [str],                // names of LATER stages to skip (exact names from the stage list)
 "prompt_now": str|null,              // a new instruction for the robot to follow right now, max 200 characters
 "note": str|null}                    // lasting feedback for how prompts and judging should go from now on
This is a test run to tune the harness, not a match: do what the operator asks, even where a match rule would
forbid it, and never argue. Only set an action the operator asked for. A question alone gets only a reply. Set
"note" only when the operator says how the robot or the judging should do things (it is kept for later runs), never
for a skip, a stop or another one-off order; word it as their instruction."""


@dataclass
class Actions:
    end_current_stage: bool = False
    skip_stages: set[str] = field(default_factory=set)
    prompts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def parse_reply(text: str, stage_names: list[str]) -> tuple[str, dict[str, Any]]:
    start, end = text.rfind("<json>"), text.rfind("</json>")
    if start < 0 or end < start:
        return text.strip(), {}
    try:
        obj = json.loads(text[start + len("<json>"):end])
    except ValueError:
        return text[:start].strip(), {}
    if not isinstance(obj, dict):
        return text[:start].strip(), {}
    out: dict[str, Any] = {}
    if obj.get("end_current_stage") is True:
        out["end_current_stage"] = True
    skips = [s for s in obj.get("skip_stages") or [] if isinstance(s, str) and s in stage_names]
    if skips:
        out["skip_stages"] = skips
    for key, limit in (("prompt_now", 300), ("note", 500)):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = " ".join(v.split())[:limit]
    reply = obj.get("reply") if isinstance(obj.get("reply"), str) else text[:start]
    return reply.strip(), out


class Operator:
    """Collects the operator's lines, asks Claude about each in a background thread, queues the actions."""

    def __init__(self, plan: Plan, log: Any, *, transport: Callable[[dict[str, Any]], dict[str, Any]] | None,
                 model: str = "claude-haiku-5-5", lessons_path: Path | None = None,
                 print_fn: Callable[[str], None] = print):
        self.plan, self.log, self._transport, self.model = plan, log, transport, model
        self.lessons_path = lessons_path
        self._print = print_fn
        self._lock = threading.Lock()
        self._actions = Actions()
        self._events: list[str] = []
        self.status: Callable[[], str] = lambda: ""   # the runner's one-line state (stage, prompt, elapsed)
        self.frames: Callable[[], dict[str, bytes]] = dict

    # -- what the runner tells it --------------------------------------------------------------
    def record(self, event: str, fields: dict[str, Any]) -> None:
        """A condensed copy of the run's events, the context for the operator's questions."""
        if event not in SHOWN:
            return
        keep = {k: v for k, v in fields.items() if k in ("stage", "prompt", "why", "reason", "done", "progress",
                                                           "status", "holding", "ok", "error", "text", "next")}
        with self._lock:
            self._events = (self._events + [f"{event} {json.dumps(keep, default=str)}"])[-MAX_CONTEXT_EVENTS:]

    def take(self) -> Actions:
        with self._lock:
            out, self._actions = self._actions, Actions()
        return out

    # -- the operator's lines ------------------------------------------------------------------
    def submit(self, line: str) -> None:
        self.log.event("operator_input", text=line)
        self.record("operator_input", {"text": line})
        if self._transport is None:
            self._print("[operator] logged (no Claude key, so nothing is done with it)")
            return
        threading.Thread(target=self._handle, args=(line,), name="operator", daemon=True).start()

    def _handle(self, line: str) -> None:
        names = [s.name for s in self.plan.stages]
        with self._lock:
            context = "\n".join(self._events)
        stages = "\n".join(f"  {s.name}: {s.prompt}" for s in self.plan.stages)
        text = (f"Task: {self.plan.task}\nStages in order:\n{stages}\nNow: {self.status()}\n"
                f"Recent events, oldest first:\n{context}\n\n{INSTRUCTION}\n\nOperator: {line}")
        content: list[dict[str, Any]] = []
        for role, jpeg in sorted(self.frames().items()):
            if valid_jpeg(jpeg):
                content += [{"type": "text", "text": f"newest {role}"},
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": base64.b64encode(jpeg).decode("ascii")}}]
        content.append({"type": "text", "text": text})
        payload: dict[str, Any] = {"model": self.model, "max_tokens": 2000, "output_config": {"effort": "low"},
                                   "messages": [{"role": "user", "content": content}]}
        if self.plan.system_prompt:
            payload["system"] = self.plan.system_prompt
        try:
            reply = self._transport(payload)
            raw = "".join(b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text")
        except Exception as exc:  # noqa: BLE001 - the operator's question must never end a run
            self.log.event("operator_reply", text=line, reply=f"error: {type(exc).__name__}: {exc}")
            self._print(f"[operator] no answer: {type(exc).__name__}")
            return
        answer, acts = parse_reply(raw, names)
        self.log.event("operator_reply", text=line, reply=answer, actions=acts)
        self.record("operator_reply", {"text": answer})
        self._print(f"[claude] {answer}" + (f"  -> {acts}" if acts else ""))
        with self._lock:
            a = self._actions
            a.end_current_stage = a.end_current_stage or bool(acts.get("end_current_stage"))
            a.skip_stages |= set(acts.get("skip_stages", []))
            if "prompt_now" in acts:
                a.prompts.append(acts["prompt_now"])
            if "note" in acts:
                a.notes.append(acts["note"])
        if "note" in acts:
            self._save_lesson(acts["note"], line)

    def _save_lesson(self, note: str, said: str) -> None:
        if self.lessons_path is None:
            return
        try:
            self.lessons_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lessons_path.open("a", encoding="utf-8") as fh:
                fh.write(f"- {note}  (operator: \"{said}\", run {self.log.path.name})\n")
        except OSError as exc:
            self.log.event("warning", message=f"lesson not saved: {exc}")


def load_lessons(path: Path | None) -> str:
    """Earlier operator lessons, for the monitor's system prompt. '' when there are none."""
    if path is None or not path.exists():
        return ""
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]
    return "\n".join(lines[-20:])
