"""The learned joint fault detector: numpy inference matches the torch models it was trained as, the features match
online and offline, and the shipped model loads and runs on live-shaped readings."""

import json

import numpy as np
import pytest

from harness.joint_fault_net import DEFAULT_MODEL, N_FEATURES, WINDOW, FaultModel, JointFaultNet, features
from harness.joint_observer import YAM_LIMITS_DEG


def _spec_from_torch(family, cfg):
    torch = pytest.importorskip("torch")
    from tools.train_joint_fault import build_model, export
    torch.manual_seed(0)
    model = build_model(family, cfg).eval()
    norm = (np.zeros(N_FEATURES, np.float32), np.ones(N_FEATURES, np.float32))
    return model, export(model, family, cfg, norm, 0.5, {})


@pytest.mark.parametrize("family,cfg", [
    ("mlp", {"hidden": [16, 8]}),
    ("tcn", {"channels": 8, "dilations": [1, 2, 4], "kernel": 3}),
    ("gru", {"hidden": 8, "layers": 2}),
    ("transformer", {"d_model": 16, "heads": 2, "layers": 2, "ff": 32}),
])
def test_numpy_forward_matches_torch(family, cfg):
    torch = pytest.importorskip("torch")
    model, spec = _spec_from_torch(family, cfg)
    x = np.random.default_rng(1).normal(size=(5, WINDOW, N_FEATURES)).astype(np.float32)
    with torch.no_grad():
        want = model(torch.from_numpy(x)).double().numpy()
    got = FaultModel(json.loads(json.dumps(spec))).logits(x)
    assert np.allclose(got, want, atol=1e-5), (got, want)


def test_online_features_match_offline():
    rng = np.random.default_rng(2)
    T = 30
    q = np.cumsum(rng.normal(0, 2, T))
    qc, dq, tau, tauc = q + 1, rng.normal(0, 20, T), rng.normal(0, 1, T), rng.normal(0, 1, T)
    off = features(q, qc, dq, tau, tauc, -90, 90, 10)
    on = [features([q[i]], [qc[i]], [dq[i]], [tau[i]], [tauc[i]], -90, 90, 10,
                   q_cmd_prev=None if i == 0 else qc[i - 1])[0] for i in range(T)]
    assert np.allclose(off, np.stack(on), atol=1e-6)


@pytest.mark.skipif(not DEFAULT_MODEL.exists(), reason="no trained model shipped")
def test_shipped_model_runs_and_flags_a_limit_run():
    model = FaultModel.load()
    net = JointFaultNet(model, YAM_LIMITS_DEG)
    quiet, loud = [], []
    for i in range(40):                                  # 2 s holding still mid-range
        quiet += net.step(i * 0.05, "left", {"wrist_yaw": {"q_deg": 0.0, "q_cmd_deg": 0.0, "dq_deg_s": 0.0,
                                                          "tau_nm": 0.1, "tau_cmd_nm": 0.1, "tau_max_nm": 10.0}})
    q = 0.0
    for i in range(40, 70):                              # then 200 deg/s straight at the +90 deg limit
        q += 10.0
        loud += net.step(i * 0.05, "left", {"wrist_yaw": {"q_deg": q, "q_cmd_deg": q + 10, "dq_deg_s": 200.0,
                                                         "tau_nm": 0.5, "tau_cmd_nm": 0.5, "tau_max_nm": 10.0}})
    assert not quiet
    assert any(a["kind"] == "learned_fault_soon" for a in loud)


def test_torque_free_model_ignores_torque():
    _, spec = _spec_from_torch("transformer", {"d_model": 16, "heads": 2, "layers": 1, "ff": 32})
    spec["torque_free"] = True
    m = FaultModel(json.loads(json.dumps(spec)))
    x = np.random.default_rng(3).normal(size=(4, WINDOW, N_FEATURES))
    y = x.copy()
    y[..., 5:8] = 7.0
    assert np.allclose(m.probs(x), m.probs(y))
    assert not FaultModel(spec | {"torque_free": False}).torque_free


@pytest.mark.parametrize("path", sorted((DEFAULT_MODEL.parent / "encoder").glob("joint_fault_*.json")),
                         ids=lambda p: p.stem)
def test_real_rollout_models_load(path):
    """The encoders trained on real Metal rollouts (tools/joint_fault_real.py --export) load and run."""
    spec = json.loads(path.read_text(encoding="utf-8"))
    if "weights" not in spec:
        pytest.skip("not a model file")
    m = FaultModel(spec)
    assert m.torque_free
    p = m.probs(np.zeros((2, WINDOW, N_FEATURES)))
    assert p.shape == (2, 2) and np.all((p >= 0) & (p <= 1))
