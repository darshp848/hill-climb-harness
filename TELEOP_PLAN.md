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

## Queue the fine-tune (when recording stops, arms not needed)

What goes in: the four reviewed folders above, and whatever is recorded from now on (leader takes: split or
recorded short with the exact text, then left wrist frozen). Each as its `-eef` copy:

```bash
cd ~/physical-ai-hacks
# add_eef on each new folder (the frozen copy for leader takes); the four reviewed ones already have -eef
mkdir -p ~/hackathon_data/ft-v1 && cp -r ~/hackathon_data/{quest-split-v4,leader-split-v4-frozen,plugin-split-v1-frozen,leader-split-v1-frozen,unplug-split-v1,leader-split-v2-frozen,unplug-v1-split,stack-split-v1-frozen,leftarm-split-v5-frozen}-eef ~/hackathon_data/ft-v1/
(cd ~/hill-climb-harness && python3 -m harness.tasks plans/hill_climb_v4.yaml ~/hackathon_data/ft-v1)   # T1, T2 counts
~/dream-env/bin/python tools/finetune.py ~/hackathon_data/ft-v1 --arm metal --name unplug-v1 --suffix unplug-v1 --check
# PAID, about $200 (1000 steps): only with the team's yes
tmux new -A -s finetune
~/dream-env/bin/python tools/finetune.py ~/hackathon_data/ft-v1 --arm metal --name unplug-v1 --suffix unplug-v1
```

The platform reads each subfolder of `ft-v1` as one session. Keep the default constant learning rate (not cosine),
which keeps `resume_from` possible. Training takes 7-8 h, so the model is ready around 04:00, and its first call can
take up to 1 h to warm up.

## Sunday morning

1. Warm the model up early: `dreamcli robot run --checkpoint dream-1-ft:<account>-unplug-v1 --task "unplug the USB
   cable at the end of the hub nearest the right arm" --dry-run` (the first call may wait up to an hour).
2. Start the agent with `PYTHONPATH=~/hill-climb-harness/station_addons dreamcli robot up --view --view-port 8093`:
   plain `up`, not `up joint` (a Metal fine-tune answers tool poses, which dreamcli solves to joints).
3. Compare fine-tune and base on the same plan, `--verifier monitor+manual`, a few runs each: unplug success first,
   then stacking. Put the winner's id in `plans/hill_climb_v4.yaml` (`checkpoint:`). If the fine-tune unplugs better
   but stacks worse, splitting the checkpoint by stage is a harness change to make then.
4. Match: `--live` with `monitor` only.
