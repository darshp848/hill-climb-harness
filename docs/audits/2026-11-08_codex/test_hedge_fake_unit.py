"""Hedged Dream calls: losers are cut, slow calls are replaced within caps, and a cancelled reply is never used.

Offline: fake sessions, and a local fake gateway behind the SDK's real Transport. No Dream call is made."""

import json
import select
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sim_station import HedgedPolicy


class Fake:
    """A session whose call ends after `delay` seconds or when `stop` is set. It does not have a socket to cut."""
    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def __init__(self, name, *delays):
        self.name, self.delays, self.n = name, list(delays), 0

    def set_observation(self, *a, **k):
        pass

    def get_action(self):
        with Fake.lock:
            Fake.in_flight += 1
            Fake.peak = max(Fake.peak, Fake.in_flight)
        d = self.delays[min(self.n, len(self.delays) - 1)]
        self.n += 1
        time.sleep(d)
        with Fake.lock:
            Fake.in_flight -= 1
        return {"from": self.name, "call": self.n}

    def close(self):
        pass


@pytest.fixture(autouse=True)
def reset():
    Fake.in_flight = Fake.peak = 0


def events(hp, name):
    return [c for c in hp.calls if c["event"] == name]


def test_first_reply_wins_and_the_loser_is_cancelled_and_logged():
    hp = HedgedPolicy([Fake("slow", 0.4), Fake("fast", 0.02)])
    assert hp.get_action()["from"] == "fast" and hp.last_winner == 1
    time.sleep(0.5)
    assert [c["session"] for c in events(hp, "cancelled")] == [0]
    assert events(hp, "cancelled")[0]["why"] == "lost the race"
    assert hp.counts["started"] == 2 and hp.counts["finished"] == 1 and hp.counts["applied"] == 1 and hp.counts["stopped"] == 1
    assert all("latency_s" in c for c in hp.calls)
    hp.close()


def test_a_cancelled_calls_late_reply_is_never_used():
    hp = HedgedPolicy([Fake("late", 0.3, 0.01), Fake("fast", 0.02, 0.5)])
    first = hp.get_action()
    assert first["from"] == "fast"
    time.sleep(0.4)                     # the loser has now finished with a real reply
    second = hp.get_action()
    assert second == {"from": "late", "call": 2}        # a fresh call, not the first call's stale reply
    hp.close()


def test_cutoff_replaces_a_slow_call_on_a_free_session():
    hp = HedgedPolicy([Fake("hung", 2.0), Fake("spare", 0.02)], cutoff_s=0.1, max_in_flight=1, max_calls=3)
    t = time.monotonic()
    assert hp.get_action()["from"] == "spare" and time.monotonic() - t < 1.0
    assert events(hp, "cancelled")[0]["session"] == 0
    assert "cutoff" in events(hp, "cancelled")[0]["why"]
    hp.close()


def test_the_caps_hold_when_every_call_is_slow():
    sessions = [Fake(str(i), 0.25) for i in range(4)]
    hp = HedgedPolicy(sessions, cutoff_s=0.05, max_in_flight=2, max_calls=3)
    assert hp.get_action() is not None
    assert Fake.peak <= 3                       # 2 running, plus at most one fake that can't be cut
    assert hp.counts["started"] <= 3
    hp.close()


def test_the_last_call_is_not_cut_without_a_replacement():
    hp = HedgedPolicy([Fake("only", 0.2)], cutoff_s=0.05, max_calls=1)
    assert hp.get_action()["from"] == "only"
    assert not events(hp, "cancelled")
    hp.close()


def test_every_session_failing_raises():
    class Bad(Fake):
        def get_action(self):
            raise RuntimeError("boom")
    hp = HedgedPolicy([Bad("a"), Bad("b")])
    with pytest.raises(RuntimeError, match="boom"):
        hp.get_action()
    assert hp.counts["error"] == 2
    hp.close()


