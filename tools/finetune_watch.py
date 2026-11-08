"""Run a Dream fine-tune overnight and watch it: upload, process, queue, train, re-queue on failure, warm the model.

Run it ON THE STATION, inside tmux, in the Dream library's environment, with the hackathon kit at ~/physical-ai-hacks
(its embodiment, its settings: the same job `tools/finetune.py --arm metal` would queue):

    tmux new -A -s finetune
    ~/dream-env/bin/python ~/hill-climb-harness/tools/finetune_watch.py start ~/hackathon_data/ft-final \\
        --name relay-final --suffix relay-final --max-requeues 1 --i-accept-charges
    # after a crash or a reboot, pick up where it was (reads the state file):
    ~/dream-env/bin/python ~/hill-climb-harness/tools/finetune_watch.py resume
    # how is it going (any terminal, any time):
    cat ~/finetune_watch/STATUS.txt ; tail -f ~/finetune_watch/watch.log

What it does, writing every step to ~/finetune_watch/{state.json, watch.log, STATUS.txt}:
1. checks the dataset locally (Dream's validate_dataset, as `finetune.py --check`), registers or reuses the kit's
   embodiment, uploads it, and waits until it is `processed`;
2. queues the job (use_eef for Metal/Maker, the service's defaults: 1000 steps, lr 1e-5, constant schedule, half Dream
   data) and polls it every --poll-s seconds, logging status, queue position, phase, step and loss;
3. when the model is published, records its id in ~/finetune_watch/MODEL.txt and (unless --no-warm) asks it for
   one action with a dummy observation until it answers, so its first start (up to an hour) happens overnight.
   With --keep-warm-until HH:MM it asks again every 20 minutes until then, so it does not go cold before the match.

What it does on trouble (Dream's error codes, docs: reference/module-fine-tuning, reference/dream-server-error):
- network down, timeouts, HTTP 5xx (`retryable`), protocol errors: waits and tries again, forever, with backoff;
- `conflict` "suffix in use": queues under <suffix>-r<N> instead; `conflict` "limit of active jobs": waits;
- `payment_required`, `invalid_request`, `validation`, auth errors, a dataset that fails processing: STOPS and says
  why in STATUS.txt (these need a person: credits, a fix to the data, a key);
- a job that ends `failed` because the training did not finish: queues it again on the same processed dataset, under
  <suffix>-r<N>, at most --max-requeues times (each is a new charge); a job that failed because of the dataset: stops;
- `cancelled`: stops. `succeeded` with a `notice` (stopped short, the newest checkpoint was published): keeps that
  model and does not re-queue (it is charged in proportion; --requeue-short re-queues anyway).
A job is never cancelled by this script (Dream refuses cancels anyway), and it never touches the arms.
Without --i-accept-charges `start` stops before anything is charged.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

HOME = Path.home()
DIR = HOME / "finetune_watch"
KIT = HOME / "physical-ai-hacks"
TERMINAL_JOB = ("failed", "cancelled")
WARM_PROMPT = "unplug the USB cable at the end of the hub nearest the right arm"


# -- errors ---------------------------------------------------------------------------------------------------------
class Stop(Exception):
    """Needs a person: the reason goes to STATUS.txt and the script ends."""


def classify(exc: BaseException) -> str:
    """retry | suffix | busy | stop, from the Dream library's exception (by duck typing, so tests need no library)."""
    name = type(exc).__name__
    if name in ("DreamTimeout", "DreamProtocolError", "ConnectionError", "TimeoutError", "OSError"):
        return "retry"
    if name in ("DreamAuthError", "DreamConfigError"):
        return "stop"
    code = str(getattr(exc, "code", "") or "")
    msg = str(exc).lower()
    if getattr(exc, "retryable", False):
        return "retry"
    if code == "payment_required" or "payment" in msg or "credit" in msg:
        return "stop"
    if code == "conflict":
        if "limit" in msg or "active" in msg or "queued or running" in msg:
            return "busy"          # the account's limit of jobs at once: wait
        if "processed" in msg:
            return "busy"          # the dataset is not processed yet: wait
        if any(w in msg for w in ("suffix", "in use", "already", "exists", "taken", "name")):
            return "suffix"        # a suffix or dataset name used before: rename
        return "stop"
    if code in ("invalid_request", "validation", "not_found", "forbidden"):
        return "stop"
    if name == "DreamServerError":
        return "stop"
    return "retry"


def dataset_fault(error: str | None) -> bool:
    """Dream's `error` on a failed job: 'what to fix in your dataset when the cause was in it, else that the training
    did not finish'."""
    e = (error or "").lower()
    if any(w in e for w in ("did not finish", "training failed", "internal", "infrastructure", "preempt", "gpu",
                            "timed out", "timeout", "out of memory", "oom", "node")):
        return False
    return any(w in e for w in ("dataset", "episode", "video", "frame", "camera", "embodiment", "parquet", "fix"))


# -- the service, behind a small adapter (tests swap in a fake) --------------------------------------------------------
class DreamService:
    def __init__(self, arm: str) -> None:
        import dream  # noqa: PLC0415 (only on the station)

        self.dream = dream
        spec = importlib.util.spec_from_file_location("kit_finetune", KIT / "tools" / "finetune.py")
        kit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(kit)              # the kit's own embodiment files, names and use_eef choice
        file_name, self.default_embodiment, self.use_eef = kit.ARMS[arm]
        self.config = json.loads((kit.EMBODIMENTS / file_name).read_text())
        self.kit = kit
        self.client = dream.DreamClient()

    def validate(self, path: str) -> str:
        report = self.dream.datasets.validate_dataset(Path(path), self.config)
        if not report.ok:
            raise Stop(f"the dataset needs fixing before it uploads: {report.error}")
        return f"{report.episodes} episodes, {report.frames} frames at {report.fps} fps"

    def upload(self, path: str, name: str) -> tuple[str, str]:
        try:
            embodiment = self.kit.embodiment_for(self.client, self.default_embodiment, self.config)
        except SystemExit as exc:           # the kit exits when the name is registered with another config
            raise Stop(str(exc)) from None
        ds = self.client.upload_dataset(path, name, embodiment)
        return ds.id, ds.name

    def dataset(self, dataset_id: str) -> tuple[str, str, str | None]:
        ds = self.client.get_dataset_status(dataset_id)
        return ds.status, ds.name, getattr(ds, "error", None)

    def create_job(self, dataset_name: str, suffix: str) -> dict:
        return self.client.finetune(dataset_name, suffix, use_eef=self.use_eef)

    def job(self, job_id: str) -> dict:
        return self.client.get_finetuning_status(job_id)

    def jobs(self) -> list[dict]:
        from dream import fine_tuning  # noqa: PLC0415

        return fine_tuning.list()

    def warm(self, model: str) -> str:
        """One get_action() on the model with a dummy observation; raises pool_warming while it starts."""
        import numpy as np  # noqa: PLC0415

        client = self.dream.DreamClient()
        try:
            client.policy({"checkpoint": model})
            black = base64.b64encode(_black_jpeg()).decode("ascii")
            frames = {f"video.{role}": black for role in self.config["video"]}
            obs = {f"state.{p}": [0.0] * (b - a) for p, (a, b) in self.config["state"].items()}
            for side in ("left", "right"):          # a valid hand pose: identity rotation, a point in front
                if f"state.{side}_eef_rot6" in obs:
                    obs[f"state.{side}_eef_rot6"] = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
                    obs[f"state.{side}_eef_xyz"] = [0.25, 0.0, 0.15]
                if f"state.{side}_grip" in obs:
                    obs[f"state.{side}_grip"] = [1.0]
            obs["prompt"] = WARM_PROMPT
            for _ in range(3):
                client.set_observation(frames, obs)
            out = client.get_action()
            rows = len(next(iter(out["per_key"].values()))) if out.get("per_key") else 0
            return f"answered ({rows} rows)"
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def _black_jpeg() -> bytes:
    """A 320x176 black JPEG, without OpenCV or PIL (a fixed minimal baseline JPEG would do; build it with PIL when
    present, else a tiny bundled one)."""
    try:
        import io  # noqa: PLC0415

        from PIL import Image  # noqa: PLC0415

        buf = io.BytesIO()
        Image.new("RGB", (320, 176)).save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415

        ok, jpg = cv2.imencode(".jpg", np.zeros((176, 320, 3), np.uint8))
        return jpg.tobytes()


# -- the watcher ----------------------------------------------------------------------------------------------------
class Watcher:
    def __init__(self, svc: Any, state_dir: Path = DIR, *, poll_s: float = 300, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], dt.datetime] = dt.datetime.now, out=print) -> None:
        self.svc, self.dir, self.poll_s, self.sleep, self.now, self.out = svc, state_dir, poll_s, sleep, now, out
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.state: dict[str, Any] = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    # bookkeeping
    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.state_path)

    def log(self, msg: str) -> None:
        line = f"{self.now():%Y-%m-%d %H:%M:%S} {msg}"
        self.out(line)
        with open(self.dir / "watch.log", "a") as f:
            f.write(line + "\n")

    def status(self, msg: str) -> None:
        (self.dir / "STATUS.txt").write_text(f"{self.now():%Y-%m-%d %H:%M:%S} {msg}\n")
        self.state["status"] = msg
        self.save()
        self.log(f"STATUS: {msg}")

    def call(self, what: str, fn: Callable[[], Any], *, on_suffix: Callable[[], Any] | None = None) -> Any:
        """fn() with Dream's error handling: retry (backoff, forever), suffix (rename and retry), busy (wait), stop."""
        delay = 30.0
        while True:
            try:
                return fn()
            except Stop:
                raise
            except Exception as exc:  # noqa: BLE001
                kind = classify(exc)
                self.log(f"{what}: {type(exc).__name__} {getattr(exc, 'code', '')}: {exc} -> {kind}")
                if kind == "stop":
                    raise Stop(f"{what} refused: {type(exc).__name__} {getattr(exc, 'code', '')}: {exc}") from exc
                if kind == "suffix":
                    if on_suffix is None:
                        raise Stop(f"{what} refused, name in use: {exc}") from exc
                    on_suffix()
                    continue
                wait = float(getattr(exc, "retry_after_s", None) or delay)
                self.sleep(wait if kind == "retry" else max(wait, 600))
                delay = min(delay * 2, 900)

    def next_suffix(self) -> str:
        n = self.state.get("suffix_n", 1) + 1
        self.state["suffix_n"] = n
        self.state["suffix"] = f"{self.state['base_suffix']}-r{n}"
        self.save()
        self.log(f"suffix now {self.state['suffix']}")
        return self.state["suffix"]

    # phases
    def start(self, dataset: str, name: str, suffix: str, max_requeues: int, requeue_short: bool,
              keep_warm_until: str | None, warm: bool) -> None:
        if self.state.get("job_id") or self.state.get("dataset_id"):
            raise Stop(f"{self.state_path} already has a run (dataset {self.state.get('dataset_id')}, job "
                       f"{self.state.get('job_id')}): use `resume`, or move the folder away to start over")
        self.state = {"dataset_path": dataset, "name": name, "base_name": name, "base_suffix": suffix, "suffix": suffix, "suffix_n": 1,
                      "max_requeues": max_requeues, "requeues": 0, "requeue_short": requeue_short,
                      "keep_warm_until": keep_warm_until, "warm": warm, "jobs": [], "started": f"{self.now():%Y-%m-%d %H:%M}"}
        self.save()
        self.run()

    def run(self) -> None:
        s = self.state
        try:
            if not s.get("dataset_id"):
                self.status("checking the dataset locally")
                self.log("validate: " + self.svc.validate(s["dataset_path"]))
                self.status("uploading the dataset (minutes)")
                def rename() -> None:              # a dataset name already used on the account
                    s["name_n"] = s.get("name_n", 1) + 1
                    s["name"] = f"{s['base_name']}-r{s['name_n']}"
                    self.log(f"dataset name in use: uploading as {s['name']}")
                ds_id, ds_name = self.call("upload", lambda: self.svc.upload(s["dataset_path"], s["name"]),
                                           on_suffix=rename)
                s["dataset_id"], s["dataset_name"] = ds_id, ds_name
                self.save()
                self.log(f"uploaded as {ds_id} ({ds_name})")
            self.wait_dataset()
            while True:
                if not s.get("job_id"):
                    self.queue()
                outcome = self.wait_job()
                if outcome == "done":
                    break
                # outcome == "requeue"
                s["requeues"] += 1
                s["job_id"] = None
                self.next_suffix()
                self.save()
            self.after_model()
        except Stop as stop:
            self.status(f"STOPPED, needs a person: {stop}")
            raise

    def wait_dataset(self) -> None:
        s = self.state
        while True:
            status, name, error = self.call("dataset status", lambda: self.svc.dataset(s["dataset_id"]))
            s["dataset_name"] = name
            if status == "processed":
                self.log(f"dataset {s['dataset_id']} processed")
                self.save()
                return
            if status == "failed":
                raise Stop(f"dataset {s['dataset_id']} failed processing: {error}")
            self.status(f"dataset {s['dataset_id']}: {status}")
            self.sleep(60)

    def queue(self) -> None:
        s = self.state
        # a job may have been queued before a crash: find it by its suffix before queueing another (another charge)
        for j in self.call("list jobs", self.svc.jobs) or []:
            if j.get("suffix") == s["suffix"] and j.get("status") not in TERMINAL_JOB:
                s["job_id"] = j["id"]
                self.save()
                self.log(f"found job {j['id']} already queued under {s['suffix']}: following it")
                return
        self.status(f"queueing the job under suffix {s['suffix']} (this is the charge)")
        job = self.call("queue job", lambda: self.svc.create_job(s["dataset_name"], s["suffix"]),
                        on_suffix=self.next_suffix)
        s["job_id"] = job["id"]
        s["jobs"].append({"id": job["id"], "suffix": s["suffix"], "queued": f"{self.now():%H:%M}"})
        self.save()
        self.log(f"queued {job['id']} position {job.get('queue_position')} hyperparameters {job.get('hyperparameters')}")

    def wait_job(self) -> str:
        s = self.state
        last = None
        while True:
            job = self.call("job status", lambda: self.svc.job(s["job_id"]))
            st, model = job.get("status"), job.get("fine_tuned_model")
            p = job.get("progress") or {}
            line = (f"job {job['id']} {st} queue {job.get('queue_position')} phase {p.get('phase')} "
                    f"step {p.get('step')}/{p.get('total_steps')} loss {p.get('loss')}")
            if line != last:
                self.log(line)
                last = line
            if model:
                s["model"] = model
                s["notice"] = job.get("notice")
                (self.dir / "MODEL.txt").write_text(model + "\n")
                if job.get("notice"):
                    self.log(f"NOTICE (stopped short; newest checkpoint published): {job['notice']}")
                    if s.get("requeue_short") and s["requeues"] < s["max_requeues"]:
                        self.status(f"model {model} stopped short; re-queueing as asked (--requeue-short)")
                        return "requeue"
                self.status(f"MODEL READY: {model}" + (f" (stopped short: {job['notice']})" if job.get("notice") else ""))
                self.save()
                return "done"
            if st == "cancelled":
                raise Stop(f"job {job['id']} was cancelled")
            if st == "failed":
                err = job.get("error")
                self.log(f"job {job['id']} FAILED: {err}")
                if dataset_fault(err):
                    raise Stop(f"job {job['id']} failed because of the dataset: {err}")
                if s["requeues"] >= s["max_requeues"]:
                    raise Stop(f"job {job['id']} failed ({err}) and the re-queue budget "
                               f"({s['max_requeues']}) is used up")
                self.status(f"job {job['id']} failed ({err}); re-queueing ({s['requeues'] + 1}/{s['max_requeues']})")
                return "requeue"
            phase = p.get("phase") or (f"queue position {job.get('queue_position')}" if st == "queued" else "")
            self.status(f"training: {st} {phase} step {p.get('step')}/{p.get('total_steps')} loss {p.get('loss')}"
                        if st == "running" else f"job {job['id']}: {st} {phase}")
            self.sleep(self.poll_s)

    def after_model(self) -> None:
        s = self.state
        if not s.get("warm", True):
            return
        model = s["model"]
        self.status(f"MODEL READY: {model}; warming it up (its first start can take an hour)")
        self.warm_once(model)
        until = s.get("keep_warm_until")
        if until:
            hh, mm = (int(x) for x in until.split(":"))
            while True:
                now = self.now()
                stop_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if stop_at <= now - dt.timedelta(hours=12):
                    stop_at += dt.timedelta(days=1)
                if now >= stop_at:
                    break
                self.sleep(1200)
                self.warm_once(model, quiet=True)
        self.status(f"MODEL READY and warm: {model}")

    def warm_once(self, model: str, quiet: bool = False) -> None:
        while True:
            try:
                r = self.svc.warm(model)
                if not quiet:
                    self.log(f"warm-up: the model {r}")
                self.state["warmed"] = f"{self.now():%H:%M}"
                self.save()
                return
            except Exception as exc:  # noqa: BLE001
                reason = getattr(exc, "reason", "") or ""
                if reason == "pool_warming" or classify(exc) == "retry":
                    self.status(f"MODEL READY: {model}; starting up ({reason or type(exc).__name__})")
                    self.sleep(float(getattr(exc, "retry_after_s", None) or 60))
                    continue
                self.log(f"warm-up refused ({type(exc).__name__} {getattr(exc, 'code', '')} {reason}: {exc}); "
                         f"the model is ready anyway, its first real call will start it")
                return


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("start", help="upload, queue and watch (charges credits)")
    st.add_argument("dataset")
    st.add_argument("--name", required=True, help="dataset name on the service (lowercase, digits, hyphens)")
    st.add_argument("--suffix", required=True, help="model id suffix (lowercase, digits, hyphens)")
    st.add_argument("--arm", default="metal", choices=("metal", "maker", "yam"))
    st.add_argument("--max-requeues", type=int, default=1, help="re-queues allowed after a failed training (each costs)")
    st.add_argument("--requeue-short", action="store_true", help="re-queue a job that stopped short too")
    st.add_argument("--keep-warm-until", metavar="HH:MM", help="keep the model warm until this local time")
    st.add_argument("--no-warm", action="store_true")
    st.add_argument("--poll-s", type=float, default=300)
    st.add_argument("--i-accept-charges", action="store_true", help="required: queueing a job charges the account")
    rs = sub.add_parser("resume", help="continue from ~/finetune_watch/state.json")
    rs.add_argument("--arm", default="metal", choices=("metal", "maker", "yam"))
    rs.add_argument("--poll-s", type=float, default=300)
    rs.add_argument("--max-requeues", type=int, help="change the re-queue budget")
    sub.add_parser("status", help="print the state")
    args = ap.parse_args(argv)
    if args.cmd == "status":
        p = DIR / "state.json"
        print(p.read_text() if p.exists() else "no run yet")
        return 0
    if args.cmd == "start" and not args.i_accept_charges:
        print("refusing: queueing a fine-tune charges the account (~$200 for 1000 steps, more per re-queue). "
              "Add --i-accept-charges once the team has agreed.", file=sys.stderr)
        return 2
    try:
        w = Watcher(DreamService(args.arm), poll_s=args.poll_s)
        if args.cmd == "start":
            w.start(os.path.expanduser(args.dataset), args.name, args.suffix, args.max_requeues, args.requeue_short,
                    args.keep_warm_until, not args.no_warm)
        else:
            if not w.state:
                print("no run to resume", file=sys.stderr)
                return 2
            if args.max_requeues is not None:
                w.state["max_requeues"] = args.max_requeues
            w.run()
    except Stop as stop:
        print(f"STOPPED: {stop}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted: the job (if queued) keeps training on the service; `resume` picks it up", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
