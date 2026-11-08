#!/bin/bash
# Tuned station: PAH_* patches from this clone. Start only with a person at the e-stop.
export PAH_TICK_LOG="$HOME/.dreamlabs/tick-log.csv"
export PAH_EEF_LOG="$HOME/.dreamlabs/eef-log.jsonl"
export PAH_FF_TAU_S="${PAH_FF_TAU_S:-0.15}"
export PAH_METAL_TICK_HZ="${PAH_METAL_TICK_HZ:-60}"
export PAH_IK_TRAJ="${PAH_IK_TRAJ:-20,100,200,5}"
export PYTHONPATH="$HOME/hill-climb-harness-tuning/nuc_shim:$HOME/pah_judge"
CMD=("$HOME/dream-env/bin/dreamcli" robot up --view --view-port 8093)
echo "PAH_TICK_LOG=$PAH_TICK_LOG PAH_EEF_LOG=$PAH_EEF_LOG PAH_FF_TAU_S=$PAH_FF_TAU_S PAH_METAL_TICK_HZ=$PAH_METAL_TICK_HZ PAH_IK_TRAJ=$PAH_IK_TRAJ PYTHONPATH=$PYTHONPATH ${CMD[*]}"
exec "${CMD[@]}"
