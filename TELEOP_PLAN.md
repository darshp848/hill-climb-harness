# Teleop recording plan: Sat 7 Nov, now until the fine-tune is queued (~21:00)

Goal: one fine-tune, queued tonight, that knows every prompt the harness will send in a match.
Each task text below is used **byte for byte** in two places: the dashboard's "Set task" box when you record
it, and the prompt in `plans/hill_climb_v4.yaml`. Check the match with:

```bash
# once, from the laptop: copy the harness to the station (it is not there yet)
rsync -a --exclude .git --exclude runs ~/Documents/GitHub/hill-climb-harness/ pah@10.103.0.143:hill-climb-harness/
# on the station, any time (read only)
cd ~/hill-climb-harness && python3 -m harness.tasks plans/hill_climb_v4.yaml ~/hackathon_data/unplug-v1 ~/hackathon_data/relay-v1
```

## The six task texts (copy-paste these, do not retype)

| # | Task text | Arm | Episode | Target |
| --- | --- | --- | --- | --- |
| T1 | `unplug the USB cable at the end of the hub nearest the right arm` | right | 10-20 s, ends holding the cable clear of the hub | 50 (7 good already) |
| T2 | `put the held cable down on the table away from the hub` | right | 5-10 s, ends gripper open, arm up | 40 |
| T3 | `plug the loose USB cable on the table into a free port on the hub` | pick one, always the same | 15-30 s, ends plug seated, gripper open, arm up | 50 |
| T4 | `put a block on top of the stack` | pick one, always the same | 10-20 s, ends gripper open, arm up and clear of the tower | 70 |
| T5 | `put a golf ball in the bin` | the arm nearest the bin | 8-15 s, ends ball released in the bin | 50 |
| T6 | `open both grippers and move the arms up` | both | 3-6 s | 25 |

T1 is the text already in `unplug-v1`, so its 7 good episodes count. The harness also uses T1 for an extra unplug
(+5), so record about 1 in 3 T1 episodes with the end port already empty: the next cable along is then "the end".
From the station tests (HARNESS_GUIDE.md section 11): a T1 that works squeezes fully, on the plug or its black strain
relief, pauses, then pulls straight out along the port. Most T1 episodes may start about 5 cm in front of the plug
(5-8 s each), plus a few full ones from home so the approach joins on. T6 is the harness's one recovery prompt.
Every other recovery in the plan just re-sends the stage's own prompt, so it needs no extra demos.

## How to record: chain the episodes

The harness changes the prompt live, without homing. So each task's demos must start where the previous task
leaves the arms. Record the relay in order, as back-to-back episodes:

- One person wears the Quest. A second person sits at the dashboard (http://localhost:8080) and presses "Set task"
  with the next text **before** the operator presses A.
- Unplug → save (A) → Set task T2 → A → put it down → save. Then reset the cable and do it again.
- Stack: one tower gives 3-4 T4 episodes in a row. Place a block, save, A, place the next one, save.
  Knock the tower down and rebuild it somewhere else.
- Balls: one ball per episode, back to back. Empty the bin every ~8 balls.
- About 1 in 4 episodes should start from the home pose instead (the match starts there).

New folder for everything recorded from now on (T1 included). It is combined with `unplug-v1` at upload:

```bash
# on the station, in tmux "collect", hand on the e-stop
cd ~/physical-ai-hacks
uv run dhk-collect --config configs/station.yaml --dataset-dir ~/hackathon_data/relay-v1
```

## Rules for every demo

- **One strategy per task.** Use the same arm and the same grasp every time. Vary where the objects are, not
  the method.
- **Start still, end still.** Wait about 1 s with the arms still at both ends of the episode.
- **Keep episodes at 10-30 s.** Throw away anything that went wrong with B. A fumble you recover from is fine.
- **T2: the unplugged cable always goes to the same spot,** far from the loose cables. An unplugged cable may never
  be plugged back in (rubric), and the model must not mistake it for a loose one.
- **T4: start from 1, 2 and 3 blocks stacked,** and take a big flat block first. Always end with the arm lifted
  clear: the tower must stand 3 s after release.
- **T5: put both black 8-balls among the golf balls in every scene, some right beside the target.** Never touch
  them. A black ball in the bin loses the match.
- **T3 is the biggest prize (+20 each, up to 4) and the hardest by teleop.** If fewer than 5 of your first 10 tries
  are good, stop recording T3 and spend that time on T4 and T5.

## Timeline

| Time | Block | What |
| --- | --- | --- |
| 14:45-15:45 | 1 | T1 → T2 chains, ~40 pairs |
| 15:45-17:15 | 2 | T4, ~70 placements across ~20 towers |
| 17:15-17:30 | break | Watch episodes on the dashboard, delete bad ones. Dry-check the data (below) |
| 17:30-18:45 | 3 | T3, ~50 (or the cut rule above) |
| 18:45-19:45 | 4 | T5, ~50 |
| 19:45-20:15 | 5 | T6 ×25, then top up whichever task is furthest from its target |
| 20:15-21:00 | queue | Clean up, `add_eef`, check, then the paid job |

If you run behind, cut in this order: T6, then T5, then T3. T1, T2 and T4 are the pass bars, so never cut them.

## Clean-up before upload (needs your yes for each episode number)

In `unplug-v1`: episode 1 is 2.4 s (accidental) and episode 6 is **96 s**; drop both. Episode 4 is 46 s; watch it
and keep it only if it's one clean unplug.

```bash
uv run dhk-dataset list ~/hackathon_data/unplug-v1
uv run dhk-dataset drop ~/hackathon_data/unplug-v1 6 1      # renumbers after each; check `list` again
```

## Queue the fine-tune (~20:30, arms not needed)

```bash
cd ~/physical-ai-hacks
~/dream-env/bin/python tools/add_eef.py ~/hackathon_data/unplug-v1 --arm metal --replace
~/dream-env/bin/python tools/add_eef.py ~/hackathon_data/relay-v1 --arm metal --replace
mkdir -p ~/hackathon_data/ft-v1 && cp -r ~/hackathon_data/unplug-v1-eef ~/hackathon_data/relay-v1-eef ~/hackathon_data/ft-v1/
(cd ~/hill-climb-harness && python3 -m harness.tasks plans/hill_climb_v4.yaml ~/hackathon_data/ft-v1)   # no MISSING?
~/dream-env/bin/python tools/finetune.py ~/hackathon_data/ft-v1 --arm metal --name relay-v1 --suffix relay-v1 --check
# PAID (about $200 for the default 1000 steps). Only after the organizers' credits are on the account:
tmux new -A -s finetune
~/dream-env/bin/python tools/finetune.py ~/hackathon_data/ft-v1 --arm metal --name relay-v1 --suffix relay-v1
```

The platform reads each subfolder of `ft-v1` as one session. Leave the learning-rate schedule constant, not
cosine: this is a multi-task model, and constant keeps `resume_from` possible on Sunday. Training takes 7-8 h,
so the model should be ready around 05:00. Its first call can then take up to 1 h to warm up.

## Sunday morning

1. Warm the model up early: `dreamcli robot run --checkpoint dream-1-ft:<account>-relay-v1 --task "put a block on
   top of the stack" --dry-run` (the first call may wait up to an hour).
2. Put the id in `plans/hill_climb_v4.yaml` (`checkpoint:`). Start the agent with
   `PYTHONPATH=~/hill-climb-harness/station_addons dreamcli robot up --view --view-port 8093`: plain `up`, not `up joint` (a Metal fine-tune answers tool poses, which dreamcli solves to joints).
3. Run the harness with `--dry-run`, then `--live` with `--verifier monitor+manual` for practice. Use `monitor`
   alone in a match.
