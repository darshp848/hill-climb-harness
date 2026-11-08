#!/usr/bin/env bash
# Keeps every Dream run's own record: the station overwrites ~/.dreamlabs/last-run.csv (20 Hz joints, commands,
# timing) and last-run_inputs/ (the frames and prompt sent to the model, once a second) on each run. When a run
# ends (the station leaves "running") and those files are new, they are copied to ~/rollouts/zeroshot_<time>/.
set -u
SRC=~/.dreamlabs
mkdir -p ~/rollouts
last=""
was_running=0
while true; do
  state=$(curl -s --max-time 3 localhost:8091/status | python3 -c 'import sys,json; print(json.load(sys.stdin)["state"])' 2>/dev/null || echo down)
  [ "$state" = running ] && was_running=1
  m=$(stat -c %Y "$SRC/last-run.csv" 2>/dev/null || echo 0)
  if [ "$state" != running ] && [ "$m" != "$last" ] && { [ "$was_running" = 1 ] || [ -z "$last" ]; }; then
    dst=~/rollouts/zeroshot_$(date -d @"$m" +%Y%m%dT%H%M%S)
    if [ ! -d "$dst" ]; then
      mkdir -p "$dst" && cp -r "$SRC/last-run.csv" "$SRC/last-run_inputs" "$dst/" && echo "$(date +%H:%M:%S) archived $dst ($(du -sh "$dst" | cut -f1))"
    fi
    last=$m; was_running=0
  fi
  sleep 3
done
