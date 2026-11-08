#!/usr/bin/env bash
# Start the overnight fine-tune of ~/hackathon_data/ft-final on the account of the key typed at the prompt.
# The key is read hidden and kept only in this shell's environment (DREAM_API_KEY wins over ~/.dreamlabs/credentials),
# so the station's saved key is not changed. Meant to run inside tmux: `tmux new -A -s finetune` then this script.
# Afterwards the shell stays open with the key set, so `finetune_watch.py resume` can be run here if needed.
cd ~ || exit 1
PY=~/dream-env/bin/python
WATCH=~/hill-climb-harness/tools/finetune_watch.py
while true; do
  printf 'Paste the Dream API key for the fine-tune account, then press Enter: '
  read -rs DREAM_API_KEY; echo
  export DREAM_API_KEY
  if $PY -c "from dream import fine_tuning; n = len(fine_tuning.list()); print(f'key OK: {n} jobs on this account')"; then
    break
  fi
  echo "That key did not work (see above). Try again."
done
$PY "$WATCH" start ~/hackathon_data/ft-final --name relay-final --suffix relay-final --max-requeues 1 --i-accept-charges
echo "watcher ended (exit $?): see ~/finetune_watch/STATUS.txt. This shell keeps the key; to continue:"
echo "  $PY $WATCH resume"
exec bash
