"""Download completed results using gcloud and emit stress-compatible JSONL."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path


def normalize(match):
    ref, harness = match["referee"], match["harness"]
    return {"scenario": match["scenario"], "seed": match["seed"], "label": match["label"],
            "points": ref["points"], "lost": ref["loses"], "dq": [ref["dq"]] if ref["dq"] else [],
            "black_in_bin": ref["eight_in_bin"], "out": ref["unplugged"], "plugged": ref["seated"],
            "tower": ref["tower_in"] * 0.0254, "balls": ref["golf_in_bin"],
            "t_end": round(harness["seconds"], 1), "reason": harness["reason"],
            "stages": [(s["name"], s["ok"], s["attempts"]) for s in match["stages"]],
            "harness_ok": harness["ok"], "stage1_at": ref["stage1_at"], "stage2_at": ref["stage2_at"],
            "first_touch": ref["first_touch"], "match_wall_s": match["match_wall_s"],
            "valid": match.get("valid", True), "invalid_because": match.get("invalid_because", []),
            "health": match.get("health", {}), "sim_t": match.get("sim_t", ref.get("t")),
            "sim_rate": match.get("sim_rate"),
            **{k: match.get(k) for k in ("judge_models", "judge_mode", "judge_reasoning", "operator_notes",
                                         "prompt_technique", "prompt_level", "dream_hedge", "dream")},
            "policy": match["policy"], "judge": match["judge"], "metrics": match["metrics"],
            "monitor_calls": match["monitor_calls"], "versions": match["versions"],
            "source_revision": match.get("source_revision", "unknown"),
            "effective_plan_sha256": match["effective_plan_sha256"]}


def collect(source):
    rows = {}
    for marker in sorted(Path(source).rglob("complete.json")):
        match_path = marker.with_name("match.json")
        if not match_path.exists():
            raise ValueError(f"completion marker without match.json: {marker}")
        row = normalize(json.loads(match_path.read_text(encoding="utf-8")))
        meta = json.loads(marker.read_text(encoding="utf-8"))
        identity = (meta["run_id"], row["label"], row["seed"])
        # Spot retries are separate attempts. Keep one complete result per job/config/seed.
        if identity in rows:
            if rows[identity]["effective_plan_sha256"] != row["effective_plan_sha256"]:
                raise ValueError(f"conflicting plans for {identity}")
            print(f"Duplicate completed attempt for {identity}; keeping first sorted attempt.")
            continue
        row["run_id"], row["attempt"] = meta["run_id"], meta["attempt"]
        rows[identity] = row
    return list(rows.values())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prefix", help="gs://bucket/runs/job")
    p.add_argument("--local", type=Path, default=Path("cloud/generated/downloads"))
    p.add_argument("--out", type=Path, default=Path("cloud/generated/matches.jsonl"))
    p.add_argument("--execute", action="store_true", help="download results and write JSONL")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    print(f"Will download {args.prefix or '(local only)'} into {args.local} and write {args.out}.")
    if not args.execute or args.dry_run:
        return
    if args.prefix:
        if not args.prefix.startswith("gs://"):
            p.error("prefix must start with gs://")
        exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
        if not exe:
            raise SystemExit("USER ACTION NEEDED: install gcloud and run gcloud auth login.")
        args.local.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([exe, "storage", "rsync", "--recursive", args.prefix, str(args.local), "--quiet"],
                                capture_output=True)
        if result.returncode:
            raise SystemExit("USER ACTION NEEDED: download failed; check gcloud auth login, prefix and objectViewer permissions.")
    rows = collect(args.local)
    if not rows:
        raise SystemExit("USER ACTION NEEDED: no completed matches found; check Batch task status and logs.")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    print(f"Wrote {len(rows)} matches to {args.out}.")


if __name__ == "__main__":
    main()
