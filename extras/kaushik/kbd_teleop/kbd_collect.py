"""dhk-collect with a keyboard in place of the Quest: same IK, speed caps, safety and dataset writer (the kit's own).

On the station (in tmux), instead of `uv run dhk-collect ...`:
    cd ~/physical-ai-hacks && .venv/bin/python ~/pah_keyboard/kbd_collect.py --config configs/station.yaml \
        --dataset-dir ~/hackathon_data/unplug-v1 --task "..." [--dry-run]
Then on the laptop: `ssh -L 8095:localhost:8095 pah@<station>` and `python3 kbd_client.py` (it shows the keys).

The keyboard is a stand-in "Quest": it listens on 127.0.0.1:8095 and turns keys into controller poses, grip, trigger
and buttons. Moves are in the arm's own frame (x forward, y left, z up), mapped back through each arm's
`quest_to_base`, so the directions are right whichever config is loaded. Nothing else of the kit is changed.
"""

import socket
import threading
import time

import numpy as np

from dream_hackathon import collect, devices, quest

PORT = 8095
STEPS_M = (0.002, 0.005, 0.01)
STEP_DEG = 3.0


def _rot(axis, deg):
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    x, y, z = axis
    return np.array([[c + x * x * (1 - c), x * y * (1 - c) - z * s, x * z * (1 - c) + y * s],
                     [y * x * (1 - c) + z * s, c + y * y * (1 - c), y * z * (1 - c) - x * s],
                     [z * x * (1 - c) - y * s, z * y * (1 - c) + x * s, c + z * z * (1 - c)]])


class Keyboard:
    """quest.Quest's interface (read, data_age_s, reconnect, close), driven by keys over TCP."""

    MOVE = {"w": (1, 0, 0), "s": (-1, 0, 0), "a": (0, 1, 0), "d": (0, -1, 0), "r": (0, 0, 1), "f": (0, 0, -1)}
    TURN = {"j": ((1, 0, 0), 1), "l": ((1, 0, 0), -1), "i": ((0, 1, 0), 1), "k": ((0, 1, 0), -1),
            "u": ((0, 0, 1), 1), "o": ((0, 0, 1), -1)}

    def __init__(self, station):
        self._m = {side: np.asarray(arm.quest_to_base, dtype=float) for side, arm in station.arms.items()}
        self._pose = {side: np.eye(4) for side in self._m}
        self._grip = {side: 0.0 for side in self._m}
        self._trig = {side: 0.0 for side in self._m}
        self._side, self._step = ("right" if "right" in self._m else next(iter(self._m))), 1
        self._pressed: set[str] = set()
        self._lock = threading.Lock()
        self._connected, self._last = False, 0.0
        self._srv = socket.create_server(("127.0.0.1", PORT), reuse_port=True)
        threading.Thread(target=self._serve, daemon=True).start()

    # -- quest.Quest's interface
    def read(self) -> quest.QuestState:
        with self._lock:
            pressed, self._pressed = frozenset(self._pressed), set()
            return quest.QuestState(poses={s: p.copy() for s, p in self._pose.items()}, grip=dict(self._grip),
                                    trigger=dict(self._trig), pressed=pressed)

    def data_age_s(self) -> float:
        return 0.0 if self._connected else float("inf")

    def reconnect(self) -> None:
        pass

    def close(self) -> None:
        self._srv.close()

    # -- keys
    def _serve(self):
        while True:          # one thread per connection: the operator's panel and a helper can both send keys
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
            self._clients = getattr(self, "_clients", 0) + 1
            self._connected = True
            conn.sendall(b"EVENT connected\n")
            try:
                conn.sendall((self._state_line() + "\n").encode())
                while chunk := conn.recv(64):
                    for ch in chunk.decode(errors="ignore"):
                        msg = self._key(ch)
                        if msg:
                            conn.sendall(("EVENT " + msg + "\n").encode())
                        conn.sendall((self._state_line() + "\n").encode())
            except OSError:
                pass
            finally:
                self._clients -= 1
                self._connected = self._clients > 0
                if not self._connected:
                    with self._lock:   # the last client gone: no drive, gripper as it was
                        for s in self._grip:
                            self._grip[s] = 0.0

    def _state_line(self):
        with self._lock:
            sd = self._side
            return (f"STATE arm={sd} drive={'on' if self._grip[sd] > 0.5 else 'off'} "
                    f"gripper={self._trig[sd]:.2f} step_mm={STEPS_M[self._step] * 1000:.0f}")

    def _key(self, ch):
        with self._lock:
            side, m = self._side, self._m[self._side]
            if ch in self.MOVE:
                d = np.asarray(self.MOVE[ch], float) * STEPS_M[self._step]
                self._pose[side][:3, 3] += m.T @ d            # base-frame step -> Quest-frame step
                return None
            if ch in self.TURN:
                axis, sign = self.TURN[ch]
                r_base = _rot(axis, sign * STEP_DEG)
                self._pose[side][:3, :3] = (m.T @ r_base @ m) @ self._pose[side][:3, :3]
                return None
            if ch == " ":
                self._grip[side] = 0.0 if self._grip[side] > 0.5 else 1.0
                return f"{side}: {'DRIVING' if self._grip[side] else 'released (arm holds)'}"
            if ch == "g":
                self._trig[side] = 0.0 if self._trig[side] > 0.5 else 1.0
                return f"{side} gripper {'CLOSE' if self._trig[side] else 'OPEN'}"
            if ch in "[]":
                self._trig[side] = float(np.clip(self._trig[side] + (0.25 if ch == "]" else -0.25), 0, 1))
                return f"{side} gripper close {self._trig[side]:.2f}"
            if ch in "\r\n":
                self._pressed.add("A")
                return "A (start / save episode; at the start: open the arms)"
            if ch in "\x7fx":
                self._pressed.add("B")
                return "B (discard episode)"
            if ch == "h":
                self._pressed.add("RJ")
                return "home (back to where the session started; not while recording)"
            if ch in "12":
                want = "left" if ch == "1" else "right"
                if want in self._m:
                    self._grip[side] = 0.0
                    self._side = want
                return f"now driving the {self._side} arm (press space to drive)"
            if ch in "-=":
                self._step = max(0, min(len(STEPS_M) - 1, self._step + (1 if ch == "=" else -1)))
                return f"step {STEPS_M[self._step] * 1000:.0f} mm"
        return None


class _KeyboardDevices(devices.DeviceSet):
    def __init__(self, station, *, sim=False, quest_ip=None, **kw):
        super().__init__(station, sim=sim, quest_ip="keyboard", open_quest=lambda _ip: Keyboard(station), **kw)


devices.DeviceSet = _KeyboardDevices   # collect builds devices.DeviceSet(...) at run time

if __name__ == "__main__":
    collect.main()
