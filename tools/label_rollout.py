"""Label Dream's own record of a harness run for fine-tuning.

Dream keeps each run's record (the station's archiver copies it to ~/rollouts/zeroshot_<time>/): last-run.csv (20 Hz
joints and commands) and last-run_inputs/inputs.jsonl (every model input: frames, state and the prompt it was given).
The harness knows what those seconds were for. This joins the two:

    python tools/label_rollout.py runs/<stamp> <rollout dir or its inputs.jsonl>

and writes <rollout>/harness_labels.jsonl, one line per model input, plus harness_segments.json:
  stage, attempt     the harness stage and attempt the input belongs to
  prompt_sent        what Dream was told (stage prompt, a monitor rewrite, a recovery, an operator prompt)
  source             stage | rewrite | recovery | operator | tower
  label              the clean task label for training: the stage's own prompt (rewrites map back to it)
  outcome            ok | failed for the attempt the input is in; usable = ok and not a recovery

The clocks differ (Dream's t_s is monotonic, the harness logs epoch time). They are aligned on the first /start:
its harness time is the first model input's time, to about a second.
"""
from __future__ import annotations

import json
import pathlib
import sys


def _load(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _source(why: str) -> str:
    why = why or ""
    if why.startswith("monitor next_prompt"):
        return "rewrite"
    if why.startswith("recovery"):
        return "recovery"
    if why == "operator":
        return "operator"
    return "stage"


def label(run: pathlib.Path, rollout: pathlib.Path) -> tuple[list[dict], list[dict]]:
    inputs_path = rollout if rollout.suffix == ".jsonl" else rollout / "last-run_inputs" / "inputs.jsonl"
    inputs = [r for r in _load(inputs_path) if r.get("kind") == "inputs"]
    events = _load(run / "events.jsonl")
    start = next(e for e in events if e["event"] == "start")
    offset = start["t"] - inputs[0]["t_s"]   # harness epoch = Dream t_s + offset

    # Spans: [t, stage, attempt, prompt, source, label], each running until the next prompt change.
    spans, stage, attempt, base = [], None, 1, ""
    outcomes: dict[tuple[str, int], str] = {}
    for e in events:
        ev = e["event"]
        if ev == "stage_start":
            stage, attempt, base = e["stage"], 1, e.get("prompt") or ""
        elif ev == "tower_prompt" and e.get("next"):
            base = e.get("prompt") or base
        elif ev == "attempt_end":   # `retry` is logged after the retry's prompt, so count from here
            outcomes[(e["stage"], e["attempt"])] = "ok" if e.get("ok") else "failed"
            attempt = e["attempt"] + 1
        elif ev == "prompt_change" and stage is not None:
            src = _source(e.get("why"))
            spans.append({"t": e["t"], "stage": stage, "attempt": attempt, "prompt_sent": e.get("prompt") or "",
                          "source": src, "label": base if src in ("stage", "rewrite") else e.get("prompt") or ""})
    rows = []
    for r in inputs:
        t = r["t_s"] + offset
        span = next((s for s in reversed(spans) if s["t"] <= t + 0.5), None)
        if span is None:
            continue
        outcome = outcomes.get((span["stage"], span["attempt"]), "unknown")
        rows.append({"t_s": r["t_s"], **{k: span[k] for k in ("stage", "attempt", "prompt_sent", "source", "label")},
                     "prompt_seen": (r.get("state") or {}).get("prompt"), "outcome": outcome,
                     "usable": outcome == "ok" and span["source"] != "recovery"})
    segs: list[dict] = []
    for row in rows:
        key = (row["stage"], row["attempt"])
        if segs and (segs[-1]["stage"], segs[-1]["attempt"]) == key:
            segs[-1]["t_end"] = row["t_s"]
            segs[-1]["inputs"] += 1
        else:
            segs.append({"stage": row["stage"], "attempt": row["attempt"], "t_start": row["t_s"], "t_end": row["t_s"],
                         "inputs": 1, "label": row["label"], "outcome": row["outcome"]})
    return rows, segs


if __name__ == "__main__":
    run, rollout = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    rows, segs = label(run, rollout)
    out = rollout.parent if rollout.suffix == ".jsonl" else rollout
    (out / "harness_labels.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (out / "harness_segments.json").write_text(json.dumps(segs, indent=2))
    mismatch = sum(1 for r in rows if r["prompt_seen"] and r["prompt_seen"] != r["prompt_sent"])
    for s in segs:
        print(f"{s['outcome']:<7} {s['stage']:<18} a{s['attempt']}  {s['t_end'] - s['t_start']:5.0f} s  "
              f"{s['inputs']:3d} inputs  {s['label'][:70]}")
    print(f"{len(rows)} inputs labelled, {sum(r['usable'] for r in rows)} usable; prompt mismatches "
          f"(alignment check, want ~0): {mismatch}  -> {out}/harness_labels.jsonl")
