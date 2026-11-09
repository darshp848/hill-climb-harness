"""Use the production monitor scheduler with ground-truth answers and measured latency."""
import hashlib
import random
import time

from experiments.sim_match import OracleVerifier, get
from harness.verifiers import MonitorVerifier


class DelayedOracle(MonitorVerifier):
    def __init__(self, plan, station_url, seed, latency=(6.5, 13.0)):
        super().__init__(plan, transport=lambda _: {})
        self.oracle = OracleVerifier(station_url)
        self.oracle._plan_guard = plan.guard
        self.rng = random.Random(seed)
        self.latency = latency
        self.calls = 0

    def _build(self, stage, frames, elapsed_s):
        payload = super()._build(stage, frames, elapsed_s)
        if payload is None:
            return None
        snap = get(self.oracle.url, "/objects")
        # Capture at request time, so a delayed verdict cannot see the future.
        self.oracle.start.setdefault(stage.name, snap["referee"])
        return {"stage": stage, "snapshot": snap, "elapsed": elapsed_s,
                "delay": self.rng.uniform(*self.latency),
                "signature": hashlib.sha256(b"".join(frames[k] for k in sorted(frames))).hexdigest()}

    def _call(self, payload, menu_len):
        self.calls += 1
        time.sleep(payload["delay"])
        verdict = self.oracle.check(payload["stage"], {}, payload["elapsed"], snapshot=payload["snapshot"])
        verdict.frames_sig = payload["signature"]
        return verdict
