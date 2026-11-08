"""The supervisor state machine. See DESIGN.md "runner.py"."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .frames import FrameSource
from .log import RunLog
from .plan import MAX_MOVE_M, TOWER_STAGES, GripAssist, Plan, Recovery, Stage
from .station_client import StationClient, StationError
from .verifiers import MonitorVerdict, Verdict, Verifier

START_TIMEOUT_S = 120.0
IDLE_TIMEOUT_S = 60.0
MAX_CAMERA_AGE_S = 2.0
_SLEEP_STEP_S = 0.25
STALL_RISE = 0.05          # progress must rise by this much to reset the stall clock
REGRESSION_DROP = 0.3      # a drop this far below the attempt's max counts as a failing verdict
FAIL_MIN_FRACTION = 0.5
DONE_WINDOW_S = 15.0       # two done verdicts this close pass the stage, flickers in between notwithstanding
EXTEND_S = 20.0            # one timeout extension while the monitor sees the gripper on the target    # a monitor "failing" ends an attempt only after this share of timeout_s
SKIP_MIN_CONFIDENCE = 0.8  # skip_if_done needs this much confidence from a non-manual verifier
MOVED_DELTA = 0.02         # progress change that counts as "moved" after a prompt change
MAX_STATION_RESTARTS = 5   # restart the run after at most this many inference-service faults
REFUSAL_RESTARTS = 1       # a prompt Dream refused (DA-4000 unsafe) is restarted this often, then the attempt fails
CONFIRM_GRACE_S = 10.0     # at timeout, wait this long for the verdict that would confirm a pending "done"
REWRITE_COOLDOWN_S = 15.0  # no monitor-written prompt sooner than this after any prompt change
REWRITE_MAX = 3            # monitor-written prompts per attempt
# Dream's eef IK fails now and then ("SVD did not converge"); a plain /start after it often failed again on the
# station, a /reset (grippers open, arms to the start pose) and the same prompt got past it. At most this many a run.
RESET_ERRORS = ("SVD did not converge", "LinAlgError")
AUTO_RESET_MAX = 2
# Dream overwrites its run record (last-run.csv, last-run_inputs/) on every /start; the station's archiver copies it
# when it sees the station not running, polling every 3 s. Run 2 (8 Nov) restarted faster and lost 9 min of record.
ARCHIVE_WAIT_S = 4.0


@dataclass
class StageResult:
    name: str
    ok: bool
    attempts: int
    seconds: float
    reason: str


@dataclass
class _Attempt:
    ok: bool
    reason: str
    max_progress: float | None = None
    recovery_hint: int | None = None   # the monitor's chosen recovery index, if any
    operator: bool = False             # the operator ended it: no recovery, no retry


@dataclass
class RunResult:
    ok: bool
    stages: list[StageResult] = field(default_factory=list)
    seconds: float = 0.0
    reason: str = ""
    guard_tripped: bool = False


def _error_text(last_error: Any) -> str:
    if isinstance(last_error, dict):
        return f"{last_error.get('type', '')} {last_error.get('error_code', '')} {last_error.get('message', '')}"
    return str(last_error or "")


# Dream's safety filter refused the instruction in this scene (DA-4000 BAD_REQUEST "unsafe instruction"). Seen 4
# times on the station on 7 Nov. Its message also says "DreamAPI refused", so it is matched before the faults below.
_REFUSAL_KEYS = ("DA-4000", "DreamBadRequest", "unsafe instruction")
# Faults of the inference path, not of the arms: the run can be restarted with the same prompt. From the NUC's
# robots-inference.jsonl: gateway timeouts, the action buffer running dry for 12 s while chunks were late
# (ControlLoopError), the eef IK's "SVD did not converge" on the first chunk (LinAlgError), and a server-side
# session replacement.
_TRANSIENT_KEYS = ("DreamAPI", "DreamServerError", "DreamProtocolError", "DreamSessionExpired", "DA-5000", "DA-6000",
                   "DA-0001", "GATEWAY_FAULT", "connection_timed_out", "ControlLoopError", "action buffer empty",
                   "LinAlgError", "SVD did not converge")


def _fault_kind(last_error: Any) -> str | None:
    """'refusal', 'transient' (restart is safe) or None (an arm, a motor, a safety stop: never retried)."""
    text = _error_text(last_error)
    if any(key in text for key in _REFUSAL_KEYS):
        return "refusal"
    if any(key in text for key in _TRANSIENT_KEYS):
        return "transient"
    return None


def _norm(piece: str) -> str:
    return " ".join(w for w in piece.lower().replace(",", " ").split() if w not in ("a", "the", "an"))


_SHAPES = {"cube": ("cube", "block"), "stick": ("stick", "cylinder", "rod", "peg")}


def _shape(text: str) -> str | None:
    t = _norm(text)
    return next((shape for shape, words in _SHAPES.items() if any(w in t for w in words)), None)


def tower_matches(seen: list[str], target: list[str]) -> bool:
    """The seen tower has the target's first len(seen) shapes, bottom to top: cubes first, then sticks. Colour order
    doesn't matter (Darsh, 8 Nov) and orientation wording varies, so neither is compared."""
    if len(seen) > len(target):
        return False
    return all(_shape(s) == _shape(t) for s, t in zip(seen, target))


def _inference_fault(last_error: Any) -> bool:
    return _fault_kind(last_error) == "transient"


def _err(last_error: Any) -> str:
    """The real station reports last_error as a dict (message, type, error_code); older or fake ones as a str."""
    if isinstance(last_error, dict):
        return str(last_error.get("message") or last_error)
    return str(last_error)


class _Fail(Exception):
    """End the whole run (the station is stopped by the handler)."""


class _Guard(Exception):
    """The plan's guard answered yes `confirm` times in a row: stop at once."""


class _Refuse(Exception):
    """Preflight refused; nothing was started so nothing is stopped."""


class _Stop(Exception):
    """request_stop() was called."""


class _Refused(Exception):
    """Dream refused this prompt again: the attempt fails, the match goes on."""


class _WriterSkip(Exception):
    """The stage-prompt writer answered SKIP (Stage.writer_notes): nothing to do in this stage."""


class Runner:
    def __init__(self, plan: Plan, station: StationClient, frames: FrameSource, verifiers: dict[str, Verifier],
                 log: RunLog, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.plan, self.station, self.frames, self.verifiers, self.log = plan, station, frames, verifiers, log
        self._clock, self._sleep = clock, sleep
        self._stop_event = threading.Event()
        self._t0 = 0.0
        self._last_progress: float | None = None
        self._change: dict[str, Any] | None = None   # the latest prompt change, for the progress_moved event
        self._prompt: str | None = None   # what the robot should be running, for a restart after a fault
        self._restarts = 0
        # frames -> (yes/no/None, why): a fast local answer to the plan guard (harness.detect.TableDetector.guard)
        self.fast_guard: Callable[[dict[str, bytes]], tuple[bool | None, str]] | None = None
        self._refusals: dict[str, int] = {}
        self._tower: list[str] | None = None   # the tower the monitor last reported, bottom to top
        self._guard_answer_t: float | None = None   # last time the guard got a yes/no answer
        self._auto_resets = 0
        self._done_in_recovery: str | None = None
        self.operator: Any = None   # harness.operator.Operator: the operator's typed words, turned into actions
        self._skip: set[str] = set()   # later stages the operator asked to skip
        self._stage_name = ""
        self._grip_seen = False   # this stage: a gripper was felt holding something (Stage.needs_grip)
        # harness.live_guard.LiveGuard: the plan guard on live /frame images in its own thread (sends /stop itself)
        self.live_guard: Any = None

    def request_stop(self) -> None:
        self._stop_event.set()

    # -- top level -----------------------------------------------------------------------------
    def run(self) -> RunResult:
        self._t0 = self._clock()
        results: list[StageResult] = []
        dry = self.station.dry_run
        tripped = False
        self.log.update_manifest(start_time=datetime.now(timezone.utc).isoformat())
        self.log.event("run_start", task=self.plan.task, dry_run=dry, stages=[s.name for s in self.plan.stages])
        try:
            self._preflight()
            if self.operator is not None:
                self.operator.status = self._status_line
                self.operator.frames = self.frames.latest
            first = True
            for what in self.plan.done_before:   # --skip in practice: those stages count as done
                self._mark_done(what)
            if self.plan.reset_first:
                self._reset_and_wait("before the first stage: both grippers open")
            for stage in self.plan.stages:
                end_now, pending = self._apply_operator()
                if pending and self.operator is not None:   # prompts typed between stages are for the next one
                    with self.operator._lock:
                        self.operator._actions.prompts[:0] = pending
                if end_now:   # "skip" pressed between stages (or during the first reset): skip the next stage
                    self.log.event("stage_skipped", stage=stage.name, reason="operator skipped it before it started")
                    results.append(StageResult(stage.name, False, 0, 0.0, "skipped by operator"))
                    continue
                if stage.requires_stage and not any(r.name == stage.requires_stage and r.ok for r in results):
                    reason = f"{stage.requires_stage} did not pass"   # e.g. no cable came out: nothing to put down
                    self.log.event("stage_skipped", stage=stage.name, reason=reason)
                    results.append(StageResult(stage.name, False, 0, 0.0, reason))
                    continue
                if stage.name in self._skip:
                    self._mark_done(f"{stage.name}: skipped by the operator, the run has moved on from it")
                    self.log.event("stage_skipped", stage=stage.name, reason="operator asked to skip it")
                    results.append(StageResult(stage.name, False, 0, 0.0, "skipped by operator"))
                    continue
                results.append(self._run_stage(stage, first=first))
                first = False
                if results[-1].ok:
                    self._mark_done(f"{stage.name}: {stage.prompt}")
            failed = [r.name for r in results if not r.ok]
            ok, reason = (not failed), ("completed" if not failed else f"completed with failed stages: {', '.join(failed)}")
            self._safe_stop("end of plan")
        except _Refuse as exc:
            ok, reason = False, f"refused: {exc}"
        except _Guard as exc:
            ok, reason = False, str(exc)
            self._safe_stop(reason)
            tripped = True
        except _Fail as exc:
            ok, reason = False, str(exc)
            self._safe_stop(reason)
        except _Refused as exc:           # only reachable from code that does not catch it itself
            ok, reason = False, str(exc)
            self._safe_stop(reason)
        except _Stop:
            ok, reason = False, "stopped by operator"
            self._safe_stop(reason)
        except KeyboardInterrupt:
            ok, reason = False, "interrupted (Ctrl+C)"
            self._safe_stop(reason)
        except StationError as exc:
            ok, reason = False, f"station error: {exc.message}"
            self._safe_stop(reason)
        except TimeoutError as exc:
            ok, reason = False, str(exc)
            self._safe_stop(reason)
        if self.live_guard is not None:
            self.live_guard.disarm()
            tripped = tripped or self.live_guard.tripped is not None
        if self.plan.reset_last and not tripped and getattr(self.station, "reset", None) is not None:
            try:   # back to the start pose for the next run (never after a guard trip: it holds an illegal ball)
                self.station.stop()
                self.station.wait_for_state({"idle", "error"}, IDLE_TIMEOUT_S)
                self.log.event("reset", why="end of run: back to the start pose")
                self.station.reset()
            except (StationError, OSError, TimeoutError) as exc:
                self.log.event("reset_failed", why="end of run", error=str(exc))
        result = RunResult(ok=ok, stages=results, seconds=self._clock() - self._t0, reason=reason,
                           guard_tripped=tripped)
        self.log.update_manifest(end_time=datetime.now(timezone.utc).isoformat(), ok=ok, reason=reason)
        self.log.event("run_end", ok=ok, reason=reason, seconds=result.seconds)
        self.log.write_summary({"task": self.plan.task, "dry_run": dry, **asdict(result)})
        return result

    # -- pieces --------------------------------------------------------------------------------
    def _preflight(self) -> None:
        dry = self.station.dry_run
        st = self.station.status()
        problems: list[str] = []
        if st.get("_synthetic"):
            problems.append("station unreachable (dry run continues with a synthetic status)")
        else:
            if st.get("state") not in ("idle", "error"):
                problems.append(f"station state is {st.get('state')!r}, expected idle")
            dead = [a for a, alive in (st.get("arms") or {}).items() if not alive]
            if dead:
                problems.append(f"arm(s) not alive: {', '.join(dead)}")
            for cam, age in (st.get("cameras") or {}).items():
                if age is None or age > MAX_CAMERA_AGE_S:
                    problems.append(f"camera {cam} is stale ({age})")
        self.log.event("preflight", status=st, problems=problems, dry_run=dry)
        self.log.update_manifest(station_status=st)
        if problems:
            if not dry:
                raise _Refuse("; ".join(problems))
            for p in problems:
                self.log.event("warning", message=f"preflight: {p}")

    def _check_global(self) -> None:
        if self._stop_event.is_set():
            raise _Stop()
        lg = self.live_guard
        if lg is not None and lg.tripped is not None:
            raise _Guard(f"guard tripped (live frames): {lg.tripped}")
        if self._clock() - self._t0 > self.plan.time_budget_s:
            raise _Fail(f"time budget of {self.plan.time_budget_s:g} s exceeded")

    def _nap(self, seconds: float) -> None:
        remaining = seconds
        while remaining > 1e-9:
            step = min(_SLEEP_STEP_S, remaining)
            self._sleep(step)
            remaining -= step
            if self._stop_event.is_set():
                raise _Stop()

    def _poll_health(self) -> dict[str, Any]:
        self._check_global()
        st = self.station.status()
        if st.get("state") == "error":
            self._on_station_error(st.get("last_error"))   # raises unless a restart is allowed
            self._maybe_reset(st.get("last_error"))
            self._nap(ARCHIVE_WAIT_S)
            return self._start_run(self._prompt)
        dead = [a for a, alive in (st.get("arms") or {}).items() if not alive]
        if dead:
            raise _Fail(f"arm(s) not alive: {', '.join(dead)}")
        return st

    def _motion(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Sends a command that can move an arm, never after an operator stop, the deadline or a guard trip. A live
        guard trip found right after the command may have been overtaken by it: stop again and end the match."""
        self._check_global()
        reply = fn(*args)
        lg = self.live_guard
        if lg is not None and lg.tripped is not None:
            self._safe_stop(f"guard tripped while sending a command: {lg.tripped}")
            raise _Guard(f"guard tripped (live frames): {lg.tripped}")
        return reply

    def _safe_stop(self, why: str) -> None:
        self.log.event("stop", why=why)
        try:
            self.station.stop()
        except (StationError, OSError) as exc:
            self.log.event("stop_failed", error=str(exc))

    def _wait(self, states: set[str], timeout_s: float, what: str) -> dict[str, Any]:
        # In 1 s slices, so a live-guard trip (its /stop means `states` may never come) ends the wait at once.
        deadline = time.monotonic() + timeout_s
        while True:
            lg = self.live_guard
            if lg is not None and lg.tripped is not None:
                raise _Guard(f"guard tripped (live frames) while waiting for {what}: {lg.tripped}")
            if self._stop_event.is_set():
                raise _Stop()
            left = deadline - time.monotonic()
            try:
                st = self.station.wait_for_state(states | {"error"}, max(0.0, min(1.0, left)))
                break
            except TimeoutError as exc:
                if left <= 1.0:
                    raise _Fail(f"timed out waiting for {what}: {exc}") from None
        if st.get("state") == "error" and "error" not in states:
            raise _Fail(f"station error: {_err(st.get('last_error'))}")
        return st

    def _on_station_error(self, error: Any) -> None:
        """The station is in state error. Returns if the run may be restarted with self._prompt, else raises."""
        kind = _fault_kind(error) if self._prompt else None
        if kind == "refusal":
            n = self._refusals[self._prompt] = self._refusals.get(self._prompt, 0) + 1
            if n > REFUSAL_RESTARTS:
                self.log.event("prompt_refused", prompt=self._prompt, refusals=n, error=_err(error))
                raise _Refused(f"Dream refused the prompt ({n}x): {_err(error)}")
        elif kind == "transient" and self._restarts < MAX_STATION_RESTARTS:
            self._restarts += 1   # one gateway timeout must not end a 5-minute match
        else:
            raise _Fail(f"station error: {_err(error)}")
        self.log.event("station_restart", error=_err(error), kind=kind, restart=self._restarts, prompt=self._prompt)

    def _start_run(self, prompt: str) -> dict[str, Any]:
        self._prompt = prompt
        while True:
            self._check_global()
            self.log.event("start", prompt=prompt, checkpoint=self.plan.checkpoint)
            self._motion(self.station.start, prompt, self.plan.checkpoint)
            left = self.plan.time_budget_s - (self._clock() - self._t0)
            st = self._wait({"running", "error"}, max(5.0, min(START_TIMEOUT_S, left)), "running")
            if st.get("state") != "error":
                return st
            self._on_station_error(st.get("last_error"))   # a refusal or fault on the way up: retry or raise
            self._maybe_reset(st.get("last_error"))
            self._nap(ARCHIVE_WAIT_S)

    def _reset_and_wait(self, why: str) -> None:
        reset = getattr(self.station, "reset", None)
        if reset is None:   # a stand-in station without /reset (experiments.stress SimStation)
            self.log.event("reset_unavailable", why=why)
            return
        lg = self.live_guard
        if lg is not None and lg.armed:
            # a /reset moves both arms and may not be stoppable once begun: never while the guard is armed
            self.log.event("reset_skipped", why=why, reason="live guard armed")
            return
        self.log.event("reset", why=why)
        self._motion(reset)
        self._nap(1.0)   # let the station leave idle before waiting for it to come back
        self._wait({"idle"}, START_TIMEOUT_S, "idle after reset")

    def _maybe_reset(self, error: Any) -> None:
        """Before restarting after Dream's transient IK error: /reset and wait for idle (at most AUTO_RESET_MAX)."""
        text = _error_text(error)
        if self._auto_resets < AUTO_RESET_MAX and any(k in text for k in RESET_ERRORS):
            self._auto_resets += 1
            self.log.event("auto_reset", error=_err(error), n=self._auto_resets, prompt=self._prompt)
            self._reset_and_wait(f"auto reset {self._auto_resets} after: {_err(error)}")

    def _status_line(self) -> str:
        return f"stage {self._stage_name or '(none yet)'}, robot prompt: {self._prompt or '(none)'}, " \
               f"{self._clock() - self._t0:.0f} s into the run of {self.plan.time_budget_s:g} s"

    def _apply_operator(self) -> tuple[bool, list[str]]:
        """Takes the operator's queued actions: notes go to the monitor, skips are kept. Returns (end the current
        stage, prompts to send now)."""
        if self.operator is None:
            return False, []
        acts = self.operator.take()
        if acts.skip_stages:
            self._skip |= acts.skip_stages
            self.log.event("operator_action", action="skip_stages", stages=sorted(acts.skip_stages))
        monitors = {id(m): m for v in self.verifiers.values() for m in (v, getattr(v, "second", None))
                    if hasattr(m, "operator_notes")}
        for note in acts.notes:
            self.log.event("operator_action", action="note", note=note)
            for m in monitors.values():
                m.operator_notes.append(note)
        if acts.end_current_stage:
            self.log.event("operator_action", action="end_current_stage", stage=self._stage_name)
        return acts.end_current_stage, acts.prompts

    def _issue(self, prompt: str, why: str, *, force_start: bool = False) -> None:
        """Gets `prompt` to the robot: set_prompt on a running station, else a fresh start. Logs a prompt_change
        event with the chunk count and tells verifiers (a monitor drops answers computed on the old prompt)."""
        st = self.station.status()
        if st.get("state") == "error" and self._prompt is not None and _fault_kind(st.get("last_error")) != "refusal":
            # The run died since the last poll (e.g. during an entry check). An arm or motor fault must end the
            # match here, not be cleared by the fresh /start below. (Before the first prompt, preflight decided;
            # a refusal was of the old prompt, not this one.)
            self._on_station_error(st.get("last_error"))
        if force_start or st.get("state") != "running":
            reply, how = self._start_run(prompt), "start"
        else:
            reply, how = self._motion(self.station.set_prompt, prompt), "set_prompt"
            self.log.event("set_prompt", prompt=prompt)
        self._prompt = prompt
        chunks = reply.get("chunks") if isinstance(reply, dict) else None
        self.log.event("prompt_change", prompt=prompt, why=why, how=how, chunks=chunks)
        self._change = {"t": self._clock(), "baseline": self._last_progress, "logged": False}
        seen: set[int] = set()
        for v in self.verifiers.values():
            fn = getattr(v, "notify_prompt_change", None)
            if fn is not None and id(v) not in seen:
                seen.add(id(v))
                fn(prompt)

    def _try_issue(self, prompt: str, why: str, *, force_start: bool = False) -> str | None:
        """_issue, but a prompt Dream keeps refusing returns the reason instead of ending the match."""
        try:
            self._issue(prompt, why, force_start=force_start)
            return None
        except _Refused as exc:
            return str(exc)

    def _frames_for_check(self, stage: Stage | None = None) -> dict[str, bytes]:
        frames = self.frames.latest()
        wanted = (stage.cameras if stage is not None and stage.cameras else None) or self.plan.cameras_for_check
        if wanted:
            frames = {r: j for r, j in frames.items() if any(w in r for w in wanted)}
        return frames

    def _grip_note(self) -> str:
        """Grip by feel from /arms: commanded closed and stopped partway -> holding; closed fully -> empty."""
        getter = getattr(self.station, "grippers", None)
        arms = getter() if getter else {}
        # Readings on this station (8 Nov run): right shut on nothing 0.03-0.06, right on a USB plug 0.07-0.09 (a
        # plug is thin), left idle and shut 0.09-0.14. So only <= 0.06 means empty for sure; up to 0.15 it is a thin
        # object or nothing, and the wrist image decides. commanded_gripper is null until a run commands it.
        notes = []
        for side, g in sorted((arms or {}).items()):
            cmd, got = g.get("commanded_gripper"), g.get("gripper")
            if got is None:
                continue
            if cmd is not None and cmd > 0.15:
                notes.append(f"{side} gripper commanded open ({got:.2f})")
            elif got <= 0.06:
                notes.append(f"{side} gripper shut on nothing ({got:.2f}): it is NOT holding anything")
            elif got <= 0.15:
                self._grip_seen = self._grip_seen or side == "right"   # a thin plug; the idle left reads 0.10 shut
                notes.append(f"{side} gripper nearly shut ({got:.2f}): a thin plug or nothing, judge from its wrist image")
            elif cmd is None:
                notes.append(f"{side} gripper opening {got:.2f}")
            else:
                self._grip_seen = True
                notes.append(f"{side} gripper commanded closed but stopped at {got:.2f}: it is holding something")
        return "; ".join(notes)

    def _ask(self, verifier: Verifier, stage: Stage, frames: dict[str, bytes], elapsed: float, *,
             sync: bool = False) -> Verdict:
        if hasattr(verifier, "grip_note"):
            verifier.grip_note = self._grip_note()
        fn = getattr(verifier, "check_now", verifier.check) if sync else verifier.check
        try:
            verdict = fn(stage, frames, elapsed)
        except Exception as exc:
            return Verdict(None, 0.0, f"verifier raised {type(exc).__name__}: {exc}")
        return verdict

    def _note_progress(self, stage: Stage, verdict: Verdict, st: dict[str, Any]) -> None:
        """Tracks monitor progress; logs the first check after a prompt change where it moved."""
        p = getattr(verdict, "progress", None)
        if p is None:
            return
        ch = self._change
        if ch is not None and not ch["logged"]:
            if ch["baseline"] is None:
                ch["baseline"] = p
            elif abs(p - ch["baseline"]) >= MOVED_DELTA:
                ch["logged"] = True
                self.log.event("progress_moved", stage=stage.name, since_change_s=round(self._clock() - ch["t"], 2),
                               before=ch["baseline"], after=p, chunks=st.get("chunks"))
        self._last_progress = p

    def _log_verdict(self, stage: Stage, attempt: int, elapsed: float, verdict: Verdict, saved: list[str],
                     st: dict[str, Any]) -> None:
        extra = {k: getattr(verdict, k) for k in ("progress", "status", "recovery", "scene_ok", "holding", "guard",
                                                   "tower", "next_prompt", "latency_s")
                 if getattr(verdict, k, None) is not None}
        tower = getattr(verdict, "tower", None)
        if tower is not None:
            self._tower = tower
        if tower is not None and self.plan.tower_target:
            extra["tower_order_ok"] = tower_matches(tower, self.plan.tower_target)
        self.log.event("verdict", stage=stage.name, attempt=attempt, elapsed_s=round(elapsed, 2),
                       done=verdict.done, confidence=verdict.confidence, reason=verdict.reason,
                       frames=saved, chunks=st.get("chunks"), **extra)

    def _watch(self, stage: Stage, verifier: Verifier, attempt: int) -> _Attempt:
        t0 = self._clock()
        confirm = stage.confirm or self.plan.confirm
        self._last_verdict = None
        last_done_sig: str | None = None
        grace = False                      # one wait for the verdict that would confirm a pending done
        gate = max(stage.min_run_s, stage.effective_settle())   # no verifier and no stall clock before this
        consec_done = consec_fail = consec_stalled = 0
        guard = self.plan.guard if self.plan.guard is not None and self.plan.guard.covers(stage.name) else None
        consec_guard = 0
        if guard is not None and self._guard_answer_t is None:
            self._guard_answer_t = self._clock()
        last_done_t: float | None = None   # a done verdict sticks: a second one within DONE_WINDOW_S passes
        extended = False                   # one timeout extension while the gripper is on the target
        max_p: float | None = None
        rise_ref = last_rise = None
        hint: int | None = None
        consec_unwatched, watch_fixes = 0, 0
        rewrites, last_prompt_t = 0, 0.0
        ga = stage.grip_assist
        gs: dict[str, Any] = {"seen": [], "regrips": ga.regrip if ga else 0, "done": False, "idle_ok": False,
                              "pulled": False}
        while True:
            spent = self._grip_assist_wait(stage, ga, gs, attempt) if ga is not None and not gs["done"] else None
            if spent is None:
                self._nap(self.plan.check_every_s)
            else:
                t0 += spent   # the stage clock counts Dream's time, not the harness's own moves
            st = self._poll_health()
            elapsed = self._clock() - t0
            if spent:         # the arm moved under the harness: verdicts start over after the settle time
                consec_done = consec_fail = consec_stalled = 0
                last_prompt_t, gate, rise_ref, last_rise = elapsed, elapsed + stage.effective_settle(), None, None
                if gs["pulled"] and ga.pass_on_pull:
                    gs["pulled"] = False
                    dz = getattr(self, "_last_pull_dz", None)
                    if ga.pull_count_dz is not None and dz is not None and dz >= ga.pull_count_dz:
                        check = None   # lifted 8+ cm with something still in the jaws: the plug is out
                        self.log.event("pull_counted", stage=stage.name, attempt=attempt, dz=round(dz, 3))
                    else:
                        check = self._confirm_check(stage, verifier)   # None: no check configured, or it said yes
                    self.log.event("pull_pass_check", stage=stage.name, attempt=attempt, passed=check is None,
                                   reason=check)
                    if check is None and ga.loop_until_s is not None:
                        gs["out"] = gs.get("out", 0) + 1
                        left = ga.loop_until_s - (self._clock() - self._t0)
                        self.log.event("cable_out", stage=stage.name, count=gs["out"], loop_s_left=round(left, 1))
                        if left > 0:   # straight back for the next plug: same stage, no stage change
                            gs.update(done=False, idle_ok=False)
                            gs["regrips"] = ga.regrip
                            self._dream_again(stage, gs, f"cable {gs['out']} out: next plug")
                            continue
                        return _Attempt(True, f"{gs['out']} cable(s) out by the loop's time limit", max_p, hint)
                    if check is None:
                        return _Attempt(True, "grip assist pulled it out, confirm agrees", max_p, hint)
                    # confirm says not out: don't leave Dream idle until the timeout, send it back to the plugs
                    gs.update(done=False, idle_ok=False)
                    self._dream_again(stage, gs, "confirm: no plug came out, Dream approaches again")
                    continue
            end_now, prompts = self._apply_operator()
            if end_now:
                if prompts and self.operator is not None:   # "skip this, do the balls": the prompt is for the next stage
                    with self.operator._lock:
                        self.operator._actions.prompts[:0] = prompts
                return _Attempt(False, "operator ended the stage", max_p, hint, operator=True)
            if prompts:
                gs["done"] = True   # the operator is steering: no grip assist on this attempt (8 Nov live: it re-gripped cubes)
            for prompt in prompts:
                self._issue(prompt, "operator")
                rewrites = self.plan.rewrite_max   # the operator's prompt stands: no monitor rewrite undoes it (run 3)
                last_prompt_t, consec_done, consec_fail, consec_stalled = elapsed, 0, 0, 0
                gate, rise_ref, last_rise = elapsed + stage.effective_settle(), None, None
            if st.get("state") == "idle" and not gs["idle_ok"]:
                return _Attempt(False, "station went idle during the stage", max_p, hint)
            if guard is not None and self.live_guard is not None and self.live_guard.last_answer_t is not None:
                self._guard_answer_t = max(self._guard_answer_t or 0.0, self.live_guard.last_answer_t)
            if guard is not None and self.fast_guard is not None:
                # Local detector on every poll, from the stage's first second: no API latency, works offline.
                local, why = self.fast_guard(self.frames.latest())
                if isinstance(local, bool):
                    self._guard_answer_t = self._clock()
                if local is True:
                    consec_guard += 1
                    self.log.event("guard_yes", stage=stage.name, attempt=attempt, count=consec_guard,
                                   question=guard.question, reason=why, source="local")
                    if consec_guard >= guard.confirm:
                        self.log.event("guard_tripped", stage=stage.name, attempt=attempt, question=guard.question,
                                       message=guard.message, source="local")
                        raise _Guard(f"guard tripped in stage {stage.name!r} (local detector): {why}")
            if elapsed >= gate:
                frames = self._frames_for_check(stage)
                verdict = self._ask(verifier, stage, frames, elapsed)
                self._last_verdict = verdict
                saved = [p.name for p in self.log.save_frames(f"{stage.name}_a{attempt}", frames)]
                self._log_verdict(stage, attempt, elapsed, verdict, saved, st)
                if guard is not None:
                    answer = getattr(verdict, "guard", None)
                    take = getattr(verifier, "take_guard_alert", None)
                    if answer is not True and take is not None and take():
                        answer = True   # a yes from a call that was in flight across a stage or prompt change
                    n = getattr(verifier, "guard_answers", None)
                    if isinstance(answer, bool) or (isinstance(n, int) and n != getattr(self, "_guard_n", n)):
                        self._guard_answer_t = self._clock()   # an answer dropped by a prompt change still saw
                    self._guard_n = n                           # the scene: the guard is not blind
                    if answer is True:
                        consec_guard += 1
                        self.log.event("guard_yes", stage=stage.name, attempt=attempt, count=consec_guard,
                                       question=guard.question, reason=verdict.reason)
                        if consec_guard >= guard.confirm:
                            self.log.event("guard_tripped", stage=stage.name, attempt=attempt,
                                           question=guard.question, message=guard.message)
                            raise _Guard(f"guard tripped in stage {stage.name!r}: {guard.question}")
                    elif answer is False:
                        consec_guard = 0
                if guard is not None and guard.max_blind_s is not None and \
                        self._clock() - self._guard_answer_t > guard.max_blind_s:
                    self.log.event("guard_blind", stage=stage.name, attempt=attempt, question=guard.question,
                                   seconds=round(self._clock() - self._guard_answer_t, 1), message=guard.message)
                    raise _Guard(f"guard blind in stage {stage.name!r}: no answer for {guard.max_blind_s:g} s")
                monitor = isinstance(verdict, MonitorVerdict) and verdict.status is not None
                failing, why = verdict.done is False and not self.plan.hands_off, f"verifier said no: {verdict.reason}"
                nxt = getattr(verdict, "next_prompt", None) if self.plan.rewrite_prompts and stage.write_prompt \
                    and not gs["done"] and not self.plan.hands_off else None   # hands_off: no mid-attempt rewrites
                if (nxt and monitor and verdict.status in ("stalled", "failing") and verdict.done is not True
                        and last_done_t is None and rewrites < self.plan.rewrite_max
                        and elapsed - last_prompt_t >= self.plan.rewrite_cooldown_s and nxt != self._prompt):
                    # The monitor's diagnosis as the robot's next instruction. Its verdict was on the old prompt,
                    # so the counters and the stall clock start over, after the stage's settle time.
                    prefix = self.plan.prompt_prefix
                    if prefix and stage.prompt.startswith(prefix) and not nxt.startswith(prefix):
                        nxt = f"{prefix} {nxt}"
                    rewrites, last_prompt_t = rewrites + 1, elapsed
                    self.log.event("prompt_rewrite", stage=stage.name, attempt=attempt, n=rewrites, prompt=nxt,
                                   previous=self._prompt, reason=verdict.reason)
                    refused = self._try_issue(nxt, f"monitor next_prompt ({verdict.reason[:120]})")
                    if refused:
                        self._issue(stage.prompt, "back to the stage prompt after a refused rewrite")
                    consec_done = consec_fail = consec_stalled = 0
                    gate, rise_ref, last_rise = elapsed + stage.effective_settle(), None, None
                    continue
                if monitor:
                    self._note_progress(stage, verdict, st)
                    if verdict.recovery is not None:
                        hint = verdict.recovery
                    p = verdict.progress
                    if p is not None:
                        if (max_p is not None and p <= max_p - REGRESSION_DROP and last_done_t is None
                                and not self.plan.hands_off):
                            self.log.event("regression", stage=stage.name, attempt=attempt, progress=p, max_progress=max_p)
                            failing, why = True, f"regressed: progress {p:.2f} vs max {max_p:.2f}"
                        if max_p is None or p > max_p:
                            max_p = p
                        if rise_ref is None or p >= rise_ref + STALL_RISE:
                            rise_ref, last_rise = p, elapsed
                    if verdict.status == "failing" and not failing and not self.plan.hands_off:
                        failing, why = True, f"monitor says failing: {verdict.reason}"
                    consec_stalled = consec_stalled + 1 if verdict.status == "stalled" else 0
                    if stage.watch is not None and verdict.done is not True:
                        consec_unwatched = consec_unwatched + 1 if verdict.watch_ok is False else 0
                        if consec_unwatched >= confirm and watch_fixes < stage.watch.max_fixes:
                            watch_fixes += 1
                            consec_unwatched = consec_done = consec_fail = consec_stalled = 0
                            self.log.event("watch_lost", stage=stage.name, attempt=attempt, fix=watch_fixes,
                                           camera=stage.watch.camera, reason=verdict.reason)
                            self._issue(stage.watch.prompt, f"watcher back in place ({stage.name})")
                            self._hold(stage.watch.settle_s)
                            self._issue(stage.prompt, f"stage {stage.name} after watcher fix")
                            gate = (self._clock() - t0) + stage.effective_settle()
                            rise_ref = last_rise = None
                            continue
                sig = getattr(verdict, "frames_sig", None)
                if verdict.done is True and sig is not None and sig == last_done_sig and not verdict.immediate:
                    # the same camera images judged twice are one look, not two: a frozen view must not confirm
                    self.log.event("same_frames", stage=stage.name, attempt=attempt,
                                   note="done verdict on the frames of the previous done: not counted")
                elif verdict.done is True and stage.needs_grip and not self._grip_seen:
                    consec_done = 0
                    self.log.event("done_without_grip", stage=stage.name, attempt=attempt, reason=verdict.reason,
                                   note="done verdict ignored: no gripper has held anything in this stage")
                elif verdict.done is True:
                    consec_fail = 0
                    if verdict.immediate or frames:
                        consec_done += 1
                        recent = last_done_t is not None and elapsed - last_done_t <= DONE_WINDOW_S
                        last_done_t, last_done_sig = elapsed, sig
                        if verdict.immediate or consec_done >= confirm or recent:
                            locked = self._lock_success(stage)
                            check = self._confirm_check(stage, verifier)
                            if check is not None:
                                consec_done, last_done_t, last_done_sig = 0, None, None
                                if locked and not stage.resume_on_no:
                                    self.log.event("lock_held", stage=stage.name, reason=check[:200],
                                                   note="resume_on_no false: Dream is not sent back onto the tower")
                                elif locked:   # not done after all: Dream carries on where it stopped
                                    back = getattr(self, "_lock_resume", None) if (stage.lock_prompt or self.plan.lock_prompt) else None
                                    self._issue(back or self._prompt or stage.prompt,
                                                f"after a locked pass: {check[:120]}", force_start=not back)
                                    last_prompt_t, gate = elapsed, elapsed + stage.effective_settle()
                                continue
                            return _Attempt(True, verdict.reason or "verified", max_p, hint)
                    else:
                        consec_done = 0
                        self.log.event("no_frames", stage=stage.name, attempt=attempt,
                                       note="done verdict ignored: no frames to back it")
                elif failing and last_done_t is not None and elapsed - last_done_t <= DONE_WINDOW_S:
                    pass                           # one flicker after a done does not undo it
                elif failing:
                    consec_done = 0
                    consec_fail += 1
                    if verdict.immediate and verdict.done is False:
                        return _Attempt(False, why, max_p, hint)
                    if consec_fail >= confirm and (not monitor or elapsed >= FAIL_MIN_FRACTION * stage.timeout_s):
                        return _Attempt(False, why, max_p, hint)
                elif verdict.done is not None or monitor:
                    consec_done = consec_fail = 0
                # A "stalled" look counts only once the robot has had stall_s since the last prompt change (8 Nov live:
                # with confirm 1 and a look a second, one early "stalled" cut stacking attempts off after 10-13 s).
                if (stage.stall_s is not None and consec_stalled >= confirm and last_done_t is None
                        and elapsed - last_prompt_t >= stage.stall_s and not self.plan.hands_off):
                    return _Attempt(False, "stalled: monitor says stalled", max_p, hint)
            # No stall once done was seen or progress is at the top: a finished stage cannot "rise" any further.
            if (stage.stall_s is not None and last_rise is not None and elapsed >= gate and last_done_t is None
                    and (max_p or 0) < 0.95 and elapsed - last_rise >= stage.stall_s and not self.plan.hands_off):
                return _Attempt(False, f"stalled: progress {max_p:.2f} has not risen for {stage.stall_s:g} s",
                                max_p, hint)
            if ga is not None and ga.loop_until_s is not None and gs.get("out") and \
                    (elapsed >= stage.timeout_s or self._clock() - self._t0 >= ga.loop_until_s):
                return _Attempt(True, f"{gs['out']} cable(s) out, loop time up", max_p, hint)
            if elapsed >= stage.timeout_s:
                if not grace and last_done_t is not None and elapsed - last_done_t <= DONE_WINDOW_S:
                    grace = True           # a done is waiting for its confirming look: don't drop it at the bell
                    self.log.event("timeout_grace", stage=stage.name, attempt=attempt, extra_s=CONFIRM_GRACE_S)
                    t0 += CONFIRM_GRACE_S
                    continue
                if not extended and self._on_target(stage):
                    extended = True
                    self.log.event("timeout_extended", stage=stage.name, attempt=attempt, extra_s=EXTEND_S)
                    t0 += EXTEND_S                 # the gripper is on the target and pulling: let it finish
                    continue
                return _Attempt(False, f"timeout after {stage.timeout_s:g} s", max_p, hint)

    # -- grip assist (Stage.grip_assist; ported from tools/try_prompt.py) -----------------------------------------
    def _grip_reading(self, arm: str) -> float | None:
        return self._grip_state(arm)[0]

    def _grip_state(self, arm: str) -> tuple[float | None, float | None]:
        """(measured opening, Dream's commanded opening) from GET /arms; None where the station gives none."""
        getter = getattr(self.station, "grippers", None)
        g = ((getter() if getter else {}) or {}).get(arm) or {}
        num = lambda v: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None  # noqa: E731
        return num(g.get("gripper")), num(g.get("commanded_gripper"))

    def _grip_assist_wait(self, stage: Stage, ga: GripAssist, gs: dict[str, Any], attempt: int) -> float:
        """Stands in for the check interval's nap: reads the gripper every poll_s. Returns the seconds the harness
        spent driving the arm (0.0 if it did not step in)."""
        end = self._clock() + self.plan.check_every_s
        lo, hi = ga.hold
        while (left := end - self._clock()) > 1e-9:
            self._nap(min(ga.poll_s, left))
            g, cmd = self._grip_state(ga.arm)
            if g is None:
                continue
            seen = gs["seen"]
            seen.append(g)
            if len(seen) < 3 or not any(x > ga.open_above for x in seen[:-2]):
                continue   # it must have been open on the way in: a gripper that starts shut proves nothing
            last_open = max(i for i, x in enumerate(seen) if x > ga.open_above)
            since = seen[last_open + 1:]
            if all(x <= lo for x in seen[-2:]) and gs["regrips"] > 0:
                action = "regrip"
            elif (all(lo < x < hi for x in seen[-2:]) and not any(x <= lo for x in since[:-2])
                  and (cmd is None or cmd <= 0.2)):
                # ... and Dream is commanding the jaws shut: a half-open approach also reads 0.07-0.17 (8 Nov live:
                # 9 pulls fired on it and squeezed air, 3 held)
                # a real hold closes from open straight onto the plug. 8 Nov live: 0.85 -> 0.035 -> 0.17 -> 0.12 was
                # Dream shutting on air on the way down and half reopening, not a plug.
                action = "pull"
            else:
                continue
            if self.station.status().get("state") != "running":
                continue
            t = self._clock()
            self._grip_assist(stage, ga, gs, attempt, action)
            return self._clock() - t
        return 0.0

    def _move(self, arm: str, dz: float, gripper: float, duration_s: float) -> None:
        """Straight up or down by dz in /move-sized pieces (<= 12 cm each), duration shared between them."""
        n = max(1, int(abs(dz) / MAX_MOVE_M - 1e-9) + 1)
        for _ in range(n):
            self._check_global()
            self._motion(self.station.move, arm, [0.0, 0.0, dz / n], gripper, duration_s / n)

    def _grip_assist(self, stage: Stage, ga: GripAssist, gs: dict[str, Any], attempt: int, action: str) -> None:
        """Dream shut on nothing (regrip) or holds something (pull): stop Dream, fix the grip, pull straight up,
        then hand back to Dream with the put-down prompt. /move only while no Dream run is going."""
        lo, hi = ga.hold
        gs.setdefault("approach", self._prompt)   # the approach prompt, not the put-down one, for every re-approach
        self.log.event("grip_assist", stage=stage.name, attempt=attempt, action=action,
                       readings=[round(x, 3) for x in gs["seen"][-4:]])
        try:
            self.station.stop()
            self._wait({"idle"}, IDLE_TIMEOUT_S, "idle before the harness moves the arm")
            if action == "regrip":
                gs["regrips"] -= 1
                self._move(ga.arm, 0.0, 0.6, 0.8)              # open in place
                self._move(ga.arm, ga.regrip_dz, 0.6, 1.0)     # lower (regrip_dz is negative: down)
                self._move(ga.arm, 0.0, 0.0, 1.0)              # close
                self._nap(0.5)
                g = self._grip_reading(ga.arm) or 0.0
                held = lo < g < hi
                self.log.event("grip_regrip", stage=stage.name, attempt=attempt, grip=round(g, 3), held=held,
                               dz=ga.regrip_dz, regrips_left=gs["regrips"])
                if not held:
                    self._dream_again(stage, gs, "regrip closed on nothing: Dream approaches again")
                    return
            self._grip_seen = True
            z = ((self.station.pose() or {}).get(ga.arm) or {}).get("xyz", [None] * 3)[2]
            if ga.min_z is not None and z is not None and z < ga.min_z:
                # holding something low: a cable lying on the table, not a plug in the strip. Let go, approach again.
                self.log.event("grip_too_low", stage=stage.name, attempt=attempt, z=z, min_z=ga.min_z)
                self._move(ga.arm, 0.0, 0.6, 0.6)
                self._move(ga.arm, 0.05, 0.6, 1.0)
                self._dream_again(stage, gs, f"gripped at z {z:.3f} < {ga.min_z}: not a plug in the strip")
                return
            outcome = self._pull(stage, ga, attempt)
        except StationError as exc:   # e.g. /move refused (409): no more help this attempt, Dream carries on
            self.log.event("grip_assist_error", stage=stage.name, attempt=attempt, error=exc.message)
            gs["done"] = True
            self._dream_again(stage, gs, f"grip assist error: {exc.message}")
            return
        if outcome != "pulled":
            self._dream_again(stage, gs, f"pull {outcome}: Dream approaches again")
            return
        gs["done"] = gs["idle_ok"] = gs["pulled"] = True
        if ga.drop_dxyz is not None:
            self._drop(stage, ga, attempt)
        elif ga.put_down_prompt:
            self._put_down(stage, ga, attempt)

    def _pull(self, stage: Stage, ga: GripAssist, attempt: int) -> str:
        """Squeeze, re-read, lift by pull_up_m steps; a slip goes back down and re-grips (at most repull).
        'pulled', 'slipped' (lost on the way up every time) or 'lost' (empty after the squeeze)."""
        lo = ga.hold[0]
        if ga.seat_dz:   # open a little, lower, so the jaws bite the plug body instead of the cord
            self._move(ga.arm, 0.0, 0.45, 0.6)
            self._move(ga.arm, -ga.seat_dz, 0.45, 1.0)
        # squeeze in place and read again: Dream's stop can loosen the jaws (8 Nov: a pull that started at 0.034 was empty)
        self._move(ga.arm, 0.0, 0.0, 0.6)
        self._nap(0.5)
        before = (self.station.pose() or {}).get(ga.arm) or {}
        g = self._grip_reading(ga.arm) or 0.0
        if g < ga.empty_below:
            self.log.event("grip_pull", stage=stage.name, attempt=attempt, outcome="lost", grip=round(g, 3))
            return "lost"
        tries = 0
        while True:
            for dz in ga.pull_up_m:
                self._move(ga.arm, dz, 0.0, ga.pull_s)
            after = self._grip_reading(ga.arm) or 0.0
            if after >= ga.empty_below or tries >= ga.repull:
                break
            tries += 1   # slipped: back down to the grip point, half open, a little lower, close, and pull again
            self.log.event("grip_slipped", stage=stage.name, attempt=attempt, grip=round(after, 3), repull=tries)
            self._move(ga.arm, -sum(ga.pull_up_m), 0.45, 1.5)
            self._move(ga.arm, -(ga.seat_dz or 0.01), 0.45, 0.8)
            self._move(ga.arm, 0.0, 0.0, 0.6)
            self._nap(0.4)
        end = (self.station.pose() or {}).get(ga.arm) or {}
        outcome = "pulled" if after >= ga.empty_below else "slipped"
        if outcome == "slipped":
            # back down to where Dream left the hand: a restart from 10+ cm up made Dream's IK fail ("SVD did not
            # converge" -> auto /reset of both arms, 8 Nov live)
            self._move(ga.arm, -sum(ga.pull_up_m), 0.6, 1.5)
        zb, za = (before.get("xyz") or [None] * 3)[2], (end.get("xyz") or [None] * 3)[2]
        gs_dz = (za - zb) if isinstance(za, (int, float)) and isinstance(zb, (int, float)) else None
        self._last_pull_dz = gs_dz
        self.log.event("grip_pull", stage=stage.name, attempt=attempt, outcome=outcome, grip_before=round(g, 3),
                       grip_after=round(after, 3), repulls=tries, steps_m=ga.pull_up_m,
                       z_before=(before.get("xyz") or [None] * 3)[2], z_after=(end.get("xyz") or [None] * 3)[2])
        return outcome

    def _drop(self, stage: Stage, ga: GripAssist, attempt: int) -> None:
        """Carry the held cable by drop_dxyz (in /move-sized steps, grip shut) and open the gripper there."""
        d = ga.drop_dxyz or [0.0, 0.0, 0.0]
        n = max(1, int((sum(x * x for x in d) ** 0.5) / MAX_MOVE_M - 1e-9) + 1)
        for _ in range(n):
            self._check_global()
            self._motion(self.station.move, ga.arm, [x / n for x in d], 0.0, 1.5)
        self._motion(self.station.move, ga.arm, [0.0, 0.0, 0.0], 0.8, 0.6)   # open: the cable drops here
        self.log.event("grip_drop", stage=stage.name, attempt=attempt, dxyz=d, grip=self._grip_reading(ga.arm))

    def _dream_again(self, stage: Stage, gs: dict[str, Any], why: str) -> None:
        gs["seen"] = []   # wait for Dream to open the gripper again before judging a grip
        self._issue(gs.get("approach") or self._prompt or stage.prompt, why, force_start=True)

    def _put_down(self, stage: Stage, ga: GripAssist, attempt: int) -> None:
        """Dream puts the held cable down; stopped once the gripper opens (or after put_down_s). The arms then hold
        still while the monitor judges the stage."""
        prompt, prefix = ga.put_down_prompt or "", self.plan.prompt_prefix
        if prefix and stage.prompt.startswith(prefix) and not prompt.startswith(prefix):
            prompt = f"{prefix} {prompt}"   # Dream refuses cable work without the safety sentence
        self._issue(prompt, "grip assist: put the held object down", force_start=True)
        t0 = self._clock()
        while self._clock() - t0 < ga.put_down_s:
            self._nap(ga.poll_s)
            st = self._poll_health()
            g = self._grip_reading(ga.arm)
            if st.get("state") != "running" or (g is not None and g > ga.open_above and self._clock() - t0 >= 3.0):
                break
        self.log.event("grip_put_down", stage=stage.name, attempt=attempt, seconds=round(self._clock() - t0, 1),
                       grip=self._grip_reading(ga.arm))
        self._safe_stop("grip assist: put-down finished")
        self._wait({"idle"}, IDLE_TIMEOUT_S, "idle after the put-down")

    def _mark_done(self, what: str) -> None:
        """Tell every monitor a stage has passed: without it the tower stage's monitor re-judged the cables and
        rewrote the robot's prompt back to unplugging (sim, 8 Nov)."""
        for v in self.verifiers.values():
            for m in (v, getattr(v, "second", None)):
                if hasattr(m, "done_stages") and what not in m.done_stages:
                    m.done_stages.append(what)

    def _lock_success(self, stage: Stage) -> bool:
        """plan.stop_on_pass: stop Dream the moment a look would pass the stage. True if a run was stopped."""
        if not self.plan.stop_on_pass or self.station.status().get("state") not in ("running", "starting"):
            return False
        lock = stage.lock_prompt or self.plan.lock_prompt
        self.log.event("stage_locked", stage=stage.name, prompt=self._prompt, how=lock or "stop")
        if lock:
            self._lock_resume = self._prompt
            self._issue(lock, f"stage {stage.name} looks done: lock it, run kept warm")
            return True
        self._safe_stop(f"stage {stage.name} looks done: lock it")
        self._wait({"idle"}, IDLE_TIMEOUT_S, "idle after locking a pass")
        return True

    def _confirm_check(self, stage: Stage, verifier: Verifier) -> str | None:
        """Stage.confirm_check before a pass: None to pass, else why not. A confirm that cannot answer passes."""
        fn = getattr(verifier, "confirm", None) or getattr(getattr(verifier, "second", None), "confirm", None)
        if not stage.confirm_check or fn is None:
            return None
        if hasattr(fn.__self__, "grip_note"):
            fn.__self__.grip_note = self._grip_note()
        v = fn(stage, self._frames_for_check(stage))
        self.log.event("confirm_check", stage=stage.name, yes=v.done, reason=v.reason,
                       latency_s=getattr(v, "latency_s", None))
        if v.done is not False:
            return None
        note = f"A careful second look said this stage is NOT done yet: {v.reason}"
        for m in {id(x): x for x in (verifier, getattr(verifier, "second", None)) if hasattr(x, "operator_notes")}.values():
            m.operator_notes.append(note[:400])   # the fast monitor sees it on its next calls
        return f"confirm said no: {v.reason}"

    def _on_target(self, stage: Stage) -> bool:
        """The last monitor verdict says a gripper holds the stage's object and progress is at least half."""
        v = getattr(self, "_last_verdict", None)
        if not isinstance(v, MonitorVerdict) or (v.progress or 0) < 0.5 or v.status == "failing":
            return False
        held = " ".join(str(x) for x in (v.holding or {}).values()).lower()
        return any(w in held for w in ("plug", "cable", "block", "cube", "connector"))

    def _hold(self, seconds: float) -> None:
        t0 = self._clock()
        while self._clock() - t0 < seconds:
            self._nap(min(self.plan.check_every_s, max(seconds - (self._clock() - t0), 0.01)))
            self._poll_health()

    def _reset_scene(self, stage: Stage, rec: Recovery, verifier: Verifier) -> tuple[bool, str]:
        """kind reset: run the recovery prompt, then wait (up to the cap) for the verifier's scene_ok."""
        cap = min(stage.timeout_s / 2, 12.0)
        settle = min(rec.settle_s, cap)
        t0 = self._clock()
        scene: bool | None = None
        while True:
            self._nap(min(self.plan.check_every_s, max(cap - (self._clock() - t0), 0.01)))
            st = self._poll_health()
            elapsed = self._clock() - t0
            if elapsed >= settle:
                frames = self._frames_for_check(stage)
                verdict = self._ask(verifier, stage, frames, elapsed)
                self._note_progress(stage, verdict, st)
                if verdict.done is True and verdict.confidence >= SKIP_MIN_CONFIDENCE:
                    self._done_in_recovery = verdict.reason   # 8 Nov: the plug was out here and nobody looked
                ok = getattr(verdict, "scene_ok", None)
                if ok is not None:
                    scene = ok
                    self.log.event("scene_check", stage=stage.name, recovery=rec.name, scene_ok=ok,
                                   elapsed_s=round(elapsed, 2), reason=verdict.reason)
                    if ok:
                        return True, "scene ok"
            if elapsed >= cap:
                if scene is False:
                    return False, f"scene still not ok after recovery {rec.name!r}"
                return True, "scene ok" if scene else "no scene verdict, assumed ok"

    def _pick_recovery(self, stage: Stage, uses: dict[int, int], last: int | None, hint: int | None) -> int | None:
        menu = stage.menu()
        usable = [i for i, r in enumerate(menu)
                  if uses.get(i, 0) < r.max_uses and i != last
                  and (not r.legacy or stage.on_fail == "retry_in_place")]
        if hint in usable:
            return hint
        return usable[0] if usable else None

    def _attempt(self, stage: Stage, verifier: Verifier, attempt: int, prompt: str) -> _Attempt:
        t_start = time.time()
        try:
            try:
                out = self._watch(stage, verifier, attempt)
            except _Refused as exc:
                out = _Attempt(False, str(exc))
        except BaseException as exc:
            self.log.segment(stage=stage.name, attempt=attempt, kind="stage", prompt=prompt, t_start=t_start,
                             t_end=time.time(), outcome="aborted", reason=f"{type(exc).__name__}: {exc}",
                             max_progress=None)
            raise
        self.log.event("attempt_end", stage=stage.name, attempt=attempt, ok=out.ok, reason=out.reason)
        self.log.segment(stage=stage.name, attempt=attempt, kind="stage", prompt=prompt, t_start=t_start,
                         t_end=time.time(), outcome="ok" if out.ok else "failed", reason=out.reason,
                         max_progress=out.max_progress)
        return out

    def _recover(self, stage: Stage, verifier: Verifier, attempt: int, idx: int) -> tuple[bool, str]:
        """Runs recovery item `idx`. Returns (scene_ok, reason); the stage prompt is NOT re-issued here."""
        rec = stage.menu()[idx]
        t_start = time.time()
        self.log.event("recovery", stage=stage.name, name=rec.name, kind=rec.kind, prompt=rec.prompt, attempt=attempt)
        outcome, reason, ok = "ok", "", True
        try:
            if rec.legacy:
                self._issue(rec.prompt, f"recovery {rec.name}")
                self._hold(min(stage.timeout_s / 2, 20.0))
            elif rec.kind == "reset":
                refused = self._try_issue(rec.prompt, f"recovery {rec.name}")
                if refused:
                    ok, reason, outcome = False, refused, "refused"
                else:
                    ok, reason = self._reset_scene(stage, rec, verifier)
                    outcome = "ok" if ok else "scene_not_ok"
            else:
                reason = "retry: stage prompt re-issued"
        except _Refused as exc:            # refused while holding or checking the scene: the recovery failed
            ok, reason, outcome = False, str(exc), "refused"
        except BaseException as exc:
            self.log.segment(stage=stage.name, attempt=attempt, kind="recovery", name=rec.name,
                             prompt=rec.prompt or stage.prompt, t_start=t_start, t_end=time.time(),
                             outcome="aborted", reason=f"{type(exc).__name__}: {exc}", max_progress=None)
            raise
        self.log.segment(stage=stage.name, attempt=attempt, kind="recovery", name=rec.name,
                         prompt=rec.prompt or stage.prompt, t_start=t_start, t_end=time.time(),
                         outcome=outcome, reason=reason, max_progress=None)
        return ok, reason

    def _precondition(self, stage: Stage, verifier: Verifier) -> bool:
        """skip_unless: one synchronous look before the prompt; the stage runs only on a confident yes."""
        probe = replace(stage, success=stage.skip_unless, expect=None)
        frames = self._frames_for_check(stage)
        verdict = self._ask(verifier, probe, frames, 0.0, sync=True)
        ok = verdict.done is True and (verdict.immediate or (bool(frames) and verdict.confidence >= SKIP_MIN_CONFIDENCE))
        self.log.event("precondition", stage=stage.name, question=stage.skip_unless, done=verdict.done,
                       confidence=verdict.confidence, reason=verdict.reason, run=bool(ok))
        return bool(ok)

    def _confirm_done(self, stage: Stage, verifier: Verifier) -> bool:
        """A second, synchronous look after a recovery's scene check saw the stage done (the two-look rule)."""
        frames = self._frames_for_check(stage)
        verdict = self._ask(verifier, stage, frames, 0.0, sync=True)
        ok = verdict.done is True and bool(frames) and verdict.confidence >= SKIP_MIN_CONFIDENCE
        self.log.event("recovery_done_check", stage=stage.name, done=verdict.done, confidence=verdict.confidence,
                       reason=verdict.reason, passed=ok)
        return ok

    def _entry_check(self, stage: Stage, verifier: Verifier) -> bool:
        """skip_if_done: one synchronous look before the prompt is sent."""
        frames = self._frames_for_check(stage)
        verdict = self._ask(verifier, stage, frames, 0.0, sync=True)
        done = verdict.done is True and (verdict.immediate or (frames and verdict.confidence >= SKIP_MIN_CONFIDENCE))
        self.log.event("entry_check", stage=stage.name, done=verdict.done, confidence=verdict.confidence,
                       reason=verdict.reason, skipped=bool(done))
        return bool(done)

    def _write_prompt(self, stage: Stage, verifier: Verifier) -> str | None:
        """plan.write_prompts: the monitor's one instruction for this stage from the current frames, with the plan's
        prompt_prefix when the stage's own prompt has it. None (use stage.prompt) when it can't write one."""
        fn = getattr(verifier, "write_prompt", None) or getattr(getattr(verifier, "second", None), "write_prompt", None)
        if fn is None:
            return None
        if hasattr(fn.__self__, "grip_note"):
            fn.__self__.grip_note = self._grip_note()
        text, why, latency = fn(stage, self._frames_for_check(stage))
        if text and stage.writer_notes and text.strip().strip(".").upper() == "SKIP":
            self.log.event("prompt_written", stage=stage.name, prompt=None, fallback=None, why="writer: SKIP",
                           latency_s=latency)
            raise _WriterSkip("the prompt writer saw nothing to do here")
        prefix = self.plan.prompt_prefix
        if text and prefix and stage.prompt.startswith(prefix) and not text.startswith(prefix):
            text = f"{prefix} {text}"
        self.log.event("prompt_written", stage=stage.name, prompt=text, fallback=stage.prompt if not text else None,
                       why=why, latency_s=latency)
        return text

    def _stage_prompt(self, stage: Stage, verifier: Verifier, *, retry: bool = False) -> str:
        """The stage's prompt (its retry_prompt on a retry), or with plan.tower_prompts on a stacking stage: the next
        target piece by name, after the tower the monitor last reported (one synchronous look if it hasn't reported
        one yet)."""
        plan = self.plan
        if retry and stage.retry_prompt:
            return stage.retry_prompt
        if plan.write_prompts and stage.write_prompt:
            written = self._write_prompt(stage, verifier)
            if written:
                return written
        if not plan.tower_prompts or not stage.name.startswith(TOWER_STAGES):
            return stage.prompt
        if self._tower is None:
            v = self._ask(verifier, stage, self._frames_for_check(stage), 0.0, sync=True)
            if getattr(v, "tower", None) is not None:
                self._tower = v.tower
        n = len(self._tower) if self._tower is not None else None
        if n is None or n >= len(plan.tower_target):
            self.log.event("tower_prompt", stage=stage.name, tower=self._tower, prompt=stage.prompt,
                           note="no tower reported" if n is None else "target complete: generic prompt")
            return stage.prompt
        if n == 0:
            n = 1   # an empty tower: the first target piece is the base, so the next to place is the second
        name = plan.tower_target[n].split(",")[0].strip()
        top = plan.tower_target[n - 1].split(",")[0].strip()
        prompt = next(v for k, v in plan.tower_prompts.items() if name.endswith(k)).replace("<piece>", name)
        prompt = prompt.replace("<top>", top)
        self.log.event("tower_prompt", stage=stage.name, tower=self._tower, next=name, prompt=prompt,
                       order_ok=tower_matches(self._tower, plan.tower_target))
        return prompt

    def _run_stage(self, stage: Stage, first: bool) -> StageResult:
        t0 = self._clock()
        verifier = self.verifiers.get(stage.verifier)
        if verifier is None:
            raise _Fail(f"stage {stage.name!r}: no {stage.verifier!r} verifier configured")
        self._check_global()
        self._last_progress = None
        self._stage_name = stage.name
        self._grip_seen = False
        self.log.event("stage_start", stage=stage.name, prompt=stage.prompt)
        lg = self.live_guard
        if lg is not None:
            if self.plan.guard is not None and self.plan.guard.covers(stage.name):
                lg.arm(stage.name)
            else:
                lg.disarm()
        if stage.skip_unless and not self._precondition(stage, verifier):
            self.log.event("stage_skipped", stage=stage.name, reason=f"precondition not met: {stage.skip_unless}")
            return StageResult(stage.name, False, 0, self._clock() - t0, "precondition not met")
        if stage.skip_if_done and self._entry_check(stage, verifier):
            self.log.event("stage_already_done", stage=stage.name)
            return StageResult(stage.name, True, 0, self._clock() - t0, "already done at entry")
        try:
            text = self._stage_prompt(stage, verifier)
            if not first and text == self._prompt and self.station.status().get("state") == "running":
                refused = None   # the previous stage's lock already switched Dream to this prompt: don't restart it
                self.log.event("prompt_kept", stage=stage.name, prompt=text, note="already running from the lock")
            else:
                refused = self._try_issue(text, "stage", force_start=first)
        except _WriterSkip as exc:
            self.log.event("stage_skipped", stage=stage.name, reason=str(exc))
            return StageResult(stage.name, False, 0, self._clock() - t0, str(exc))
        attempts = 0
        uses: dict[int, int] = {}
        last_rec: int | None = None
        while True:
            attempts += 1
            if refused:
                self.log.event("attempt_end", stage=stage.name, attempt=attempts, ok=False, reason=refused)
                out = _Attempt(False, refused)
                self._nap(min(self.plan.check_every_s, 3.0))   # don't spin on a prompt Dream keeps refusing
            else:
                out = self._attempt(stage, verifier, attempts, self._prompt or stage.prompt)
            if out.ok:
                return StageResult(stage.name, True, attempts, self._clock() - t0, out.reason)
            if out.operator:
                # tell the monitor: 8 Nov live, after a skipped tower it judged every ball look "failing: build the
                # tower first"
                self._mark_done(f"{stage.name}: skipped by the operator, the run has moved on from it")
                self.log.event("stage_skipped", stage=stage.name, reason=out.reason)
                return StageResult(stage.name, False, attempts, self._clock() - t0, out.reason)
            reason, hint = out.reason, out.recovery_hint
            if stage.on_fail == "abort":
                raise _Fail(f"stage {stage.name!r} failed (on_fail abort): {reason}")
            if stage.on_fail == "skip":
                self.log.event("stage_skipped", stage=stage.name, reason=reason)
                return StageResult(stage.name, False, attempts, self._clock() - t0, reason)
            while True:   # choose a recovery; a reset that leaves the scene broken burns an attempt
                if attempts >= stage.max_attempts:
                    raise _Fail(f"stage {stage.name!r} failed after {attempts} attempts: {reason}")
                idx = None if self.plan.hands_off else self._pick_recovery(stage, uses, last_rec, hint)
                if idx is None:
                    break
                uses[idx] = uses.get(idx, 0) + 1
                last_rec = idx
                self._done_in_recovery = None
                scene_ok, rreason = self._recover(stage, verifier, attempts, idx)
                if self._done_in_recovery and self._confirm_done(stage, verifier):
                    return StageResult(stage.name, True, attempts, self._clock() - t0,
                                       f"done during recovery: {self._done_in_recovery}")
                if scene_ok:
                    break
                attempts += 1
                reason, hint = rreason, None
                self.log.event("attempt_end", stage=stage.name, attempt=attempts, ok=False, reason=rreason)
            if idx is None and stage.on_fail == "home_and_retry":
                self.log.event("retry", stage=stage.name, attempt=attempts + 1, mode="home_and_retry")
                self.station.stop()
                self._wait({"idle"}, IDLE_TIMEOUT_S, "idle after stop")
                if self.live_guard is not None and self.live_guard.armed:
                    raise _Fail("refusing /home while the live guard is armed")   # /home can't be stopped once begun
                self._motion(self.station.home)
                self._wait({"idle"}, IDLE_TIMEOUT_S, "idle after home")
                refused = self._try_issue(self._stage_prompt(stage, verifier, retry=True), "retry", force_start=True)
            else:
                try:
                    nxt = self._stage_prompt(stage, verifier, retry=True)
                    if self.plan.hands_off and nxt == self._prompt and self.station.status().get("state") == "running":
                        refused = None   # Dream is already on it: a resend would only restart its motion
                        self.log.event("retry", stage=stage.name, attempt=attempts + 1, mode="hands_off_keep_running")
                        continue
                    refused = self._try_issue(nxt, "retry")
                except _WriterSkip as exc:
                    self.log.event("stage_skipped", stage=stage.name, reason=str(exc))
                    return StageResult(stage.name, False, attempts, self._clock() - t0, str(exc))
                self.log.event("retry", stage=stage.name, attempt=attempts + 1, mode="retry_in_place")
