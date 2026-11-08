"""Hedged calls: send the same request N times at once and use the first good answer.

Model APIs (Claude, Dream-1) have long latency tails: a Haiku 5.5 monitor call took 6.5-13 s for the same request
(8 Nov). With N copies in flight, the answer arrives at the minimum of N draws instead of one draw, so the tail
shrinks; the floor (the fastest a call can be) does not move. Cost and rate-limit use grow N-fold.

`first_of` is generic: any function of no arguments that returns a result or raises. The losing calls keep running
in their daemon threads and their results are dropped (HTTP calls cannot be cancelled mid-flight from here).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass
class HedgeResult(Generic[T]):
    value: T | None
    winner: int | None              # which copy answered first (0-based), None if none was good
    latency_s: float
    errors: list[str] = field(default_factory=list)
    answered: int = 0               # copies that had finished when the winner was taken


def first_of(fn: Callable[[], T], n: int, *, timeout_s: float = 60.0,
             good: Callable[[T], bool] | None = None, stagger_s: float = 0.0,
             clock: Callable[[], float] = time.monotonic) -> HedgeResult[T]:
    """Run `fn` `n` times in parallel; return the first result that `good` accepts (default: any result that did
    not raise). If every copy fails or is rejected, returns the last good-or-not result with winner None.
    `stagger_s` delays copy k by k*stagger_s (a smaller extra load when the first copy is usually fast)."""
    n = max(1, int(n))
    t0 = clock()
    if n == 1:
        try:
            v = fn()
            return HedgeResult(v, 0 if good is None or good(v) else None, clock() - t0, [], 1)
        except Exception as exc:  # noqa: BLE001
            return HedgeResult(None, None, clock() - t0, [f"{type(exc).__name__}: {exc}"], 1)
    cond = threading.Condition()
    state: dict[str, Any] = {"done": 0, "winner": None, "value": None, "fallback": None, "errors": []}

    def run(k: int) -> None:
        if stagger_s > 0 and k:
            time.sleep(k * stagger_s)
            with cond:
                if state["winner"] is not None:
                    return               # someone already answered: don't spend the call
        try:
            v = fn()
            ok = good is None or good(v)
            err = None
        except Exception as exc:  # noqa: BLE001
            v, ok, err = None, False, f"{type(exc).__name__}: {exc}"
        with cond:
            state["done"] += 1
            if err:
                state["errors"].append(err)
            elif not ok and state["fallback"] is None:
                state["fallback"] = v
            if ok and state["winner"] is None:
                state["winner"], state["value"] = k, v
            cond.notify_all()

    for k in range(n):
        threading.Thread(target=run, args=(k,), name=f"hedge-{k}", daemon=True).start()
    deadline = t0 + timeout_s
    with cond:
        while state["winner"] is None and state["done"] < n:
            left = deadline - clock()
            if left <= 0:
                break
            cond.wait(min(left, 0.05))
        w = state["winner"]
        value = state["value"] if w is not None else state["fallback"]
        return HedgeResult(value, w, clock() - t0, list(state["errors"]), state["done"])
