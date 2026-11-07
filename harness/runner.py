"""The supervisor state machine. See DESIGN.md "runner.py"."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .frames import FrameSource, camera_matches
from .log import RunLog
from .plan import Plan, Recovery, Stage
from .station_client import StationClient, StationError
from .verifiers import MonitorVerdict, Verdict, Verifier

START_TIMEOUT_S = 120.0
IDLE_TIMEOUT_S = 60.0
MAX_CAMERA_AGE_S = 2.0
_SLEEP_STEP_S = 0.25
STALL_RISE = 0.05          # progress must rise by this much to reset the stall clock
REGRESSION_DROP = 0.3      # a drop this far below the attempt's max counts as a failing verdict
FAIL_MIN_FRACTION = 0.5    # a monitor "failing" ends an attempt only after this share of timeout_s
SKIP_MIN_CONFIDENCE = 0.8  # skip_if_done needs this much confidence from a non-manual verifier
MOVED_DELTA = 0.02         # progress change that counts as "moved" after a prompt change
MATCH_REQUEST_S = 5.0      # cap a station or camera request before applying the remaining budget


@dataclass
class StageResult:
    name: str
    ok: bool
    attempts: int
    seconds: float
    reason: str
    abandoned: bool = False


@dataclass
class _Attempt:
    ok: bool
    reason: str
    max_progress: float | None = None
    recovery_hint: int | None = None   # the monitor's chosen recovery index, if any


@dataclass
class RunResult:
    ok: bool
    stages: list[StageResult] = field(default_factory=list)
    seconds: float = 0.0
    reason: str = ""
    match_gates_passed: bool | None = None
    stop_confirmed: bool | None = None


@dataclass
class _StageRun:
    stage: Stage
    started: float
    deadline: float
    attempts: int = 0


def _err(last_error: Any) -> str:
    """The real station reports last_error as a dict (message, type, error_code); older or fake ones as a str."""
    if isinstance(last_error, dict):
        return str(last_error.get("message") or last_error)
    return str(last_error)


class _Fail(Exception):
    """End the whole run (the station is stopped by the handler)."""


class _Refuse(Exception):
    """Preflight refused; nothing was started so nothing is stopped."""


class _Stop(Exception):
    """request_stop() was called."""


class _StageExpired(Exception):
    """The active stage used its allocation."""


class Runner:
    def __init__(self, plan: Plan, station: StationClient, frames: FrameSource, verifiers: dict[str, Verifier],
                 log: RunLog, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.plan, self.station, self.frames, self.verifiers, self.log = plan, station, frames, verifiers, log
        self._clock, self._sleep = clock, sleep
        self._stop_event = threading.Event()
        self._t0 = 0.0
        self._work_end = 0.0
        self._active: _StageRun | None = None
        self._motion_requested = False
        self._stop_confirmed: bool | None = None
        self._last_progress: float | None = None
        self._change: dict[str, Any] | None = None   # the latest prompt change, for the progress_moved event

    def request_stop(self) -> None:
        self._stop_event.set()

    # -- top level -----------------------------------------------------------------------------
    def run(self) -> RunResult:
        self._t0 = self._clock()
        self._work_end = self._t0 + self.plan.time_budget_s
        self._motion_requested = False
        self._stop_confirmed = None
        results: list[StageResult] = []
        dry = self.station.dry_run
        self.log.update_manifest(start_time=datetime.now(timezone.utc).isoformat())
        self.log.event("run_start", task=self.plan.task, dry_run=dry, stages=[s.name for s in self.plan.stages])
        try:
            self._preflight()
            if self.plan.match:
                self._t0 = self._clock()
                self._work_end = self._t0 + self.plan.time_budget_s - self.plan.match.stop_reserve_s
                self.log.event("match_clock_start", budget_s=self.plan.time_budget_s,
                               stop_reserve_s=self.plan.match.stop_reserve_s)
            for i, stage in enumerate(self.plan.stages):
                if stage.requires_stage and not any(r.name == stage.requires_stage and r.ok for r in results):
                    reason = f"prerequisite {stage.requires_stage!r} was not verified"
                    if stage.on_fail == "abandon":
                        results.append(self._abandon(_StageRun(stage, self._clock(), self._work_end), reason))
                    elif self.plan.match:
                        raise _Fail(f"required stage {stage.name!r}: {reason}")
                    else:
                        self.log.event("stage_skipped", stage=stage.name, reason=reason)
                        results.append(StageResult(stage.name, False, 0, 0.0, reason))
                    continue
                results.append(self._run_stage(stage, first=(i == 0)))
            failed = [r.name for r in results if not r.ok and not r.abandoned]
            ok, reason = (not failed), ("completed" if not failed else f"completed with failed stages: {', '.join(failed)}")
            self._safe_stop("end of plan")
        except _Refuse as exc:
            ok, reason = False, f"refused: {exc}"
        except _Fail as exc:
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
        seconds = self._clock() - self._t0
        gates_passed = None
        if self.plan.match:
            gates_passed = set(self.plan.match.gates) <= {r.name for r in results if r.ok}
            if ok and not self._stop_confirmed:
                ok, reason = False, "station stop was not confirmed"
            if self._stop_confirmed and seconds >= self.plan.time_budget_s:
                late = "station stop acknowledged after the match deadline"
                ok, reason = False, late if ok else f"{reason}. {late}"
        result = RunResult(ok=ok, stages=results, seconds=seconds, reason=reason,
                           match_gates_passed=gates_passed, stop_confirmed=self._stop_confirmed)
        self.log.update_manifest(end_time=datetime.now(timezone.utc).isoformat(), ok=ok, reason=reason)
        self.log.event("run_end", ok=ok, reason=reason, seconds=result.seconds)
        self.log.write_summary({"task": self.plan.task, "dry_run": dry, **asdict(result)})
        return result

    # -- pieces --------------------------------------------------------------------------------
    def _preflight(self) -> None:
        if self.plan.match and any(s.verifier != "monitor" for s in self.plan.stages):
            raise _Refuse("match plans require the monitor verifier")
        if any(s.required_cameras and s.verifier == "time" for s in self.plan.stages):
            raise _Refuse("stages with required_cameras need a visual verifier")
        dry = self.station.dry_run
        st = self.station.status(timeout_s=MATCH_REQUEST_S) if self.plan.match else self.station.status()
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

    def _check_global(self) -> float:
        if self._stop_event.is_set():
            raise _Stop()
        now = self._clock()
        active = self._active
        if active and now >= active.deadline and (active.deadline < self._work_end or
                                                  active.stage.on_fail == "abandon"):
            raise _StageExpired()
        if now >= self._work_end:
            if self.plan.match:
                raise _Fail(f"match work cutoff reached ({self.plan.match.stop_reserve_s:g} s stop reserve)")
            raise _Fail(f"time budget of {self.plan.time_budget_s:g} s exceeded")
        return min(self._work_end, active.deadline if active else self._work_end) - now

    def _in_budget(self, call: Callable[..., Any], *args: Any) -> Any:
        remaining = self._check_global()
        reply = call(*args, timeout_s=min(remaining, MATCH_REQUEST_S)) if self.plan.match else call(*args)
        self._check_global()
        return reply

    def _nap(self, seconds: float) -> None:
        remaining = seconds
        while remaining > 1e-9:
            step = min(_SLEEP_STEP_S, remaining, self._check_global())
            self._sleep(step)
            remaining -= step
        self._check_global()

    def _poll_health(self) -> dict[str, Any]:
        st = self._in_budget(self.station.status)
        if st.get("state") == "error":
            raise _Fail(f"station error: {_err(st.get('last_error'))}")
        dead = [a for a, alive in (st.get("arms") or {}).items() if not alive]
        if dead:
            raise _Fail(f"arm(s) not alive: {', '.join(dead)}")
        return st

    def _safe_stop(self, why: str) -> None:
        if self.plan.match and not self._motion_requested:
            return
        self.log.event("stop", why=why)
        try:
            if self.plan.match:
                remaining = self._t0 + self.plan.time_budget_s - self._clock()
                status = self.station.stop(timeout_s=min(MATCH_REQUEST_S, max(0.05, remaining)))
                self._stop_confirmed = status.get("state") == "idle" and not self.station.dry_run
                self.log.event("stop_reply", state=status.get("state"), confirmed=self._stop_confirmed)
            else:
                self.station.stop()
        except (StationError, OSError) as exc:
            if self.plan.match:
                self._stop_confirmed = False
            self.log.event("stop_failed", error=str(exc))

    def _wait(self, states: set[str], timeout_s: float, what: str) -> dict[str, Any]:
        if self.plan.match:
            until = self._clock() + timeout_s
            while self._clock() < until:
                st = self._poll_health()
                if st.get("state") in states:
                    return st
                self._nap(min(0.5, until - self._clock()))
            raise _Fail(f"timed out waiting for {what}")
        try:
            st = self.station.wait_for_state(states | {"error"}, timeout_s)
        except TimeoutError as exc:
            raise _Fail(f"timed out waiting for {what}: {exc}") from None
        if st.get("state") == "error" and "error" not in states:
            raise _Fail(f"station error: {_err(st.get('last_error'))}")
        return st

    def _start_run(self, prompt: str) -> dict[str, Any]:
        self.log.event("start", prompt=prompt, checkpoint=self.plan.checkpoint)
        self._check_global()
        self._motion_requested = True
        try:
            self._in_budget(self.station.start, prompt, self.plan.checkpoint)
        except StationError as exc:
            if exc.status_code in (400, 409):
                self._motion_requested = False
            raise
        return self._wait({"running"}, START_TIMEOUT_S, "running")

    def _issue(self, prompt: str, why: str, *, force_start: bool = False,
               require_new_trip: bool = False) -> int | None:
        """Gets `prompt` to the robot: set_prompt on a running station, else a fresh start. Logs a prompt_change
        event with the chunk count and tells verifiers (a monitor drops answers computed on the old prompt)."""
        previous_trip = self._in_budget(self.frames.latest_n) if require_new_trip else None
        if force_start or (self.plan.match and not self._motion_requested):
            reply, how = self._start_run(prompt), "start"
        elif self._in_budget(self.station.status).get("state") != "running":
            if self.plan.match:
                raise _Fail("match station is no longer running")
            reply, how = self._start_run(prompt), "start"
        else:
            reply, how = self._in_budget(self.station.set_prompt, prompt), "set_prompt"
            self.log.event("set_prompt", prompt=prompt)
        chunks = reply.get("chunks") if isinstance(reply, dict) else None
        self.log.event("prompt_change", prompt=prompt, why=why, how=how, chunks=chunks)
        self._change = {"t": self._clock(), "baseline": self._last_progress, "logged": False}
        seen: set[int] = set()
        for v in self.verifiers.values():
            fn = getattr(v, "notify_prompt_change", None)
            if fn is not None and id(v) not in seen:
                seen.add(id(v))
                fn(prompt)
        return previous_trip

    def _frames_for_check(self, stage: Stage | None = None, *,
                          frames: dict[str, bytes] | None = None) -> dict[str, bytes]:
        if frames is None:
            frames = self._in_budget(self.frames.latest) if self.plan.match else self.frames.latest()
        wanted = (stage.cameras if stage is not None and stage.cameras else None) or self.plan.cameras_for_check
        if wanted:
            frames = {r: j for r, j in frames.items() if any(camera_matches(r, w) for w in wanted)}
        return frames

    def _ask(self, verifier: Verifier, stage: Stage, frames: dict[str, bytes], elapsed: float, *,
             sync: bool = False) -> Verdict:
        fn = getattr(verifier, "check_now", verifier.check) if sync else verifier.check
        try:
            return fn(stage, frames, elapsed)
        except Exception as exc:
            return Verdict(None, 0.0, f"verifier raised {type(exc).__name__}: {exc}")

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
                     st: dict[str, Any], trip_n: int | None = None) -> None:
        extra = {k: getattr(verdict, k) for k in ("progress", "status", "recovery", "scene_ok", "holding")
                 if getattr(verdict, k, None) is not None}
        if trip_n is not None:
            extra["trip"] = trip_n
        self.log.event("verdict", stage=stage.name, attempt=attempt, elapsed_s=round(elapsed, 2),
                       done=verdict.done, confidence=verdict.confidence, reason=verdict.reason,
                       frames=saved, chunks=st.get("chunks"), **extra)

    def _watch(self, stage: Stage, verifier: Verifier, attempt: int, last_trip: int | None) -> _Attempt:
        t0 = self._clock()
        confirm = self.plan.confirm
        gate = max(stage.min_run_s, stage.effective_settle())   # no verifier and no stall clock before this
        consec_done = consec_fail = consec_stalled = 0
        max_p: float | None = None
        rise_ref = last_rise = None
        hint: int | None = None
        while True:
            interval = self.plan.check_every_s
            if self.plan.match:
                interval = min(interval, stage.timeout_s - (self._clock() - t0))
            self._nap(interval)
            st = self._poll_health()
            elapsed = self._clock() - t0
            if self.plan.match and elapsed >= stage.timeout_s:
                return _Attempt(False, f"timeout after {stage.timeout_s:g} s", max_p, hint)
            if st.get("state") == "idle":
                return _Attempt(False, "station went idle during the stage", max_p, hint)
            if elapsed >= gate:
                if stage.required_cameras:
                    trip = self._in_budget(self.frames.latest_trip)
                    repeated = trip is not None and last_trip is not None and trip.n <= last_trip
                    frames = self._frames_for_check(stage, frames=trip.frames if trip is not None else {})
                    problem = None
                    if trip is None:
                        problem = "no camera trip available"
                    elif trip.prompt != stage.prompt:
                        problem = "camera trip is for a different prompt"
                    else:
                        ages = st.get("cameras") or {}
                        missing = [
                            role for role in stage.required_cameras
                            if not any(camera_matches(r, role) for r in frames)
                            or any(camera_matches(r, role) and (age is None or age > MAX_CAMERA_AGE_S)
                                   for r, age in ages.items())
                        ]
                        if missing:
                            problem = f"required camera views missing or stale: {', '.join(missing)}"
                    if trip is not None and not repeated:
                        last_trip = trip.n
                    if problem or repeated:
                        if problem:
                            consec_done = consec_fail = consec_stalled = 0
                            notify = getattr(verifier, "notify_prompt_change", None)
                            if notify is not None:
                                notify(stage.prompt)
                        reason = problem or "waiting for a new camera trip"
                        self.log.event("camera_evidence_wait", stage=stage.name, attempt=attempt,
                                       trip=trip.n if trip is not None else None, reason=reason)
                        if elapsed >= stage.timeout_s:
                            return _Attempt(False, f"timeout after {stage.timeout_s:g} s: {reason}", max_p, hint)
                        continue
                else:
                    frames = self._frames_for_check(stage)
                verdict = self._ask(verifier, stage, frames, elapsed)
                self._check_global()
                if self.plan.match and self._clock() - t0 >= stage.timeout_s:
                    return _Attempt(False, f"timeout after {stage.timeout_s:g} s", max_p, hint)
                saved = [p.name for p in self.log.save_frames(f"{stage.name}_a{attempt}", frames)]
                self._log_verdict(stage, attempt, elapsed, verdict, saved, st, last_trip)
                self._check_global()
                monitor = isinstance(verdict, MonitorVerdict) and verdict.status is not None
                failing, why = verdict.done is False, f"verifier said no: {verdict.reason}"
                if monitor:
                    self._note_progress(stage, verdict, st)
                    if verdict.recovery is not None:
                        hint = verdict.recovery
                    p = verdict.progress
                    if p is not None:
                        if max_p is not None and p <= max_p - REGRESSION_DROP:
                            self.log.event("regression", stage=stage.name, attempt=attempt, progress=p, max_progress=max_p)
                            failing, why = True, f"regressed: progress {p:.2f} vs max {max_p:.2f}"
                        if max_p is None or p > max_p:
                            max_p = p
                        if rise_ref is None or p >= rise_ref + STALL_RISE:
                            rise_ref, last_rise = p, elapsed
                    if verdict.status == "failing" and not failing:
                        failing, why = True, f"monitor says failing: {verdict.reason}"
                    consec_stalled = consec_stalled + 1 if verdict.status == "stalled" else 0
                if verdict.done is True:
                    consec_fail = 0
                    if verdict.immediate or frames:
                        consec_done += 1
                        if verdict.immediate or consec_done >= confirm:
                            return _Attempt(True, verdict.reason or "verified", max_p, hint)
                    else:
                        consec_done = 0
                        self.log.event("no_frames", stage=stage.name, attempt=attempt,
                                       note="done verdict ignored: no frames to back it")
                elif failing:
                    consec_done = 0
                    consec_fail += 1
                    if verdict.immediate and verdict.done is False:
                        return _Attempt(False, why, max_p, hint)
                    if consec_fail >= confirm and (not monitor or elapsed >= FAIL_MIN_FRACTION * stage.timeout_s):
                        return _Attempt(False, why, max_p, hint)
                elif verdict.done is not None or monitor:
                    consec_done = consec_fail = 0
                if stage.stall_s is not None and consec_stalled >= confirm:
                    return _Attempt(False, "stalled: monitor says stalled", max_p, hint)
            if (stage.stall_s is not None and last_rise is not None and elapsed >= gate
                    and elapsed - last_rise >= stage.stall_s):
                return _Attempt(False, f"stalled: progress {max_p:.2f} has not risen for {stage.stall_s:g} s",
                                max_p, hint)
            if elapsed >= stage.timeout_s:
                return _Attempt(False, f"timeout after {stage.timeout_s:g} s", max_p, hint)

    def _hold(self, seconds: float) -> None:
        t0 = self._clock()
        while self._clock() - t0 < seconds:
            self._nap(min(self.plan.check_every_s, max(seconds - (self._clock() - t0), 0.01)))
            self._poll_health()

    def _reset_scene(self, stage: Stage, rec: Recovery, verifier: Verifier) -> tuple[bool, str]:
        """kind reset: run the recovery prompt, then wait (up to the cap) for the verifier's scene_ok."""
        verification_window = min(stage.timeout_s / 2, 20.0)
        settle = rec.settle_s if self.plan.match else min(rec.settle_s, verification_window)
        cap = settle + verification_window if self.plan.match else verification_window
        t0 = self._clock()
        scene: bool | None = None
        while True:
            self._nap(min(self.plan.check_every_s, max(cap - (self._clock() - t0), 0.01)))
            st = self._poll_health()
            elapsed = self._clock() - t0
            if self.plan.match and elapsed >= cap:
                return False, f"scene not verified within recovery {rec.name!r} budget"
            if elapsed >= settle:
                if self.plan.match:
                    trip = self._in_budget(self.frames.latest_trip)
                    frames = trip.frames if trip is not None and trip.prompt == rec.prompt else {}
                else:
                    frames = self._frames_for_check(stage)
                verdict = self._ask(verifier, stage, frames, elapsed) if frames or not self.plan.match else Verdict(None)
                self._check_global()
                if self.plan.match and self._clock() - t0 >= cap:
                    return False, f"scene not verified within recovery {rec.name!r} budget"
                self._note_progress(stage, verdict, st)
                ok = getattr(verdict, "scene_ok", None)
                if ok is not None:
                    scene = ok
                    self.log.event("scene_check", stage=stage.name, recovery=rec.name, scene_ok=ok,
                                   elapsed_s=round(elapsed, 2), reason=verdict.reason)
                    if ok:
                        return True, "scene ok"
            if elapsed >= cap:
                if scene is False or self.plan.match:
                    return False, f"scene still not ok after recovery {rec.name!r}"
                return True, "scene ok" if scene else "no scene verdict, assumed ok"

    def _pick_recovery(self, stage: Stage, uses: dict[int, int], last: int | None, hint: int | None,
                       *, reset_required: bool = False) -> int | None:
        menu = stage.menu()
        usable = [i for i, r in enumerate(menu)
                  if uses.get(i, 0) < r.max_uses and i != last
                  and (not reset_required or r.kind == "reset")
                  and (not r.legacy or stage.on_fail in ("retry_in_place", "abandon"))]
        if hint in usable:
            return hint
        return usable[0] if usable else None

    def _attempt(self, stage: Stage, verifier: Verifier, attempt: int, prompt: str, last_trip: int | None) -> _Attempt:
        t_start = time.time()
        try:
            out = self._watch(stage, verifier, attempt, last_trip)
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
            if rec.legacy and not self.plan.match:
                self._issue(rec.prompt, f"recovery {rec.name}")
                self._hold(min(stage.timeout_s / 2, 20.0))
            elif rec.kind == "reset":
                self._issue(rec.prompt, f"recovery {rec.name}")
                ok, reason = self._reset_scene(stage, rec, verifier)
                outcome = "ok" if ok else "scene_not_ok"
            else:
                reason = "retry: stage prompt re-issued"
        except BaseException as exc:
            self.log.segment(stage=stage.name, attempt=attempt, kind="recovery", name=rec.name,
                             prompt=rec.prompt or stage.prompt, t_start=t_start, t_end=time.time(),
                             outcome="aborted", reason=f"{type(exc).__name__}: {exc}", max_progress=None)
            raise
        self.log.segment(stage=stage.name, attempt=attempt, kind="recovery", name=rec.name,
                         prompt=rec.prompt or stage.prompt, t_start=t_start, t_end=time.time(),
                         outcome=outcome, reason=reason, max_progress=None)
        return ok, reason

    def _entry_check(self, stage: Stage, verifier: Verifier) -> bool:
        """skip_if_done: one synchronous look before the prompt is sent."""
        frames = self._frames_for_check(stage)
        verdict = self._ask(verifier, stage, frames, 0.0, sync=True)
        self._check_global()
        done = verdict.done is True and (verdict.immediate or (frames and verdict.confidence >= SKIP_MIN_CONFIDENCE))
        self.log.event("entry_check", stage=stage.name, done=verdict.done, confidence=verdict.confidence,
                       reason=verdict.reason, skipped=bool(done))
        return bool(done)

    def _run_stage(self, stage: Stage, first: bool) -> StageResult:
        started = self._clock()
        deadline = self._work_end - stage.reserve_after_s
        if stage.stage_budget_s is not None:
            deadline = min(deadline, started + stage.stage_budget_s)
        active = _StageRun(stage, started, deadline)
        self._active = active
        try:
            return self._run_stage_attempts(active, first)
        except _StageExpired:
            reason = ("stage time budget exhausted" if active.attempts else
                      "insufficient time for the stage after its reserve")
            return self._exhausted(active, reason)
        finally:
            self._active = None

    def _abandon(self, active: _StageRun, reason: str) -> StageResult:
        self.log.event("stage_abandoned", stage=active.stage.name, attempts=active.attempts, reason=reason)
        return StageResult(active.stage.name, False, active.attempts, self._clock() - active.started,
                           reason, abandoned=True)

    def _exhausted(self, active: _StageRun, reason: str) -> StageResult:
        if active.stage.on_fail == "abandon":
            return self._abandon(active, reason)
        raise _Fail(f"stage {active.stage.name!r} failed after {active.attempts} attempts: {reason}")

    def _run_stage_attempts(self, active: _StageRun, first: bool) -> StageResult:
        stage, t0 = active.stage, active.started
        remaining = self._check_global()
        if stage.on_fail == "abandon" and remaining <= max(stage.min_run_s, stage.effective_settle()):
            return self._abandon(active, "insufficient time for a verification check")
        verifier = self.verifiers.get(stage.verifier)
        if verifier is None:
            raise _Fail(f"stage {stage.name!r}: no {stage.verifier!r} verifier configured")
        self._last_progress = None
        self.log.event("stage_start", stage=stage.name, prompt=stage.prompt)
        if stage.skip_if_done and self._entry_check(stage, verifier):
            self.log.event("stage_already_done", stage=stage.name)
            return StageResult(stage.name, True, 0, self._clock() - t0, "already done at entry")
        require_new_trip = bool(stage.required_cameras)
        active.attempts = 1
        last_trip = self._issue(stage.prompt, "stage", force_start=first, require_new_trip=require_new_trip)
        uses: dict[int, int] = {}
        last_rec: int | None = None
        while True:
            attempts = active.attempts
            out = self._attempt(stage, verifier, attempts, stage.prompt, last_trip)
            if out.ok:
                return StageResult(stage.name, True, attempts, self._clock() - t0, out.reason)
            reason, hint = out.reason, out.recovery_hint
            if stage.on_fail == "abort":
                raise _Fail(f"stage {stage.name!r} failed (on_fail abort): {reason}")
            if stage.on_fail == "skip":
                self.log.event("stage_skipped", stage=stage.name, reason=reason)
                return StageResult(stage.name, False, attempts, self._clock() - t0, reason)
            reset_required = False
            while True:   # choose a recovery; a reset that leaves the scene broken burns an attempt
                if attempts >= stage.max_attempts:
                    return self._exhausted(active, reason)
                remaining = self._check_global()
                if stage.on_fail == "abandon" and remaining <= max(stage.min_run_s, stage.effective_settle()):
                    return self._abandon(active, "insufficient time to verify another attempt")
                idx = self._pick_recovery(stage, uses, last_rec, hint, reset_required=reset_required)
                if idx is None:
                    if reset_required:
                        return self._exhausted(active, reason)
                    break
                uses[idx] = uses.get(idx, 0) + 1
                last_rec = idx
                scene_ok, rreason = self._recover(stage, verifier, attempts, idx)
                if scene_ok:
                    break
                reset_required = self.plan.match is not None
                attempts += 1
                active.attempts = attempts
                reason, hint = rreason, None
                self.log.event("attempt_end", stage=stage.name, attempt=attempts, ok=False, reason=rreason)
            self._check_global()
            active.attempts = attempts + 1
            if idx is None and stage.on_fail == "home_and_retry":
                self.log.event("retry", stage=stage.name, attempt=attempts + 1, mode="home_and_retry")
                self.station.stop()
                self._wait({"idle"}, IDLE_TIMEOUT_S, "idle after stop")
                self.station.home()
                self._wait({"idle"}, IDLE_TIMEOUT_S, "idle after home")
                last_trip = self._issue(stage.prompt, "retry", force_start=True, require_new_trip=require_new_trip)
            else:
                last_trip = self._issue(stage.prompt, "retry", require_new_trip=require_new_trip)
                self.log.event("retry", stage=stage.name, attempt=attempts + 1, mode="retry_in_place")
