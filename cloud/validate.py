"""Offline container gate: original Dream example, relay scene, render, HTTP and detector."""
import io
import json
import urllib.request

import numpy as np
from PIL import Image

from harness.detect import TableDetector
from sim_station import SimStation, load_example


def main():
    y, _ = load_example(None)
    original = y.YamEnv()
    try:
        for _ in range(40):
            original.step(y.HOME, y.HOME)
        assert all(np.isfinite(v).all() for v in original.state().values())
        assert all(rgb.std() > 10 for rgb in original.images().values())
    finally:
        original.close()
    with SimStation(policy="scripted", scene="hill_climb", api_port=0, view_port=0) as sim:
        for cam in ("scene", "wrist_l", "wrist_r"):
            with urllib.request.urlopen(sim.api_url + f"/frame/{cam}.jpg?w=320") as response:
                im = Image.open(io.BytesIO(response.read()))
                assert im.size == (320, 180) and np.asarray(im).std() > 10
        with urllib.request.urlopen(sim.api_url + "/arms") as response:
            assert all(0 <= arm["gripper"] <= 1 for arm in json.load(response).values())
        with urllib.request.urlopen(sim.api_url + "/frame/scene.jpg") as response:
            boxes = TableDetector.from_file("plans/sim_table_colors.yaml").boxes({"scene": response.read()})["scene"]
        assert sum(b.role == "target" for b in boxes) >= 4
        assert sum(b.role == "avoid" for b in boxes) >= 2
    print("Offline original-example and relay build/step/render/API/detector gates passed.")


if __name__ == "__main__":
    main()
