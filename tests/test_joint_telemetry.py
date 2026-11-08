"""Live joint telemetry: can_live decodes Dream's CAN frames; joint_telemetry turns torques into signals."""
import json
import math
import time
import urllib.request

import pytest

from harness.can_live import ArmState, decode_mit
from harness.can_torque import LIMITS
from harness.joint_telemetry import JointTelemetry, Thresholds


def _f2u(x, lo, hi, bits):
    x = min(max(x, lo), hi)
    return int((x - lo) * ((1 << bits) - 1) / (hi - lo))


def encode_mit(model, kp, kd, pos_deg, vel_deg_s, tau):
    """Same packing as robots.damiao.encode_mit (dreamlabs 1.0.37), used when that package is not installed."""
    pmax, vmax, tmax = LIMITS[model]
    q, dq = _f2u(math.radians(pos_deg), -pmax, pmax, 16), _f2u(math.radians(vel_deg_s), -vmax, vmax, 12)
    kp_u, kd_u, t = _f2u(kp, 0, 500, 12), _f2u(kd, 0, 5, 12), _f2u(tau, -tmax, tmax, 12)
    return bytes([q >> 8, q & 0xFF, dq >> 4, ((dq & 0xF) << 4) | (kp_u >> 8), kp_u & 0xFF, kd_u >> 4,
                  ((kd_u & 0xF) << 4) | (t >> 8), t & 0xFF])


def encode_reply(motor_id, model, pos_deg, vel_deg_s, tau, status=1):
    pmax, vmax, tmax = LIMITS[model]
    q, dq = _f2u(math.radians(pos_deg), -pmax, pmax, 16), _f2u(math.radians(vel_deg_s), -vmax, vmax, 12)
    t = _f2u(tau, -tmax, tmax, 12)
    return bytes([(status << 4) | motor_id, q >> 8, q & 0xFF, dq >> 4, ((dq & 0xF) << 4) | (t >> 8), t & 0xFF, 31, 35])


def test_decode_mit_inverts_dreams_encoder():
    try:
        from robots.damiao import encode_mit as dream_encode       # the real one, where installed
    except ImportError:
        dream_encode = encode_mit
    d = decode_mit(dream_encode("metal_j2", 40.0, 1.5, 30.0, -12.0, 4.5), "metal_j2")
    assert d["kp"] == pytest.approx(40.0, abs=0.2) and d["kd"] == pytest.approx(1.5, abs=0.01)
    assert d["q_cmd_deg"] == pytest.approx(30.0, abs=0.02) and d["dq_cmd_deg_s"] == pytest.approx(-12.0, abs=0.5)
    assert d["tau_ff_nm"] == pytest.approx(4.5, abs=0.1)


def test_arm_state_pairs_command_and_reply_into_commanded_torque():
    arm = ArmState("right")
    arm.feed(2, encode_mit("metal_j2", 40.0, 1.0, 30.0, 0.0, 2.0), 1.0)           # shoulder_lift command
    arm.feed(0x12, encode_reply(2, "metal_j2", 25.0, 0.0, 6.0), 1.001)            # its reply
    j = arm.snapshot()["shoulder_lift"]
    expected = (j["kp"] * math.radians(j["q_cmd_deg"] - j["q_deg"]) + j["kd"] * math.radians(j["dq_cmd_deg_s"] - j["dq_deg_s"])
                + j["tau_ff_nm"])                       # gains as decoded: 12 bits quantize kp to ~0.12 N.m/rad
    assert j["tau_cmd_nm"] == pytest.approx(expected, abs=2e-3) and j["tau_nm"] == pytest.approx(6.0, abs=0.1)
    assert j["temp_rotor_c"] == 35 and j["tau_max_nm"] == 120.0
    arm.feed(2, b"\xff" * 7 + b"\xfc", 1.002)                                      # enable frame: ignored
    assert arm.snapshot()["shoulder_lift"]["kp"] == pytest.approx(40.0, abs=0.2)


def _snap(grip_tau=0.0, lift_tau=0.0, lag=0.0, speed=10.0, tau_cmd=None):
    return {"arms": {"right": {
        "gripper": {"q_deg": 0.02, "q_cmd_deg": 0.0, "tau_nm": grip_tau, "tau_max_nm": 20.0},
        "shoulder_lift": {"q_deg": 10.0, "q_cmd_deg": 10.0 + lag, "dq_deg_s": speed, "tau_nm": lift_tau,
                          "tau_cmd_nm": lift_tau if tau_cmd is None else tau_cmd, "tau_max_nm": 120.0}}}}


def test_signals_need_consecutive_readings_and_are_worded_for_the_monitor():
    feed = iter([_snap(grip_tau=8.0)] * 2 + [_snap(grip_tau=1.0)] + [_snap(grip_tau=8.0)] * 3)
    events = []
    jt = JointTelemetry("http://x", fetch=lambda: next(feed), log=lambda *a, **k: events.append(k),
                        thresholds=Thresholds(persist=3))
    for _ in range(3):
        jt.poll_once()
    assert not jt.holding()["right"]                       # a dropout resets the streak
    for _ in range(3):
        jt.poll_once()
    assert jt.holding()["right"] and "squeezing an object" in jt.note()
    assert events and events[0]["kind"] == "holding" and jt.alerts()


@pytest.mark.parametrize("snap, kind", [
    (_snap(lift_tau=90.0), "strain"),
    (_snap(lag=15.0, speed=0.5), "blocked"),
    (_snap(lift_tau=50.0, tau_cmd=5.0), "contact"),
])
def test_strain_blocked_and_contact(snap, kind):
    jt = JointTelemetry("http://x", fetch=lambda: snap, thresholds=Thresholds(persist=1))
    jt.poll_once()
    assert kind in {k for k, *_ in jt._active}


def test_feed_down_gives_no_note():
    def boom():
        raise OSError("no route")
    jt = JointTelemetry("http://x", fetch=boom)
    jt.poll_once()
    assert jt.note() == "" and not jt.ok


def test_sim_serves_joints_in_can_live_shape():
    sim_station = pytest.importorskip("sim_station")
    try:
        sim = sim_station.SimStation(policy="scripted", scene="hill_climb", realtime=True, api_port=0,
                                     view_port=0).start()
    except SystemExit:
        pytest.skip("Dream's yam_mujoco example is not installed here")
    try:
        time.sleep(0.5)
        with urllib.request.urlopen(sim.api_url + "/joints", timeout=10) as r:
            snap = json.loads(r.read())
        for arm in ("left", "right"):
            joints = snap["arms"][arm]
            assert set(joints) == {"shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_yaw",
                                   "wrist_roll", "gripper"}
            assert all({"q_deg", "q_cmd_deg", "tau_nm", "tau_cmd_nm", "tau_max_nm"} <= set(j) for j in joints.values())
        jt = JointTelemetry(sim.api_url, thresholds=Thresholds(persist=1))
        jt.poll_once()
        assert jt.ok
    finally:
        sim.close()
