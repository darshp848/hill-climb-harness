"""PAH_EEF_HOOK=pah_topdown:topdown: the right gripper always points straight down ("eagle's eye").

Dream-1 approaches blocks, balls and plugs from the side (8 Nov, station: the gripper's pointing axis lay flat along
the table 2.6 cm above it), so the jaws close at an angle and a held block is set down tilted. This keeps every one
of Dream's positions and grips and replaces only the hand's rotation: pointing axis (the flange x, rot6[:3]) straight
down, jaw axis (rot6[3:6]) = Dream's own, flattened onto the table plane, so the jaws still close the way Dream
chose. rot6 is the first two columns of the hand's rotation in the arm's base frame (checked against GET /pose).

PAH_TOPDOWN_SIDES=right (default) or right,left.
"""
from __future__ import annotations

import os

import numpy as np

DOWN = np.array([0.0, 0.0, -1.0])
SIDES = {s.strip() for s in os.environ.get("PAH_TOPDOWN_SIDES", "right").split(",") if s.strip()}


def topdown(side, xyz, rot6, grip, observation):
    if side not in SIDES:
        return xyz, rot6, grip
    out = np.array(rot6, dtype=np.float64, copy=True)
    for i, r in enumerate(out):
        jaw = np.array([r[3], r[4], 0.0])
        if np.linalg.norm(jaw) < 1e-3:   # Dream's jaw axis was vertical: take the horizontal normal of its pointing axis
            jaw = np.array([-r[1], r[0], 0.0])
        if np.linalg.norm(jaw) < 1e-3:
            jaw = np.array([1.0, 0.0, 0.0])
        out[i, :3] = DOWN
        out[i, 3:6] = jaw / np.linalg.norm(jaw)
    return xyz, out, grip
