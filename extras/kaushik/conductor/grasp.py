"""Grasp primitive: Jev finds the plug in the right wrist camera, our code moves the arm (station /move), not Dream.

  python grasp.py eagle                 # point the right gripper straight down (camera looks down)
  python grasp.py find                  # Jev Box on the target plug; prints its pixel centre and saves a marked frame
  python grasp.py center                # servo until the plug sits under the fingertips
  python grasp.py roll along|across     # turn the wrist so the jaws close along / across the strip
  python grasp.py down 0.02             # move the tool down 2 cm (up with a negative number)
  python grasp.py grip                  # close and report whether it holds something
  python grasp.py pull 0.05             # straight up by 5 cm
  python grasp.py open
  python grasp.py auto along|across     # the whole sequence: center, roll, descend while re-centering, grip, pull
Needs TYPESAFE_API_KEY (doppler run -p typesafe -c dev).
"""
import base64
import json
import math
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import cv2
import numpy as np
import requests

ST = os.environ.get("STATION", "http://127.0.0.1:8091")
OUT = Path(__file__).parent / "grasp_frames"
OUT.mkdir(exist_ok=True)
TARGET = os.environ.get("TARGET", "the black plug body of an orange USB cable that is plugged into the power strip's "
                                  "USB ports, the one nearest the bottom of `photo`")
AIM = (648, 500)  # pixel under the fingertips (1280x720 wrist_r, pointing down)
# scene shift in pixels per metre of tool motion, measured at tool height H0 above the plugs (x fwd, y left)
JX, JY, H0 = np.array([-900.0, 1600.0]), np.array([1660.0, 973.0]), 0.09


def _post(route, body):
    req = urllib.request.Request(ST + route, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{route}: {e.code} {e.read().decode()[:200]}")


def move(**kw):
    kw.setdefault("arm", "right")
    return _post("/move", kw)


def pose():
    with urllib.request.urlopen(ST + "/pose", timeout=10) as r:
        return json.loads(r.read())["right"]


def frame():
    with urllib.request.urlopen(ST + "/frame/wrist_r.jpg", timeout=10) as r:
        return r.read()


def jev_box(jpg, what=TARGET):
    rid = str(uuid.uuid4())
    body = {"model": "jev-latest", "state": {"photo": rid},
            "assets": {rid: {"type": "image", "format": "jpg", "data_base64": base64.b64encode(jpg).decode()}},
            "questions": {"where": {"type": "box", "instructions": what},
                          "seen": {"type": "noul", "instructions": f"Is {what} visible in `photo`?"}}}
    for attempt in range(4):
        r = requests.post("https://api.typesafe.ai/v1/systemone", json=body, timeout=30, headers={
            "Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}", "TypeSafe-Danger-Zone-Images": "true"})
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        a = r.json()["answers"]
        return a["where"]["box"], a["where"]["confidence"], a["seen"]["noul"]
    r.raise_for_status()


def find(tag="find"):
    jpg = frame()
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    b, conf, seen = jev_box(jpg)
    x0, y0, x1, y1 = int(b["left"] * w), int(b["top"] * h), int(b["right"] * w), int(b["bottom"] * h)
    c = ((x0 + x1) / 2, (y0 + y1) / 2)
    cv2.rectangle(img, (x0, y0), (x1, y1), (0, 255, 255), 3)
    cv2.drawMarker(img, AIM, (255, 0, 255), cv2.MARKER_CROSS, 40, 3)
    path = OUT / f"{time.strftime('%H%M%S')}_{tag}.jpg"
    cv2.imwrite(str(path), img)
    print(f"plug at px {c[0]:.0f},{c[1]:.0f} box {x1 - x0}x{y1 - y0} conf {conf:.2f} seen {seen:.2f}  ({path.name})")
    return np.array(c), conf, seen, (x1 - x0, y1 - y0)


def height_above_plug():
    return max(0.02, pose()["xyz"][2] - PLUG_Z)


PLUG_Z = float(os.environ.get("PLUG_Z", "0.05"))  # tool z where the fingertips reach the plug body (tune)


def center(tol=20, rounds=5):
    for _ in range(rounds):
        c, conf, seen, _ = find("center")
        if seen < 0.5:
            print("plug not visible; stopping")
            return False
        err = np.array(AIM) - c  # the scene must shift by this
        if np.linalg.norm(err) < tol:
            print("centred")
            return True
        scale = H0 / height_above_plug()
        J = np.column_stack([JX, JY]) * scale
        dxy = np.linalg.lstsq(J, err, rcond=None)[0] * 0.5
        dxy = np.clip(dxy, -0.015, 0.015)
        print(f"  move x {dxy[0] * 100:+.1f} cm, y {dxy[1] * 100:+.1f} cm")
        move(dxyz=[float(dxy[0]), float(dxy[1]), 0.0], keep_orientation=True, duration_s=0.8)
    return False


def _orb_shift(a_jpg, b_jpg):
    o, bf = cv2.ORB_create(2000), cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    def kd(j):
        i = cv2.imdecode(np.frombuffer(j, np.uint8), cv2.IMREAD_GRAYSCALE)
        m = np.zeros_like(i)
        m[:450] = 255  # above the fingers
        return o.detectAndCompute(i, m)
    k0, d0 = kd(a_jpg)
    k1, d1 = kd(b_jpg)
    ms = sorted(bf.match(d0, d1), key=lambda m: m.distance)[:150]
    return np.median([np.subtract(k1[m.trainIdx].pt, k0[m.queryIdx].pt) for m in ms], axis=0)


def calibrate(step=0.015):
    """Measure how the scene shifts per metre of x and y tool motion at this height and wrist roll."""
    global JX, JY, H0
    f0 = frame()
    move(dxyz=[step, 0, 0], keep_orientation=True, duration_s=0.6)
    time.sleep(0.3)
    fx = frame()
    move(dxyz=[-step, step, 0], keep_orientation=True, duration_s=0.6)
    time.sleep(0.3)
    fy = frame()
    move(dxyz=[0, -step, 0], keep_orientation=True, duration_s=0.6)
    JX, JY, H0 = _orb_shift(f0, fx) / step, _orb_shift(f0, fy) / step, height_above_plug()
    print(f"calibrated: x -> {JX.round(0)}, y -> {JY.round(0)} px/m at {H0 * 100:.0f} cm")


def strip_angle():
    """The strip's long axis in the image, degrees (from the image x axis), from the biggest dark blob."""
    img = cv2.imdecode(np.frombuffer(frame(), np.uint8), cv2.IMREAD_GRAYSCALE)[:470]
    _, m = cv2.threshold(img, 60, 255, cv2.THRESH_BINARY_INV)
    cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    (cx, cy), (w, h), ang = cv2.minAreaRect(max(cs, key=cv2.contourArea))
    return ang if w >= h else ang + 90


ROLL_SIGN = float(os.environ.get("ROLL_SIGN", "1"))  # image rotation direction per + wrist_roll (calibrate)


def roll(style):
    """The jaws close along the image x axis. along: turn so they close along the strip; across: across it."""
    a = strip_angle()
    want = a if style == "along" else a + 90
    want = (want + 90) % 180 - 90  # smallest turn
    print(f"strip at {a:.0f} deg in the image; turning the wrist {want:.0f} deg")
    move(djoints={"5": ROLL_SIGN * math.radians(want)}, duration_s=1.2)
    calibrate()


def grip():
    move(gripper=0.0, duration_s=1.0)
    time.sleep(0.5)
    g = pose()["gripper"]
    held = g > 0.08
    print(f"gripper {g:.2f}: {'HOLDING something' if held else 'shut on nothing'}")
    return held


def auto(style):
    move(gripper=1.0, duration_s=0.6)
    if not center():
        return False
    roll(style)
    center()
    while height_above_plug() > 0.025:
        move(dxyz=[0, 0, -min(0.03, height_above_plug() - 0.02)], keep_orientation=True, duration_s=1.0)
        center(tol=15, rounds=2)
    move(dxyz=[0, 0, -0.02], keep_orientation=True, duration_s=1.0)
    find("before_grip")
    if not grip():
        move(gripper=1.0, dxyz=[0, 0, 0.05], keep_orientation=True)
        return False
    move(dxyz=[0, 0, 0.06], keep_orientation=True, duration_s=1.5)
    find("after_pull")
    held = pose()["gripper"] > 0.08
    print("PULLED: still holding" if held else "slipped during the pull")
    return held


if __name__ == "__main__":
    cmd, arg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
    if cmd == "eagle":
        move(point=[0, 0, -1], duration_s=1.5)
    elif cmd == "find":
        find()
    elif cmd == "center":
        center()
    elif cmd == "roll":
        roll(arg)
    elif cmd == "down":
        print(move(dxyz=[0, 0, -float(arg)], keep_orientation=True)["pose"]["xyz"])
    elif cmd == "grip":
        grip()
    elif cmd == "pull":
        move(dxyz=[0, 0, float(arg or 0.05)], keep_orientation=True)
    elif cmd == "open":
        move(gripper=1.0)
    elif cmd == "auto":
        auto(arg or "along")
    elif cmd == "calibrate":
        calibrate()
    elif cmd == "angle":
        print(strip_angle())
    print("tool z", pose()["xyz"][2])


# ---- front approach: the camera looks at the strip's end face; servo sideways (y) and up/down (z), then advance
def calibrate_front(step=0.012):
    f0 = frame()
    move(dxyz=[0, step, 0], keep_orientation=True, duration_s=0.6); time.sleep(0.3); fy = frame()
    move(dxyz=[0, -step, step], keep_orientation=True, duration_s=0.6); time.sleep(0.3); fz = frame()
    move(dxyz=[0, 0, -step], keep_orientation=True, duration_s=0.6)
    J = np.column_stack([_orb_shift(f0, fy) / step, _orb_shift(f0, fz) / step])
    print(f"front calibration: y -> {J[:, 0].round(0)}, z -> {J[:, 1].round(0)} px/m")
    return J


def center_front(J, aim=AIM, tol=20, rounds=6, gain=0.6):
    for _ in range(rounds):
        c, conf, seen, size = find("front")
        err = np.array(aim) - c
        if np.linalg.norm(err) < tol:
            return c, size
        dyz = np.clip(np.linalg.lstsq(J, err, rcond=None)[0] * gain, -0.012, 0.012)
        move(dxyz=[0, float(dyz[0]), float(dyz[1])], keep_orientation=True, duration_s=0.6)
    return c, size


def advance(dist):
    a = np.array(pose()["R"])[:, 2]
    a = a / np.linalg.norm(a)
    # no sag correction here: aiming past the miss is what drove the wrist into the strip (motor guard stop)
    move(dxyz=[float(v) for v in a * dist], keep_orientation=True, duration_s=0.8, no_overshoot=True)
