"""Live joint telemetry from the arms' CAN buses on the station NUC: what the Dream API never returns.

Receive-only (it never sends a frame): Dream's agent drives each Metal arm over SocketCAN (`can_follower_l`,
`can_follower_r`, station.log), and Linux delivers every frame on a link to every raw socket bound to it, so this
sees both directions:

- Dream's MIT command to motor i (arbitration id i): target position, target velocity, kp, kd, feed-forward torque
  (robots/damiao.py encode_mit, dreamlabs 1.0.37);
- the motor's reply (id 0x10 + i): measured position, velocity, torque (from phase current), temperatures, status.

From the pair it computes the torque the controller commands, `kp (q_cmd - q) + kd (dq_cmd - dq) + tau_ff`, next to
the torque the motor reports. Served as JSON, latest values plus a short history:

    python3 harness/can_live.py --serve 0.0.0.0:8097 --arm left=can_follower_l --arm right=can_follower_r
    GET /joints                 {"t", "rate_hz": {arm}, "arms": {arm: {joint: {...}}}}
    GET /joints/history?s=2     the last s seconds, one row per arm per reply cycle (decimated to <= 100 Hz)

Standard library only (Python 3.8+ on Linux), so it runs on the NUC with nothing installed: copy this file and
harness/can_torque.py next to it. The gripper runs in force-position mode (commands at 0x300 + id), whose frames
are kept raw; its reply is decoded like any joint's. A CANable in slcan (serial) mode is exclusive to Dream's
agent: there is nothing to listen to then, and this says so.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

try:
    from harness.can_torque import LIMITS, MOTORS, REPLY_OFFSET, decode as decode_reply
except ImportError:                                  # run as a lone script next to can_torque.py on the NUC
    from can_torque import LIMITS, MOTORS, REPLY_OFFSET, decode as decode_reply  # type: ignore

KP_RANGE, KD_RANGE = (0.0, 500.0), (0.0, 5.0)        # robots/damiao.py
FORCE_POSITION_OFFSET = 0x300                        # robots/damiao.py MODE_OFFSETS[MODE_FORCE_POSITION]


def _u2f(x: int, lo: float, hi: float, bits: int) -> float:
    return x * (hi - lo) / ((1 << bits) - 1) + lo


def decode_mit(data: bytes, model: str) -> dict[str, float]:
    """Inverse of robots.damiao.encode_mit: [q16, dq12, kp12, kd12, tau12]."""
    pmax, vmax, tmax = LIMITS[model]
    q = (data[0] << 8) | data[1]
    dq = (data[2] << 4) | (data[3] >> 4)
    kp = ((data[3] & 0x0F) << 8) | data[4]
    kd = (data[5] << 4) | (data[6] >> 4)
    tau = ((data[6] & 0x0F) << 8) | data[7]
    return {"q_cmd_deg": math.degrees(_u2f(q, -pmax, pmax, 16)), "dq_cmd_deg_s": math.degrees(_u2f(dq, -vmax, vmax, 12)),
            "kp": _u2f(kp, *KP_RANGE, 12), "kd": _u2f(kd, *KD_RANGE, 12), "tau_ff_nm": _u2f(tau, -tmax, tmax, 12)}


class ArmState:
    """Latest command and reply per joint of one arm, plus a short history."""

    def __init__(self, arm: str, history_s: float = 10.0):
        self.arm = arm
        self.joints: dict[str, dict] = {name: {} for name, _ in MOTORS.values()}
        self.lock = threading.Lock()
        self.history: deque = deque(maxlen=int(history_s * 100))
        self._replies: deque = deque(maxlen=400)
        self._last_row_t = 0.0

    def feed(self, can_id: int, data: bytes, t: float) -> None:
        if len(data) != 8:
            return
        if can_id in MOTORS:                                         # Dream's MIT command
            name, model = MOTORS[can_id]
            if data == b"\xff" * 7 + data[7:]:                       # enable / disable / zero, not a command
                return
            with self.lock:
                self.joints[name].update(decode_mit(data, model), t_cmd=t)
        elif can_id - FORCE_POSITION_OFFSET in MOTORS:               # gripper force-position command: raw
            name, _ = MOTORS[can_id - FORCE_POSITION_OFFSET]
            with self.lock:
                self.joints[name].update(fp_cmd_hex=data.hex(), t_cmd=t)
        elif can_id - REPLY_OFFSET in MOTORS and (data[0] & 0x0F) == can_id - REPLY_OFFSET:
            name, model = MOTORS[can_id - REPLY_OFFSET]
            r = decode_reply(data, model)
            with self.lock:
                j = self.joints[name]
                j.update(q_deg=r["pos_deg"], dq_deg_s=r["vel_deg_s"], tau_nm=r["torque_nm"], status=r["status"],
                         temp_mos_c=r["temp_mos_c"], temp_rotor_c=r["temp_rotor_c"], t_reply=t,
                         tau_max_nm=LIMITS[model][2])
                if "kp" in j:
                    j["tau_cmd_nm"] = (j["kp"] * math.radians(j["q_cmd_deg"] - j["q_deg"])
                                       + j["kd"] * math.radians(j["dq_cmd_deg_s"] - j["dq_deg_s"]) + j["tau_ff_nm"])
                self._replies.append(t)
                if t - self._last_row_t >= 0.01:                     # history at <= 100 Hz
                    self._last_row_t = t
                    self.history.append({"t": round(t, 4), **{
                        n: [round(v.get(k, float("nan")), 3) for k in ("q_deg", "q_cmd_deg", "tau_nm", "tau_cmd_nm")]
                        for n, v in self.joints.items() if "q_deg" in v}})

    def snapshot(self) -> dict:
        with self.lock:
            return {n: {k: (round(v, 4) if isinstance(v, float) else v) for k, v in j.items()}
                    for n, j in self.joints.items()}

    def rate_hz(self, now: float) -> float:
        with self.lock:
            recent = [t for t in self._replies if now - t <= 1.0]
        return round(len(recent) / len(MOTORS), 1)          # reply cycles per second (one reply per motor each)


def listen(channel: str, arm: ArmState, stop: threading.Event) -> None:
    """Receive-only raw SocketCAN loop. Never sends."""
    import socket

    if not hasattr(socket, "AF_CAN"):
        raise SystemExit("SocketCAN needs Linux: run this on the station NUC.")
    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    sock.bind((channel,))
    sock.settimeout(0.5)
    while not stop.is_set():
        try:
            frame = sock.recv(16)
        except socket.timeout:
            continue
        can_id, dlc = struct.unpack("=IB3x", frame[:8])
        arm.feed(can_id & 0x7FF, frame[8:8 + dlc], time.time())


def serve(arms: dict[str, ArmState], host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def do_GET(self):
            url = urlsplit(self.path)
            now = time.time()
            if url.path == "/joints":
                body = {"t": now, "rate_hz": {a: s.rate_hz(now) for a, s in arms.items()},
                        "arms": {a: s.snapshot() for a, s in arms.items()}}
            elif url.path == "/joints/history":
                secs = float(parse_qs(url.query).get("s", ["2"])[0])
                body = {"t": now, "arms": {a: [r for r in list(s.history) if now - r["t"] <= secs]
                                           for a, s in arms.items()}}
            else:
                self.send_error(404)
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--serve", default="0.0.0.0:8097", help="host:port for the JSON routes")
    p.add_argument("--arm", action="append", default=[], help="name=channel, e.g. right=can_follower_r")
    args = p.parse_args()
    pairs = dict(a.split("=", 1) for a in (args.arm or ["left=can_follower_l", "right=can_follower_r"]))
    arms = {name: ArmState(name) for name in pairs}
    stop = threading.Event()
    for name, channel in pairs.items():
        threading.Thread(target=listen, args=(channel, arms[name], stop), daemon=True).start()
    host, port = args.serve.rsplit(":", 1)
    serve(arms, host, int(port))
    print(f"can_live: listening (receive only) on {pairs}; GET http://{args.serve}/joints", flush=True)
    try:
        while True:
            time.sleep(5)
            now = time.time()
            print("rates Hz:", {a: s.rate_hz(now) for a, s in arms.items()}, flush=True)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
