"""How long the sim's camera renders take here (3 cameras per capture), and which settings make software rendering cheap.

    MUJOCO_GL=osmesa python tools/render_bench.py      # software (cloud VMs)
    MUJOCO_GL=egl python tools/render_bench.py         # GPU through /dev/dri (docker --device /dev/dri)
"""
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim_station import load_example  # noqa: E402
from sim_scenes import hill_climb_table  # noqa: E402

import mujoco  # noqa: E402

y, dc = load_example(None)
hill_climb_table.patch(y)
env = y.YamEnv()
s = env.state()
out = {"gl": os.environ.get("MUJOCO_GL"), "threads": os.environ.get("LP_NUM_THREADS")}
base = {"shadowsize": int(env.model.vis.quality.shadowsize), "offsamples": int(env.model.vis.quality.offsamples)}
out["model"] = base


def bench(label, w, h, shadows=True, reflection=True, offsamples=None, shadowsize=None):
    if offsamples is not None:
        env.model.vis.quality.offsamples = offsamples
    if shadowsize is not None:
        env.model.vis.quality.shadowsize = shadowsize
    env.renderer = mujoco.Renderer(env.model, height=h, width=w)
    env.renderer.scene.flags[mujoco.mjtRndFlag.mjRND_SHADOW] = shadows
    env.renderer.scene.flags[mujoco.mjtRndFlag.mjRND_REFLECTION] = reflection
    original = env.renderer.update_scene

    def update(data, camera=None, scene_option=None):        # keep the flags across update_scene calls
        original(data, camera=camera, scene_option=scene_option)
        env.renderer.scene.flags[mujoco.mjtRndFlag.mjRND_SHADOW] = shadows
        env.renderer.scene.flags[mujoco.mjtRndFlag.mjRND_REFLECTION] = reflection
    env.renderer.update_scene = update
    env.images()
    times = []
    for _ in range(10):
        env.step(s["left"], s["right"])
        t = time.perf_counter()
        env.images()
        times.append(time.perf_counter() - t)
    ms = round(1000 * statistics.median(times), 1)
    out[label] = ms
    env.model.vis.quality.offsamples, env.model.vis.quality.shadowsize = base["offsamples"], base["shadowsize"]


bench("640x480", 640, 480)
bench("640x480_noshadow", 640, 480, shadows=False)
bench("640x480_noreflect", 640, 480, reflection=False)
bench("640x480_nomsaa", 640, 480, offsamples=0)
bench("640x480_shadow1024", 640, 480, shadowsize=1024)
bench("640x480_all_off", 640, 480, shadows=False, reflection=False, offsamples=0)
bench("320x176_all_off", 320, 176, shadows=False, reflection=False, offsamples=0)
t = time.perf_counter()
for _ in range(40):
    env.step(s["left"], s["right"])
step = 1000 * (time.perf_counter() - t) / 40
out["step_ms"] = round(step, 2)
out["sim_rate_max"] = {k: round(min(1.0, 200 / (4 * step + v)), 2) for k, v in out.items()
                       if isinstance(v, float) and k.startswith(("640", "320"))}
print(out, flush=True)
os._exit(0)          # skip MuJoCo's osmesa context teardown, which raises at exit
