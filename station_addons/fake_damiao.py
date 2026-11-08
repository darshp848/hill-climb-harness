"""A fake CAN adapter with seven simulated Damiao motors behind it, for benching Dream's Metal driver offline.

It speaks the frames robots.damiao sends (MIT commands, state refreshes, register reads and writes, enable and
disable) and answers like the real arm as MakerMods measured it: motors 1-6 reply in ~0.25 ms, the gripper (id 7)
often holds its reply until the next frame appears on the bus. Each motor is a rigid joint (inertia, viscous
friction, gravity from Dream's own URDF model without the SDK's over-compensation factors) under the MIT law the
motor runs on board: tau = kp*(q* - q) + kd*(dq* - dq) + tau_ff, clipped to the motor's TMAX, integrated at 5 kHz.

Not a hardware substitute: it is for timing the driver's tick (Python + bus round trips) and comparing setpoint
rates on the same plant. Real inertias, friction and the USB-CAN adapter's latency differ.
"""

from __future__ import annotations

import math
import random
import struct
import threading
import time
from collections import deque
from typing import Any

from robots import damiao, metal, robstride

CAN = robstride.python_can()

# Rough reflected inertias (kg m^2) and viscous friction (N.m s/rad) of the Metal joints, base to gripper.
INERTIA = (0.12, 0.25, 0.10, 0.03, 0.01, 0.006, 0.004)
FRICTION = (0.4, 0.6, 0.4, 0.15, 0.05, 0.05, 0.05)
SIM_DT = 1e-3
JOINT_REPLY_S = 0.00025


class FakeMotor:
    def __init__(self, motor_id: int, model: str, q_deg: float) -> None:
        self.id, self.model = motor_id, model
        self.q, self.dq = math.radians(q_deg), 0.0
        self.tau = 0.0
        self.enabled = False
        self.cmd = (0.0, 0.0, self.q, 0.0, 0.0)  # kp kd q dq tau
        self.registers: dict[int, Any] = {
            damiao.REG_CTRL_MODE: damiao.MODE_MIT,
            damiao.REG_TIMEOUT: 0,
            damiao.REG_PMAX: damiao.MOTOR_LIMITS[model][0],
            damiao.REG_VMAX: damiao.MOTOR_LIMITS[model][1],
            damiao.REG_TMAX: damiao.MOTOR_LIMITS[model][2],
        }

    def state_frame(self) -> bytes:
        pmax, vmax, tmax = damiao.MOTOR_LIMITS[self.model]
        q = robstride.float_to_uint(self.q, -pmax, pmax, 16)
        dq = robstride.float_to_uint(self.dq, -vmax, vmax, 12)
        tau = robstride.float_to_uint(self.tau, -tmax, tmax, 12)
        status = damiao.STATUS_ENABLED if self.enabled else damiao.STATUS_DISABLED
        return bytes(
            [(status << 4) | self.id, q >> 8, q & 0xFF, dq >> 4, ((dq & 0xF) << 4) | (tau >> 8), tau & 0xFF, 30, 30]
        )

    def decode_mit(self, data: bytes) -> None:
        pmax, vmax, tmax = damiao.MOTOR_LIMITS[self.model]
        q = (data[0] << 8) | data[1]
        dq = (data[2] << 4) | (data[3] >> 4)
        kp = ((data[3] & 0xF) << 8) | data[4]
        kd = (data[5] << 4) | (data[6] >> 4)
        tau = ((data[6] & 0xF) << 8) | data[7]
        u = robstride.uint_to_float
        self.cmd = (
            u(kp, *damiao.KP_RANGE, 12),
            u(kd, *damiao.KD_RANGE, 12),
            u(q, -pmax, pmax, 16),
            u(dq, -vmax, vmax, 12),
            u(tau, -tmax, tmax, 12),
        )


class FakeMetalBus:
    """python-can's send/recv/shutdown over a simulated Metal arm."""

    def __init__(self, q0_deg: tuple[float, ...], *, gripper_late: float = 0.5, seed: int = 0,
                 external_nm: tuple[float, ...] | None = None) -> None:
        self.motors = {j.motor_id: FakeMotor(j.motor_id, j.model, q) for j, q in zip(metal.JOINTS, q0_deg)}
        self.pending: deque[tuple[float, int, bytes]] = deque()
        self.held: list[tuple[int, bytes]] = []  # gripper replies waiting for the next frame
        self.gripper_late = gripper_late
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.t_sim = time.perf_counter()
        self.frames_sent = 0
        self.external_nm = external_nm or (0.0,) * 7
        self.log: list[tuple[float, tuple[float, ...], tuple[float, ...]]] = []  # (t, q, q_cmd)

    # physics
    def _advance(self, now: float) -> None:
        if self.t_sim + SIM_DT > now:
            return
        q_deg = [math.degrees(m.q) for m in self.motors.values()][:6]
        g = metal.gravity_nm(tuple(q_deg))  # once per advance: gravity changes slowly over a few ms
        g_true = [gi / f for gi, f in zip(g, metal.GRAVITY_FACTORS)] + [0.0]
        motors = list(self.motors.values())
        while self.t_sim + SIM_DT <= now:
            for i, m in enumerate(motors):
                if m.enabled:
                    kp, kd, qs, dqs, tff = m.cmd
                    tmax = damiao.MOTOR_LIMITS[m.model][2]
                    m.tau = max(-tmax, min(tmax, kp * (qs - m.q) + kd * (dqs - m.dq) + tff))
                else:
                    m.tau = 0.0
                # gravity acts once the motor holds (before that the arm is taken as supported)
                load = g_true[i] if m.enabled else 0.0
                acc = (m.tau - FRICTION[i] * m.dq - load + self.external_nm[i]) / INERTIA[i]
                m.dq += acc * SIM_DT
                m.q += m.dq * SIM_DT
            self.t_sim += SIM_DT

    def _reply(self, motor: FakeMotor, data: bytes, now: float) -> None:
        rid = damiao.REPLY_OFFSET + motor.id
        if motor.id == 7 and self.rng.random() < self.gripper_late:
            self.held.append((rid, data))
        else:
            self.pending.append((now + JOINT_REPLY_S, rid, data))

    # python-can
    def send(self, msg: Any, timeout: float | None = None) -> None:
        now = time.perf_counter()
        with self.lock:
            self._advance(now)
            self.frames_sent += 1
            for rid, data in self.held:  # any frame releases a held gripper reply
                self.pending.append((now + JOINT_REPLY_S, rid, data))
            self.held = []
            aid, data = msg.arbitration_id, bytes(msg.data)
            if aid == damiao.PARAM_ID:
                mid, op = data[0] | (data[1] << 8), data[2]
                motor = self.motors.get(mid)
                if motor is None:
                    return
                if op == damiao.CMD_REFRESH:
                    self._reply(motor, motor.state_frame(), now)
                elif op in (damiao.OP_READ, damiao.OP_WRITE):
                    rid = data[3]
                    fmt = "f" if rid in (damiao.REG_PMAX, damiao.REG_VMAX, damiao.REG_TMAX) else "I"
                    if op == damiao.OP_WRITE:
                        motor.registers[rid] = struct.unpack("<" + fmt, data[4:8])[0]
                    value = motor.registers.get(rid, 0)
                    self._reply(motor, data[:4] + struct.pack("<" + fmt, value), now)
                return
            mid, offset = aid & 0xFF, aid & 0xF00
            motor = self.motors.get(mid)
            if motor is None:
                return
            if data[:7] == b"\xff" * 7:
                motor.enabled = data[7] == damiao.CMD_ENABLE
                if motor.enabled:
                    motor.cmd = (0.0, 0.0, motor.q, 0.0, 0.0)
            elif offset == 0:
                motor.decode_mit(data)
                if mid == 1:
                    self.log.append((now, tuple(m.q for m in self.motors.values()),
                                     tuple(m.cmd[2] for m in self.motors.values())))
            self._reply(motor, motor.state_frame(), now)

    def recv(self, timeout: float | None = None) -> Any:
        deadline = time.perf_counter() + (timeout or 0.0)
        while True:
            now = time.perf_counter()
            with self.lock:
                if self.pending and self.pending[0][0] <= now:
                    self._advance(now)
                    _, rid, data = self.pending.popleft()
                    return CAN.Message(arbitration_id=rid, data=data, is_extended_id=False)
            if now >= deadline:
                return None
            time.sleep(min(5e-5, max(0.0, deadline - now)))

    def shutdown(self) -> None:
        pass
