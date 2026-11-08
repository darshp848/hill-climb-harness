"""Offline benchmark of the harness's live path, for the match laptop (Apple Silicon MacBook Air) and any other box.

No station, no network, no paid API call: it replays recorded wrist frames (tests/data/guard_frames by default) through
the same code the match runs, and measures per-iteration latency (p50 / p95 / max, ms) and CPU (% of one core, from
process CPU time over wall time) for:

  guard          TableDetector.guard on one wrist JPEG (the live guard's and fast loop's per-frame work)
  guard_ref      the same on the frozen pre-optimisation detector (tests/_detect_reference.py): before/after on one box
  fastloop       FastLoop.step with two new wrist frames a tick (detector included), as at 20 Hz in a match
  statespace     Observer.fast + StateSpace.step alone (the decision model, no perception)
  jpeg_decode    decode one 640 px frame to RGB;  jpeg_encode: encode it at q85
  stitch         verifiers.stitch on 9 history frames, one of them new (the monitor's grid image, one per check)
  annotate       TableDetector.annotate_frames on scene + both wrists (boxes drawn on the monitor's frames)
  monitor_build  MonitorVerifier._build: annotate + stitch + base64 + stage text, i.e. one whole monitor request
  prompt         promptgen.compose at level 5 (the longest layered prompt)

--sustained S runs the match load for S seconds (live guard 10 Hz + fast loop 20 Hz on both wrists, in their own
threads, a monitor request every 3 s) and prints a row per 30 s window: guard p50/p95, fast-loop tick p95, overruns
(ticks longer than their period) and CPU %. On a fanless Air, a p95 that climbs window after window is thermal
throttling. --sustained-mode max runs the guard flat out on --threads threads instead (worst-case heat).

    python tools/mac_bench.py                              # component table, ~30 s
    python tools/mac_bench.py --sustained 300              # + 5 minutes of match load
    python tools/mac_bench.py --sustained 300 --sustained-mode max --threads 4
Results go to bench_results/mac_bench_<host>_<time>.json (--out to change).
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.perf import cap_threads  # noqa: E402

cap_threads()

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from harness.detect import TableDetector  # noqa: E402
from harness.fastloop import FastLoop  # noqa: E402
from harness.plan import load_plan  # noqa: E402
from harness.promptgen import Context, compose  # noqa: E402
from harness.statespace import Observer, StateSpace, Weights  # noqa: E402
from harness.verifiers import MonitorVerifier, stitch  # noqa: E402


def _reference_detector(cfg_path: Path):
    ref = ROOT / "tests" / "_detect_reference.py"
    if not ref.exists():
        return None
    spec = importlib.util.spec_from_file_location("_detect_reference", ref)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod      # dataclasses look their module up by name
    spec.loader.exec_module(mod)
    return mod.TableDetector.from_file(cfg_path)


def _stats(ms: list[float], cpu_s: float, wall_s: float) -> dict[str, float]:
    a = np.sort(np.asarray(ms))
    return {"n": int(a.size), "p50_ms": float(np.percentile(a, 50)), "p95_ms": float(np.percentile(a, 95)),
            "max_ms": float(a[-1]), "mean_ms": float(a.mean()), "cpu_pct": 100.0 * cpu_s / max(wall_s, 1e-9)}


def bench(fn, *, seconds: float, min_iters: int = 20) -> dict[str, float]:
    """Runs fn() back to back for `seconds` (at least min_iters times) after a short warm-up."""
    for _ in range(3):
        fn()
    ms: list[float] = []
    c0, w0 = time.process_time(), time.perf_counter()
    while len(ms) < min_iters or time.perf_counter() - w0 < seconds:
        t = time.perf_counter()
        fn()
        ms.append(1000 * (time.perf_counter() - t))
    return _stats(ms, time.process_time() - c0, time.perf_counter() - w0)


class Cycle:
    """Endless supply of recorded frames. Each one is made unique (a counter after the JPEG's end marker, which
    decoders ignore), as a live camera's frames are, so no digest-keyed cache can reuse an earlier result."""

    def __init__(self, frames: list[bytes]):
        self.frames, self.i = frames, 0

    def next(self) -> bytes:
        j = self.frames[self.i % len(self.frames)]
        self.i += 1
        return j + self.i.to_bytes(8, "little")


def _load_frames(folder: Path) -> tuple[list[bytes], list[bytes]]:
    files = sorted(folder.glob("*.jpg"))
    if not files:
        raise SystemExit(f"no .jpg frames in {folder}")
    wrists = [f.read_bytes() for f in files if "wrist" in f.name] or [f.read_bytes() for f in files]
    # overhead-like frames for the monitor's scene view; a top-down one first if there is one
    others = [f.read_bytes() for f in sorted((f for f in files if "wrist" not in f.name),
                                             key=lambda f: "topdown" not in f.name)] or wrists
    return wrists, others


def components(args, det: TableDetector, ref, wrists: list[bytes], others: list[bytes]) -> dict[str, dict]:
    s = args.seconds
    out: dict[str, dict] = {}
    cyc = Cycle(wrists)
    out["guard"] = bench(lambda: det.guard({"wrist_r": cyc.next()}), seconds=s)
    if ref is not None:
        cyc_r = Cycle(wrists)
        out["guard_ref"] = bench(lambda: ref.guard({"wrist_r": cyc_r.next()}), seconds=s)

    cyc_f = Cycle(wrists)
    fl = FastLoop(frames=lambda: {"wrist_l": cyc_f.next(), "wrist_r": cyc_f.next()}, guard_judge=det.guard,
                  grippers=lambda: {"left": {"opening": 0.5}, "right": {"opening": 0.1}})
    fl.attempt_start("pick_ball", 60.0, 20.0, guarded=True)
    out["fastloop"] = bench(fl.step, seconds=s)

    ss, ob = StateSpace(Weights()), Observer(Weights())
    ss.start(60.0, 20.0)
    ob.reset()
    u = ss.u(dt=0.05, l_guard_no=1.0)

    def ss_step():
        ob.fast({"illegal_grip": 0.0, "holding": 1.0})
        ss.step(u, ob.z)
    out["statespace"] = bench(ss_step, seconds=s, min_iters=2000)

    jpeg = wrists[0]
    out["jpeg_decode"] = bench(lambda: Image.open(io.BytesIO(jpeg)).convert("RGB"), seconds=s)
    rgb = Image.open(io.BytesIO(jpeg)).convert("RGB")

    def enc():
        b = io.BytesIO()
        rgb.save(b, "JPEG", quality=85)
    out["jpeg_encode"] = bench(enc, seconds=s)
    # A check's grid: the stage-start frame plus a sliding history, so each check brings one new frame
    cyc_s = Cycle(wrists)
    hist = [("stage start", cyc_s.next())] + [(f"t={2 * i} s", cyc_s.next()) for i in range(8)]

    def stitch_check():
        del hist[1]
        hist.append(("newest history", cyc_s.next()))
        stitch(hist)
    out["stitch"] = bench(stitch_check, seconds=s)
    scene = others[0]
    trio = {"scene": scene, "wrist_l": wrists[0], "wrist_r": wrists[1 % len(wrists)]}
    out["annotate"] = bench(lambda: det.annotate_frames(trio), seconds=s)

    plan = load_plan(ROOT / "plans" / "hill_climb_v4.yaml")
    mv = MonitorVerifier(plan, transport=lambda payload: {"content": []})
    mv.annotator, mv.legend = det.annotate_frames, det.legend()
    stage = next(st for st in plan.stages if plan.guard is not None and plan.guard.covers(st.name))
    cyc_m = Cycle(wrists)
    t_el = [0.0]

    def build():
        t_el[0] += 3.0
        payload = mv._build(stage, {"scene": scene, "wrist_l": cyc_m.next(), "wrist_r": cyc_m.next()}, t_el[0])
        json.dumps({k: v for k, v in (payload or {}).items() if not k.startswith("_")})
    out["monitor_build"] = bench(build, seconds=s)

    ctx = Context(prefix="The strip has no power.", holding={"right": "a cable", "left": "nothing"},
                  tower=["blue cube", "pink cube"], attempt=2, progress=0.4, last_failure="the gripper slipped")
    p = "The strip has no power. Pull the black cable out of the hub with the right arm."
    out["prompt"] = bench(lambda: compose(p, 5, ctx), seconds=s, min_iters=2000)
    return out


def sustained(args, det: TableDetector, wrists: list[bytes], others: list[bytes]) -> list[dict]:
    """Match load (or flat out) for args.sustained seconds; one row per args.window seconds."""
    stop = threading.Event()
    lock = threading.Lock()
    rec: dict[str, list[float]] = {"guard": [], "tick": []}
    over = {"n": 0}

    def timed_guard(frames):
        t = time.perf_counter()
        r = det.guard(frames)
        with lock:
            rec["guard"].append(1000 * (time.perf_counter() - t))
        return r

    threads: list[threading.Thread] = []
    if args.sustained_mode == "match":
        cyc_g, cyc_f = Cycle(wrists), Cycle(wrists[::-1])

        def periodic(period: float, body, record_tick: bool = False):
            def run():
                while not stop.is_set():
                    t = time.perf_counter()
                    body()
                    took = time.perf_counter() - t
                    with lock:
                        if record_tick:
                            rec["tick"].append(1000 * took)
                        if took > period:
                            over["n"] += 1
                    stop.wait(max(0.0, period - took))
            return run
        fl = FastLoop(frames=lambda: {"wrist_l": cyc_f.next(), "wrist_r": cyc_f.next()}, guard_judge=timed_guard,
                      grippers=lambda: {"right": {"opening": 0.1}})
        fl.attempt_start("pick_ball", 1e9, None, guarded=True)

        def live_guard_tick():
            for role in ("wrist_l", "wrist_r"):
                timed_guard({role: cyc_g.next()})
        plan = load_plan(ROOT / "plans" / "hill_climb_v4.yaml")
        mv = MonitorVerifier(plan, transport=lambda payload: {"content": []})
        mv.annotator = det.annotate_frames
        stage = next(st for st in plan.stages if plan.guard is not None and plan.guard.covers(st.name))
        cyc_m, t_el = Cycle(wrists), [0.0]

        def monitor_tick():
            t_el[0] += 3.0
            mv._build(stage, {"scene": others[0], "wrist_l": cyc_m.next(), "wrist_r": cyc_m.next()}, t_el[0])
        threads = [threading.Thread(target=periodic(0.1, live_guard_tick), daemon=True),
                   threading.Thread(target=periodic(0.05, fl.step, True), daemon=True),
                   threading.Thread(target=periodic(3.0, monitor_tick), daemon=True)]
    else:
        def flat_out(k: int):
            cyc = Cycle(wrists[k:] + wrists[:k])
            while not stop.is_set():
                timed_guard({"wrist_r": cyc.next()})
        threads = [threading.Thread(target=flat_out, args=(k,), daemon=True) for k in range(args.threads)]

    rows: list[dict] = []
    t_end = time.perf_counter() + args.sustained
    for th in threads:
        th.start()
    print(f"\nsustained {args.sustained_mode} load, {args.sustained:.0f} s"
          f"{'' if args.sustained_mode == 'match' else f' on {args.threads} threads'}:")
    print(f"{'window':>8} {'guard p50':>10} {'guard p95':>10} {'guards/s':>9} {'tick p95':>9} {'overruns':>9} "
          f"{'cpu %':>7}  thermal")
    w = 0
    while time.perf_counter() < t_end:
        c0, w0 = time.process_time(), time.perf_counter()
        stop.wait(min(args.window, t_end - w0))
        with lock:
            g, tk, ov = rec["guard"], rec["tick"], over["n"]
            rec["guard"], rec["tick"], over["n"] = [], [], 0
        wall = time.perf_counter() - w0
        row = {"t_s": round((w + 1) * args.window), "guards": len(g), "guards_per_s": len(g) / wall,
               "guard_p50_ms": float(np.percentile(g, 50)) if g else None,
               "guard_p95_ms": float(np.percentile(g, 95)) if g else None,
               "tick_p95_ms": float(np.percentile(tk, 95)) if tk else None, "overruns": ov,
               "cpu_pct": 100.0 * (time.process_time() - c0) / wall, "thermal": thermal_state()}
        rows.append(row)
        f = lambda v: f"{v:10.2f}" if v is not None else f"{'-':>10}"  # noqa: E731
        print(f"{row['t_s']:>7}s {f(row['guard_p50_ms'])} {f(row['guard_p95_ms'])} {row['guards_per_s']:9.1f} "
              f"{f(row['tick_p95_ms'])[1:]} {ov:9d} {row['cpu_pct']:7.1f}  {row['thermal'] or ''}")
        w += 1
    stop.set()
    for th in threads:
        th.join(timeout=2.0)
    return rows


def thermal_state() -> str | None:
    """macOS: `pmset -g therm` (CPU_Speed_Limit 100 = not throttled). None elsewhere."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    vals = [ln.strip().replace(" ", "") for ln in out.splitlines() if "=" in ln]
    return ",".join(vals) or "no warning"


def machine() -> dict[str, str | int | None]:
    info: dict[str, str | int | None] = {"host": socket.gethostname(), "platform": platform.platform(),
                                         "machine": platform.machine(), "python": platform.python_version(),
                                         "cpus": os.cpu_count(), "numpy": np.__version__,
                                         "pillow": Image.__version__}
    try:
        import scipy
        info["scipy"] = scipy.__version__
    except ImportError:
        info["scipy"] = None
    if sys.platform == "darwin":
        for key in ("machdep.cpu.brand_string", "hw.perflevel0.physicalcpu", "hw.perflevel1.physicalcpu"):
            try:
                info[key] = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True,
                                           timeout=2).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
    else:
        info["cpu"] = platform.processor()
    info["threads_env"] = {k: os.environ.get(k) for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                                                          "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS")}
    return info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--frames", type=Path, default=ROOT / "tests" / "data" / "guard_frames",
                    help="folder of recorded JPEGs (names with 'wrist' are wrist views)")
    ap.add_argument("--config", type=Path, default=ROOT / "plans" / "table_colors.yaml")
    ap.add_argument("--seconds", type=float, default=2.0, help="time per component")
    ap.add_argument("--sustained", type=float, default=0.0, help="seconds of sustained load (300 = 5 min)")
    ap.add_argument("--sustained-mode", choices=("match", "max"), default="match")
    ap.add_argument("--threads", type=int, default=4, help="--sustained-mode max: guard threads")
    ap.add_argument("--window", type=float, default=30.0, help="sustained: seconds per row")
    ap.add_argument("--no-ref", action="store_true", help="skip the pre-optimisation detector")
    ap.add_argument("--label", default="", help="free text saved in the JSON (e.g. 'on battery, lid open')")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    wrists, others = _load_frames(args.frames)
    det = TableDetector.from_file(args.config)
    ref = None if args.no_ref else _reference_detector(args.config)
    info = machine()
    print(f"{info['host']}  {info['platform']}  {info['machine']}  python {info['python']}  "
          f"numpy {info['numpy']}  pillow {info['pillow']}  scipy {info['scipy']}")
    print(f"{len(wrists)} wrist frames, {len(others)} other frames from {args.frames}")
    t0 = time.time()
    comp = components(args, det, ref, wrists, others)
    print(f"\n{'component':<14} {'p50 ms':>9} {'p95 ms':>9} {'max ms':>9} {'cpu %':>7} {'n':>7}")
    for name, r in comp.items():
        print(f"{name:<14} {r['p50_ms']:9.3f} {r['p95_ms']:9.3f} {r['max_ms']:9.3f} {r['cpu_pct']:7.1f} {r['n']:7d}")
    if "guard_ref" in comp:
        print(f"\nguard speedup over the pre-optimisation detector: "
              f"p50 x{comp['guard_ref']['p50_ms'] / comp['guard']['p50_ms']:.2f}, "
              f"p95 x{comp['guard_ref']['p95_ms'] / comp['guard']['p95_ms']:.2f}")
    # What the match asks of the guard: 10 Hz live guard + 20 Hz fast loop, two wrists each = 60 frames/s worst case
    load = 60 * comp["guard"]["mean_ms"] / 1000
    print(f"match guard load (60 wrist frames/s): ~{100 * load:.0f}% of one core")
    rows = sustained(args, det, wrists, others) if args.sustained > 0 else []
    result = {"machine": info, "label": args.label, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                                                             time.localtime(t0)),
              "frames": str(args.frames), "components": comp, "match_guard_core_fraction": load,
              "sustained": {"mode": args.sustained_mode, "seconds": args.sustained, "rows": rows} if rows else None}
    out = args.out or ROOT / "bench_results" / (f"mac_bench_{info['host']}_"
                                                f"{time.strftime('%Y%m%dT%H%M%S', time.localtime(t0))}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
