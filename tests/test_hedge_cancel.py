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


# ------------------------------------------------- the SDK's real Transport against a local fake gateway
transport = pytest.importorskip("dream.transport")


class Gateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.delays, self.requests, self.saw_disconnect = [], 0, threading.Event()

    def handle_error(self, request, client_address):
        pass                                     # a cut connection is the point of these tests


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        srv = self.server
        srv.requests += 1
        delay = srv.delays.pop(0) if srv.delays else 0.0
        end = time.monotonic() + delay
        while time.monotonic() < end:                    # holds the reply; notices the client going away
            ready, _, _ = select.select([self.connection], [], [], 0.01)
            if ready and not self.connection.recv(1, 2):  # MSG_PEEK: EOF
                srv.saw_disconnect.set()
                self.close_connection = True
                return
        body = json.dumps({"n": srv.requests}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Session:
    def __init__(self, port):
        self._transport = transport.Transport("127.0.0.1", port, 5000)

    def set_observation(self, *a, **k):
        pass

    def get_action(self):
        return self._transport.request({"x": 1}, {})

    def close(self):
        self._transport.close()


@pytest.fixture
def gateway():
    g = Gateway()
    threading.Thread(target=g.serve_forever, daemon=True).start()
    yield g
    g.shutdown()


def test_a_real_connection_is_cut_at_once_and_not_resent(gateway):
    port = gateway.server_address[1]
    slow, fast = Session(port), Session(port)
    hp = HedgedPolicy([slow, fast])
    gateway.delays[:] = [0.0, 0.0]
    hp.get_action()                              # both connections are now reused keep-alives
    time.sleep(0.2)
    before = gateway.requests
    gateway.delays[:] = [3.0, 0.02]              # the first request to land is slow, the second fast
    t = time.monotonic()
    assert hp.get_action() is not None and time.monotonic() - t < 2.0
    assert gateway.saw_disconnect.wait(1.0)      # the server really saw the connection close
    time.sleep(0.5)
    stopped = events(hp, "stopped")
    assert stopped and stopped[-1]["after_cut_s"] < 0.5
    assert gateway.requests == before + 2        # the cut call was not sent again by the SDK's retry
    hp.close()


def test_a_session_works_again_after_being_cut(gateway):
    port = gateway.server_address[1]
    hp = HedgedPolicy([Session(port), Session(port)])
    gateway.delays[:] = [3.0, 0.02]
    hp.get_action()
    gateway.delays[:] = [0.0, 0.0]
    time.sleep(0.2)
    assert hp.get_action() is not None           # the cut session reconnects for the next call
    assert sum(hp.wins) == 2
    hp.close()
