"""station_addons/pah_tuning.py: off by default, the maths, and (with dreamlabs 1.0.37 installed) the patches."""

import importlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

ADDONS = Path(__file__).resolve().parents[1] / "station_addons"
sys.path.insert(0, str(ADDONS))
import pah_tuning  # noqa: E402


def _clear(monkeypatch):
    for name in list(os.environ):
        if name.startswith("PAH_"):
            monkeypatch.delenv(name, raising=False)


def test_off_by_default(monkeypatch):
    _clear(monkeypatch)
    assert not pah_tuning.Settings.from_env().active()
    assert pah_tuning.install() is None


def test_settings_parse_and_refuse(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("PAH_TORQUE_CAP_NM", "4")
    assert pah_tuning.Settings.from_env().torque_cap_nm == [4.0] * 6
    monkeypatch.setenv("PAH_METAL_TICK_HZ", "500")
    with pytest.raises(ValueError):
        pah_tuning.Settings.from_env()
    monkeypatch.setenv("PAH_METAL_TICK_HZ", "60")
    monkeypatch.setenv("PAH_TORQUE_CAP_NM", "1,2,3")
    with pytest.raises(ValueError):
        pah_tuning.Settings.from_env()
    assert pah_tuning.install() is None  # a bad setting patches nothing


def test_ff_alpha_keeps_time_constant():
    a60 = pah_tuning.ff_alpha_at(60.0, 0.04)
    # same decay per second: (1-a30)^30 == (1-a60)^60
    assert math.isclose((1 - 0.04) ** 30, (1 - a60) ** 60, rel_tol=1e-9)


def test_cap_goal():
    # kp 100 N.m/rad, cap 5 N.m -> at most 0.05 rad = 2.865 deg of error
    assert math.isclose(pah_tuning.cap_goal_deg(30.0, 0.0, 100.0, 5.0), math.degrees(0.05))
    assert math.isclose(pah_tuning.cap_goal_deg(-30.0, 10.0, 100.0, 5.0), 10.0 - math.degrees(0.05))
    assert pah_tuning.cap_goal_deg(1.0, 0.0, 100.0, 5.0) == 1.0  # inside the cap: untouched


def test_pull_settings_parse_and_refuse(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("PAH_PULL_FORCE_N", "r:20")
    with pytest.raises(ValueError):
        pah_tuning.Settings.from_env()  # a force needs a direction
    monkeypatch.setenv("PAH_PULL_DIR", "r:0,-2,0;l:tool")
    s = pah_tuning.Settings.from_env()
    assert s.pull_force_n == {"right": 20.0}
    assert s.pull_dir == {"right": [0.0, -1.0, 0.0], "left": "tool"}  # normalised
    assert (s.pull_max_s, s.pull_travel_m) == (3.0, 0.012)
    assert s.pull_tau_cap_nm == list(pah_tuning.PULL_TAU_CAP_NM)
    for bad in (("PAH_PULL_FORCE_N", "r:60"), ("PAH_PULL_DIR", "r:0,0,0"), ("PAH_PULL_MAX_S", "30"),
                ("PAH_PULL_TRAVEL_M", "0.2"), ("PAH_PULL_TAU_CAP_NM", "20")):
        monkeypatch.setenv(*bad)
        with pytest.raises(ValueError):
            pah_tuning.Settings.from_env()
        monkeypatch.delenv(bad[0])
        monkeypatch.setenv("PAH_PULL_FORCE_N", "r:20")
        monkeypatch.setenv("PAH_PULL_DIR", "r:0,-2,0;l:tool")
    monkeypatch.setenv("PAH_PULL_FORCE_N", "l:20")
    monkeypatch.setenv("PAH_PULL_DIR", "r:0,-1,0")
    with pytest.raises(ValueError):
        pah_tuning.Settings.from_env()  # the left arm has a force and no direction


# A planar test arm: six z-axis joints, 0.1 m apart along x (not the Metal; its own maths is checked below).
_TEST_LINKS = tuple(((0.1 if i else 0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), 1.0, (0.0, 0.0, 0.0))
                    for i in range(6))


def _pull(monkeypatch, **env):
    _clear(monkeypatch)
    monkeypatch.setenv("PAH_PULL_FORCE_N", "r:20")
    monkeypatch.setenv("PAH_PULL_DIR", "r:0,1,0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    events = []
    return pah_tuning.PullController("right", pah_tuning.Settings.from_env(), _TEST_LINKS, events.append), events


def test_pull_state_machine(monkeypatch):
    pull, events = _pull(monkeypatch)
    q = (0.0,) * 6
    assert pull.torques(q, False, 0.0) == [0.0] * 6 and pull.state == "idle"
    assert pull.torques(q, True, 0.0) == [0.0] * 6 and pull.state == "wait"  # holding, not yet PULL_DELAY_S
    tau = pull.torques(q, True, pah_tuning.PULL_DELAY_S)
    assert pull.state == "pull" and tau == [0.0] * 6  # ramp starts at 0
    tau = pull.torques(q, True, pah_tuning.PULL_DELAY_S + pah_tuning.PULL_RAMP_S / 2)
    assert math.isclose(pull.last_force_n, 10.0)
    # planar arm along x, tool point at x = 0.5 + 0.136: joint i's torque for +y force is F * (lever arm)
    assert math.isclose(tau[0], 10.0 * (0.5 + 0.136), rel_tol=1e-3) or tau[0] == pull.tau_cap[0]
    assert all(abs(t) <= c + 1e-9 for t, c in zip(tau, pull.tau_cap))
    # the gripper lets go: nothing added at once, back to idle, and a new grasp waits again
    assert pull.torques(q, False, 1.0) == [0.0] * 6 and pull.state == "idle"
    assert any("let go" in e for e in events)
    pull.torques(q, True, 2.0)
    assert pull.state == "wait"


def test_pull_stops_on_travel_and_time(monkeypatch):
    pull, events = _pull(monkeypatch, PAH_PULL_TRAVEL_M="0.01", PAH_PULL_MAX_S="1")
    q = [0.0] * 6
    pull.torques(q, True, 0.0)
    pull.torques(q, True, 0.3)
    assert pull.state == "pull"
    q[0] = math.degrees(0.02 / 0.636)  # base turns: the tool point moves ~2 cm along +y, the pull axis
    assert pull.torques(q, True, 0.4) == [0.0] * 6 and pull.state == "spent"
    assert pull.torques(q, True, 5.0) == [0.0] * 6 and pull.state == "spent"  # spent until the gripper lets go
    pull.torques(q, False, 5.1)
    pull.torques(q, True, 6.0)
    pull.torques(q, True, 6.5)
    assert pull.state == "pull"
    assert pull.torques(q, True, 7.6) == [0.0] * 6 and pull.state == "spent"  # PAH_PULL_MAX_S
    assert any("time limit" in e for e in events) and any("moved" in e for e in events)


def test_pull_off_by_default(monkeypatch):
    _clear(monkeypatch)
    s = pah_tuning.Settings.from_env()
    assert s.pull_force_n is None and not s.active()


dreamlabs = pytest.importorskip("robots.metal", reason="needs dreamlabs 1.0.37 (the station's env)")


def test_pull_kinematics_match_dream_and_mujoco():
    import numpy as np

    metal_plant = pytest.importorskip("metal_plant")
    from robots import metal

    rng = np.random.default_rng(1)
    for _ in range(10):
        q_deg = np.array([rng.uniform(*j.limits_deg) for j in metal.JOINTS[:6]])
        p, rot, axes, pivots = pah_tuning.arm_kinematics(metal.LINKS, q_deg, pah_tuning.PULL_TCP_M)
        for a, b in ((1.0, 0.3), (-0.4, 2.0)):
            assert np.allclose(pah_tuning._rpy(a, b, 0.7), metal._rpy(a, b, 0.7))
        plant = metal_plant.MetalPlant(np.radians(q_deg))
        assert np.allclose(p, plant.tool_point(pah_tuning.PULL_TCP_M), atol=1e-6)
        force = rng.normal(size=3) * 10
        tau = pah_tuning.wrench_torques(axes, pivots, p, force)
        assert np.allclose(tau, plant.point_jacobian(p).T @ force, atol=1e-6)  # J^T F, MuJoCo's own Jacobian


def _bench_pull(env_extra, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PAH_")} | env_extra
    out = subprocess.run([sys.executable, str(ADDONS / "bench_pull.py"), *args],
                         env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads([ln for ln in out.stdout.splitlines() if ln.startswith("{")][-1])


def test_pull_gets_a_plug_out_that_dreams_driver_leaves_in():
    args = ("--withdraw", "15", "--stiffness", "20000", "--seconds", "3")
    base = _bench_pull({}, *args)
    assert not base["plug_out"] and base["peak_plug_force_n"] < 2.0
    pulled = _bench_pull({"PAH_PULL_FORCE_N": "r:20", "PAH_PULL_DIR": "r:0,-1,0"}, *args)
    assert pulled["plug_out"], pulled
    assert any("moved" in e for e in pulled["events"]), pulled  # it stopped itself once the plug gave


def _run_bench(env_extra, seconds=2.0):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PAH_")} | env_extra
    out = subprocess.run([sys.executable, str(ADDONS / "bench_metal_tick.py"), "--seconds", str(seconds)],
                         env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    line = [ln for ln in out.stdout.splitlines() if ln.startswith("{")][-1]
    return json.loads(line), out.stderr


def test_bench_default_is_dreams_30hz():
    result, log = _run_bench({})
    assert result["tick_hz_set"] == 30.0
    assert "pah_tuning" not in log


def test_bench_patched_rate_and_ff():
    result, log = _run_bench({"PAH_METAL_TICK_HZ": "60", "PAH_FF_TAU_S": "0.15"})
    assert result["tick_hz_set"] == 60.0
    assert result["tick_hz_achieved"] > 35.0
    assert "Metal bus tick 30 Hz -> 60 Hz" in log


def test_ik_weights_patch():
    code = (
        "import sys; sys.path.insert(0, %r); import pah_tuning; pah_tuning.install();"
        "from dream_ik.specs import ServeSpec; s = ServeSpec(smooth_window=0, solve='traj');"
        "print(s.traj_vel_weight, s.traj_acc_weight, s.traj_jerk_weight, s.traj_history_weight)" % str(ADDONS)
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("PAH_")} | {"PAH_IK_TRAJ": "20,100,200,5"}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert out.stdout.split()[-4:] == ["20.0", "100.0", "200.0", "5.0"], out.stderr


def test_eef_transform_offset_floor_hook(monkeypatch):
    import numpy as np

    _clear(monkeypatch)
    monkeypatch.setenv("PAH_EEF_OFFSET_M", "r:0,0,-0.02;l:0.01,0,0")
    monkeypatch.setenv("PAH_EEF_Z_MIN_M", "0.03")
    s = pah_tuning.Settings.from_env()
    xyz = np.array([[0.3, 0.0, 0.04], [0.3, 0.0, 0.10]])
    x, r, g = pah_tuning.eef_transform(s, None, "right", xyz, np.zeros((2, 6)), np.ones((2, 1)), {})
    assert np.allclose(x[:, 2], [0.03, 0.08])  # offset, then the floor
    x, _, _ = pah_tuning.eef_transform(s, None, "left", xyz, np.zeros((2, 6)), np.ones((2, 1)), {})
    assert np.allclose(x[:, 0], 0.31)

    def hook(side, xyz, rot6, grip, obs):
        return xyz, rot6, grip * 2.0  # clipped back to 0..1

    _, _, g = pah_tuning.eef_transform(s, hook, "right", xyz, np.zeros((2, 6)), np.full((2, 1), 0.7), {})
    assert np.allclose(g, 1.0)
    monkeypatch.setenv("PAH_EEF_OFFSET_M", "0,0,-0.2")
    with pytest.raises(ValueError):
        pah_tuning.Settings.from_env()  # more than 5 cm is refused


def test_eef_override_through_dreams_client_ik():
    env = {k: v for k, v in os.environ.items() if not k.startswith("PAH_")} | {"PAH_EEF_OFFSET_M": "r:0,0,-0.02"}
    out = subprocess.run([sys.executable, str(ADDONS / "bench_eef_override.py"), "--trace",
                          str(ADDONS / "data" / "run3_2026-11-08.csv"), "--chunks", "3"],
                         env=env, capture_output=True, text=True, timeout=300)
    line = [ln for ln in out.stdout.splitlines() if ln.startswith("{")][-1]
    d = json.loads(line)
    assert abs(d["right"]["mean_shift_cm"][2] + 2.0) < 0.2, d  # the solved hand moved down by the offset
    assert abs(d["left"]["mean_shift_cm"][2]) < 0.5, d  # the other arm untouched
