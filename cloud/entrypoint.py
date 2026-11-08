"""Run K sequential matches per Batch task, upload logs, then publish completion last."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

from cloud.control import tasks


def redact(text):
    for name in ("DREAM_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        value = os.environ.get(name)
        if value:
            text = text.replace(value, "[REDACTED]")
    return text


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--matrix", default="/app/cloud/matrix.yaml")
    p.add_argument("--judge", choices=["oracle", "api", "openrouter"], default="oracle")
    p.add_argument("--judge-models", default="google/gemini-3.1-flash-lite")
    p.add_argument("--judge-mode", choices=["race", "alternate"], default="race")
    p.add_argument("--judge-hedge", type=int, default=1)
    p.add_argument("--dream-hedge", type=int, default=1)
    p.add_argument("--matches-per-task", type=int, default=1)
    p.add_argument("--limit", type=int)
    p.add_argument("--out", type=Path, default=Path("/out"))
    p.add_argument("--config", help="optional one-config sequential seed range, outside Batch")
    p.add_argument("--seed-start", type=int, default=0)
    p.add_argument("--matches", type=int, default=1)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if min(args.matches_per_task, args.matches) < 1:
        p.error("match counts must be positive")
    entries = tasks(args.matrix)
    expected_matrix = os.environ.get("MATRIX_SHA256")
    if expected_matrix and hashlib.sha256(Path(args.matrix).read_bytes()).hexdigest() != expected_matrix:
        raise SystemExit("USER ACTION NEEDED: matrix differs from built image; build a new image tag.")
    if args.config:
        config = next((x for x in entries if x["name"] == args.config), None)
        if config is None:
            p.error("unknown --config")
        entries = [{**config, "seed": seed} for seed in range(args.seed_start, args.seed_start + args.matches)]
    else:
        entries = entries[:args.limit]
        index = int(os.environ.get("BATCH_TASK_INDEX", "0"))
        entries = entries[index * args.matches_per_task:(index + 1) * args.matches_per_task]
    if not entries:
        p.error("task index outside matrix")
    prefix = os.environ.get("OUTPUT_PREFIX", "")
    attempt = uuid.uuid4().hex
    print(f"Will run {len(entries)} Dream matches with {args.judge} judge, write {args.out}, "
          f"upload to {prefix or '(missing OUTPUT_PREFIX)'}, attempt {attempt}.", flush=True)
    if args.dry_run:
        print(json.dumps([{k: x[k] for k in ("name", "seed", "plan", "args")} for x in entries]))
        return
    if not prefix.startswith("gs://") or not os.environ.get("DREAM_API_KEY"):
        raise SystemExit("USER ACTION NEEDED: set OUTPUT_PREFIX and provide DREAM_API_KEY from Secret Manager.")
    if args.judge == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("USER ACTION NEEDED: upload anthropic-api-key and submit with --judge api.")
    if args.judge == "openrouter" and not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit("USER ACTION NEEDED: upload openrouter-api-key and submit with --judge openrouter.")
    failed = False
    for entry in entries:
        key = f"{entry['name']}-{entry['seed']}"
        folder = args.out / attempt / key
        folder.mkdir(parents=True)
        command = [sys.executable, "-m", "cloud.run", "--policy", "dream", "--judge", args.judge,
                   "--plan", entry["plan"], "--seed", str(entry["seed"]), "--label", entry["name"],
                   "--out", str(folder), "--judge-models", args.judge_models, "--judge-mode", args.judge_mode,
                   "--judge-hedge", str(args.judge_hedge), "--dream-hedge", str(args.dream_hedge), *entry["args"]]
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        signal.signal(signal.SIGTERM, lambda *_: child.terminate())
        with (folder / "worker.log").open("w", encoding="utf-8") as log:
            for line in child.stdout:
                log.write(redact(line))
                log.flush()
        code = child.wait()
        # Scrub known secrets from structured logs too, before they leave this task.
        for path in folder.rglob("*"):
            if path.suffix in (".json", ".jsonl", ".txt", ".log"):
                path.write_text(redact(path.read_text(encoding="utf-8")), encoding="utf-8")
        destination = f"{prefix.rstrip('/')}/{attempt}/{key}"
        upload = subprocess.run(["gcloud", "storage", "cp", "--recursive", str(folder),
                                 f"{prefix.rstrip('/')}/{attempt}/", "--quiet"], capture_output=True)
        if upload.returncode:
            raise SystemExit("USER ACTION NEEDED: output upload failed; check bucket IAM and network. Local logs remain in /out.")
        result_file = folder / "match.json"
        complete = result_file.exists() and json.loads(result_file.read_text(encoding="utf-8")).get("complete", False)
        if code or not complete:
            failed = True
            print(f"Match {key} failed with exit {code}; diagnostic logs uploaded.", flush=True)
            continue
        marker = folder / "complete.json"
        marker.write_text(json.dumps({"match": key, "run_id": os.environ.get("RUN_ID", "manual"),
                                      "attempt": attempt}), encoding="utf-8")
        uploaded = subprocess.run(["gcloud", "storage", "cp", str(marker), destination + "/complete.json",
                                   "--quiet"], capture_output=True)
        if uploaded.returncode:
            raise SystemExit("USER ACTION NEEDED: completion upload failed; rerun collection after checking bucket IAM.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
