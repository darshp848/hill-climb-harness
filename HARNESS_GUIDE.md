# Hill Climb harness: how to use it

The harness runs Dream-1 through a long task as a list of stages. For each stage it:

1. sends a prompt,
2. watches the cameras,
3. decides whether the stage worked,
4. recovers when it didn't.

Version 0.2.0. The design contracts are `DESIGN.md` (v1) and `DESIGN_v2.md` (v2). The short reference is `README.md`.

## 1. What it is, and what it never does

- **It never touches a motor.** It only talks to Dream's own station agent (`dreamcli robot up`) over HTTP on 127.0.0.1:
  - `/status`, `/start`, `/stop`, `/home`, `/reset`, `/prompt` on port 8091
  - the frames from the debug view on port 8093 (`/trips`, `/trip/<n>`)

  Every speed cap, clamp, gripper force cap and home move stays inside Dream's control loop.
- **Preflight refuses to start** if:
  - the station is not idle (or is in error),
  - an arm is dead,
  - a camera is older than 2 s.
- **It fails safe.** Each of these sends `POST /stop`, and the arms hold where they are:
  - an arm dies
  - the station goes into `error`
  - the time budget runs out
  - Ctrl+C
  - `s` + Enter
- It never homes the arms at the end. You do that yourself.
- The physical e-stop is still the real safety. Keep a hand on it during `--live` runs.

## 2. Install

Python 3.12 or newer. The harness needs only PyYAML. On the station, the `dream-env` already has it.

```bash
unzip hill_climb_harness.zip && cd hill_climb_harness
pip install pyyaml pytest             # pytest only for the tests
python -m pytest -q                   # offline, against fake_station.py
```

## 3. Where to run it

**Option A: on the station.** This needs two terminals, both with `source ~/dream-env/bin/activate`.

```bash
# terminal 1: Dream's station agent (leave it running)
dreamcli robot up --view-port 8093          # YAM fine-tune: dreamcli robot up joint --view-port 8093

# terminal 2: the harness
python -m harness plans/<plan>.yaml --dry-run
python -m harness plans/<plan>.yaml --live
```

**Option B: on your laptop, through an SSH tunnel to the station or NUC.** The station agent still runs on the station. The laptop only sends HTTP through the tunnel.

```bash
ssh -L 8091:localhost:8091 -L 8093:localhost:8093 <user>@<station-ip>
# in another laptop terminal:
python -m harness plans/<plan>.yaml --dry-run      # then --live
```

Both endpoints listen on 127.0.0.1 on the station, so the tunnel is the only way to reach them from outside. If you also want the dashboard, add `-L 8080:localhost:8080`.

## 4. The three modes (exactly one is required)

| Mode | What it does |
| --- | --- |
| `--dry-run` | Reads status and frames, and prints every POST it *would* send. It sends nothing that moves an arm. Always do this first on a new station. |
| `--live` | The real run. It prints the safety lines (hand on the e-stop, people clear) and waits until you type `yes`. |
| `--mock` | Starts `fake_station.py` in-process, so no station is needed. Add `--scenario happy\|arm_dies\|start_error\|slow_start\|camera_stale` to practise failure handling. |

Other options:

- `--station URL` (default `http://127.0.0.1:8091`)
- `--view URL` (default `http://127.0.0.1:8093`)
- `--runs-dir DIR` (default `runs`)
- `--verifier NAME` overrides every stage's verifier.
- `--var NAME=VALUE` fills a `{NAME}` placeholder in the plan, for example `--var color=orange`. It can be repeated.

While it runs, type `y` / `n` + Enter to answer a manual check, or `s` + Enter to stop.

## 5. Verifiers (who decides "stage done")

| Name | How it decides | Needs |
| --- | --- | --- |
| `manual` | You watch. `y` means the stage is done. `n` means the attempt failed now. | nothing |
| `time` | Done at 90% of `timeout_s`. Use it for stages with no check, or for unattended practice. | nothing |
| `claude` | Sends one frame per camera and the stage's yes/no question. It counts as done only at confidence >= 0.7. | `ANTHROPIC_API_KEY` |
| `claude+manual` | Same as `claude`, but your `y` / `n` overrules it at once. | `ANTHROPIC_API_KEY` |
| `monitor` | The v2 monitor. It sends a frame **history** (the stage's first overhead frame, the last `history_frames` overhead frames, and the newest wrist frames) with the elapsed time, the timeout, the stage list and the recovery menu. It first writes a few questions about the scene and answers them, then returns JSON: `stage_done`, `progress` 0..1, `status` ok/stalled/failing, `holding` left/right, `recovery` index, `scene_ok`, `confidence`. It runs in the background so health checks keep going. | `ANTHROPIC_API_KEY` |
| `monitor+manual` | Same as `monitor`, but your key press wins. This is the recommended choice for practice runs. | `ANTHROPIC_API_KEY` |

The API key is only sent to api.anthropic.com and is never logged. The default model is `claude-haiku-5-5`, and `claude.model` in the plan changes it.

**Rules:** human `y`/`n` is banned in a match (see 6b). Use `manual` and `monitor+manual` only in practice.

## 6. Writing a plan (YAML)

### Plan-level fields

```yaml
task: "stack the cubes, insert the pegs, plug in the cables"   # for logs and the monitor
checkpoint: null            # dream-1-ft:... for your fine-tune, null for base Dream-1
time_budget_s: 900          # whole run; exceeded -> stop, fail
check_every_s: 3.0          # how often the verifier is asked
verifier: monitor+manual    # default for stages
confirm: 2                  # v2: consecutive "done" (or "failing") verdicts needed; 1 = v1 behaviour
cameras_for_check: [scene]  # optional: camera roles the verifier sees (substring match), default all
claude: {model: claude-haiku-5-5, max_tokens: 600, history_frames: 6}
```

### Stage fields

```yaml
stages:
  - name: pick_blue                      # unique
    prompt: "pick up the blue cube"      # one line, <= 512 chars, sent to Dream-1 as-is
    success: "Is the blue cube held in a gripper, off the table?"   # the yes/no question
    expect: "The arm closes on the cube and lifts it."              # v2, optional, helps the monitor
    timeout_s: 60                        # an attempt fails after this
    max_attempts: 3
    min_run_s: 4                         # no check before this
    settle_s: 6                          # v2: after any prompt change, no check and no stall clock
    stall_s: 15                          # v2: fail "stalled" if progress hasn't risen 0.05 in 15 s
    skip_if_done: false                  # v2: check once before prompting; already done -> skip
    verifier: manual                     # optional per-stage override
    recovery:                            # see below
      - name: keep_going
        kind: retry                      # scene is fine: just re-issue the stage prompt
        when: "the arm is still near the cube"
        max_uses: 2
      - name: reopen_and_lift
        kind: reset                      # scene broken: run this prompt, check scene_ok, then retry
        prompt: "open both grippers and move the arms up"
        when: "the cube was dropped or knocked over"
        settle_s: 8
      - "move the arm back above the cube"   # a plain v1 string still works
    on_fail: retry_in_place              # retry_in_place | home_and_retry | skip | abort
```

### How a run flows

- **Starting.** The first stage starts the run. Each later stage changes the prompt live (`/prompt`), with no stop in between. If the run has ended, the harness starts a new one.
- **When an attempt fails,** the harness picks the next recovery:
  - the monitor's suggestion, if that item still has uses left;
  - otherwise the next unused item;
  - never the same item twice in a row.

  When the menu is used up, `on_fail` takes over:
  - `retry_in_place` runs the recovery prompt, then the stage prompt again.
  - `home_and_retry` does stop, home, start.
  - `skip` marks the stage failed and moves on.
  - `abort` stops the run.

  When the attempts run out, `skip` moves on and anything else fails the run.
- **Rules from the papers:**
  - Ask for at least 2 confirmations (`confirm: 2`) before accepting a success. A false success is expensive.
  - A "failing" verdict only counts after 50% of the timeout.
  - A progress drop of 0.3 or more is logged as a regression and counts as a failing verdict.
- **Writing prompts:** keep each prompt atomic, with one object and one action. If the model needs to know which hand to use, name it ("with the left arm").
- **Validation:** a bad plan is rejected before anything runs, and the error names the stage and the field.

### Plans included

| Plan | What it is |
| --- | --- |
| `plans/example_hill_climb.yaml` | v1 example (tidy-up placeholder) |
| `plans/example_hill_climb_v2.yaml` | Uses every v2 field, with comments |
| `plans/sim_fruit_bowl.yaml` | For the MuJoCo sim scene (apple, orange, banana, bowl, plate) |
| `plans/hill_climb_sprint.yaml` | **Match plan.** Unplug 1 cable, put it down, then stack 2, stack 3, move clear. Then it prints "CALL STOP NOW". |
| `plans/hill_climb_full.yaml` | The sprint stages, then up to 8 balls of your colour. Needs `--var color=<colour>`. |

## 6b. Match rules and how the plans follow them (from `hillclimb_rubric_v2.pdf`)

The match is a 5-minute 1v1. The stages, in order:

1. **Unplug.** The pass bar is 1 cable out: +10, and +5 for each further unplug. Plugging a loose cable into a free port is +20.
2. **Stack.** The pass bar is a free-standing stack 3 layers / 6 inches tall that still stands 3 s after release: +20, and +10 for each further block. If the stack falls later, those points are lost.
3. **Balls.** Own colour +5, other colour -5. A black 8-ball in the bin loses the match.

Time bonus: +1 per 2 s left on the clock, but only if Stages 1 and 2 passed and a team member calls "stop".

What the rules mean for the harness:

- **No human input after "go".** In a match, use only `verifier: monitor` (the plans set it). Typing `y`/`n` is human input and gets you DQ'd, so never use `manual` or `monitor+manual` in a match. Don't use `time` either: it would move on without the pass bar being met.
- **A false "done" is a DQ.** Touching a later stage's objects before the pass bar is met is a DQ. That's why the plans:
  - require `confirm: 2`,
  - never use `on_fail: skip` on Stages 1-2 (they retry until the clock runs out),
  - make the last stack check need two "done" verdicts 3 s apart.
- **Stop early.** Stopping right after Stage 2 usually wins. A ball pays only if it takes under 10 s (it costs 5 bonus points of clock), and an extra block pays only if it takes under 20 s. So the sprint plan ends after the stack and prints `CALL STOP NOW`, and the designated member says "stop" out loud. Use the full plan only if practice shows balls are fast.
- The run time budget is 295 s.

```bash
python -m harness plans/hill_climb_sprint.yaml --live
python -m harness plans/hill_climb_full.yaml --live --var color=orange   # colour from the coin flip
```

Still unclear in the rubric (ask the organizers):

- Is an outside model (Claude) as the harness's checker allowed? The rubric bans only human input and teleop.
- Does pressing Enter to start the harness at "go" count as keyboard input?

## 6c. A watcher arm for the cable plug-in (second camera view)

The overhead camera is poor at depth, so it can't tell whether a plug is lined up with the port or pushed in. In `hill_climb_full.yaml`, the plug-in has three stages:

1. `watcher_into_place` asks Dream-1 to park the other arm beside the hub, with its wrist camera looking at the ports from the side. This stage is optional (`on_fail: skip`).
2. `plug_loose_cable` has one arm plug the cable in while the watcher holds still. The monitor gets:
   - the frame history of the watcher's wrist camera, not only of the overhead camera (`history_cameras`),
   - a note per camera saying what it shows (`camera_notes`), so it judges depth from the side view.
3. `release_before_stack` opens both grippers and lifts the arms clear before the stack.

You choose the arms with `--var work_arm=left --var watch_arm=right --var work_cam=wrist_l --var watch_cam=wrist_r`. The defaults are: right arm works, left arm watches.

The new stage fields work in any plan:

| Field | What it does |
| --- | --- |
| `cameras` | The camera roles this stage's checks see. This overrides the plan's `cameras_for_check`. |
| `history_cameras` | Extra roles whose frame history the monitor keeps. |
| `camera_notes` | Maps a role to a sentence telling the monitor what that view shows. |

Plans can also set defaults with `vars:`. `--var` overrides them.

**Limit:** the harness can't move an arm itself. Dream-1 has to do the watcher pose from the prompt, and base Dream-1 may ignore "stay still" or "park beside the hub". To make it reliable, record teleop demos of the watcher pose for the fine-tune. Skipping it costs nothing: if the watcher never gets into place, the stage times out after 15 s and the plug-in still runs with the overhead and gripper views.

## 7. Test in simulation (before the robot)

`sim_station.py` serves the same API (8091) and debug view (8093), driven by Dream's MuJoCo YAM simulator (`dream_examples/yam_mujoco`). The harness can't tell it apart from a real station, and nothing physical moves, so `--live` is safe here. Note that the Hill Climb uses Metal arms, while the sim is YAM only. That makes it a test of the harness logic and prompts, not of the real task.

```bash
pip install dreamlabs mujoco pillow numpy pyyaml
# Dream key: save it once in ~/.dreamlabs/credentials (Windows: %USERPROFILE%\.dreamlabs\credentials)
#   as the block from platform.dreamlabs.ai/setup-api, e.g. a line  DREAM_API_KEY=...
#   or set the DREAM_API_KEY environment variable. Never paste it into a chat.

# terminal 1: simulated station with the real Dream-1
python sim_station.py --policy dream [--checkpoint dream-1-ft:...]
# terminal 2:
python -m harness plans/sim_fruit_bowl.yaml --live --verifier time
```

- **No key or no network:** `python sim_station.py --policy scripted` sends small fake joint chunks of the right shape. It tests the state machine, frames and logs.
- **sim_station.py options:**
  - `--no-realtime` runs the sim as fast as it renders.
  - `--example-dir PATH` points at the Dream example if it isn't installed.
  - `--actions N` sets the rows executed per chunk, 1 to 48.
- **Headless Linux:** set `MUJOCO_GL=egl` (needs a GPU) or `MUJOCO_GL=osmesa`. osmesa is slow, about 5 s per chunk.

## 8. Logs, data and the report

Each run writes `runs/<UTC timestamp>/`:

| File | What it holds |
| --- | --- |
| `events.jsonl` | Every decision, one JSON event per line. This includes `prompt_change` events (with the chunk count) and the first check where progress moved, so you can measure prompt lag. |
| `<seq>_<tag>_<role>.jpg` | The frames each check looked at |
| `segments.jsonl` | One line per stage attempt and per recovery: stage, attempt, kind, prompt, wall-clock start/end, outcome, reason, max progress. These line up by time with Dream's own recordings, so you can cut out successful segments for fine-tuning and drop failed ones. |
| `manifest.json` | Plan path and sha256, harness version, station status at start, CLI args, host, start/end time |
| `summary.json` | The final result |

To compare runs:

```bash
python -m harness.report runs/ [--last N]
```

The report gives:

- run success, and stages ok out of total
- per-stage success rate, mean attempts and mean seconds
- recovery use and how often each recovery worked
- the most common failure reasons
- a "possible false success" count, as a lead rather than proof

## 9. Checklist on the day

1. `python -m pytest -q` passes. Then run `--mock --scenario arm_dies` once to watch it stop safely.
2. Run `dreamcli robot up --view-port 8093` on the station. Check that `curl -s localhost:8091/status` shows `idle`, both arms alive, and cameras fresh.
3. `--dry-run` with your plan: check the printed POSTs and that the frames arrive (`runs/.../*.jpg`).
4. `--live` with `monitor+manual` or `manual`, a hand on the e-stop and people clear. Correct the monitor with `y`/`n`.
5. After each run, read `python -m harness.report runs/`. Tune prompts, timeouts and recoveries, and keep the good segments for the fine-tune.
6. At the end: home the arms yourself, then `dreamcli robot down`.

## 10. Open questions for the organizers

- Can an outside model (Claude) check stages during a match? (Human `y`/`n` is clearly not allowed.)
- How do we start the harness at "go" without it counting as keyboard input?
- Can the harness change the prompt live during a match, or is it one prompt per run?
- Is Ethernet hard mode on, and how tall is one block (are 3 layers already 6 inches)?
