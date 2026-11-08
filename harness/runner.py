"""The supervisor state machine. See DESIGN.md "runner.py"."""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timezone
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .frames import FrameSource
from .log import RunLog
from .plan import TOWER_STAGES, Plan, Recovery, Stage
from .start_pose import StartPoses, joints_from_arms, joints_from_observation
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
HOME_RESERVE_S = 30.0      # /home can't be cancelled (the station refuses /stop while homing): need this much left
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


def tower_matches(seen: list[str], target: list[str]) -> bool:
    """The seen tower is the target's first len(seen) pieces. Per piece, the colour (first word) and the shape (last
    word before the comma) of the target must appear in the seen one; orientation wording varies and is not compared."""
    if len(seen) > len(target):
        return False
    for s, t in zip(seen, target):
        words = _norm(t.split(",")[0]).split()
        key = [words[0], words[-1]] if words else []
        if not all(k in _norm(s).split() or k in _norm(s) for k in key):
            return False
    return True


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
        # harness.live_guard.LiveGuard: the plan guard on live /frame images in its own thread (sends /stop itself)
        self.live_guard: Any = None
        # harness.start_pose.StartPoses for plan.start_pose; None = no start-pose checks
        self.start_poses: StartPoses | None = None
        self._homes: dict[str, int] = {}
        self.fast = None   # harness.fastloop.FastLoop: perception + observer + state-space decisions at 20 Hz
        self._fast_sig: dict[str, str] = {}   # per camera: the image the per-poll local guard judged last
        # harness.decide.Shadow: the decision equations run next to the rules below and log what they would do
        self.shadow: Any = None
        try:
            from .decide import Shadow  # noqa: PLC0415
            self.shadow = Shadow.from_plan(plan.decide)
        except Exception as exc:  # noqa: BLE001 (the shadow must never stop a run)
            log.event("warning", message=f"decide shadow off: {exc}")
        self._auto_resets = 0
        self._done_in_recovery: str | None = None
        self.operator: Any = None   # harness.operator.Operator: the operator's typed words, turned into actions
        self._skip: set[str] = set()   # later stages the operator asked to skip
        self._stage_name = ""

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
            if self.plan.reset_first:
                self._reset_and_wait("before the first stage: both grippers open")
            for stage in self.plan.stages:
                self._apply_operator()
                if stage.requires_stage and not any(r.name == stage.requires_stage and r.ok for r in results):
                    reason = f"{stage.requires_stage} did not pass"   # e.g. no cable came out: nothing to put down
                    self.log.event("stage_skipped", stage=stage.name, reason=reason)
                    results.append(StageResult(stage.name, False, 0, 0.0, reason))
                    continue
                if stage.name in self._skip:
                    self.log.event("stage_skipped", stage=stage.name, reason="operator asked to skip it")
                    results.append(StageResult(stage.name, False, 0, 0.0, "skipped by operator"))
                    continue
                results.append(self._run_stage(stage, first=first))
                first = False
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
        lg = self.live_guard
        if lg is not None and fn == self.station.home and lg.armed:
            # /home can't be stopped once begun: never while the guard could fire (a trip racing this /home)
            raise _Fail("refusing /home while the live guard is armed")
        reply = fn(*args)
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
            # like /home, a /reset moves both arms and may not be stoppable once begun: never while the guard is armed
            self.log.event("reset_skipped", why=why, reason="live guard armed")
            return
        self.log.event("reset", why=why)
        self._motion(reset)   # checks operator stop, the deadline and a guard trip first, like every motion command
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
            self._check_global()
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
                notes.append(f"{side} gripper nearly shut ({got:.2f}): a thin plug or nothing, judge from its wrist image")
            elif cmd is None:
                notes.append(f"{side} gripper opening {got:.2f}")
            else:
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
        if self.shadow is not None:
            self.shadow.start(stage.timeout_s, stage.stall_s, confirm)
        if self.fast is not None:
            self.fast.attempt_start(stage.name, stage.timeout_s, stage.stall_s, guarded=guard is not None)
        last_save = -1e9
        rewrites, last_prompt_t = 0, 0.0
        while True:
            self._nap(self.plan.check_every_s)
            st = self._poll_health()
            elapsed = self._clock() - t0
            end_now, prompts = self._apply_operator()
            if end_now:
                if prompts and self.operator is not None:   # "skip this, do the balls": the prompt is for the next stage
                    with self.operator._lock:
                        self.operator._actions.prompts[:0] = prompts
                return _Attempt(False, "operator ended the stage", max_p, hint, operator=True)
            for prompt in prompts:
                self._issue(prompt, "operator")
                rewrites = REWRITE_MAX   # the operator's prompt stands: no monitor rewrite undoes it (run 3)
                last_prompt_t, consec_done, consec_fail, consec_stalled = elapsed, 0, 0, 0
                gate, rise_ref, last_rise = elapsed + stage.effective_settle(), None, None
            if st.get("state") == "idle":
                return _Attempt(False, "station went idle during the stage", max_p, hint)
            if self.fast is not None and self.fast.mode == "live":
                fd = self.fast.decision
                if self.fast.tripped is not None or fd.get("guard"):
                    raise _Guard(f"guard tripped in stage {stage.name!r} (fast loop)")
                if fd.get("pass"):
                    return _Attempt(True, "fast loop: done evidence reached", max_p, hint)
                if fd.get("fail") or fd.get("stall"):
                    return _Attempt(False, "fast loop: " + ("failing" if fd.get("fail") else "stalled"), max_p, hint)
            if guard is not None and self.live_guard is not None and self.live_guard.last_answer_t is not None:
                self._guard_answer_t = max(self._guard_answer_t or 0.0, self.live_guard.last_answer_t)
            fast_frames = self.frames.latest() if guard is not None and self.fast_guard is not None else {}
            new_frames = {}
            for role, jpeg in fast_frames.items():    # kept with a live guard too, as a fallback
                sig = hashlib.sha1(jpeg).hexdigest()
                if self._fast_sig.get(role) != sig:
                    self._fast_sig[role] = sig
                    new_frames[role] = jpeg
            if new_frames:
                # Local detector on every poll, from the stage's first second: no API latency, works offline.
                # Only camera images not judged before count: the debug view repeats a trip's frames until the next
                # one, and a frozen wrist view must neither confirm a yes nor keep max_blind_s satisfied.
                local, why = self.fast_guard(new_frames)
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
                real = isinstance(verdict, MonitorVerdict) and verdict.status is not None
                if real or elapsed - last_save >= 3.0:   # a 0.25 s loop must not write 12 images a second
                    saved = [p.name for p in self.log.save_frames(f"{stage.name}_a{attempt}", frames)]
                    last_save = elapsed
                else:
                    saved = []
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
                failing, why = verdict.done is False, f"verifier said no: {verdict.reason}"
                if self.fast is not None and monitor:
                    sig_ = getattr(verdict, "frames_sig", None)
                    held = " ".join(str(x) for x in (verdict.holding or {}).values()).lower()
                    self.fast.verdict(done=verdict.done, failing=verdict.done is False or verdict.status == "failing",
                                      same_frames=verdict.done is True and sig_ is not None and sig_ == last_done_sig,
                                      stalled=verdict.status == "stalled", progress=verdict.progress,
                                      guard=getattr(verdict, "guard", None),
                                      holding=None if not held else not any(w in held for w in ("nothing", "none")))
                nxt = getattr(verdict, "next_prompt", None) if self.plan.rewrite_prompts else None
                if (nxt and monitor and verdict.status in ("stalled", "failing") and verdict.done is not True
                        and last_done_t is None and rewrites < REWRITE_MAX
                        and elapsed - last_prompt_t >= REWRITE_COOLDOWN_S and nxt != self._prompt):
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
                        if max_p is not None and p <= max_p - REGRESSION_DROP and last_done_t is None:
                            self.log.event("regression", stage=stage.name, attempt=attempt, progress=p, max_progress=max_p)
                            failing, why = True, f"regressed: progress {p:.2f} vs max {max_p:.2f}"
                        if max_p is None or p > max_p:
                            max_p = p
                        if rise_ref is None or p >= rise_ref + STALL_RISE:
                            rise_ref, last_rise = p, elapsed
                    if verdict.status == "failing" and not failing:
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
                self._shadow_look(stage, attempt, elapsed, verdict, failing, monitor, consec_stalled, last_rise,
                                  same=verdict.done is True and sig is not None and sig == last_done_sig
                                  and not verdict.immediate)
                if verdict.done is True and sig is not None and sig == last_done_sig and not verdict.immediate:
                    # the same camera images judged twice are one look, not two: a frozen view must not confirm
                    self.log.event("same_frames", stage=stage.name, attempt=attempt,
                                   note="done verdict on the frames of the previous done: not counted")
                elif (verdict.done is True and not verdict.immediate and last_done_t is not None
                      and elapsed - last_done_t < self.plan.confirm_gap_s):
                    # Two hedged / parallel calls can land a second apart on near-identical frames: they are one
                    # look, not two. Keeps "stands 3 s after release" true and stops paired false dones (stress sim).
                    self.log.event("done_too_close", stage=stage.name, attempt=attempt,
                                   gap_s=round(elapsed - last_done_t, 2), min_gap_s=self.plan.confirm_gap_s)
                elif verdict.done is True:
                    consec_fail = 0
                    if verdict.immediate or frames:
                        consec_done += 1
                        recent = last_done_t is not None and elapsed - last_done_t <= DONE_WINDOW_S
                        last_done_t, last_done_sig = elapsed, sig
                        if verdict.immediate or consec_done >= confirm or recent:
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
                if stage.stall_s is not None and consec_stalled >= confirm and last_done_t is None:
                    return _Attempt(False, "stalled: monitor says stalled", max_p, hint)
            # No stall once done was seen or progress is at the top: a finished stage cannot "rise" any further.
            if (stage.stall_s is not None and last_rise is not None and elapsed >= gate and last_done_t is None
                    and (max_p or 0) < 0.95 and elapsed - last_rise >= stage.stall_s):
                return _Attempt(False, f"stalled: progress {max_p:.2f} has not risen for {stage.stall_s:g} s",
                                max_p, hint)
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

    def _shadow_look(self, stage: Stage, attempt: int, elapsed: float, verdict: Verdict, failing: bool, monitor: bool,
                     consec_stalled: int, last_rise: float | None, *, same: bool) -> None:
        """Logs what harness.decide would decide on this look (event `decide`). Never acts, never raises."""
        if self.shadow is None:
            return
        try:
            out = self.shadow.look(elapsed, verdict.done, failing, same_frames=same, monitor=monitor,
                                   consec_stalled=consec_stalled,
                                   since_rise_s=None if last_rise is None else elapsed - last_rise)
            self.log.event("decide", stage=stage.name, attempt=attempt, elapsed_s=round(elapsed, 2), **out)
        except Exception as exc:  # noqa: BLE001
            self.log.event("warning", message=f"decide shadow failed, off for this run: {exc}")
            self.shadow = None

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
        self._attempt_n = attempt + 1
        if not out.ok:
            self._last_fail_reason = out.reason
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

    def _stage_prompt(self, stage: Stage, verifier: Verifier) -> str:
        """The stage's prompt, or with plan.tower_prompts on a stacking stage: the next target piece by name, after
        the tower the monitor last reported (one synchronous look if it hasn't reported one yet)."""
        return self._layered(stage, self._stage_prompt_base(stage, verifier))

    def _layered(self, stage: Stage, prompt: str) -> str:
        """plan.prompt_level (harness.promptgen): add context levels to the prompt for base Dream-1. 1 = as is."""
        level = int(getattr(self.plan, "prompt_level", 1))
        technique = (stage.technique or "") if getattr(self.plan, "prompt_technique", False) else ""
        if level == 1 and not technique:
            return prompt
        from .promptgen import Context, compose  # noqa: PLC0415
        v = getattr(self, "_last_verdict", None)
        ctx = Context(prefix=self.plan.prompt_prefix or "", holding=getattr(v, "holding", None),
                      tower=self._tower, attempt=getattr(self, "_attempt_n", 1),
                      progress=getattr(v, "progress", None), last_failure=getattr(self, "_last_fail_reason", ""),
                      technique=technique)
        out = compose(prompt, level, ctx)
        self.log.event("prompt_layered", stage=stage.name, level=level, prompt=out)
        return out

    def _stage_prompt_base(self, stage: Stage, verifier: Verifier) -> str:
        plan = self.plan
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

    def _joint_state(self, st: dict[str, Any]) -> tuple[dict[str, list[float]] | None, str | None]:
        """Joint angles from GET /arms (add-on, any state), else from the newest trip (only while running: an
        idle station's last trip predates any /home)."""
        getter = getattr(self.station, "grippers", None)
        q = joints_from_arms(getter() if getter else {})
        if q:
            return q, "arms"
        obs = getattr(self.frames, "latest_observation", None)
        if st.get("state") == "running" and obs is not None:
            q = joints_from_observation(obs())
            if q:
                return q, "trip"
        return None, None

    def _start_pose_gate(self, stage: Stage, prompt: str, why: str) -> bool:
        """Logs how far the arms are from the training starts for `prompt`. On a home_before stage with a far start,
        stops (if running) and homes the arms. Returns True if it homed, so the caller must /start afresh."""
        cfg = self.plan.start_pose
        if cfg is None or self.start_poses is None:
            return False
        st = self.station.status()
        joints, source = self._joint_state(st)
        chk = self.start_poses.check(prompt, joints, cfg.tolerance_rad, source)
        self.log.event("start_pose", stage=stage.name, why=why, prompt=prompt, tolerance_rad=cfg.tolerance_rad,
                       **chk.as_event())
        if chk.status != "far" or not cfg.homes(stage.name):
            return False
        if self._homes.get(stage.name, 0) >= cfg.max_homes:
            self.log.event("start_pose_home", stage=stage.name, done=False, reason=f"max_homes {cfg.max_homes} used")
            return False
        home = self.start_poses.check(prompt, cfg.home_pose, cfg.tolerance_rad, "home_pose")
        if home.status != "ok":
            self.log.event("start_pose_home", stage=stage.name, done=False,
                           reason="the home pose is not a training start for this prompt either", **home.as_event())
            return False
        left = self.plan.time_budget_s - (self._clock() - self._t0)
        if left < HOME_RESERVE_S:
            self.log.event("start_pose_home", stage=stage.name, done=False,
                           reason=f"only {left:.0f} s left; /home can't be cancelled once started")
            return False
        self._check_global()
        self._homes[stage.name] = self._homes.get(stage.name, 0) + 1
        self.log.event("start_pose_home", stage=stage.name, done=True, why=why)
        if st.get("state") != "idle":
            self.station.stop()
            self._wait({"idle"}, IDLE_TIMEOUT_S, "idle after stop")
        self._check_global()   # an operator stop, the deadline or a guard trip during the stop: hold, don't home
        if self.plan.time_budget_s - (self._clock() - self._t0) < HOME_RESERVE_S:
            raise _Fail("not enough match time left to home safely after the stop")
        self._motion(self.station.home)
        self._wait({"idle"}, IDLE_TIMEOUT_S, "idle after home")
        return True

    def _run_stage(self, stage: Stage, first: bool) -> StageResult:
        t0 = self._clock()
        verifier = self.verifiers.get(stage.verifier)
        if verifier is None:
            raise _Fail(f"stage {stage.name!r}: no {stage.verifier!r} verifier configured")
        self._check_global()
        self._last_progress = None
        self._stage_name = stage.name
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
        prompt = self._stage_prompt(stage, verifier)
        homed = self._start_pose_gate(stage, prompt, "stage start")
        refused = self._try_issue(prompt, "stage", force_start=first or homed)
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
                idx = self._pick_recovery(stage, uses, last_rec, hint)
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
                if self.plan.time_budget_s - (self._clock() - self._t0) < HOME_RESERVE_S:
                    raise _Fail("not enough match time left to home safely after the stop")
                self._motion(self.station.home)   # checks operator stop, deadline and guard first
                self._wait({"idle"}, IDLE_TIMEOUT_S, "idle after home")
                refused = self._try_issue(self._stage_prompt(stage, verifier), "retry", force_start=True)
            else:
                prompt = self._stage_prompt(stage, verifier)
                homed = self._start_pose_gate(stage, prompt, "retry")
                refused = self._try_issue(prompt, "retry", force_start=homed)
                self.log.event("retry", stage=stage.name, attempt=attempts + 1,
                               mode="home_then_retry" if homed else "retry_in_place")
