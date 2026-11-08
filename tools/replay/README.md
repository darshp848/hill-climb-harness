# Replay viewers for Dream station runs

These tools build self-contained HTML replays of archived station runs (`~/rollouts/zeroshot_<time>/`, written by
`~/archive_runs.sh`). Each replay plays the camera video synced with the traces:
- the prompt timeline;
- gripper gap, commanded vs measured;
- tool height, from the Metal URDF FK;
- arm freezes and the Dream chunk round trips;
- gaps between steps, including scripted lifts made between steps.

The tools need only the Python standard library and `ffmpeg`, and none of them touches the station.

| File | What it does |
|---|---|
| `build_session_viewer.py <session dir>` | Stitches every `zeroshot_*` run in a folder into one replay: `session_viewer.html` and `session_replay.mp4`. It skips runs that never moved (for example a cold fine-tune with no actions). |
| `build_viewer.py <rollout dir>` | Builds the replay of a single run: `<run>_viewer.html` and `_replay.mp4`. |
| `analyze_run.py <rollout dir> tools/replay tools/replay/metal_with_gripper.urdf` | Prints a text timeline of one run (prompts, grasps and releases, heights, holds) and writes `timeline.json`. |
| `viewer_template.html` | The page both builders fill in. |
| `metal_fk.py` and `metal_with_gripper.urdf` | Pure-Python forward kinematics. The tool point is [0.135, 0, 0] in link6. |

## Gather a session (read-only on the station)

    mkdir -p replays/mysession && cd replays/mysession
    ssh <station> 'cd ~/rollouts && tar cf - zeroshot_20261108T14*' | tar xf -     # the runs you want
    # the two log excerpts covering the same window (station log is local time, inference log is UTC):
    ssh <station> "awk '\$1\" \"\$2 >= \"2026-11-08 13:46:50\"' ~/.dreamlabs/station.log" > station_log_excerpt.txt
    ssh <station> "python3 -c \"import json,sys
    for l in open('/home/pah/robots-inference.jsonl'):
        try: t=json.loads(l).get('t','')
        except Exception: continue
        if t >= '2026-11-08T21:46:50': sys.stdout.write(l)\"" > inference_excerpt.jsonl
    cd - && python3 -I tools/replay/build_session_viewer.py replays/mysession
    open replays/mysession/session_viewer.html

The `awk` filter keeps only the station log's timestamped lines. The `pah_tuning: pull …` lines carry no timestamp,
so to keep them, copy everything from the first line of the window onward instead.

Each step in the replay is one station run. The viewer marks how long the session waited before each step and what
the arms did in between (homed, lifted straight up, and so on).
