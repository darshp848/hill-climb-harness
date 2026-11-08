#!/bin/bash
# Darsh's start_tuned.sh settings + 20 N pull straight up + the top-down hook (right gripper points straight down).
# Rollback: dreamcli robot down, then ~/hill-climb-harness-tuning/station_addons/start_tuned.sh
export PAH_TICK_LOG="$HOME/.dreamlabs/tick-log.csv"
export PAH_EEF_LOG="$HOME/.dreamlabs/eef-log.jsonl"
export PAH_FF_TAU_S=0.15 PAH_METAL_TICK_HZ=60 PAH_IK_TRAJ=20,100,200,5
export PAH_PULL_FORCE_N=r:20 PAH_PULL_DIR=r:0,0,1
export PAH_EEF_HOOK=pah_topdown:topdown PAH_TOPDOWN_SIDES=right
export PYTHONPATH="$HOME/hill-climb-harness-tuning/station_addons/nuc_shim:$HOME/pah_judge:$HOME/pah_hooks"
exec "$HOME/dream-env/bin/dreamcli" robot up --view --view-port 8093
