"""Pure-Python forward kinematics for the Metal Arm URDF: tool point position in the arm's base (world) frame.

Usage: python3 -I metal_fk.py <urdf> ; prints joint chain, limits and the TCP for a set of test poses.
Mirrors the kit's FK: URDF joints = follower joints x joint_signs (all +1), TCP site at [0.135, 0, 0] in link6.
"""
import math
import sys
import xml.etree.ElementTree as ET

TCP = (0.135, 0.0, 0.0)
FLANGE = "link6"


def mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def rpy_xyz(rpy, xyz):
    r, p, y = rpy
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    rot = [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]
    return [rot[0] + [xyz[0]], rot[1] + [xyz[1]], rot[2] + [xyz[2]], [0, 0, 0, 1]]


def axis_angle(axis, q):
    x, y, z = axis
    n = math.sqrt(x * x + y * y + z * z) or 1.0
    x, y, z = x / n, y / n, z / n
    c, s, t = math.cos(q), math.sin(q), 1 - math.cos(q)
    return [
        [t * x * x + c, t * x * y - s * z, t * x * z + s * y, 0],
        [t * x * y + s * z, t * y * y + c, t * y * z - s * x, 0],
        [t * x * z - s * y, t * y * z + s * x, t * z * z + c, 0],
        [0, 0, 0, 1],
    ]


def floats(text, default):
    return tuple(float(v) for v in text.split()) if text else default


def load(path):
    root = ET.parse(path).getroot()
    joints = {}
    for j in root.findall("joint"):
        o = j.find("origin")
        a = j.find("axis")
        lim = j.find("limit")
        joints[j.find("child").get("link")] = {
            "name": j.get("name"),
            "type": j.get("type"),
            "parent": j.find("parent").get("link"),
            "xyz": floats(o.get("xyz") if o is not None else None, (0, 0, 0)),
            "rpy": floats(o.get("rpy") if o is not None else None, (0, 0, 0)),
            "axis": floats(a.get("xyz") if a is not None else None, (1, 0, 0)),
            "limit": (float(lim.get("lower")), float(lim.get("upper"))) if lim is not None and lim.get("lower") else None,
        }
    chain = []
    link = FLANGE
    while link in joints:
        chain.append(joints[link])
        link = joints[link]["parent"]
    return list(reversed(chain))


def tcp(chain, q):
    t = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    i = 0
    for j in chain:
        t = mat_mul(t, rpy_xyz(j["rpy"], j["xyz"]))
        if j["type"] in ("revolute", "continuous"):
            t = mat_mul(t, axis_angle(j["axis"], q[i]))
            i += 1
    p = [sum(t[r][k] * (list(TCP) + [1])[k] for k in range(4)) for r in range(3)]
    return p


def main():
    chain = load(sys.argv[1])
    revolute = [j for j in chain if j["type"] in ("revolute", "continuous")]
    print("chain:", " -> ".join(f"{j['name']}({j['type']})" for j in chain))
    for j in revolute:
        lim = j["limit"]
        lim_s = f"[{math.degrees(lim[0]):+.0f}, {math.degrees(lim[1]):+.0f}] deg" if lim else "none"
        print(f"  {j['name']}: axis {j['axis']}, origin xyz {j['xyz']} rpy {j['rpy']}, limit {lim_s}")
    d = math.radians
    poses = {
        "zero (all joints 0)": [0, 0, 0, 0, 0, 0],
        "left home (dreamcli)": [0.1436, 0.0018, 0.0091, -0.0256, 0.2147, -0.03],
        "right home (dreamcli)": [0.008, 0.0005, -0.0419, 0.1758, -0.003, -0.0792],
        "j1 +30 deg": [d(30), 0, 0, 0, 0, 0],
        "j1 -30 deg": [d(-30), 0, 0, 0, 0, 0],
        "j2 -30 deg": [0, d(-30), 0, 0, 0, 0],
        "j2 -30, j3 +60": [0, d(-30), d(60), 0, 0, 0],
        "j2 -60, j3 +90": [0, d(-60), d(90), 0, 0, 0],
        "j4 +30 deg": [0, 0, 0, d(30), 0, 0],
        "j5 +30 deg": [0, 0, 0, 0, d(30), 0],
        "j6 +45 deg": [0, 0, 0, 0, 0, d(45)],
    }
    for name, q in poses.items():
        x, y, z = tcp(chain, q)
        print(f"{name:24s} tool [{x:+.3f} {y:+.3f} {z:+.3f}] m")


if __name__ == "__main__":
    main()
