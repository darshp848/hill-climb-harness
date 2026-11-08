# Teleop recording plan: Sat 7 Nov, until the fine-tune is queued

Station: `ssh pah@nuck-5` (Tailscale; works off the venue LAN too). On the LAN it is also `pah@10.103.0.143`.

## Decision (17:50): one fine-tune on UNPLUG and PUT-DOWN only

- **Unplug is everything.** It is Stage 1's pass bar: without it the run scores 0, and base Dream-1 unplugs about
  1 try in 6. Every demo minute goes here.
- **Plug-in: in the fine-tune, not in the match plan (yet).** Base Dream-1 never seated a plug. The 18:45-18:52
  takes have 12 checked insertions (below); the match plan gets a plug-in stage back only if Sunday's runs seat one.
- **Stacking and balls stay on base Dream-1's own skill** (it stacks 2-4 cubes zero-shot). The fine-tune mixes in
  Dream's data by default (`base_share` 0.5), which keeps general skill. Sunday morning, test the fine-tune on a
  stack; if it got worse than base, run those stages on base Dream-1 (the harness can switch checkpoint per run).
- **One job, queued when recording stops** (not at 20:00: takes are still coming in). 1000 steps (the default,
  ~$200), constant learning rate. Training takes 7-8 h, plus any queue, plus up to 1 h of warm-up on the first call:
  queue no later than about 10 h before the first match.

## The two task texts (set one in the dashboard before EVERY take)

| # | Task text | Take | Have | Want by 19:45 |
| --- | --- | --- | --- | --- |
| T1 | `unplug the USB cable at the end of the hub nearest the right arm` | Enter with the gripper open ~5 cm from the end plug. Squeeze fully, pause, pull straight up until clear. Save while holding it up. 5-10 s | 70 (20:00) | done |
| T2 | `put the held cable down on the table away from the hub` | Starts holding the cable. Carry it to the same spot each time, open, lift. Save. 3-6 s | 52 (20:00) | done |

Chain them: T1, save, switch the text to T2, Enter, put down, save. Then plug the cable back in by hand
(arms still, between takes) and repeat. About 1 in 3 T1 takes with the end port already empty (the harness's extra
unplug), and about 1 in 5 starting from home instead of 5 cm away.

Other texts the harness sends (`put a block on top of the stack`, `put a golf ball in the bin`,
`open both grippers and move the arms up`) are left to base skill: `harness.tasks` will list them as MISSING, which
is expected.

## Rules for every take

- **Press Enter before touching anything,** gripper open, so the take starts before the grasp.
- **Under 30 s, one sub-task, exact text.** Discard (`d` / B) any take that stalls, fails or fumbles.
- **Full squeeze, straight pull.** The good takes pull the trigger fully for most of the grasp.
- **No hands in the overhead camera,** nobody else at the table during a take, left arm (leader) held low.
- **Leader/follower recording:** record into its own folder, then freeze the left wrist camera (below).

## Splitting a long take into task episodes

A take that does several sub-tasks in a row can be cut into one episode per sub-task, each under its exact text,
into a NEW dataset (the source is never touched). Find the cut times from the video, write a spec like
`tools/splits/plug-unplug-v1_ep3.json`, then run it on the laptop (it needs pyarrow, numpy and ffmpeg; the station
has no ffmpeg):

```bash
rsync -a pah@nuck-5:hackathon_data/<source> ~/hackathon_data/          # the spec's paths start at ~/hackathon_data
python tools/split_episodes.py tools/splits/<spec>.json
rsync -a ~/hackathon_data/<out> pah@nuck-5:hackathon_data/
# on the station: ~/dream-env/bin/python tools/add_eef.py ~/hackathon_data/<out> --arm metal   (runs Dream's checks)
```

**Automatic cut times:** `tools/auto_split.py` proposes the spec from the right gripper's grasps (a grasp at the
hub that lifts and releases away from it = unplug + put down; a grasp away from it that releases at the hub =
plug-in), and writes one review image per episode (right wrist at each segment's start and end, overhead at its
end). Look at every row, delete bad segments from the spec, then run `split_episodes.py`:

```bash
python tools/auto_split.py ~/hackathon_data/<dataset> --out ~/hackathon_data/<dataset>-split --spec spec.json --sheets sheets/
```

Done so far (21:30): every take in `plug-unplug-v1` (42 episodes, 13:20-21:07) and `unplug-v1` checked. Each folder
has `-eef` built and passes Dream's checks; specs in `tools/splits/`. Every kept segment was checked on its frames.

| Folder | From | T1 unplug | T2 put down | T3 plug-in | T7 cylinder | other |
| --- | --- | --- | --- | --- | --- | --- |
| `quest-split-v4` | Quest takes 12-22 (18:12-18:31); replaces v3 (one put-down where the left arm moved removed) | 40 | 34 | | | |
| `leader-split-v4-frozen` | leader takes 3, 9, 25-35 (17:15-19:46), left wrist frozen | 14 | 8 | 3 | 6 | 1 × T5 ball |
| `plugin-split-v1-frozen` | leader takes 25-26 (18:45-18:52), left wrist frozen | | | 12 | | |
| `leader-split-v1-frozen` | leader takes 0-2 (17:09-17:16), left wrist frozen | 8 | 6 | | | |
| `unplug-split-v1` | the 60 s Quest take (16:44) | 3 | 3 | | | |
| `leader-split-v2-frozen` | leader takes 6 and 9 (17:30-17:53), left wrist frozen | 3 | | | | |
| `unplug-v1-split` | the good parts of `unplug-v1` (13:20-13:47) | 2 | 1 | | | |
| `stack-split-v1-frozen` | leader-right takes 10-11, right wrist frozen | | | | 2 | 2 × T4 block |
| `leftarm-split-v5-frozen` | leader-right takes 36-38, 41 (19:51-21:07, left arm works), right wrist frozen | | | | 3 | 11 × T5 ball, 2 × T4 block |
| **total** | | **70** | **52** | **15** | **11** | 12 × T5, 4 × T4 |

How each was checked: unplug ends at the top of the pull, with the plug gripped and held up (jaw stops at 2-7%;
the cut follows the lift until the hand stops rising or starts moving sideways, +0.3 s; an earlier cut at
+6 cm ended most unplugs mid-pull, so all 67 auto-cut unplugs were re-cut at 20:15);
put-down ends open with the cable on the table at least 8 cm from the ports; plug-in ends with the cable still in a
USB port after release (6 of the 15 are "insertion only": those takes pulled a plug out and pushed it back in one
grasp, so only the insertion half is kept); cylinder (T7 `stand the cylinder upright in the hole of the top block`)
ends with the cylinder standing in the block's hole.

Plug-in and cylinders are in the fine-tune but not in the match plan yet: add a stage on Sunday if the fine-tuned
model does them in practice runs. `quest-split-v3` is superseded by v4: do not upload both.

Not used: raw `unplug-v1` (its other cuts end with an empty gripper); `plug-unplug-v1` 4, 5, 7, 8, 24 (start
already holding, or never release), 39-40 (right-arm cable takes at 20:56-21:02: the cables end draped over the
strip, so no unplug or insertion can be confirmed), 23 (59 s, nothing moves), 31, 33, 34 (no complete grasp-and-release), and
every cycle that ended unclear on the frames: cables moved around the table, re-seats, blocks moved without
stacking, a cylinder take of 54 s (29), and one ball take with a person's hand holding a cup in view (35). The raw
`plug-unplug-v1` (placeholder text) never goes up as it is. `~/hackathon_backup/plug-unplug-v1-1714` is a 17:14
copy of its first 5 takes, already covered.
Better still, record each sub-task as its own episode from the start.

## Leader/follower episodes: freeze the left wrist camera

With `follower_code/metal/leader_collect.py` the left arm is the leader, moved by hand, so its wrist camera swings
around and films the operator (measured on the 17:09 takes: the view wanders 59-63 on a 0-255 scale, against 6 for
a parked camera). In a match the left arm is parked and that camera is still. Before `add_eef`, write a copy where
that camera shows the parked view (laptop; the station has no ffmpeg):

```bash
rsync -a pah@nuck-5:hackathon_data/<leader-dataset> ~/hackathon_data/
python tools/freeze_camera.py ~/hackathon_data/<leader-dataset> --ref ~/hackathon_data/<quest-dataset>:EP:0 --list
python tools/freeze_camera.py ~/hackathon_data/<leader-dataset> ~/hackathon_data/<leader-dataset>-frozen \
    --episodes all --ref ~/hackathon_data/<quest-dataset>:EP:0
rsync -a ~/hackathon_data/<leader-dataset>-frozen pah@nuck-5:hackathon_data/
# station: ~/dream-env/bin/python tools/add_eef.py ~/hackathon_data/<leader-dataset>-frozen --arm metal
```

The reference must be a parked-left-arm frame from the CURRENT table layout. Keep leader and Quest episodes in
separate folders, so `--episodes all` is right. Dream's checks pass on a frozen copy (tested 17:15).

## Clean-up done

17:00: 8 empty or no-grasp episodes dropped (unplug-v1 1, 2, 5, 7, 8, 9; plug-unplug-v1 1, 2). `unplug-v1` keeps 4
(30, 15, 46 and 96 s). Long multi-task takes stay in `plug-unplug-v1` and never go up as they are: only their
reviewed splits do.

## Queue the fine-tune: `~/hackathon_data/ft-final` (ready 22:30, check passed)

Built 22:30 from every take (42 episodes, 13:20-21:07), the unplugs re-cut to the top of the pull, and David's
review applied (his drops and his relabels from `~/hackathon_review/review_marks.json` and
`leader-split-v4-frozen_fix-ep8.json`). The kit's check: "ready to upload: 170 episodes, 48428 frames at 30 fps".

| Folder in ft-final | Episodes |
| --- | --- |
| `quest-split-v4-final-eef` (Quest 18:12-18:31, David's 9 drops) | 65 |
| `leader-split-v4-final-frozen-eef` (leader takes 25-35, David's fix and 5 drops) | 28 |
| `leader-split-v6-frozen-eef` (leader takes 39-40, 20:56-21:02: cable unplug / put down / plug-in) | 21 |
| `leftarm-split-v5-frozen-eef` (left arm 36-38, 41: balls, cylinders, blocks) | 16 |
| `leader-split-v1-frozen-eef` | 14 |
| `plugin-split-v1-final-frozen-eef` (David's 1 drop) | 11 |
| `unplug-split-v1-final-eef` (David's 1 drop) | 5 |
| `stack-split-v1-frozen-eef` | 4 |
| `leader-split-v2-frozen-eef` | 3 |
| `unplug-v1-split-eef` | 3 |

Per task: 69 unplug, 55 put down, 19 plug-in, 12 ball, 10 cylinder in a block's hole, 4 block, 1 cylinder on top
of the stack (David's text). 26.9 min. Not reviewed by David yet: `leader-split-v6` and `leftarm-split-v5`.
`ft-v1` / `ft-v2` (21:22 / 21:41) are older: built before the unplug re-cut, without takes 36-41.

### Final checks (23:00), all passed

- `tools/check_dataset.py --videos` on the 10 source folders: 0 errors, 0 warnings. Meta vs tables, frame and index
  continuity, timestamps, no NaN, joints and grippers in range, one working arm with the other parked, every
  camera's frame count, every leader camera frozen, no duplicates, and how each task ends (every unplug holding
  the plug, every other task released). It found one ball take that ended re-closing an empty gripper; it now ends
  at the release.
- The laptop copies are byte-identical to the station's (checksums of every file).
- The `-eef` data the job trains on (all 170 episodes, 96,826 rows): state 34 wide, action 32 (Dream trains up to
  34), every hand rotation orthonormal, grips 0..1, hands within 0.9 m.
- Dream's own check: `finetune.py --check` -> "ready to upload: 170 episodes, 48413 frames at 30 fps".
- A random last-frame sample of every task matches its label.
- Cross-checked against Dream's docs and the kit's `finetune.py`: embodiment `hackathon-metal` (makermods.json,
  3 cameras, head camera `scene`), `use_eef=True` (required for Metal), service defaults (1000 steps, lr 1e-5,
  constant schedule, base_share 0.5: half Dream data keeps general skill), dataset as a parent of subfolders (one
  session each). The account has no jobs, datasets or embodiments yet (no name clash). Dream's queue at 22:43: 0
  jobs ahead, 36 of 50 slots free. `/tmp` has 5.5 GB for the upload's temporary pack (ft-final is 727 MB).

### Queued 23:00 (Sat 7 Nov)

- Started 22:54 from the `finetune` tmux session on the station, with the fine-tune account's key held only in
  that shell (`tools/start_finetune.sh`; the station's saved key is a different account and was not changed).
- Dataset `ds_5ae1c3b4e8739af1` (`muhammad.mukthar11/relay-final`), uploaded 22:54, processed 23:00.
- Job `ftj_fdff469db8e2b643`, suffix `relay-final`, queue position 1. The service's settings: 1000 steps, lr 1e-5,
  batch 32, `use_eef`, constant schedule, resumable. The model will be `dream-1-ft:<account>-relay-final`, and only
  that account's key can run it: on Sunday the station needs that key (export `DREAM_API_KEY` in the shell that
  runs `dreamcli robot up`, or save it with Step 2 of Dream's Getting started).

### Start it, and the overnight watcher (`tools/finetune_watch.py`, copied to the station)

One command does the upload, the processing, the job and the watching, inside tmux on the station:

```bash
tmux new -A -s finetune
~/dream-env/bin/python ~/hill-climb-harness/tools/finetune_watch.py start ~/hackathon_data/ft-final \
    --name relay-final --suffix relay-final --max-requeues 1 --i-accept-charges
```

It queues exactly what `tools/finetune.py --arm metal` would (the kit's embodiment and settings). Overnight:
retries network and 5xx errors forever; renames on a used suffix or dataset name; waits on the account's job limit;
re-queues a failed training once (`--max-requeues 1`, a second charge) on the same processed dataset under
`relay-final-r2`; stops and says why for anything that needs a person (no credits, a data problem, a bad key).
When the model is published it writes `~/finetune_watch/MODEL.txt` and warms the model up (its first start can
take an hour). `--keep-warm-until 08:30` keeps it warm until then (each ping is a small inference charge).

```bash
cat ~/finetune_watch/STATUS.txt            # one line: where it is
tail -f ~/finetune_watch/watch.log         # every poll
cat ~/finetune_watch/MODEL.txt             # the model id, once ready
~/dream-env/bin/python ~/hill-climb-harness/tools/finetune_watch.py resume   # after a crash or reboot
```

Without `--i-accept-charges` it refuses. The kit's own command still works too (no watcher):
`~/dream-env/bin/python tools/finetune.py ~/hackathon_data/ft-final --arm metal --name relay-final --suffix relay-final`.

Training takes 7-8 h plus up to 1 h warm-up: queued at 23:15, the model is ready and warm around 07:00-08:30.

## Sunday morning: the fine-tune is ready

**Model: `dream-1-ft:bb4b9e5f-relay-final`**, ready 07:02, warm 07:14 (8 Nov). Only the fine-tune account's key runs
it, and that key is not the station's saved one: start every Dream program below in a shell that has it, either the
tmux session `finetune` (it still holds the key: `tmux attach -t finetune`) or a new shell after
`read -rs DREAM_API_KEY; export DREAM_API_KEY`. Base Dream-1 runs under that key too, so one agent serves both
models. Every call is billed to that account.

Checked with no arm moving (8 Nov 08:30): the plan's prompts against the trained texts (`python -m harness.tasks`:
T1 69 episodes, T2 55, stacking 4 and balls 12 "low", T6 none: the fine-tune keeps base Dream-1's half for it),
the Claude monitor live on the table photo (4/4 right, 8.5 s a reply), the harness tests.

Before the match, at the robot, a hand on the e-stop. These move the arms; a person runs them.

1. **Dream's own check of the loop and the model** (`python -m dream.check`, about 2 minutes; the station agent must
   be stopped). LOOP moves the arms in small circles; INPUTS asks the person whether the cameras on its page
   (`http://127.0.0.1:8643/`) look right; MODEL asks the fine-tune about 8 moments of a recorded unplug, with nothing
   moving (8 billed calls), and sets each answer against the recording:

       ~/dream-env/bin/python -m dream.check --recording ~/hackathon_data/ft-final/quest-split-v4-final-eef -- \
         ~/dream-env/bin/dreamcli robot run --checkpoint dream-1-ft:bb4b9e5f-relay-final \
         --task "unplug the USB cable at the end of the hub nearest the right arm"

   FAULT `none found` means go on. `your control loop` means fix that line first; `the model` means the fine-tune
   answers wrongly on its own training data (use base Dream-1 for the match and send Dream the four lines).
2. **Look before it moves**: the same `dreamcli robot run ... --dry-run --view-3d` (runs the model, writes no target
   to an arm) for each model, with the cables in the hub. The 3D page draws each chunk's hand path: the fine-tune's
   right hand should head to the plug at the hub's right end and close on it.
3. **A/B on Stage 1** (the station agent up: `PYTHONPATH=~/hill-climb-harness/station_addons dreamcli robot up
   --view --view-port 8093`, plain `up`). Alternate base and fine-tune run by run, 5 runs each, all 4 cables in the
   hub before each run:

       python -m harness plans/bench_cable.yaml --live --verifier monitor+manual --runs-dir runs/bench
       python -m harness plans/bench_cable.yaml --live --verifier monitor+manual --runs-dir runs/bench \
         --var model=dream-1-ft:bb4b9e5f-relay-final
       python -m harness.report runs/bench --by-checkpoint

   Pick the model with the higher unplug rate (then the fewer attempts). A tie goes to the fine-tune only if
   put_cable_down is better too.
4. **Stacking and balls**: the fine-tune saw only 4 block and 12 ball episodes. Two runs of the whole match plan on
   each model (`plans/hill_climb_v4.yaml`, same `--var model=...`) show whether it stacks as well as base Dream-1.
   If the fine-tune wins the cable but stacks worse, a per-stage model is a harness change to make then. If stacking
   runs on the fine-tune, delete `tower_prompts` from the plan (the fine-tune only knows "put a block on top of the
   stack").
5. **Match**: `python -m harness plans/hill_climb_v4.yaml --live` (verifier `monitor` only, no human input after go),
   with `--var model=dream-1-ft:bb4b9e5f-relay-final` if the fine-tune won.
