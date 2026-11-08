#!/bin/bash
# Rollback: the station as started on 8 Nov 09:55 PST, no PAH_* patches. Start only with a person at the e-stop.
for v in $(compgen -e | grep "^PAH_"); do unset "$v"; done
export PYTHONPATH="$HOME/pah_judge"
CMD=("$HOME/dream-env/bin/dreamcli" robot up --view --view-port 8093)
echo "PYTHONPATH=$PYTHONPATH ${CMD[*]}"
exec "${CMD[@]}"
