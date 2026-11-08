"""Run one isolated, real-time sim match. Scripted policy is for offline validation only."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.metadata
import json
import os
import signal
import threading
import time
import urllib.request
from pathlib import Path

import psutil

from cloud.judge import DelayedOracle
from harness.detect import TableDetector
from harness.frames import FrameSource
from harness.live_guard import LiveFrames, LiveGuard
from harness.log import RunLog
from harness.plan import load_plan
from harness.runner import Runner
from harness.start_pose import StartPoses
from harness.station_client import StationClient
from harness.verifiers import MonitorVerifier, anthropic_transport
from sim_station import SimStation

ROOT = Path(__file__).resolve().parents[1]


def sim_text(value):
    """The sim's balls have the real table's colours (sim_scenes.hill_climb_table: green, yellow and orange golf
    balls legal; a white golf ball and a black 8-ball illegal), so plan texts apply as written: no translation."""
    return value


def start_live_snapshots(sim, log, folder, stop, every_s=15.0, keep_every=4):
    """For watching running jobs: every `every_s` write live/{scene,wrist_l,wrist_r}.jpg (latest), live/status.json
    and a line in live/timeline.jsonl; every `keep_every`-th scene frame is also kept as live/scene_NNN.jpg.
    The entrypoint uploads live/ while the match runs. Never raises into the match."""
    folder.mkdir(parents=True, exist_ok=True)
    state = {"stage": None, "attempt": None, "last_verdict": None, "started": time.monotonic()}

    def track(event, fields):
        if event == "stage_start":
            state["stage"] = fields.get("stage")
        elif event == "attempt_start":
            state["attempt"] = fields.get("attempt")
        elif event == "verdict":
            state["last_verdict"] = {k: fields.get(k) for k in ("done", "confidence", "reason")}
            state["last_verdict"]["reason"] = str(state["last_verdict"]["reason"])[:200]
    log.listeners.append(track)

    def loop():
        n = 0
        while not stop.wait(every_s if n else 2.0):
            try:
                for cam in ("scene", "wrist_l", "wrist_r"):
                    with urllib.request.urlopen(f"{sim.api_url}/frame/{cam}.jpg?w=480", timeout=5) as reply:
                        (folder / f"{cam}.jpg").write_bytes(reply.read())
                if n % keep_every == 0:
                    (folder / f"scene_{n // keep_every:03d}.jpg").write_bytes((folder / "scene.jpg").read_bytes())
                with sim._lock:
                    ref = dict(sim._poses.get("referee") or {})
                status = {"wall_s": round(time.monotonic() - state["started"], 1), "sim_t": ref.get("t"),
                          "stage": state["stage"], "attempt": state["attempt"], "points": ref.get("points"),
                          "unplugged": ref.get("unplugged"), "tower_in": ref.get("tower_in"),
                          "balls": ref.get("golf_in_bin"), "eight_in_bin": ref.get("eight_in_bin"),
                          "dream": sim.dream_summary(),
                          "last_verdict": state["last_verdict"]}
                (folder / "status.json").write_text(json.dumps(status), encoding="utf-8")
                with (folder / "timeline.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(status) + "\n")
            except Exception as exc:   # a missed snapshot must never touch the match
                (folder / "snapshot_error.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
            n += 1

    thread = threading.Thread(target=loop, name="live-snapshots", daemon=True)
    thread.start()
    return thread


def prepare_plan(path, skip=(), *, notes=True, technique=None, level=None):
    """technique/level None = as the plan file says."""
    plan = sim_text(load_plan(path, operator_notes=notes, prompt_technique=technique))
    if level is not None:
        plan.prompt_level = level
    plan.stages = [s for s in plan.stages if not any(s.name.startswith(x) for x in skip)]
    if not plan.stages or any(s.verifier != "monitor" for s in plan.stages):
        raise ValueError("cloud matches require monitor-only stages and no human input")
    if not plan.guard or plan.time_budget_s > 300:
        raise ValueError("cloud matches require a guard and a budget no longer than 300s")
    plan.claude_user["parallel"] = 2
    # Read-only practice lessons, as in --match. Adapt their ball colours too.
    lesson = ROOT / "lessons/operator.md"
    if lesson.exists():
        plan.system_prompt += "\n" + sim_text(lesson.read_text(encoding="utf-8"))
    plan.system_prompt += ("\nSIM SCENE: the robot is a pair of YAM arms. The green, yellow and orange golf balls are "
                           "legal; the white golf ball and the black 8-ball are illegal. The hub is silver. The bin is "
                           "white. Judge the rendered scene, not the physical station photographs.")
    return plan


def run_match(args):
    print(f"Will create one {args.policy}/{args.judge} match and logs under {args.out}", flush=True)
    os.environ["HARNESS_CLAUDE_BACKEND"] = "api"
    if args.judge == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise ValueError("USER ACTION NEEDED: supply ANTHROPIC_API_KEY through Secret Manager")
    plan_path = ROOT / args.plan
    plan = prepare_plan(plan_path, args.skip, notes=args.operator_notes != "off",
                        technique={"on": True, "off": False}.get(args.prompt_technique), level=args.prompt_level)
    detector = TableDetector.from_file(ROOT / args.detect)
    log = RunLog(args.out)
    proc = psutil.Process()
    started, cpu0 = time.monotonic(), time.process_time()
    peak = [proc.memory_info().rss]
    done = threading.Event()

    def sample():
        while not done.wait(0.2):
            peak[0] = max(peak[0], proc.memory_info().rss)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    sim = None
    runner = None
    summary = None
    try:
        sim = SimStation(policy=args.policy, scene="hill_climb", realtime=True,
                         api_port=0, view_port=0, max_trips=4,
                         execution_horizon=args.execution_horizon or None, sync_above_ms=args.sync_above_ms)
        sim.hedge = args.dream_hedge     # Dream sessions raced per action chunk (sim_station.HedgedPolicy)
        sim.hedge_cutoff_s, sim.hedge_max_in_flight, sim.hedge_max_calls = (
            args.dream_hedge_cutoff, args.dream_hedge_max_in_flight, args.dream_hedge_max_calls)
        sim.dream_pipeline = args.dream_pipeline   # Dream sessions used in turn for throughput (PipelinedPolicy)
        sim = sim.start()
        station = StationClient(sim.api_url)
        live_thread = start_live_snapshots(sim, log, Path(args.out) / "live", done)
        station.reset()
        if args.judge == "oracle":
            mon = DelayedOracle(plan, sim.api_url, args.seed)
        elif args.judge == "openrouter":
            from harness.verifiers import openrouter_transport
            plan.claude_user["hedge"] = args.judge_hedge          # identical copies per call, first good reply wins
            models = [m.strip() for m in args.judge_models.split(",") if m.strip()]
            mon = MonitorVerifier(plan, model=models[0],
                                  transport=openrouter_transport(models, mode=args.judge_mode))
        else:
            mon = MonitorVerifier(plan, transport=anthropic_transport(os.environ["ANTHROPIC_API_KEY"]))
        mon.annotator, mon.legend = detector.annotate_frames, detector.legend()
        frames = FrameSource(sim.view_url, station_url=sim.api_url)
        runner = Runner(plan, station, frames, {"monitor": mon}, log)
        runner.fast_guard = detector.guard
        # Infrastructure health, so a judge/Dream outage can't pass as a low-scoring "complete" match.
        health = {"verdicts": 0, "monitor_errors": 0, "guard_blind": 0, "attempts_aborted": 0, "last_error": None}

        def count_health(event, fields):
            if event == "verdict":
                health["verdicts"] += 1
                if str(fields.get("reason", "")).startswith("monitor error"):
                    health["monitor_errors"] += 1
                    health["last_error"] = str(fields["reason"])[:300]
            elif event == "guard_blind":
                health["guard_blind"] += 1
            elif event == "attempt_end" and str(fields.get("reason", "")).startswith(("RuntimeError", "OSError")):
                health["attempts_aborted"] += 1
                health["last_error"] = str(fields["reason"])[:300]
        log.listeners.append(count_health)
        if args.judge == "oracle":
            def stage_baseline(event, fields):
                if event == "stage_start":
                    with sim._lock:
                        mon.oracle.start[fields["stage"]] = dict(sim._poses["referee"])
            log.listeners.append(stage_baseline)
        from harness.joint_telemetry import JointTelemetry
        runner.joints = JointTelemetry(sim.api_url, log=log.event,                # live joint torques -> monitor, log
                                       record_to=str(Path(args.out) / "joints.jsonl")).start()   # observer training data
        live = LiveFrames(sim.api_url)
        if set(live.probe()) != {"wrist_l", "wrist_r"}:
            raise RuntimeError("live guard wrist-camera probe failed")
        runner.live_guard = LiveGuard(live.grab, detector.guard, station.stop,
                                      rate_hz=10, confirm_frames=3, log=log.event)
        if plan.start_pose:
            runner.start_poses = StartPoses.load(ROOT / "plans" / plan.start_pose.reference)
        plan_data = dataclasses.asdict(plan)
        log.update_manifest(match=True, policy=args.policy, judge=args.judge, seed=args.seed,
                            judge_models=args.judge_models, judge_mode=args.judge_mode,
                            judge_hedge=args.judge_hedge, dream_hedge=args.dream_hedge,
                            label=args.label, scene="hill_climb", scene_randomisation="none",
                            detector=args.detect, live_guard=True, operator=False,
                            plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                            effective_plan=plan_data, sim_colour_adaptation=True,
                            versions={p: importlib.metadata.version(p) for p in ("mujoco", "dreamlabs")})
        runner.live_guard.start()
        signal.signal(signal.SIGTERM, lambda *_: runner.request_stop())
        match_start = time.monotonic()
        result = runner.run()
        # An early harness finish holds the arms through the rest of the scoring clock.
        while time.monotonic() - match_start < 300 and not runner._stop_event.is_set():
            time.sleep(min(0.25, 300 - (time.monotonic() - match_start)))
        with sim._lock:
            ref = dict(sim._poses["referee"])
        wall = time.monotonic() - match_start
        complete = not runner._stop_event.is_set() and wall >= 299
        sim_t = ref.get("t")
        error_rate = health["monitor_errors"] / max(1, health["verdicts"])
        problems = [why for why, bad in [
            ("monitor errors over 20% of verdicts", error_rate > 0.2),
            ("guard went blind", health["guard_blind"] > 0),
            ("station/Dream error aborted an attempt", health["attempts_aborted"] > 0),
            ("harness stopped on an error", "error" in str(result.reason).lower()),
            ("no judge verdicts", health["verdicts"] == 0)] if bad]
        summary = {"schema_version": 1, "scenario": "hill_climb", "label": args.label,
                   "seed": args.seed, "policy": args.policy, "judge": args.judge,
                   "judge_models": args.judge_models, "judge_mode": args.judge_mode,
                   "judge_reasoning": os.environ.get("HARNESS_OPENROUTER_REASONING"),
                   "operator_notes": bool(plan.operator_notes), "prompt_technique": plan.prompt_technique,
                   "prompt_level": plan.prompt_level, "dream_hedge": args.dream_hedge, "dream_pipeline": args.dream_pipeline,
                   "execution_horizon": args.execution_horizon,
                   "dream": sim.dream_summary(),
                   "harness": dataclasses.asdict(result), "referee": ref,
                   "stages": [dataclasses.asdict(s) for s in result.stages],
                   "match_wall_s": wall,
                   "complete": complete,
                   # valid = complete AND the infrastructure worked; compare judges on valid rows only.
                   "valid": complete and not problems, "invalid_because": problems, "health": health,
                   "sim_t": sim_t, "sim_rate": round(sim_t / wall, 3) if sim_t and wall else None,
                   "monitor_calls": getattr(mon, "calls", mon._seq),
                   "milestones": {"stage1_at": ref["stage1_at"], "stage2_at": ref["stage2_at"],
                                  "first_touch": ref["first_touch"]},
                   "effective_plan_sha256": hashlib.sha256(json.dumps(plan_data, sort_keys=True).encode()).hexdigest(),
                   "versions": {p: importlib.metadata.version(p) for p in ("mujoco", "dreamlabs")},
                   "source_revision": os.environ.get("SOURCE_REVISION", "local")}
    finally:
        if runner and runner.live_guard:
            runner.live_guard.close()
        if runner and getattr(runner, "joints", None):
            runner.joints.close()
        if sim:
            sim.close()
        done.set()
        thread.join()
    summary["metrics"] = {"wall_s": round(time.monotonic() - started, 3),
                          "cpu_s": round(time.process_time() - cpu0, 3),
                          "peak_rss_mib": round(peak[0] / 2**20, 2),
                          "host_cpu_count": os.cpu_count(), "platform": os.name}
    target = Path(args.out) / "match.json"
    target.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"result": str(target), "metrics": summary["metrics"], "referee": summary["referee"]}), flush=True)
    return summary


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--policy", choices=["dream", "scripted"], default="dream")
    p.add_argument("--judge", choices=["oracle", "api", "openrouter"], default="oracle")
    p.add_argument("--judge-models", default="google/gemini-3.1-flash-lite",
                   help="OpenRouter model ids, comma-separated (--judge openrouter)")
    p.add_argument("--judge-mode", choices=["race", "alternate"], default="race")
    p.add_argument("--judge-hedge", type=int, default=1)
    p.add_argument("--dream-hedge", type=int, default=1, help="Dream sessions raced per chunk (N x Dream usage)")
    p.add_argument("--dream-hedge-cutoff", type=float, default=None, help="seconds before a slow call is cut and resent")
    p.add_argument("--dream-hedge-max-in-flight", type=int, default=None, help="most hedged calls running at once")
    p.add_argument("--dream-hedge-max-calls", type=int, default=None, help="most hedged calls per action chunk")
    p.add_argument("--plan", default="plans/hill_climb_v4.yaml")
    p.add_argument("--skip", action="append", default=[])
    p.add_argument("--operator-notes", choices=["plan", "off"], default="plan",
                   help="off = ignore the plan's operator_notes (A/B control)")
    p.add_argument("--prompt-technique", choices=["plan", "on", "off"], default="plan")
    p.add_argument("--prompt-level", type=int, choices=range(0, 6), default=None)
    p.add_argument("--execution-horizon", type=int, default=10,
                   help="Dream async pacing: rows per chunk before the next takes over (10 at 20 Hz = 2 Hz calls); "
                        "0 = Dream's example loop (whole chunk, physics holds during inference)")
    p.add_argument("--dream-pipeline", type=int, default=1,
                   help="Dream sessions used in turn, so a new chunk can land every 0.5 s even with a ~1 s round trip "
                        "(N x Dream usage); needs --execution-horizon")
    p.add_argument("--sync-above-ms", type=float, default=1900.0,
                   help="round trip past which the pacing goes sync (10 rows + 1.9 s x 20 Hz = the 48-row chunk)")
    p.add_argument("--detect", default="plans/sim_table_colors.yaml")
    p.add_argument("--match", action="store_true", default=True)
    p.add_argument("--live-guard", action="store_true", default=True)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--label", default="hill_climb_v4")
    p.add_argument("--out", type=Path, default=Path("/out"))
    return p


if __name__ == "__main__":
    run_match(parser().parse_args())
