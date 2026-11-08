"""experiments/sim_match.SimClock: the harness runs on simulated time, so a slow sim doesn't shrink the match."""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "experiments"))
import sim_match  # noqa: E402


class Sim:
    """/objects with a sim time running at `rate` x wall time; `reset()` restarts it at 0."""

    def __init__(self, rate):
        self.rate, self.t0 = rate, time.monotonic()
        sim = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"t": (time.monotonic() - sim.t0) * sim.rate}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def reset(self):
        self.t0 = time.monotonic()


def test_sleep_waits_for_sim_time_and_the_clock_survives_a_reset():
    sim = Sim(rate=0.25)
    clock = sim_match.SimClock(sim.url)
    try:
        w0, c0 = time.monotonic(), clock()
        clock.sleep(0.2)                          # 0.2 s of sim time = ~0.8 s of wall time at 0.25x
        assert time.monotonic() - w0 >= 0.6 and clock() - c0 >= 0.2
        before = clock()
        sim.reset()
        time.sleep(0.4)
        assert clock() >= before                  # never runs backwards
    finally:
        clock.close()
        sim.srv.shutdown()


def test_a_stalled_sim_cannot_hang_the_harness():
    sim = Sim(rate=0.0)
    clock = sim_match.SimClock(sim.url, wall_cap=2.0)
    try:
        w0 = time.monotonic()
        clock.sleep(0.2)                          # the sim never advances: give up after 2 x 0.2 s of wall time
        assert time.monotonic() - w0 < 1.5
    finally:
        clock.close()
        sim.srv.shutdown()
