"""Hill Climb harness: run a plan of sub-tasks on Dream-1, judge each with Jev, retry / recover / skip, log every try.

The arms are driven by Dream's own station loop (`dreamcli robot up`: its IK, speed caps, seam smoothing, safety),
through the station's local HTTP API on 127.0.0.1:8091. The harness only chooses the prompt:
  /start {prompt}   begin       /prompt {prompt}   switch task, arm keeps going (fresh episode at a safe point)
  /stop             arms hold   /home              arms to home pose (only when stopped)
Jev judges from a camera of our own (the spare webcam on the table edge): the station process holds its cameras.

  python harness.py plan.json --fake                     # whole loop: fake station API, camera and judge
  # on the station, with `dreamcli robot up` running in another tmux window:
  TYPESAFE_API_KEY=... ~/dream-env/bin/python harness.py plan.json --checkpoint dream-1-ft:... --camera 4
"""

import argparse
import base64
import csv
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
JEV_URL = "https://api.typesafe.ai/v1/systemone"
STATION_URL = "http://127.0.0.1:8091"


# ---------------------------------------------------------------- the arms: Dream's station API

class StationRunner:
    """`dreamcli robot up`'s control API. Every call answers the station's status dict."""

    def __init__(self, url=STATION_URL, checkpoint=None):
        self.url, self.checkpoint = url.rstrip("/"), checkpoint

    def _call(self, route, body=None, method="POST"):
        data = json.dumps(body or {}).encode() if method == "POST" else None
        req = urllib.request.Request(self.url + route, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=70) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"station {route}: {e.code} {e.read().decode()[:300]}") from None

    def status(self):
        return self._call("/status", method="GET")

    def run(self, prompt):
        """Start a run on `prompt`, or switch the running one to it without stopping the arms."""
        if self.status()["state"] in ("running", "starting"):
            return self._call("/prompt", {"prompt": prompt})
        body = {"prompt": prompt}
        if self.checkpoint:
            body["checkpoint"] = self.checkpoint
        return self._call("/start", body)

    def stop(self):
        return self._call("/stop")

    def home(self):
        self.stop()
        return self._call("/home")

    def problem(self):
        """Why the run cannot go on (state error, a dead arm), or None."""
        st = self.status()
        dead = [a for a, ok in (st.get("arms") or {}).items() if not ok]
        if st["state"] == "error" or dead:
            return f"{st['state']}: {st.get('last_error') or ''} dead arms {dead}".strip()
        return None


class FakeRunner:
    """The station API's states, no arms."""

    def __init__(self):
        self.state, self.prompt, self.calls = "idle", None, []

    def status(self):
        return {"state": self.state, "prompt": self.prompt, "arms": {"left": True, "right": True}}

    def run(self, prompt):
        self.calls.append(("prompt" if self.state == "running" else "start", prompt))
        self.state, self.prompt = "running", prompt

    def stop(self):
        self.calls.append(("stop",))
        self.state = "idle"

    def home(self):
        self.stop()
        self.calls.append(("home",))
        time.sleep(0.2)

    def problem(self):
        return None


# ---------------------------------------------------------------- the judge's eyes: a camera of our own

class Camera:
    """A USB webcam read on its own thread; frames() is {"judge": latest JPEG}."""

    def __init__(self, index, width=1280, height=720):
        import cv2
        self.cv2, self.cap = cv2, cv2.VideoCapture(index)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if not self.cap.isOpened():
            raise SystemExit(f"camera {index} did not open")
        self.latest, self.lock = None, threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()
        for _ in range(50):
            if self.latest is not None:
                break
            time.sleep(0.1)

    def _read(self):
        while True:
            ok, img = self.cap.read()
            if ok:
                with self.lock:
                    self.latest = img

    def frames(self):
        with self.lock:
            img = self.latest
        return {"judge": self.cv2.imencode(".jpg", img, [self.cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()}


class StationCamera:
    """The station's own camera, through the GET /frame/<name>.jpg route `pah_judge/sitecustomize.py` adds to
    Dream's station API (start `dreamcli robot up` with PYTHONPATH=~/pah_judge)."""

    def __init__(self, url=STATION_URL, name="scene"):
        self.srcs = {n: f"{url.rstrip('/')}/frame/{n}.jpg" for n in name.split(",")}  # "scene,wrist_r" sends both

    def grippers(self):
        """{arm: {"gripper": opening 0 closed..1 open, "commanded_gripper", "gripper_effort"}} via the /arms route."""
        base = next(iter(self.srcs.values())).rsplit("/frame/", 1)[0]
        with urllib.request.urlopen(base + "/arms", timeout=5) as r:
            return json.loads(r.read())

    def frames(self):
        out = {}
        for n, src in self.srcs.items():
            with urllib.request.urlopen(src, timeout=10) as r:
                out[n] = r.read()
        return out


class FakeCamera:
    JPEG = base64.b64decode(  # 8x8 grey baseline JPEG
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0"
        "Hyc5PTgyPC4zNDL/wAALCAAIAAgBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8A"
        "VN//2Q==")

    def frames(self):
        return {"judge": self.JPEG}


# ---------------------------------------------------------------- checker: Jev in the background

class JevChecker:
    """Asks Jev whether the current view looks like the sub-task's `done` examples. Says done only after
    `confirm` verdicts in a row at or above `threshold`: a false "done" costs more than a late one."""

    def __init__(self, api_key, model="jev-1.14.0", threshold=0.8, confirm=2, period=1.5):
        self.key, self.model, self.th, self.confirm, self.period = api_key, model, threshold, confirm, period

    def ask(self, sub, now_jpegs):
        import requests
        assets, state = {}, {"context": sub.get("context", ""), "task": sub["prompt"]}

        def add(group, jpegs):
            state[group] = []
            for j in jpegs:
                r = str(uuid.uuid4())
                assets[r] = {"type": "image", "format": "jpg", "data_base64": base64.b64encode(jev_jpeg(j)).decode()}
                state[group].append(r)
        add("done_examples", load_examples(sub, "done"))
        add("not_done_examples", load_examples(sub, "not_done"))
        add("now", now_jpegs)
        q = {"done": {"type": "noul", "instructions": sub["check"] + " Judge only the images in `now`; "
                      "`done_examples` show the task finished and `not_done_examples` show it not finished yet."}}
        body = {"model": self.model, "state": state, "assets": assets, "questions": q}
        for attempt in range(4):
            r = requests.post(JEV_URL, json=body, timeout=60, headers={
                "Authorization": f"Bearer {self.key}", "TypeSafe-Danger-Zone-Images": "true"})
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return float(r.json()["answers"]["done"]["noul"])
        r.raise_for_status()

    def watch(self, sub, camera, done: threading.Event, stop: threading.Event, log):
        streak, self.best = 0, 0.0
        while not stop.is_set() and not done.is_set():
            try:
                jpegs = list(camera.frames().values())
                p = self.ask(sub, jpegs)
                if getattr(self, "save_dir", None):
                    d = Path(self.save_dir) / sub["name"]
                    d.mkdir(parents=True, exist_ok=True)
                    (d / f"{time.strftime('%H%M%S')}_p{p:.2f}.jpg").write_bytes(jpegs[0])
            except Exception as e:  # noqa: BLE001 - a judge error is "not done", never "done"
                log(f"  checker error: {e}")
                p = 0.0
            self.best = max(self.best, p)
            streak = streak + 1 if p >= sub.get("threshold", self.th) else 0
            log(f"  P(done)={p:.2f} streak {streak}/{self.confirm}")
            if streak >= self.confirm:
                done.set()
            stop.wait(self.period)


class FakeChecker(JevChecker):
    """Each attempt succeeds with probability `p_success`, judged a few seconds in."""

    def __init__(self, p_success=0.6, **kw):
        super().__init__(api_key=None, **kw)
        self.p, self.period, self.best = p_success, 0.3, 0.0

    def watch(self, sub, camera, done, stop, log):
        will = random.random() < self.p
        t_done = time.monotonic() + random.uniform(0.5, 1.5)
        while not stop.is_set() and not done.is_set():
            if will and time.monotonic() >= t_done:
                done.set()
            stop.wait(self.period)


class RefereeChecker:
    """Claude referee (referee.py) as the judge: each check reads the whole relay state, and a sub-task is done when
    every condition in its `done_when` holds, e.g. {"cables_in_hub": ["<=", 1], "cable_in_gripper": ["==", false]},
    `confirm` reads in a row."""

    OPS = {"<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b, "==": lambda a, b: a == b}

    def __init__(self, model="claude-haiku-5-5", confirm=3, period=2.0, crop=None, **_):
        import referee
        self.ref, self.confirm, self.period, self.best, self.last = referee.Referee(model), confirm, period, 0.0, None
        self.crop, self.start = crop, None  # start = the relay's first frames, taken once, compared on every read

    def watch(self, sub, camera, done, stop, log):
        streak, self.best = 0, 0.0
        while not stop.is_set() and not done.is_set():
            try:
                jpegs = list(camera.frames().values())
                if self.start is None:
                    self.start = jpegs
                state, dt, _ = self.ref.read(jpegs, start_jpegs=self.start, crop=self.crop)
                self.last = state.model_dump()
                self.last["feel"] = feel(camera, sub.get("arm", "right"))
                ok = state.confidence != "low" and \
                    all(self.OPS[op](self.last[k], v) for k, (op, v) in sub["done_when"].items())
                if self.last["feel"] == "closed_empty":  # closed all the way: the fingers hold nothing
                    self.last["grip_fix"] = "open the right gripper fully, then grip the black rubbery part of the plug again and squeeze hard"
                if getattr(self, "save_dir", None):
                    d = Path(self.save_dir) / sub["name"]
                    d.mkdir(parents=True, exist_ok=True)
                    (d / f"{time.strftime('%H%M%S')}_{'ok' if ok else 'no'}.jpg").write_bytes(_sheet(jpegs))
                    (d / f"{time.strftime('%H%M%S')}.json").write_text(json.dumps(self.last))
            except Exception as e:  # noqa: BLE001 - a referee error is "not done", never "done"
                log(f"  referee error: {e}")
                ok, state, dt = False, None, 0
            streak = streak + 1 if ok else 0
            self.best = max(self.best, streak / self.confirm)
            if state is not None:
                log(f"  [{dt:.1f}s] removed={state.cables_removed_since_start} added={state.cables_added_since_start} "
                    f"in_hub={state.cables_in_hub} held={state.cable_in_gripper} grip={state.grip_on} feel={self.last['feel']}"
                    f"{'@port' if state.grip_near_port else ''} "
                    f"layers={state.stack_layers} touched={state.stack_touched} conf={state.confidence} "
                    f"-> {'MET' if ok else 'not yet'} {streak}/{self.confirm} | {state.note}")
            if streak >= self.confirm:
                done.set()
            stop.wait(self.period)


def feel(camera, arm="right", closed_cmd=0.15, empty=0.06):
    """The grip by feel: commanded closed and stopped partway open -> 'holding'; closed to ~0 -> 'closed_empty';
    not commanded closed -> 'open'; no /arms route -> 'unknown'."""
    try:
        g = camera.grippers()[arm]
    except Exception:  # noqa: BLE001 - a station without the /arms route, or a fake camera
        return "unknown"
    cmd = g.get("commanded_gripper")
    if cmd is None or cmd > closed_cmd:
        return "open"
    return "closed_empty" if g["gripper"] <= empty else f"holding({g['gripper']:.2f})"


def _sheet(jpegs, h=360):
    """All cameras of one moment side by side (overhead, left wrist, right wrist), for review."""
    import cv2
    import numpy as np
    ims = [cv2.imdecode(np.frombuffer(j, np.uint8), cv2.IMREAD_COLOR) for j in jpegs]
    ims = [cv2.resize(i, (round(i.shape[1] * h / i.shape[0]), h)) for i in ims]
    return cv2.imencode(".jpg", np.hstack(ims), [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()


def jev_jpeg(jpeg, min_side=336):
    """Jev refuses small images ("Invalid image dimensions"; 320x240 fails, 448x336 passes): upscale if needed."""
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    if min(h, w) >= min_side:
        return jpeg
    k = min_side / min(h, w)
    img = cv2.resize(img, (round(w * k), round(h * k)), interpolation=cv2.INTER_CUBIC)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()


def load_examples(sub, kind):
    d = Path(sub.get("examples", "")) / kind
    return [p.read_bytes() for p in sorted(d.glob("*.jpg"))[:4]] if d.is_dir() else []


# ---------------------------------------------------------------- the loop

class EvalLog:
    FIELDS = ["run", "time", "checkpoint", "config", "subtask", "attempt", "outcome", "seconds", "note"]

    def __init__(self, path):
        self.path = Path(path)
        new = not self.path.exists()
        self.f = self.path.open("a", newline="")
        self.w = csv.DictWriter(self.f, self.FIELDS)
        if new:
            self.w.writeheader()

    def row(self, **kw):
        self.w.writerow({k: kw.get(k, "") for k in self.FIELDS})
        self.f.flush()


def run_plan(plan, runner, camera, checker, log_csv, run_id, checkpoint, verbose=print):
    """One scored attempt at the whole plan. Returns the sub-tasks reached, in order."""
    budget_end = time.monotonic() + plan.get("time_limit_s", 1e9)
    reserve = plan.get("skip_if_less_than_s", 0)
    cfg = plan.get("name", "")
    log = EvalLog(log_csv)
    reached = []

    def row(sub, attempt, outcome, secs, note=""):
        log.row(run=run_id, time=time.strftime("%H:%M:%S"), checkpoint=checkpoint, config=cfg, subtask=sub["name"],
                attempt=attempt, outcome=outcome, seconds=secs, note=note)

    try:
        for sub in plan["subtasks"]:
            for attempt in range(1, sub.get("tries", 3) + 1):
                left = budget_end - time.monotonic()
                if left < max(reserve, 1):
                    verbose(f"[{sub['name']}] skip: {left:.0f}s left")
                    row(sub, attempt, "skipped_time", 0)
                    return reached
                verbose(f"[{sub['name']}] try {attempt}: '{sub['prompt']}'")
                done, stop = threading.Event(), threading.Event()
                watcher = threading.Thread(target=checker.watch, args=(sub, camera, done, stop, verbose), daemon=True)
                runner.run(sub["prompt"])
                watcher.start()
                t0, why, note = time.monotonic(), "timeout", ""
                end = t0 + min(sub.get("timeout_s", 30), left)
                fb = sub.get("feedback")  # {"after_s": 20, "every_s": 20}: correct the policy from the referee's read
                next_fb = t0 + fb["after_s"] if fb else None
                while time.monotonic() < end:
                    if done.wait(1.0):
                        why = "done"
                        break
                    last = getattr(checker, "last", None) or {}
                    if next_fb and time.monotonic() >= next_fb and last.get("grip_fix"):
                        fix = last["grip_fix"].rstrip(".")
                        verbose(f"  FEEDBACK -> '{fix}' (grip_on={last.get('grip_on')}, near_port={last.get('grip_near_port')})")
                        runner.run(f"{fix}. Then {sub['prompt'][0].lower()}{sub['prompt'][1:]}")
                        note = (note + f" fb:{fix[:40]}").strip()
                        next_fb = time.monotonic() + fb["every_s"]
                    if (note := runner.problem() or ""):
                        why = "error"
                        break
                stop.set()
                watcher.join()
                secs = round(time.monotonic() - t0, 1)
                note = f"{note} jev_max={getattr(checker, 'best', 0):.2f}".strip()
                row(sub, attempt, why, secs, note)
                verbose(f"[{sub['name']}] {why} after {secs}s {note}")
                if why == "done":
                    reached.append(sub["name"])
                    break  # next sub-task's prompt goes in while the arm keeps moving
                if why == "error":
                    return reached  # an arm fault: a person resets the station (`/reset` or `dreamcli`)
                recover(sub, runner, verbose)
            else:
                verbose(f"[{sub['name']}] out of tries")
                if sub.get("required", True):
                    return reached  # later sub-tasks depend on this one
        return reached
    finally:
        runner.stop()  # arms hold where they are; the next attempt's reset is the operator's


def recover(sub, runner, verbose):
    """The sub-task's `recover`: "home" (stop, arms home, grippers open), a prompt to run for some seconds, or
    "none" (retry the same prompt at once: /prompt starts a fresh episode)."""
    rec = sub.get("recover", "home")
    if isinstance(rec, dict):
        verbose(f"  recover: '{rec['prompt']}' for {rec.get('seconds', 8)}s")
        runner.run(rec["prompt"])
        time.sleep(rec.get("seconds", 8))
    elif rec == "home":
        verbose("  recover: home")
        runner.home()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan", type=Path)
    ap.add_argument("--fake", action="store_true", help="fake station API, camera and judge")
    ap.add_argument("--p-success", type=float, default=0.6, help="--fake: chance each try succeeds")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--checkpoint", default=None, help="the model; the station's when left out")
    ap.add_argument("--station", default=STATION_URL)
    ap.add_argument("--camera", default="station:scene",
                    help="judge camera: station:<name> (the station's own, via the frame route) or an OpenCV index")
    ap.add_argument("--home-after", action="store_true", help="send the arms home after each run")
    ap.add_argument("--log", type=Path, default=HERE / "eval_log.csv")
    ap.add_argument("--wait", action="store_true", help="wait for Enter (the reset) between runs")
    a = ap.parse_args()
    plan = json.loads(a.plan.read_text())
    if a.fake:
        runner, camera, checker = FakeRunner(), FakeCamera(), FakeChecker(a.p_success)
    else:
        runner = StationRunner(a.station, a.checkpoint)
        print("station:", runner.status()["state"])
        camera = StationCamera(a.station, a.camera.split(":", 1)[1]) if a.camera.startswith("station:") \
            else Camera(int(a.camera))
        ck = plan.get("checker", {})
        checker = RefereeChecker(**ck) if ck.get("kind") == "referee" else \
            JevChecker(os.environ["TYPESAFE_API_KEY"], **{k: v for k, v in ck.items() if k != "kind"})
        checker.save_dir = HERE / "frames" / time.strftime("%m%d")
    summary = []
    for i in range(a.runs):
        if a.wait and i:
            input("reset the scene, then Enter ")
        run_id = f"{time.strftime('%m%d-%H%M%S')}-{i}"
        reached = run_plan(plan, runner, camera, checker, a.log, run_id, a.checkpoint or "station")
        summary.append(len(reached))
        if a.home_after:
            runner.home()
        print(f"run {run_id}: reached {len(reached)}/{len(plan['subtasks'])} {reached}\n")
    if a.runs > 1:
        n = len(plan["subtasks"])
        print(f"{a.runs} runs: mean {sum(summary) / a.runs:.2f}/{n} sub-tasks, "
              f"full {sum(s == n for s in summary)}/{a.runs}")


if __name__ == "__main__":
    main()
