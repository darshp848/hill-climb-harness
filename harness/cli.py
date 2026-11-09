"""`python -m harness plans/x.yaml (--dry-run | --live | --mock)`."""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import socket
import sys
from pathlib import Path

from . import __version__
from .frames import FrameSource
from .log import RunLog
from .operator import Operator, load_lessons
from .plan import load_plan
from .runner import Runner
from .station_client import StationClient, StationError
from .verifiers import ManualVerifier, MonitorVerifier, Verifier, make_verifier

SAFETY = """\
LIVE RUN. This drives the real robot through the station agent.
  - Keep your hand on the e-stop for the whole run.
  - Make sure the workspace is clear of people and loose objects.
  - Ctrl+C (or typing s + Enter) stops the run; the arms hold where they are."""


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m harness", description="Hill Climb harness for Dream-1")
    p.add_argument("plan", help="path to a plan YAML file")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="read status/frames only; send no POSTs")
    mode.add_argument("--live", action="store_true", help="real run (asks you to type yes)")
    mode.add_argument("--mock", action="store_true", help="run against an in-process fake station")
    p.add_argument("--station", default="http://127.0.0.1:8091", help="station API URL")
    p.add_argument("--joints", default=None,
                   help="live joint torques: the NUC's harness.can_live URL (e.g. http://10.103.0.143:8097), or "
                        "'station' when the station/sim serves GET /joints itself; adds measured grip/strain to checks")
    p.add_argument("--view", default="http://127.0.0.1:8093", help="debug view URL")
    p.add_argument("--runs-dir", default="runs", help="where run logs go")
    p.add_argument("--scenario", default="happy", help="fake station scenario (--mock only)")
    p.add_argument("--detect", default=None, metavar="COLORS.yaml",
                   help="local ball/block detector (e.g. plans/table_colors.yaml): fast black-ball guard + boxes on "
                        "the monitor's frames")
    p.add_argument("--cable-ends", default=None, metavar="MODEL.onnx",
                   help="with --detect: label USB-C / USB-A cable ends on the monitor's frames (e.g. models/cable_end.onnx)")
    p.add_argument("--live-guard", action="store_true",
                   help="with --detect: run the guard on live GET /frame/<wrist>.jpg images (station add-on) in its own "
                        "thread, which sends /stop itself; replaces the per-poll guard on debug-view trips")
    p.add_argument("--live-guard-hz", type=float, default=10.0, help="live guard looks per second (default 10)")
    p.add_argument("--live-guard-frames", type=int, default=3,
                   help="yes answers on distinct live frames in a row that trip the live guard (default 3)")
    p.add_argument("--verifier", choices=["manual", "claude", "time", "claude+manual", "monitor", "monitor+manual"], default=None,
                   help="override every stage's verifier")
    p.add_argument("--claude-backend", choices=["api", "subscription", "openrouter"], default=None,
                   help="how the claude/monitor verifiers reach Claude: api (ANTHROPIC_API_KEY, the default) or "
                        "subscription (the logged-in `claude` CLI); same as $HARNESS_CLAUDE_BACKEND")
    p.add_argument("--skip", action="append", default=[], metavar="PREFIX",
                   help="drop the stages whose names start with PREFIX, e.g. --skip cable_ (repeatable; practice)")
    p.add_argument("--checkpoint", default=None, help="override the plan's checkpoint, e.g. a fine-tune")
    p.add_argument("--no-rewrite", action="store_true",
                   help="never send monitor-written prompts (a fine-tune only follows its recorded task texts)")
    p.add_argument("--budget", type=float, default=None, metavar="S",
                   help="override the plan's time_budget_s (practice; a match is 295 s)")
    p.add_argument("--lessons", default="lessons/operator.md", metavar="FILE",
                   help="operator lessons: read into the monitor's system prompt, and new ones appended")
    p.add_argument("--no-operator", action="store_true",
                   help="ignore typed lines other than s (stop). Use this in a match: any human input is a DQ")
    p.add_argument("--match", action="store_true",
                   help="match mode: no operator console (any human input is a DQ), no --budget override, no manual "
                        "verifier; operator lessons from practice are still read into the monitor")
    p.add_argument("--var", action="append", default=[], metavar="NAME=VALUE",
                   help="fill a {NAME} placeholder in the plan, e.g. --var color=orange (repeatable)")
    return p


MANUAL_VERIFIERS = ("manual", "claude+manual", "monitor+manual")


def match_problems(args: argparse.Namespace, plan) -> list[str]:
    """Why these args and this plan can't run as a match (empty list = fine). Any human input after go is a DQ."""
    problems = []
    if args.budget:
        problems.append("--budget: a match runs on the plan's own time_budget_s")
    if args.verifier in MANUAL_VERIFIERS:
        problems.append(f"--verifier {args.verifier}: a typed y/n is human input")
    manual = sorted({st.name for st in plan.stages if st.verifier in MANUAL_VERIFIERS})
    if manual and args.verifier not in ("monitor", "claude", "time"):
        problems.append(f"stages answered by a person: {', '.join(manual)} (use --verifier monitor)")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.match:
        args.no_operator = True
    variables: dict[str, str] = {}
    for item in args.var:
        name, sep, value = item.partition("=")
        if not sep or not name.strip() or not value.strip():
            print(f"bad --var {item!r}: use NAME=VALUE", file=sys.stderr)
            return 2
        variables[name.strip()] = value.strip()
    try:
        plan = load_plan(args.plan, variables)
    except (ValueError, OSError) as exc:
        print(f"plan error: {exc}", file=sys.stderr)
        return 2
    if args.claude_backend:
        os.environ["HARNESS_CLAUDE_BACKEND"] = args.claude_backend
    if args.skip:
        plan.done_before = [f"{st.name}: {st.prompt}" for st in plan.stages if any(st.name.startswith(x) for x in args.skip)]
        plan.stages = [st for st in plan.stages if not any(st.name.startswith(x) for x in args.skip)]
        if not plan.stages:
            print("plan error: --skip removed every stage", file=sys.stderr)
            return 2
    if args.budget:
        plan.time_budget_s = args.budget
    if args.checkpoint:
        plan.checkpoint = args.checkpoint
    if args.no_rewrite:
        plan.rewrite_prompts = False
    lessons_path = None if args.no_operator else Path(args.lessons)
    # In a match the operator console is off, but the lessons written down in practice still guide the monitor.
    lessons = load_lessons(Path(args.lessons) if args.match else lessons_path)
    if lessons:
        plan.system_prompt = (plan.system_prompt + "\n\n" if plan.system_prompt else "") + \
            "LESSONS FROM THE OPERATOR IN EARLIER TEST RUNS (follow them)\n" + lessons
    if args.match:
        problems = match_problems(args, plan)
        if problems:
            print("not a match setup:\n  " + "\n  ".join(problems), file=sys.stderr)
            return 2
        if not (args.detect and args.live_guard) and plan.guard is not None:
            print("WARNING: match without --detect --live-guard: the illegal-ball guard only sees debug-view trips "
                  "(~3.4 s apart)", file=sys.stderr)
    if args.verifier:
        for stage in plan.stages:
            stage.verifier = args.verifier
    if plan.guard is not None and any(st.verifier not in ("monitor", "monitor+manual")
                                      for st in plan.stages if plan.guard.covers(st.name)):
        print("WARNING: plan.guard is only answered by the monitor; guarded stages without it are unguarded",
              file=sys.stderr)

    fake = None
    runner: Runner | None = None
    station_url, view_url = args.station, args.view
    if args.mock:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import fake_station  # noqa: PLC0415 (path set just above)

        fake = fake_station.FakeStation(scenario=args.scenario)
        fake.start()
        station_url, view_url = fake.api_url, fake.view_url
        print(f"mock station ({args.scenario}) at {station_url}")

    try:
        if args.live:
            print(SAFETY)
            if input('Type "yes" to start: ').strip() != "yes":
                print("not started.")
                return 1

        log = RunLog(args.runs_dir)
        print(f"logging to {log.path}")
        plan_path = Path(args.plan).resolve()
        log.update_manifest(
            plan_file=str(plan_path), plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            harness_version=__version__, args={k: v for k, v in vars(args).items()}, match=bool(args.match),
            host=socket.gethostname())
        dry = args.dry_run
        station = StationClient(station_url, dry_run=dry, log=lambda m: (print(m), log.event("dry_run", message=m))[0])
        # Small single frames from the station's /frame add-on, not whole debug-view round trips: those carry every
        # camera's 9-frame window (~2 MB a check) over the station's Wi-Fi and slowed Dream from ~0.8 s to 2-6 s.
        # The debug view is the fallback (no add-on, or HARNESS_FRAMES=view; the mock station has only the view).
        light = os.environ.get("HARNESS_FRAMES", "view" if args.mock else "station") == "station"
        frames = FrameSource(view_url, station_url=station_url if light else None)
        log.update_manifest(frames="station /frame (light)" if light else "debug view /trip",
                            skipped_prefixes=args.skip, lessons=str(lessons_path) if lessons else None)

        holder: dict[str, Runner] = {}
        manual = None
        verifiers: dict[str, Verifier] = {}
        try:
            # One stdin reader serves y/n answers and the s + Enter stop.
            manual = ManualVerifier(on_stop=lambda: holder["r"].request_stop())
            for name in {s.verifier for s in plan.stages} | {"manual"}:
                verifiers[name] = make_verifier(name, plan, manual=manual)
        except ValueError as exc:
            print(f"verifier error: {exc}", file=sys.stderr)
            return 2

        runner = Runner(plan, station, frames, verifiers, log)
        if not args.no_operator:
            monitor = next((m for v in verifiers.values() for m in (v, getattr(v, "second", None))
                            if isinstance(m, MonitorVerifier)), None)
            op = Operator(plan, log, transport=monitor._transport if monitor else None,
                          model=monitor.model if monitor else "claude-haiku-5-5", lessons_path=lessons_path)
            log.listeners.append(op.record)
            manual.on_text = op.submit
            runner.operator = op
        det = None
        if args.detect:
            from .detect import TableDetector  # noqa: PLC0415 (numpy + Pillow only when asked for)
            det = TableDetector.from_file(args.detect)
            if args.cable_ends:
                from .cable_end import CableEndClassifier  # noqa: PLC0415
                det.cable_ends = CableEndClassifier(args.cable_ends)
                log.event("cable_ends", model=str(args.cable_ends))
            runner.fast_guard = det.guard
            for v in verifiers.values():
                for m in (v, getattr(v, "second", None)):
                    if hasattr(m, "annotator"):
                        m.annotator, m.legend = det.annotate_frames, det.legend()
            log.event("detector", config=str(args.detect))
            if args.live_guard and plan.guard is not None:
                from .live_guard import LiveFrames, LiveGuard  # noqa: PLC0415
                wrists = ("wrist_l", "wrist_r")   # station camera names (GET /status cameras)
                live = LiveFrames(station_url, wrists)
                found = live.probe()
                if not found:
                    # no add-on or other camera names: say so. It still runs, in case the cameras come up; the
                    # per-poll guard on debug-view trips keeps running either way.
                    print(f"--live-guard: no live wrist frame at {station_url}/frame/<camera>.jpg yet "
                          f"({live.last_error}); the per-poll guard covers until one answers", file=sys.stderr)
                    log.event("warning", message=f"live guard: no /frame answer at startup ({live.last_error})")
                # All roles stay in, fetched in parallel: a camera missing at startup is used once it answers.
                runner.live_guard = LiveGuard(live.grab, det.guard, station.stop, rate_hz=args.live_guard_hz,
                                              confirm_frames=args.live_guard_frames, log=log.event)
                runner.live_guard.start()
                log.event("live_guard", station=station_url, roles=list(wrists), answering=found,
                          rate_hz=args.live_guard_hz, confirm_frames=args.live_guard_frames)
        elif args.live_guard:
            print("--live-guard needs --detect (the colour detector judges the live frames)", file=sys.stderr)
            return 2
        if args.joints:
            from .joint_telemetry import JointTelemetry  # noqa: PLC0415
            joints_url = station_url if args.joints == "station" else args.joints
            runner.joints = JointTelemetry(joints_url, log=log.event).start()
            log.event("joint_telemetry", url=joints_url)
        if plan.start_pose is not None:
            from .start_pose import StartPoses  # noqa: PLC0415
            ref = Path(plan.start_pose.reference)
            if not ref.is_absolute() and not ref.exists():
                ref = plan_path.parent / ref            # relative to the plan file
            try:
                runner.start_poses = StartPoses.load(ref)
                log.event("start_pose_reference", path=str(ref), prompts=len(runner.start_poses.by_prompt))
            except (OSError, ValueError) as exc:
                print(f"start_pose: {exc}; start-pose checks are off", file=sys.stderr)
                log.event("warning", message=f"start_pose reference not loaded: {exc}")
        if plan.fast:
            from .fastloop import FastLoop  # noqa: PLC0415
            from .statespace import Weights  # noqa: PLC0415
            fw = Weights(**(plan.fast.get("weights") or {}))
            frames_fn = None
            if det is not None:
                from .live_guard import LiveFrames  # noqa: PLC0415
                frames_fn = LiveFrames(station_url, ("wrist_l", "wrist_r")).grab
            runner.fast = FastLoop(weights=fw, rate_hz=float(plan.fast.get("rate_hz", 20.0)),
                                   mode=str(plan.fast.get("mode", "shadow")), frames=frames_fn,
                                   guard_judge=det.guard if det is not None else None,
                                   grippers=getattr(station, "grippers", None), stop=station.stop, log=log.event)
            runner.fast.start()
            log.event("fast_loop", rate_hz=plan.fast.get("rate_hz", 20.0), mode=plan.fast.get("mode", "shadow"),
                      weights=plan.fast.get("weights") or {})
        holder["r"] = runner
        # Turn SIGTERM into the same clean stop as Ctrl+C.
        try:
            signal.signal(signal.SIGTERM, lambda *_: runner.request_stop())
        except ValueError:
            pass
        mode = "DRY RUN" if dry else ("MOCK" if args.mock else "LIVE")
        print(f"{mode}: {plan.task or '(no task)'}  [{len(plan.stages)} stages]  type s + Enter to stop")
        if runner.operator is not None:
            print("Type anything else to talk to the harness (\"skip the next step\", \"why did it grab the pink one?\", "
                  "\"approach the tower slower next time\"). Testing only: human input in a match is a DQ.")
        try:
            result = runner.run()
        except StationError as exc:
            print(f"station error: {exc.message}", file=sys.stderr)
            return 1
        for r in result.stages:
            print(f"  {'ok  ' if r.ok else 'FAIL'} {r.name:<20} attempts={r.attempts} {r.seconds:6.1f}s  {r.reason}")
        print(f"{'SUCCESS' if result.ok else 'FAILED'}: {result.reason}  ({result.seconds:.1f}s)  log: {log.path}")
        if result.guard_tripped and plan.guard is not None:
            bar = "!" * (len(plan.guard.message) + 8)
            print(f"\a\a\n{bar}\n!   {plan.guard.message}   !\n{bar}\n")
        if result.ok and plan.done_message:
            bar = "#" * (len(plan.done_message) + 8)
            print(f"\a\n{bar}\n#   {plan.done_message}   #\n{bar}\n")
        if not dry and not args.mock:
            print("The arms hold where they are. Home them with: dreamcli robot down")
        return 0 if result.ok else 1
    finally:
        if runner is not None and runner.live_guard is not None:
            runner.live_guard.close()
        if runner is not None and getattr(runner, "fast", None) is not None:
            runner.fast.close()
        if fake is not None:
            fake.close()
