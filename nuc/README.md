# NUC snapshot (8 Nov 2026)

Copied from the station NUC (`pah@nuck-5`), where these files existed only on the machine and not in any repo.
This folder is a record, not the code the station runs: the station runs `station_addons/` from this branch.

| Here | On the NUC | Notes |
|---|---|---|
| `pah_judge/` | `~/pah_judge/` | HTTP route add-on (move/judge endpoints) loaded via `nuc_shim` |
| `station.yaml` | `~/.dreamlabs/station.yaml` | Live station config. Gripper hold torque 2.0 N.m on both arms since 8 Nov 19:14 UTC (was 1.5, originally 0.5) |
| `repo_root_scripts/`, `nuc_shim/` | `~/hill-climb-harness-tuning/start_*.sh`, `nuc_shim/` | Older, untracked copies at the clone's root (hard-coded paths); the committed versions in `station_addons/` are what start_tuned.sh runs |
| `archive_runs.sh`, `teleop_supervisor.sh` | `~/` | Rollout archiving and teleop supervisor helpers |
| `follower_code_quest-continuity_uncommitted.diff` | `~/follower_code` | Uncommitted edit to `metal/leader_collect.py` on branch `quest-continuity` |

Not copied: Dream's own package (`~/dream-env`, pip-installed), credentials, and run data (rollouts, logs).
