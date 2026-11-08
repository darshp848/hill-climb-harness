"""Benchmark the real stage monitor. --dry-run validates requests without keys or network.

Examples:
    python tools/bench_judges.py --dry-run --no-sim
    python tools/bench_judges.py --judge openrouter:google/gemini-3.1-flash-lite -n 3
    python tools/bench_judges.py --judge openrouter:anthropic/claude-haiku-5.5,google/gemini-3.1-flash-lite --hedge 1 --hedge 2

Live runs cost money. The spend threshold stops NEW trials after accounting for all
copies of the current trial. One trial can overshoot it. Set a key credit limit too.
"""

from __future__ import annotations

import argparse
import base64
import copy
import csv
import hashlib
import json
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.plan import Plan, load_plan  # noqa: E402
from harness.verifiers import (  # noqa: E402
    MonitorVerifier, anthropic_transport, claude_cli_transport, frames_digest,
    openrouter_request_body, openrouter_transport, valid_jpeg,
)
from tools.check_monitor import CASES, PHOTO, SIM_CASES, sim_frames  # noqa: E402

# Public /api/v1/models, checked 2026-11-08. All accept images and return text.
# Prices and availability change; this tool never fetches the catalog implicitly.
DEFAULT_MODELS = (
    "anthropic/claude-haiku-5.5",
    "google/gemini-3.1-flash-lite",
    "qwen/qwen3.8-flash",
    "z-ai/glm-5.3-flash",
    "meta-llama/llama-4-scout",
    "openai/gpt-5-nano",
    "mistralai/mistral-small-2603",
    "google/gemma-4-26b-a4b-it",
)
BASELINE = DEFAULT_MODELS[0]
SUMMARY_FIELDS = (
    "judge", "trials", "guard_trials", "stage_done_accuracy", "accepted_done_accuracy", "guard_accuracy",
    "parse_failure_rate", "transport_failure_rate", "dry_run_valid_rate", "latency_p50_s", "latency_p90_s",
    "latency_max_s", "attempts_finished", "cost_complete", "known_cost_usd", "cost_usd", "cost_per_call_usd",
    "cost_per_trial_usd",
)


@dataclass(frozen=True)
class Judge:
    backend: str
    models: tuple[str, ...]
    hedge: int = 1

    @property
    def name(self) -> str:
        return f"{self.backend}:{','.join(self.models)};hedge={self.hedge}"

    @property
    def attempts(self) -> int:
        return self.hedge * len(self.models)


def judge_spec(value: str) -> tuple[str, tuple[str, ...]]:
    backend, sep, names = value.partition(":")
    if not sep or backend not in ("openrouter", "api", "subscription"):
        raise argparse.ArgumentTypeError("use BACKEND:MODEL[,MODEL], backend openrouter/api/subscription")
    models = tuple(m.strip() for m in names.split(","))
    if not all(models) or any(any(c.isspace() for c in m) for m in models):
        raise argparse.ArgumentTypeError("model IDs must be nonempty and contain no whitespace")
    if backend != "openrouter" and len(models) != 1:
        raise argparse.ArgumentTypeError("model races require the openrouter backend")
    return backend, models


def positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def nonnegative_float(value: str) -> float:
    n = float(value)
    if not math.isfinite(n) or n < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return n


def validate_translation(payload: dict, body: dict) -> int:
    """Check every translated block against its source, including image bytes and order."""
    expected = []
    system = payload.get("system")
    if system:
        text = system if isinstance(system, str) else " ".join(b.get("text", "") for b in system)
        expected.append({"role": "system", "content": text})
    images = 0
    for message in payload["messages"]:
        content = message["content"]
        if isinstance(content, list):
            parts = []
            for block in content:
                if block["type"] == "text":
                    parts.append({"type": "text", "text": block["text"]})
                elif block["type"] == "image":
                    src = block["source"]
                    data = base64.b64decode(src["data"], validate=True)
                    if src["media_type"] != "image/jpeg" or not valid_jpeg(data):
                        raise ValueError("monitor input must contain complete JPEG images")
                    parts.append({"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{src['data']}"}})
                    images += 1
                else:
                    raise ValueError("unsupported monitor content block")
            content = parts
        expected.append({"role": message["role"], "content": content})
    if body["messages"] != expected or body["max_tokens"] != payload["max_tokens"] or images == 0:
        raise ValueError("monitor request translation mismatch")
    if set(body) != {"model", "messages", "max_tokens"}:
        raise ValueError("unexpected wire fields")
    json.dumps(body, allow_nan=False)
    return images


def numeric_cost(usage: Any) -> float | None:
    value = usage.get("cost") if isinstance(usage, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


class Accounting:
    """One trial's attempts. No prompts, reply text, headers or exception messages are retained."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.events: list[dict] = []

    def record(self, event: dict) -> None:
        usage = event.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        # Retain only numeric usage fields, never provider-supplied strings.
        safe_usage = {k: v for k, v in usage.items()
                      if k in ("cost", "prompt_tokens", "completion_tokens", "total_tokens",
                               "input_tokens", "output_tokens")
                      and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}
        with self.condition:
            self.events.append({"model": event["model"], "usage": safe_usage,
                                "cost_usd": numeric_cost(usage),
                                "latency_s": event.get("latency_s"),
                                "error_type": event.get("error_type")})
            self.condition.notify_all()

    def drain(self, expected: int, timeout_s: float) -> tuple[list[dict], bool]:
        with self.condition:
            complete = self.condition.wait_for(lambda: len(self.events) >= expected, timeout=timeout_s)
            return list(self.events), complete


def transport_for(judge: Judge, accounting: Accounting, *, dry_run: bool, timeout_s: float):
    if dry_run:
        def dry(payload: dict) -> dict:
            for model in judge.models:
                started = time.monotonic()
                error = None
                try:
                    validate_translation(payload, openrouter_request_body(model, payload))
                except Exception as exc:
                    error = type(exc).__name__
                    raise
                finally:
                    accounting.record({"model": model, "latency_s": time.monotonic() - started,
                                       "error_type": error})
            # A stub exercises the monitor parser and hedge only. It is never scored.
            return {"content": [{"type": "text", "text": '<json>{"stage_done": false, '
                                 '"guard": false, "confidence": 1, "reason": "dry-run stub"}</json>'}]}
        return dry
    if judge.backend == "openrouter":
        return openrouter_transport(list(judge.models), timeout_s=timeout_s, on_attempt=accounting.record)
    if judge.backend == "api":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError("api backend needs ANTHROPIC_API_KEY")
        raw = anthropic_transport(key, timeout_s)
    else:
        raw = claude_cli_transport(timeout_s)

    def observed(payload: dict) -> dict:
        started, reply, error = time.monotonic(), {}, None
        try:
            reply = raw(payload)
            return reply
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            accounting.record({"model": judge.models[0], "usage": reply.get("usage"),
                               "latency_s": time.monotonic() - started, "error_type": error})
    return observed


def run_trial(plan: Plan, judge: Judge, case: tuple, frames: dict[str, bytes], repeat: int,
              *, dry_run: bool, timeout_s: float) -> dict:
    source, stage_name, want_done, want_guard = case
    trial_plan = copy.deepcopy(plan)
    trial_plan.claude_user.update(hedge=judge.hedge, timeout_s=timeout_s)
    accounting = Accounting()
    transport = transport_for(judge, accounting, dry_run=dry_run, timeout_s=timeout_s)
    mon = MonitorVerifier(trial_plan, model=judge.models[0], transport=transport, timeout_s=timeout_s)
    stage = next(s for s in trial_plan.stages if s.name == stage_name)
    started = time.monotonic()
    verdict = mon.check_now(stage, frames, 5.0)
    latency = time.monotonic() - started
    events, drained = accounting.drain(judge.attempts, timeout_s + 5)
    transport_failed = any(e["error_type"] for e in events)
    parse_failed = verdict.stage_done is None and any(not e["error_type"] for e in events)
    cost_complete = drained and all(e["cost_usd"] is not None for e in events)
    return {
        "judge": judge.name, "backend": judge.backend, "models": list(judge.models), "hedge": judge.hedge,
        "source": source, "stage": stage_name, "repeat": repeat, "frames_sha1": frames_digest(frames),
        "expected_stage_done": want_done, "expected_guard": want_guard,
        "stage_done": None if dry_run else verdict.stage_done,
        "accepted_done": None if dry_run else verdict.done is True,
        "guard": None if dry_run else verdict.guard,
        "stage_correct": None if dry_run else verdict.stage_done is want_done,
        "accepted_done_correct": None if dry_run else verdict.stage_done is not None and (verdict.done is True) == want_done,
        "guard_correct": None if dry_run or want_guard is None else verdict.guard is want_guard,
        "parse_failure": None if dry_run else parse_failed,
        "transport_failure": None if dry_run else transport_failed,
        "dry_run_valid": drained and not transport_failed if dry_run else None,
        "latency_s": None if dry_run else latency,
        "attempts_expected": judge.attempts, "attempts_finished": len(events), "drained": drained,
        "known_cost_usd": sum(e["cost_usd"] or 0 for e in events),
        "cost_usd": sum(e["cost_usd"] for e in events) if cost_complete and not dry_run else None,
        "cost_complete": cost_complete and not dry_run, "attempts": events,
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * fraction
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def summaries(rows: list[dict]) -> list[dict]:
    out = []
    for name in dict.fromkeys(r["judge"] for r in rows):
        group = [r for r in rows if r["judge"] == name]
        def mean(field: str):
            values = [r[field] for r in group if r[field] is not None]
            return sum(values) / len(values) if values else None
        times = [r["latency_s"] for r in group if r["latency_s"] is not None]
        finished = sum(r["attempts_finished"] for r in group)
        complete = all(r["cost_complete"] for r in group)
        known = sum(r["known_cost_usd"] for r in group)
        out.append({"judge": name, "trials": len(group), "guard_trials": sum(r["expected_guard"] is not None for r in group),
                    "stage_done_accuracy": mean("stage_correct"), "accepted_done_accuracy": mean("accepted_done_correct"),
                    "guard_accuracy": mean("guard_correct"), "parse_failure_rate": mean("parse_failure"),
                    "transport_failure_rate": mean("transport_failure"), "dry_run_valid_rate": mean("dry_run_valid"),
                    "latency_p50_s": percentile(times, .5), "latency_p90_s": percentile(times, .9),
                    "latency_max_s": max(times) if times else None,
                    "attempts_finished": finished, "cost_complete": complete, "known_cost_usd": known,
                    "cost_usd": known if complete else None,
                    "cost_per_call_usd": known / finished if complete and finished else None,
                    "cost_per_trial_usd": known / len(group) if complete else None})
    return out


def write_reports(out: Path, rows: list[dict], metadata: dict) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    summary = summaries(rows)
    (out / "results.json").write_text(json.dumps({**metadata, "summary": summary, "trials": rows},
                                                indent=2, allow_nan=False) + "\n", encoding="utf-8")
    with (out / "summary.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--judge", action="append", type=judge_spec, metavar="BACKEND:MODEL[,MODEL]",
                    help="repeat for multiple configs; default: eight OpenRouter singles plus a Haiku/Gemini race")
    ap.add_argument("--hedge", action="append", type=positive_int,
                    help="claude.hedge copies, repeat to compare e.g. 1 and 2; default: 1")
    ap.add_argument("-n", "--repeat", type=positive_int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-sim", action="store_true", help="photo cases only; default includes MuJoCo renders")
    ap.add_argument("--plan", type=Path, default=ROOT / "plans/hill_climb_v4.yaml")
    ap.add_argument("--max-cost-usd", type=nonnegative_float, default=1.0,
                    help="stop new trials at reported spend; missing cost also stops live runs; default $1")
    ap.add_argument("--timeout-s", type=nonnegative_float, default=90.0)
    ap.add_argument("--out", type=Path, default=ROOT / "runs" / "bench_judges")
    args = ap.parse_args(argv)
    if args.timeout_s == 0:
        ap.error("--timeout-s must be positive")
    specs = args.judge or [("openrouter", (m,)) for m in DEFAULT_MODELS] + [
        ("openrouter", (BASELINE, "google/gemini-3.1-flash-lite"))]
    judges = list(dict.fromkeys(Judge(b, m, h) for b, m in specs for h in (args.hedge or [1])))
    plan = load_plan(args.plan)
    cases = list(CASES) + ([] if args.no_sim else list(SIM_CASES))
    if not {c[1] for c in cases} <= {s.name for s in plan.stages}:
        ap.error("plan is missing required known-answer stages")
    if not plan.guard or not all(plan.guard.covers(c[1]) for c in cases if c[3] is not None):
        ap.error("plan must ask the guard on every guard-labelled case")
    metadata = {"dry_run": args.dry_run, "max_cost_usd": args.max_cost_usd,
                "plan": str(args.plan), "plan_sha256": hashlib.sha256(args.plan.read_bytes()).hexdigest(),
                "repeats": args.repeat, "sim": not args.no_sim,
                "configs": [j.name for j in judges], "stopped": None}
    rows: list[dict] = []
    sources = {}
    print(f"judge benchmark: {'DRY RUN, no network' if args.dry_run else 'LIVE, billable'}; {len(judges)} configs")
    try:
        if not args.dry_run and args.max_cost_usd == 0:
            metadata["stopped"] = "max_cost_usd"
        else:
            sources["photo"] = {"overhead": PHOTO.read_bytes()}
            if not args.no_sim:
                sources["sim_empty"], sources["sim_eight_held"] = sim_frames(False), sim_frames(True)
            spent = 0.0
            for judge in judges:
                for case in cases:
                    for repeat in range(1, args.repeat + 1):
                        if metadata["stopped"]:
                            break
                        row = run_trial(plan, judge, case, sources[case[0]], repeat,
                                        dry_run=args.dry_run, timeout_s=args.timeout_s)
                        rows.append(row)
                        spent += row["known_cost_usd"]
                        if args.dry_run:
                            if not row["dry_run_valid"]:
                                metadata["stopped"] = "dry_run_validation_failed"
                        elif not row["cost_complete"]:
                            metadata["stopped"] = "incomplete_cost_accounting"
                        elif spent >= args.max_cost_usd:
                            metadata["stopped"] = "max_cost_usd"
                        write_reports(args.out, rows, metadata)
                    if metadata["stopped"]:
                        break
                if metadata["stopped"]:
                    break
    except KeyboardInterrupt:
        metadata["stopped"] = "interrupted"
    except (Exception, SystemExit) as exc:
        # Credentials can occur in upstream exception text. Report only the type.
        metadata["stopped"] = f"setup_or_run_error:{type(exc).__name__}"
    summary = write_reports(args.out, rows, metadata)
    for item in summary:
        print(json.dumps(item, allow_nan=False))
    print(f"reports: {args.out / 'summary.csv'} and {args.out / 'results.json'}")
    if metadata["stopped"]:
        print(f"stopped: {metadata['stopped']}")
        return 2
    return 0 if args.dry_run or all(r["stage_correct"] and r["guard_correct"] is not False for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
