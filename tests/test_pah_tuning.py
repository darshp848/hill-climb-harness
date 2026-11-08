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
    for name in ("PAH_METAL_TICK_HZ", "PAH_TORQUE_CAP_NM", "PAH_GRAVITY_SCALE", "PAH_IK_TRAJ", "PAH_TICK_LOG",
                 "PAH_FF_TAU_S"):
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


dreamlabs = pytest.importorskip("robots.metal", reason="needs dreamlabs 1.0.37 (the station's env)")


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
