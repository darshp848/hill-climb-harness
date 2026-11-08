"""Dream's async pacing in the sim (sim_station._async_control): 2 Hz calls, arms never hold for inference."""
import json
import time
import urllib.request

import pytest

from sim_station import SimStation


def call(sim, method, path, body=None):
    req = urllib.request.Request(sim.api_url + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read() or b"{}")


@pytest.mark.parametrize("latency, pipeline", [(0.05, 1), (0.4, 1), (0.9, 2), (1.3, 3)])
def test_async_pacing_calls_at_2hz_and_physics_keeps_running(latency, pipeline):
    """One session reaches 2 Hz while its round trip is under 0.5 s; past that, sessions in turn (pipeline)."""
    sim = SimStation(policy="scripted", scene="hill_climb", realtime=True, api_port=0, view_port=0,
                     scripted_latency_s=latency, execution_horizon=10)
    sim.dream_pipeline = pipeline
    sim = sim.start()
    try:
        assert call(sim, "POST", "/start", {"prompt": "go"})[0] == 200
        t0, sim_t0 = time.monotonic(), None
        while time.monotonic() - t0 < 8:
            time.sleep(0.25)
            with sim._lock:
                t = sim._poses.get("referee", {}).get("t") if sim._poses.get("referee") else None
            if sim_t0 is None and sim.dream_stats["replies"] >= 1:
                sim_t0, wall0 = t, time.monotonic()
        with sim._lock:
            sim_t1 = sim._poses["referee"]["t"]
        wall = time.monotonic() - wall0
        s = sim.dream_summary()
        assert s["mode"] == ("async" if pipeline == 1 else "pipelined") and s["errors"] == 0
        assert 1.6 <= s["call_hz"] <= 2.4, s                # a new chunk every ~0.5 s
        assert (sim_t1 - sim_t0) / wall > 0.85, (sim_t1 - sim_t0, wall)   # physics never paused for a reply
        assert s.get("dry_rows", 0) < 10, s                  # the next chunk lands before the current runs out
    finally:
        sim.close()


def test_sync_mode_is_unchanged_without_a_horizon():
    sim = SimStation(policy="scripted", scene="hill_climb", realtime=True, api_port=0, view_port=0)
    try:
        assert sim.execution_horizon is None and sim.dream_stats["mode"] == "sync"
    finally:
        sim.close()
