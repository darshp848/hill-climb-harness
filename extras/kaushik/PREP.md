# MakerMods Physical AI Hackathon — prep (Sat–Sun 2026-11-07/08)

Founders Inc., **Building C, 3rd floor**, Fort Mason, 2 Marina Blvd, SF. Approved 2026-11-06 (Builder).
Luma: https://luma.com/pah · track applied for: **Hill Climb**.

## Schedule (from the 2026-11-06 organiser email; may shift)

| When | What |
|---|---|
| Sat 9:00 | Doors + kickoff: tracks, **rules and scoring** (first time they're public) |
| Sat 9:10 | Quick-starts: arms (MakerMods), Dream-1 API, LiveKit, Dimensional |
| Sat 9:50 | **Team formation** + track declaration (3–6, ~5 ideal) |
| Sat 10:00 | Hacking + **robot station booking opens** — book immediately |
| Sat 3–6 PM | Talks (Jeremy/Dream-1 3:00, Kushil/Dream infra 3:30, LiveKit, Dimensional, Enact, Lucen) |
| Sat 10 PM | Venue closes |
| Sun 10:00 | Doors |
| **Sun 3:00 PM** | **Submissions due**; leading Hill Climb teams run live scored attempts |
| Sun 5:30 | Awards |

## RUBRIC (hillclimb_rubric_v2.pdf, 2026-11-07) — overrides the tower notes below

1v1 relay, 5 min, policy only (no teleop, no touching, no human input after "go"). Stages in order:
1. **Unplug** USB cable(s) from a **hub**: pass = 1 unplugged (+10), +5 per further unplug, +20 per *loose* cable plugged into a free port (an unplugged cable may not go back in).
2. **Stack** ~10 shapes: pass = **3 layers / 6 in, free-standing 3 s after release** (+20), +10 per extra block (lost if it falls later; the stage still counts).
3. **Ping-pong balls**: +5 own colour, −5 other colour, **black 8-ball in your bin = lose**.
- **Time bonus +1 per 2 s left**, only if stages 1+2 are passed AND a designated member calls stop (+30/min).
- **DQ:** touching a later stage's objects before the current pass bar, any human touch, any human input.

**Strategy: unplug 1 → stack 3 → stop.** The time bonus beats balls and extra blocks unless they're very fast. Stage 3 is skipped by default (8-ball risk).
**Harness jobs:** strict unplug verification (a false pass → touching blocks → DQ), stage gating, verifying the 3 s free-standing hold, signalling the member to call stop, retrying a stalled unplug or a fallen stack within 5 min.
**FT #1 demos:** ~35 hub unplugs (grip the plug body in the middle, pull straight, set it down away from the hub), ~25 three-layer stacks (centred, slow release, back away), ~10 recoveries. No ball demos.
Zero-shot evidence: 3-cube stack ✅; unplug ❌ (grip off-centre and weak, `gripper_hold_torque_nm: 0.5`).

## Dream's hackathon kit (read 2026-11-07 morning) — this overrides older notes below

Repo: https://github.com/dreamlabs-co/physical-ai-hack-2026 (cloned to `dreamlabs-hack-repo/`, last commit 8:47 AM).
- **Stations are two-armed** (YAM, MakerMods Metal or Maker Arm v1): one overhead/scene camera + a camera on each wrist, headless. You SSH in as `pah@<ip>` with `-L 8080:localhost:8080`; everything runs on the station.
- **Recording is solved:** `uv run dhk-collect --config configs/station.yaml --dataset-dir ~/hackathon_data/<team>-<task>` (Meta Quest teleop: grip = drive, trigger = gripper, A = start/save, B = discard). Writes **LeRobot v2.1** directly. The dashboard (localhost:8080) can play back and delete episodes. Use `--task "..."` / "Set task" per sub-task; Dream also auto-labels from the overhead camera.
- **Fine-tune:** Maker/Metal: `~/dream-env/bin/python tools/add_eef.py <dir> --arm maker` (writes `<dir>-eef`; `--replace` after more demos), then `tools/finetune.py <dir>-eef --arm maker --name X --suffix X`, run inside tmux. **7–8 h of training + queue + up to 1 h first start.** Price by steps: $100 / 500, **$200 / 1,000 (default)**, $400 / 2,000. **Check with the organizers before queueing.** `--lr-schedule cosine` suits one task. One fine-tune can learn several tasks (several folders under one parent).
- **Run:** `dreamcli robot run --task "..." [--checkpoint ...] --dry-run`, then the same without `--dry-run`, hand on the e-stop. It homes at start and end and runs until Ctrl-C. **Base Dream-1 runs on these arms with no fine-tune**, so get a zero-shot baseline first. Only one program may hold the arms (close `dhk-collect` first).
- **The harness hook:** `dreamcli robot up` runs Dream's station loop with a local HTTP API on 127.0.0.1:8091: `/start {prompt}`, **`/prompt {prompt}` (switch task while the arm keeps going)**, `/stop`, `/home`, `/reset`, `GET /status`. No frames over the API, so **the checker uses our own webcam** (the spare straight-lens one).
- **Harvesting zero-shot runs as training data (decided 2026-11-07):** Dream's loop has a rollout recorder that saves each run as a LeRobot v2.1 episode (`rollouts: {enabled: true}` in `~/.dreamlabs/station.yaml`, off by default, writes to `~/.dreamlabs/rollouts`). Ask the organizers before turning it on, since it's their config. Keep **only** runs that both Jev and a person call clean successes. Do it only on sub-tasks where base Dream-1 succeeds at least ~20% of the time, and keep them ≤30% of the dataset, for FT #2. Unverified: whether a rollout episode has the same keys as a `dhk-collect` one and passes `add_eef.py`. Check that on the first one.
- Dream's advice: 50+ episodes of one task, 10–30 s each; tens of good demos beat hundreds of rushed ones.
- **Dry run first, every time anything is new.** MakerMods arms have no brakes: start them resting where they can go limp.

### The Hill Climb task (announced 2026-11-07 morning): "stack the blocks/sticks as high as possible"

Pieces: 2 blue cubes, 1 pink cube (each with a hole), 1 pink stick, 1 yellow stick. **Target tower, built by hand** (`harness/results/target-tower-by-hand.jpg`), bottom to top: blue cube → pink cube → pink stick upright in its hole → second blue cube on the stick → yellow stick upright in that cube's hole.

- **Zero-shot (base `dreamlabs/dream-1-beta`, one prompt):** 1 try built blue + blue + pink cube + pink stick upright in 64 s. Cubes OK; the stick levels are the gap. Reach/pick prompts also picked the cube up. Stop-start motion: round trips about 0.9–1.3 s median (sync mode).
- **Chain:** `harness/sweep/tower5-chain.json`, levels L1–L4, one prompt each, judge = overhead + both wrist cams. **Overhead alone can't judge the sticks** (L2 0.25 vs 0.72 with the wrists).
- **Judge on the finished tower:** L1 0.87, L2 0.72, L3 0.68, **L4 0.40**. Thresholds set to 0.70 / 0.60 / 0.55 / 0.33, provisional until we have "not done" frames.
- **USB zero-shot (12:00):** refused while the strip was plugged in; once it was unplugged (true), Dream grabbed the cable and aimed it at a port but never inserted it. Unplug: closed on the plug 4 times but never pulled it out. Its grasp lands **off the middle of the plug body and too weakly** (station `gripper_hold_torque_nm: 0.5`, so ask the organizers), and **the cables sometimes block the camera's view of the plug**. Plug demos: grip the connector body in the middle, pull or push straight, keep the wires clear of the camera.
- **Prompting lesson:** for base Dream-1, one task-level prompt beat our 4-step chain (each `/prompt` switch starts a fresh episode, and narrow wording grabbed the wrong stick). Server lag spikes (median 0.9 → 1.9 s) cause the stop-start "freezes": Dream's loop holds the arm between chunks whenever the median round trip goes over 900 ms.
- **Demos for FT #1** (`--task` text = the chain prompt for each level):
  - ~30 full 5-level builds, task "stack the blocks and sticks as high as possible"
  - ~25 stick levels only (L2–L4), starting from a pre-built base
  - ~10 recoveries (a stick fallen over, a cube knocked off)

## Hill Climb in one line

Shared long-horizon task on the Maker/Metal arm stations. Rank = eval score only, no pitch.
Collect demos → fine-tune Dream-1 → build the harness (what next, did it work, recover).

## What you already have (your team-formation pitch)

> "I've already got the Dream-1 fine-tune pipeline working end to end: LeRobot v2.1 recorder that
> passes Dream's dataset checker, embodiment register/upload/train/poll script, Maker Arm IK verified
> against Dream's FK to the mm, and a vision success-checker (Jev) that drove a real pick-and-place.
> I need people for demo collection and the station."

- `robotics/robotics-lab/prism/finetune.py`: status / upload / train / poll. **Smoke-tested 2026-11-06 night: key OK**, queue 41/50 slots free.
- `prism/record.py`: LeRobot v2.1 writer, Dream body layout (right_arm/right_gripper/left_arm/left_gripper + overhead/wrist_l/wrist_r 320x176).
- `prism/dream_run.py`: inference loop (session per episode, 48-step chunks, `ik: {"arm": "makers"}`).
- `prism/maker_arm.py` + `maker_arm_ik.urdf`: Maker Arm FK/IK.
- Dream wire-format notes: `robotics/inspect-robots-dk1/docs/dream-api-notes.md`.
- Jev "held?/done?" checker pattern (Noul with example images + Box): see the DK1 pick-place loop in `robotics/dk1`.
- Run: `doppler run -p typesafe -c dev -- prism/.venv-dream/bin/python prism/<script>.py` (Python 3.12 venv).

## Lessons from your own runs (don't repeat these)

1. **Check the overhead camera frame before every run.** 2 of 4 Maker episodes were wasted on a camera aimed at the wrong place.
2. **Zero-shot won't do it** (0/9 grasps, ~4 cm lateral miss). Fine-tuning is the whole game; get a job queued early.
3. Gripper convention: Dream = **0 closed, 1 open**.
4. One process on the serial port at a time (a double launch dropped the DK1 arm).
5. Barrel-distorted cameras broke pixel→table mapping on DK1; if the station cam is fisheye, use colour/Box, not a pinhole map.
6. Jev: read live docs first; describe objects by how they look on camera; example images for "held"/"done"; retry 429/5xx.

## Hill Climb game plan

**The two things that limit you: fine-tune turnaround (7–8 h + up to 1 h first start, so ~2 tries, the second can't learn from the first's eval) and time on the arms.**
Everything below protects those two.

### Principles

1. **Score = reliability, not best run.** Find out at 9:00 if sub-steps earn partial credit. If they do, make the first 1–2 sub-steps nearly certain before touching the later ones.
2. **One prompt per sub-task.** Record each sub-task as its own episode with its own prompt ("pick up the red block", "place it in the bin"). That's what lets the harness "decide what happens next": it sequences prompts. One long episode with one prompt gives the harness nothing to steer.
3. **Consistent demos beat many demos.** Same strategy every time (same grasp, same approach side, same speed). Vary only what the eval varies (object start positions, lighting). Drop any wobbly or corrected demo.
4. **Match the eval reset.** Learn exactly how the judges set up an attempt, and record demos from that distribution.
5. **10–20% recovery demos.** Start from a typical failure (object dropped, knocked over, half-grasped) and finish the sub-task. These are what make "recover" possible.
6. **A false "done" is worse than a missed one.** The checker must not say success when it isn't. Tune it toward "not done".
7. **Change one thing at a time on Sunday.** Every eval run goes in the log.

### Roles (team of ~5; merge them if you're 3)

| Role | Owns |
|---|---|
| **Teleop lead** | Best hands on the leader arm; records every demo the same way |
| **Data QA** | Watches each episode back, keeps or drops it, labels sub-task/prompt, counts |
| **Fine-tune (you)** | Upload/train/poll, checkpoint bookkeeping, Dream inference loop |
| **Harness + checker** | Sub-task sequencer, Jev "done?" checks, retry/recover logic |
| **Eval + station** | Station bookings, eval log, resets, timer; runs the live attempt |

### Timeline

| When | Do |
|---|---|
| **Sat 9:00–10:00** | Read the rules: task, sub-steps, partial credit, time limit, reset protocol, how many attempts count. Ask Dream: who pays for fine-tunes, and can a team run **2 jobs**? Which arm do we get (Maker/Metal/YAM)? Can a fresh model be **warmed from a laptop** with a dummy `get_action` (so the overnight model is warm by 10 AM)? |
| **Sat 10:00** | Book every station slot allowed (Sat afternoon/evening + Sun morning). |
| Sat 10–10:45 | On the station: `dhk-collect --dry-run`, then **zero-shot baseline**: `dreamcli robot run --task "<sub-task 1>" --dry-run`, then real, ~3 tries per sub-task. Record 3 demos → `add_eef.py` passes. The webcam for the judge goes on the table edge, framed and taped. |
| Sat 10:45–1:00 | **Demo sprint #1:** 50+ clean demos (10–30 s), one `--task` text per sub-task, weighted to the first sub-tasks. Data QA deletes bad ones on the dashboard. |
| **Sat ~1:00–1:30** | **FT #1 queued** (`add_eef.py --replace` → `finetune.py`, tmux; organizer OK). Ready ≈ **9–10:30 PM**. Meanwhile the harness runs on **base Dream-1 via `robot up`**, and `examples.py --test` calibrates each sub-task's checker threshold on our recorded demos (needs the frames from our webcam, so record a few demos with it running). |
| Sat 1:30–8 | Demo sprint #2 toward 150+: later sub-tasks + recovery demos (start from typical base-model failures). |
| **Sat ~8–9 PM** | **FT #2 queued** on all kept demos (ready ≈ 4–6 AM). If FT #1 is ready before 10 PM, dry-run it once to start its warm-up. Leave by 10. |
| Sun 10–12 | 10-run evals: FT #1 vs FT #2 vs base, same harness. Expect the first call of each new model to sit up to an hour warming up. Pick the winner by sub-steps per attempt. |
| Sun 12–1 | Harness tuning only: retries, thresholds, timeouts, skip rule. One change → 3 runs → keep or revert. |
| **Sun 1:00** | **Freeze the config.** 5 dry runs with the official reset. |
| **Sun 2:45** | Submit (deadline 3:00). Live attempt: same config, camera check, same reset as practice. |

### How Dream-1 is actually run

- **Let Dream's station loop drive the arms** (`dreamcli robot up`, or `robot run` for single tasks). It streams camera ticks, refills chunks asynchronously while the arm moves, smooths the seams, solves the IK (Maker/Metal fine-tunes answer tool poses) and applies the speed caps and keep-out rules. Our harness only changes the prompt through the local API. Writing our own loop is the fallback (the `dreamlabs_client` two-thread pattern), not the plan.
- `/prompt` starts a fresh episode on the new prompt "at the next safe point" without stopping, so chaining sub-tasks never homes the arm in between. Recovery picks `home` (stop → home → start) or a recovery prompt.
- Warm-up: a new model's first call waits up to an hour. Trigger it the moment the job finishes.

### Harness (write once the rules are known)

```
for subtask in plan:                      # ordered by score value
    for attempt in 1..N:                  # N ~ 2-3, set by the time limit
        run dream(prompt=subtask.prompt) for up to T seconds (chunked)
        if checker.done(subtask, frame):  # Jev: Noul + example images from your own demos; Box for "where"
            break
        recover(subtask)                  # re-home arm, re-open gripper, re-prompt (or recovery prompt)
    else:
        if time is short: skip and keep partial credit
```

- Checker examples come from your own demo videos (last frame = done, first frame = not done), per sub-task.
- Log every attempt: checkpoint, harness config, sub-tasks reached, failure mode, seconds.
- Keep code in charge of the motion, and Dream in charge of what each chunk does (the pattern that worked on DK1).

### Sim and public data (decided 2026-11-06 night)

- **No sim demos.** Dream-1 acts from the cameras. MuJoCo renders look nothing like the station, and the task is unknown until 9 AM.
- **No public data in the fine-tune.** The `makermods` org on Hugging Face and the earlier MakerMods hackathon (May 9–10, ~20 team datasets) are mostly SO-101 arms, or a single Maker Arm (7-D state, side+wrist cams) with 5–20 episodes. Wrong arm or camera layout, wrong task, and with ~2 fine-tunes you can't A/B whether it hurts.
- **Format risk (resolved 2026-11-07: the kit's `dhk-collect` writes v2.1, so our converter is only a backup):** MakerMods' recorder writes **LeRobot v3.0**, and Dream's `validate_dataset` accepts **only v2.1** (dreamlabs_client 1.0.14 README). Ask at 9:00 whether the station recorder exports Dream-ready v2.1. If not, convert v3.0 → v2.1 (test the converter on `makermods/maker_arm_place_motor_in_box_20260821_163049` + `client.validate_dataset`, which needs no key).
- MakerMods' own datasets show their recipe: `2cam/3cam_200ep_blue_cube_orange_tray` + `rollout_correction_*` + `merged_200ep_24corr3x_10trans` = ~200 demos, then correction demos from failed rollouts (weighted 3x). Same idea as the targeted demos for FT #2.

### Built overnight 2026-11-07 (tested; nothing sent to Dream, no fine-tune)

Run everything with `~/Documents/Work/robotics/robotics-lab/prism/.venv-dream/bin/python`.

- **`tools/v3_to_v21.py`: MakerMods v3.0 → Dream v2.1.** On `makermods/maker_arm_place_motor_in_box_20260821_163049` (20 eps, 13,411 frames): **passes `validate_dataset`** in 69 s. Cuts are frame-accurate (video frames = rows per episode; pixel match at offset 0). Writes `meta/embodiment.json` (arm [0,6] + gripper [6,7], cameras by role). Flags: `--drop 3,7` (bad demos), `--role side=overhead`, `--deg2rad`, `--grip CLOSED,OPEN`, `--size W:H`; several inputs → one session folder each. Output: `data/dream/place-motor/`.
  - **Units question for 9:00:** MakerMods records LeRobot degrees (gripper about −111..−2), and Dream's client speaks radians with the grip 0 closed / 1 open. Ask Dream what units a Makers/Metal fine-tune wants. The converter copies values as recorded unless `--deg2rad` / `--grip` are passed.
- **`harness/harness.py`: the loop (rewritten the morning of 2026-11-07 on Dream's station API).** Plan JSON → per sub-task: `/start` or `/prompt` the sub-task, Jev watches our webcam in the background; done → next prompt (arm keeps going), timeout → recover (`home` / a recovery prompt / `none`) → retry, out of tries or time → skip, arm fault → stop. Every try goes into `eval_log.csv`; `--wait` pauses for the reset between runs.
  - Tested against **Dream's real `robots.station_api` server** with a stub controller: start / prompt / stop / home calls land as intended, an error state is caught, and a refused 409 surfaces. `--fake`: 30 runs → 24 full (theory ≈ 23.6).
  - On the station: `dreamcli robot up` in one tmux window; `TYPESAFE_API_KEY=... ~/dream-env/bin/python harness/harness.py plan.json --checkpoint <model> --camera <idx>` in another. Copy `harness/` over with `scp -r`. Needs opencv + requests in `~/dream-env`; check at 10:00.
- **`harness/examples.py`: checker calibration from demos.** Pulls first frames (not done) and last frames (done) per task, holds out episodes, asks Jev about them and prints the threshold that separates them. Tested with real Jev on the public data (one camera, side view):
  - **Jev refuses small images** (320×240 → "Invalid image dimensions"; 448×336 is fine). The checker upscales anything under 336 px, including Dream's 320×176 frames.
  - A vague question ("is the motor in the box?") scored finished episodes only **~0.45**. A camera-literal one ("small silver cylinder … inside the black foam tray of the open white box, rather than on the white table") scored **0.69–0.84 done vs 0.01–0.02 not done, 6/6 held-out separated.**
  - **Done scores sit around 0.7, not 0.9, so set the threshold per sub-task from `--test`** (0.42 here) rather than a global 0.8. Example images didn't help on this data; A/B them on the real task.
  - Template: `harness/plan.example.json`. The judge camera is now our own webcam, so calibrate `examples.py` on clips from that webcam, not on the station's recordings.

### Decision points

- **Dataset fails to process at 10:30** → you fix the format, everyone else keeps recording. Don't stop demos.
- **FT #1 is no better than base** → check the data (prompt labels, gripper convention 0=closed/1=open, camera keys) before recording more.
- **Fine-tune queue too slow for 2 tries** → put all the effort into FT #1 data quality and the harness. Retries and recovery still lift a weak model's score.
- **Stuck on sub-task k** → submit with a reliable 1..k-1 and skip k. Partial credit beats a fragile full run.

## Pack tonight

- Laptop + charger + **power strip** (stations run out of outlets)
- USB-C hub, USB-A↔C cables
- Your straight-lens spare webcam (backup context/overhead cam)
- Phone charged (Luma ticket for check-in), water/snacks
- Be there **9:00 sharp** (team formation at 9:50 needs you in the room)

## Business goal (one thing)

Three Dream Labs founding researchers are judging (Kaushil Kundalia, Jeremy Collins, Grace Zhang). Have **one priced
conversation** about demonstration / verification data: "If we delivered N verified demos on your station layout,
what would that be worth per hour / per episode?" Log the answer in nousbiz `customers/` same day.
