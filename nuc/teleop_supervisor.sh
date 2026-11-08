#!/usr/bin/env bash
# Runs the teleop recorder in the mode the dashboard picks and restarts it when the dashboard's mode switch ends a
# session. Ctrl+C (no switch pending) ends the supervisor too. Don't edit this file while it runs (bash reads it as it goes).
#   DATASET=~/hackathon_data/plug-unplug-v1 TASK="..." ~/teleop_supervisor.sh
set -u
cd ~/physical-ai-hacks
STATE=~/.cache/dream_hackathon
mkdir -p "$STATE"
DATASET=${DATASET:-$HOME/hackathon_data/plug-unplug-v1}
TASK=${TASK:-plug and unplug USB cables (to be split)}
[ -f "$STATE/teleop_mode" ] || echo leader > "$STATE/teleop_mode"
touch "$STATE/teleop_supervised"
trap 'rm -f "$STATE/teleop_supervised"' EXIT
while true; do
  rm -f "$STATE/teleop_switch"
  mode=$(cat "$STATE/teleop_mode")
  echo "=== teleop mode: $mode  (dataset $DATASET) ==="
  side=left; extra=""; kin=0
  case "$mode" in
    leader_right) side=right ;;
    guide_right) side=right; extra=--record-leader; kin=1 ;;
    guide_left) side=left; extra=--record-leader; kin=1 ;;
  esac
  if [ "$mode" = quest ]; then
    .venv/bin/python ~/follower_code/quest/quest_collect.py --config configs/station.yaml --dataset-dir "$DATASET" --task "$TASK"
  else
    LEADER_COLLECT_KINESTHETIC=$kin .venv/bin/python ~/follower_code/metal/leader_collect.py --config configs/station.yaml \
      --leader "$side" $extra --dataset-dir "$DATASET" --task "$TASK"
  fi
  if [ ! -f "$STATE/teleop_switch" ]; then
    echo "=== recorder ended (not a mode switch): supervisor stops ==="
    break
  fi
  sleep 3
done
