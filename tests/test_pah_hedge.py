"""station_addons/pah_hedge.py: hedged Dream calls on the real station, offline (fake clients, a fake robots.run)."""

import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "station_addons"))

import pah_hedge  # noqa: E402


class Client:
    """A stand-in dream.DreamClient: get_action after `delay` s; pacing and session id are its own."""

    def __init__(self, name, delay):
        self.name, self.delay, self.obs, self.resets, self.closed = name, delay, 0, 0, False
        self.session_id = f"sess-{name}"

    def set_observation(self, frames, state, *, timestamp):
        self.obs += 1

    def reset_episode(self):
        self.resets += 1

    def get_action(self, *, commanded=None):
        time.sleep(self.delay)
        return {"from": self.name, "commanded": commanded}

    def refill_due(self, done, remaining):
        return self.name == "a"

    def close(self):
        self.closed = True


def _run_module(delays):
    made = iter([Client(n, d) for n, d in zip("abc", delays)])
    mod = types.SimpleNamespace(opened=[])

    def open_client(cfg):
        c = next(made)
        mod.opened.append((c, cfg))
        return c

    mod.open_client = open_client
    return mod


def test_off_unless_two_or_more():
    mod = _run_module([0.0])
    orig = mod.open_client
    assert not pah_hedge.install(mod, {"PAH_HEDGE_N": "1"}) and mod.open_client is orig
    assert not pah_hedge.install(mod, {}) and mod.open_client is orig


def test_open_client_races_the_clients_like_one_client(tmp_path):
    mod = _run_module([0.3, 0.01])
    log = tmp_path / "hedge.jsonl"
    assert pah_hedge.install(mod, {"PAH_HEDGE_N": "2", "PAH_HEDGE_LOG": str(log)})
    assert pah_hedge.install(mod, {"PAH_HEDGE_N": "2"})            # installing twice wraps once
    client = mod.open_client({"checkpoint": "dreamlabs/dream-1-beta"})
    assert len(mod.opened) == 2 and all(cfg["checkpoint"] == "dreamlabs/dream-1-beta" for _, cfg in mod.opened)
    client.set_observation({}, {}, timestamp=1.0)
    client.reset_episode()
    assert all(c.obs == 1 and c.resets == 1 for c, _ in mod.opened)  # every client sees every observation
    out = client.get_action(commanded={"q": [0.1]})
    assert out == {"from": "b", "commanded": {"q": [0.1]}}           # the fast one drives the arms
    assert client.session_id == "sess-a" and client.refill_due(0, 10) is True   # the rest is the first client's
    client.close()
    assert all(c.closed for c, _ in mod.opened)
    assert '"event": "started"' in log.read_text()


def test_cutoff_fires_a_fresh_call_when_the_first_is_slow():
    clients = [Client("a", 0.5), Client("b", 0.5)]
    hp = pah_hedge.HedgedClient(clients, cutoff_s=0.05, max_in_flight=1, max_calls=2)
    t0 = time.monotonic()
    hp.get_action()
    assert hp.counts["started"] == 2 and time.monotonic() - t0 < 0.9
    hp.close()


def test_one_client_failing_to_open_runs_unhedged():
    mod = types.SimpleNamespace()
    good = Client("a", 0.0)
    calls = iter([good, RuntimeError("quota")])

    def open_client(cfg):
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return v

    mod.open_client = open_client
    pah_hedge.install(mod, {"PAH_HEDGE_N": "2"})
    assert mod.open_client({}) is good
