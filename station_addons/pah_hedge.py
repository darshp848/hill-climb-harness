"""Hedged Dream calls on the real station: N Dream clients fed the same observations, each get_action races them and
the first good reply drives the arms. Off unless PAH_HEDGE_N >= 2 in the station's environment.

8 Nov: single get_action calls took 7-12 s after a fresh start and the arm froze; a Dream Labs engineer advised firing
a new call every 1-2 s and using the first good answer. This is Darsh's HedgedPolicy (origin/cloud-sims a15d5bf,
written for sim_station.py) put in front of the station's own client: robots.run.open_client is wrapped so the
station's control loop gets a HedgedClient. Loaded from sitecustomize.py; Dream's files are untouched.

    PAH_HEDGE_N=2              clients (Dream sessions) per run; 1 or unset = off
    PAH_HEDGE_CUTOFF_S=1.5     a call older than this is cut and a fresh one sent (within the caps); unset = no cutoff
    PAH_HEDGE_MAX_CALLS=3      calls started per get_action at most
    PAH_HEDGE_MAX_IN_FLIGHT    calls running at once at most (default N)
    PAH_HEDGE_LOG=path.jsonl   one line per call started / finished / cancelled / stopped / dropped / error

Costs: up to MAX_CALLS x the Dream usage (a cut call may still be billed). Pacing (refill_due), the session id, the
round-trip hooks of the debug view and anything else the loop asks for come from the first client, and with an eef
checkpoint each client solves its own IK from the commanded joints. Tested offline only (tests/test_pah_hedge.py).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any


class HedgedPolicy:
    """N Dream sessions fed the same observations; each `get_action` races them and returns the first reply.

    The Dream API is input -> output: the same observation sent to N sessions gives N samples of the policy, and the
    one that comes back first is used (harness.hedge's idea applied to the action chunks). This cuts the latency
    tail of a round trip, not its floor, and costs N x the Dream usage. A session that errors is skipped while any
    other is still running; only if every session fails does the error reach the control loop.

    When a reply is taken the other calls are cancelled. Dream has no cancel route (only /v1/get_action and
    /v1/end_session), so cancelling means cutting the HTTP connection: it frees the session here at once, but the
    server may finish the work and bill it. With `cutoff_s`, a call older than that is cut and a fresh one sent, if
    `max_calls` and `max_in_flight` allow; the last call running is never cut without a replacement to send."""

    def __init__(self, sessions: list[Any], *, cutoff_s: float | None = None, max_in_flight: int | None = None,
                 max_calls: int | None = None, log: Any = None):
        self.sessions = sessions
        self._pool = ThreadPoolExecutor(max_workers=len(sessions), thread_name_prefix="dream-hedge")
        self.wins = [0] * len(sessions)
        self.last_winner: int | None = None
        self.cutoff_s = cutoff_s
        self.max_in_flight = max(1, min(max_in_flight or len(sessions), len(sessions)))
        self.max_calls = max(1, max_calls or len(sessions) + 1)     # per get_action
        self._log = log
        self.calls: deque[dict[str, Any]] = deque(maxlen=5000)      # started, finished, cancelled, stopped, dropped
        self.counts = {"started": 0, "finished": 0, "cancelled": 0, "stopped": 0, "dropped": 0, "applied": 0, "error": 0}
        self._note_lock = threading.Lock()
        self._busy: dict[int, Any] = {}
        self._live: dict[int, dict[str, Any]] = {}
        self._next_id = 0
        self._cut = [threading.Event() for _ in sessions]
        self._guard_resend()

    def set_observation(self, *args: Any, **kwargs: Any) -> None:
        for s in self.sessions:
            s.set_observation(*args, **kwargs)

    def _running(self) -> int:
        """Calls in flight that can still be used; a cut call no longer counts against the cap."""
        return sum(not r["cancelled"] for r in list(self._live.values()))

    def _note(self, rec: dict[str, Any], event: str, **more: Any) -> None:
        row = {"event": event, "call": rec["id"], "session": rec["k"], "t": time.monotonic(),
               "latency_s": round(time.monotonic() - rec["t0"], 4), **more}
        with self._note_lock:
            self.calls.append(row)
            self.counts[event] = self.counts.get(event, 0) + 1
        if self._log is not None:
            self._log(row)

    @staticmethod
    def _transport(session: Any) -> Any:
        """The SDK's Transport under a Dream session, or None for anything else (a scripted or fake session)."""
        for owner in (session, getattr(session, "_policy", None), getattr(session, "_client", None)):
            transport = getattr(owner, "_transport", None)
            if transport is not None and hasattr(transport, "_connection"):
                return transport
        return None

    def _guard_resend(self) -> None:
        """The SDK resends once when a reused connection dies before the reply, and a cut looks just like that:
        without this, a cancelled call would go out again as a new billed call."""
        for k, session in enumerate(self.sessions):
            transport = self._transport(session)
            if transport is None:
                continue
            try:
                from dream.errors import DreamProtocolError
                from dream.transport import _StaleConnectionError
            except ImportError:
                return

            def guarded(*args: Any, _send: Any = transport._exchange, _cut: Any = self._cut[k], **kwargs: Any) -> Any:
                try:
                    return _send(*args, **kwargs)
                except _StaleConnectionError:
                    if _cut.is_set():
                        raise DreamProtocolError("cancelled by the hedge") from None
                    raise
            transport._exchange = guarded

    def _abort(self, rec: dict[str, Any]) -> bool:
        """Cut the call's connection, so the blocked read fails now. True if a live socket was cut. Only the socket
        is touched from this thread: the call's own failure path drops the connection."""
        k = rec["k"]
        self._cut[k].set()
        connection = getattr(self._transport(self.sessions[k]), "_connection", None)
        sock = getattr(connection, "sock", None)
        if sock is None:
            return False
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            return False
        return True

    def _cancel(self, rec: dict[str, Any], why: str) -> None:
        if rec["cancelled"] or rec["future"].done():
            return
        rec["cancelled"] = True
        rec["cut_at"] = time.monotonic()
        self._note(rec, "cancelled", why=why, socket_cut=self._abort(rec))

    def _done(self, rec: dict[str, Any], future: Any) -> None:
        self._live.pop(rec["id"], None)
        if rec["cancelled"]:
            self._note(rec, "stopped", after_cut_s=round(time.monotonic() - rec["cut_at"], 4))

    def _idle(self) -> list[int]:
        return [k for k in range(len(self.sessions)) if k not in self._busy or self._busy[k].done()]

    def _fire(self, k: int, commanded: Any) -> dict[str, Any]:
        s = self.sessions[k]
        self._cut[k].clear()
        rec: dict[str, Any] = {"id": self._next_id, "k": k, "t0": time.monotonic(), "cancelled": False}
        self._next_id += 1
        f = self._pool.submit(s.get_action, commanded=commanded) if commanded is not None else \
            self._pool.submit(s.get_action)
        rec["future"] = f
        self._busy[k] = f
        self._live[rec["id"]] = rec
        self._note(rec, "started")
        f.add_done_callback(lambda fut: self._done(rec, fut))
        return rec

    def get_action(self, commanded: Any = None) -> Any:
        from concurrent.futures import FIRST_COMPLETED, wait

        # A Dream client runs one get_action at a time: a session whose call is still unwinding sits this round out
        # instead of raising "another get_action() is running".
        idle = self._idle()
        if not idle:                  # every session still busy: wait for the first to free up
            wait(list(self._busy.values()), return_when=FIRST_COMPLETED)
            idle = self._idle()
        room = max(1, self.max_in_flight - self._running())
        calls = [self._fire(k, commanded) for k in idle[:min(room, self.max_calls)]]
        started, owed = len(calls), 0
        first_error: BaseException | None = None
        running = {r["future"]: r for r in calls}
        while running or owed:
            now = time.monotonic()
            live = [r for r in running.values() if not r["cancelled"]]
            timeout = 0.02 if owed else None
            if not owed and self.cutoff_s is not None and live and started < self.max_calls:
                timeout = max(0.02, min(r["t0"] + self.cutoff_s for r in live) - now)
            done, _ = wait(set(running) or set(self._busy.values()), timeout=timeout, return_when=FIRST_COMPLETED)
            for f in [f for f in done if f in running]:
                rec = running.pop(f)
                if rec["cancelled"]:
                    self._note(rec, "dropped", why="reply or error of a cancelled call")
                    continue
                if f.exception() is not None:
                    self._note(rec, "error", error=type(f.exception()).__name__)
                    first_error = first_error or f.exception()
                    continue
                self._note(rec, "finished")
                for other in running.values():
                    self._cancel(other, "lost the race")
                self.wins[rec["k"]] += 1
                self.last_winner = rec["k"]
                self.counts["applied"] += 1
                return f.result()
            if self.cutoff_s is not None:
                now = time.monotonic()
                for rec in [r for r in running.values() if not r["cancelled"] and now - r["t0"] >= self.cutoff_s]:
                    if started + owed >= self.max_calls:
                        break                     # nothing left to replace it with: let it finish
                    if self._idle() or self._transport(self.sessions[rec["k"]]) is not None:
                        self._cancel(rec, f"older than the {self.cutoff_s} s cutoff")
                        owed += 1
            while owed and self._idle() and self._running() < self.max_in_flight:
                rec = self._fire(self._idle()[0], commanded)
                running[rec["future"]] = rec
                started += 1
                owed -= 1
        raise first_error if first_error is not None else RuntimeError("no Dream session answered")

    def stats(self) -> dict[str, Any]:
        """Counts for match.json. `started` is what the account may be billed for; `applied` is what was used."""
        with self._note_lock:
            return {**self.counts, "sessions": len(self.sessions), "cutoff_s": self.cutoff_s,
                    "max_in_flight": self.max_in_flight, "max_calls": self.max_calls}

    def close(self) -> None:
        for rec in list(self._live.values()):
            self._cancel(rec, "closing")
        for s in self.sessions:
            try:
                s.close()
            except Exception:
                pass
        self._pool.shutdown(wait=False, cancel_futures=True)


class HedgedClient(HedgedPolicy):
    """A stand-in for one dream.DreamClient in the station's control loop: set_observation, reset_episode and close go
    to every client, get_action races them, everything else is the first client's."""

    def __getattr__(self, name: str) -> Any:
        if name == "sessions" or name.startswith("__"):
            raise AttributeError(name)
        return getattr(self.sessions[0], name)

    def reset_episode(self, *args: Any, **kwargs: Any) -> None:
        for s in self.sessions:
            s.reset_episode(*args, **kwargs)


def install(run_module: Any = None, environ: dict[str, str] | None = None) -> bool:
    """Wraps run_module.open_client (default robots.run) when PAH_HEDGE_N >= 2. True if installed."""
    env = os.environ if environ is None else environ
    try:
        n = int(env.get("PAH_HEDGE_N", "1") or 1)
    except ValueError:
        n = 1
    if n < 2:
        return False
    if run_module is None:
        from robots import run as run_module  # noqa: PLC0415 (only on the station)
    if getattr(run_module.open_client, "_pah_hedged", False):
        return True
    num = (lambda k, kind: kind(env[k]) if env.get(k, "").strip() else None)
    cutoff, max_calls, max_in_flight = num("PAH_HEDGE_CUTOFF_S", float), num("PAH_HEDGE_MAX_CALLS", int), \
        num("PAH_HEDGE_MAX_IN_FLIGHT", int)
    log_path = env.get("PAH_HEDGE_LOG")
    lock = threading.Lock()

    def log(row: dict[str, Any]) -> None:
        if not log_path:
            return
        with lock, open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    original = run_module.open_client

    def open_client(dream_config: dict[str, Any]) -> Any:
        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = [ex.submit(original, dream_config) for _ in range(n)]
        clients, error = [], None
        for f in futures:
            try:
                clients.append(f.result())
            except BaseException as exc:  # noqa: BLE001
                error = error or exc
        if not clients:
            raise error  # type: ignore[misc]
        if len(clients) == 1:
            print(f"pah_hedge: only 1 of {n} Dream clients opened ({error}); running unhedged", file=sys.stderr)
            return clients[0]
        return HedgedClient(clients, cutoff_s=cutoff, max_in_flight=max_in_flight, max_calls=max_calls, log=log)

    open_client._pah_hedged = True  # type: ignore[attr-defined]
    run_module.open_client = open_client
    print(f"pah_hedge: {n} Dream clients per run, cutoff {cutoff} s, max calls {max_calls}", file=sys.stderr)
    return True
