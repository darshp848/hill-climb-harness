"""A MuJoCo model of one Metal arm built from Dream's own link table (robots.metal.LINKS), for fake_damiao.

Kinematics and link masses / centres of mass are Dream's (MakerMods' metal_with_gripper.urdf as Dream copied it), so
gravity and the coupling between joints match the arm Dream's feed-forward assumes. Unknowns are parameters, meant
to be swept and fitted against the station trace: each joint's armature (the motor's reflected rotor inertia),
viscous damping and Coulomb friction, and a link inertia radius (link inertia = m r^2 per axis).
"""

from __future__ import annotations

import dataclasses
import math

import mujoco
import numpy as np
from robots import metal

DT = 0.001


@dataclasses.dataclass(frozen=True)
class PlantParams:
    armature: tuple[float, ...] = (0.03, 0.06, 0.03, 0.01, 0.005, 0.005)
    damping: tuple[float, ...] = (0.3, 0.5, 0.3, 0.1, 0.05, 0.05)
    friction: tuple[float, ...] = (0.3, 0.5, 0.3, 0.15, 0.08, 0.08)
    inertia_radius_m: float = 0.04

    def scaled(self, armature: float = 1.0, damping: float = 1.0, friction: float = 1.0) -> PlantParams:
        return PlantParams(
            tuple(a * armature for a in self.armature),
            tuple(d * damping for d in self.damping),
            tuple(f * friction for f in self.friction),
            self.inertia_radius_m,
        )


def _mjcf(p: PlantParams) -> str:
    body = ""
    close = ""
    for i, ((xyz, rpy, axis, mass, com), arm, damp, fric) in enumerate(
        zip(metal.LINKS, p.armature, p.damping, p.friction, strict=True)
    ):
        ixx = mass * p.inertia_radius_m**2
        body += (
            f'<body name="l{i}" pos="{xyz[0]} {xyz[1]} {xyz[2]}" euler="{rpy[0]} {rpy[1]} {rpy[2]}">'
            f'<joint name="j{i}" axis="{axis[0]} {axis[1]} {axis[2]}" armature="{arm}" damping="{damp}" '
            f'frictionloss="{fric}" limited="false"/>'
            f'<inertial pos="{com[0]} {com[1]} {com[2]}" mass="{mass}" diaginertia="{ixx} {ixx} {ixx}"/>'
        )
        close += "</body>"
    return (
        '<mujoco><compiler angle="radian" eulerseq="XYZ"/>'
        f'<option timestep="{DT}" gravity="0 0 -9.81" integrator="implicitfast"/>'
        f"<worldbody>{body}{close}</worldbody>"
        '<actuator>' + "".join(f'<motor joint="j{i}" gear="1"/>' for i in range(6)) + "</actuator></mujoco>"
    )


class MetalPlant:
    """Six joints under externally computed torques; step() advances one DT."""

    def __init__(self, q0_rad: np.ndarray, params: PlantParams | None = None) -> None:
        self.params = params or PlantParams()
        self.model = mujoco.MjModel.from_xml_string(_mjcf(self.params))
        self.data = mujoco.MjData(self.model)
        self.data.qpos[:] = q0_rad
        mujoco.mj_forward(self.model, self.data)

    @property
    def q(self) -> np.ndarray:
        return self.data.qpos

    @property
    def dq(self) -> np.ndarray:
        return self.data.qvel

    def gravity_nm(self) -> np.ndarray:
        """The torque each joint needs to hold the arm still here (MuJoCo's bias at zero velocity)."""
        d = mujoco.MjData(self.model)
        d.qpos[:] = self.data.qpos
        mujoco.mj_forward(self.model, d)
        return d.qfrc_bias.copy()

    def hand_xyz(self, q_rad: np.ndarray) -> np.ndarray:
        """Position of the last link's origin (the wrist flange), m, at joints q."""
        d = mujoco.MjData(self.model)
        d.qpos[:] = q_rad
        mujoco.mj_kinematics(self.model, d)
        return d.xpos[-1].copy()

    def step(self, tau: np.ndarray) -> None:
        self.data.ctrl[:] = tau
        mujoco.mj_step(self.model, self.data)


def check_gravity(samples: int = 20, seed: int = 0) -> float:
    """Worst |MuJoCo gravity - Dream's gravity_nm / SDK factor| over random poses, N.m (should be ~0)."""
    rng = np.random.default_rng(seed)
    worst = 0.0
    for _ in range(samples):
        q = np.array([rng.uniform(*np.radians(j.limits_deg)) for j in metal.JOINTS[:6]])
        plant = MetalPlant(q)
        dream = np.array(metal.gravity_nm(tuple(math.degrees(x) for x in q))) / np.array(metal.GRAVITY_FACTORS)
        worst = max(worst, float(np.abs(plant.gravity_nm() - dream).max()))
    return worst
