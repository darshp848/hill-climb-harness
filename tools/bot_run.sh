#!/bin/sh
# One live run on the bot from any harness checkout, then the run report (runs/bot_report.html) rebuilt.
#   tools/bot_run.sh <checkout> <plan> [harness args...]
#   tools/bot_run.sh ../darsh-cloud-sims plans/hybrid_v4.yaml --skip cable_ --no-operator
# Needs the ssh tunnel to the station on :8091 and ANTHROPIC_API_KEY / OPENROUTER_API_KEY (doppler run -p nous -c dev).
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="${HARNESS_PY:-$HOME/Documents/Work/hackathon/makermods-pah/harness/.venv/bin/python}"
CO="$(cd "$1" && pwd)"; PLAN="$2"; shift 2
mkdir -p "$CO/runs/bot"
LOG="$CO/runs/bot/$(date +%H%M%S)_$(basename "$PLAN" .yaml).log"
echo yes | (cd "$CO" && PYTHONUNBUFFERED=1 "$PY" -m harness "$PLAN" --live --station http://127.0.0.1:8091 \
  --runs-dir runs/bot "$@") 2>&1 | tee "$LOG"
"$PY" "$HERE/tools/bot_report.py"
