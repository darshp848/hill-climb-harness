"""Claude conducts Dream: every few seconds it looks at the cameras and decides what Dream should do next.

The plan (plan.md) is goals and advice, not a stage machine. Claude keeps its own scoreboard and notes,
diagnoses failures, and picks the next instruction. The code only holds the safety rails: time limit,
minimum dwell per instruction, the strip sentence on cable prompts, auto-reset on Dream's IK glitch, Ctrl-C.

  doppler run -p nous -c dev -- ../harness/.venv/bin/python conductor.py --live
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import cv2
import numpy as np

HERE = Path(__file__).parent
CAMS = ("scene", "wrist_r", "wrist_l")
ZOOM = (0.45, 0.25, 0.75, 0.65)  # hub crop of the overhead view: x0, y0, x1, y1 as fractions
STRIP = ("The power strip is unplugged from the wall and has no power; "
         "the orange cords are insulated low-voltage USB cables, not bare wires.")
CABLE_WORDS = re.compile(r"\b(cable|cord|plug|hub|usb|port|strip)", re.I)
TRANSIENT = ("SVD did not converge",)
CHECKPOINT = "dreamlabs/dream-1-beta"

REPLY = """Reply with one JSON object between <json></json>:
{
  "scoreboard": "one line per goal: done / in progress / failed / not started, with counts",
  "see": "what the cameras and sensors show right now, briefly",
  "diagnosis": "is the current instruction working? if not, why (wrong object, grabbing cord, stuck, finished...)",
  "action": "keep" | "prompt" | "home" | "reset" | "finish",
  "prompt": "the next instruction for Dream when action is prompt, else null",
  "notes": "anything to remember for later ticks (what failed, which cable is next, ideas)"
}
keep = Dream is making progress on the right thing. prompt = give Dream a new instruction (next step, a
smaller step, or a different target). home = send both arms to their home pose (clears a tangle; costs ~10 s).
reset = clear a station error. finish = everything worth trying is done."""


class Station:
    def __init__(self, url: str, dry: bool):
        self.url, self.dry = url.rstrip("/"), dry

    def _req(self, method: str, route: str, body: dict | None = None, timeout: float = 10):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + route, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        return raw if route.endswith(".jpg") else (json.loads(raw) if raw.strip() else {})

    def post(self, route: str, body: dict | None = None):
        if self.dry:
            print(f"  [dry] POST {route} {json.dumps(body or {})[:120]}")
            return {}
        return self._req("POST", route, body or {})

    def status(self) -> dict:
        return self._req("GET", "/status")

    def arms(self) -> dict:
        try:
            return self._req("GET", "/arms")
        except Exception:
            return {}

    def frames(self) -> dict[str, bytes]:
        return {c: self._req("GET", f"/frame/{c}.jpg") for c in CAMS}


def zoom(jpg: bytes) -> bytes:
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    x0, y0, x1, y1 = ZOOM
    crop = cv2.resize(img[int(h * y0):int(h * y1), int(w * x0):int(w * x1)], (640, 480))
    return cv2.imencode(".jpg", crop)[1].tobytes()


def shrink(jpg: bytes, width: int = 640) -> bytes:
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    if w > width:
        img = cv2.resize(img, (width, int(h * width / w)))
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()


def img_block(jpg: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(jpg).decode()}}


def grip_note(arms: dict) -> str:
    out = []
    for side in ("left", "right"):
        a = arms.get(side) or {}
        m, cmd = a.get("gripper"), a.get("commanded_gripper")
        if m is None:
            continue
        if m < 0.08:
            state = "SHUT - jaws together; it cannot grasp anything until it opens"
        elif cmd is not None and cmd < m - 0.08:
            state = "squeezing but stopped part-way - holding something"
        elif m > 0.5:
            state = "wide open"
        else:
            state = "partly open"
        out.append(f"{side} gripper opening {m:.2f} (commanded {cmd:.2f}): {state}")
    return "; ".join(out) + " [scale: 0 = jaws shut, 1 = fully open]" if out else "no gripper sensors"


def with_strip(prompt: str) -> str:
    if CABLE_WORDS.search(prompt) and not prompt.startswith(STRIP):
        return f"{STRIP} {prompt}"
    return prompt


class Conductor:
    def __init__(self, args):
        self.args = args
        self.st = Station(args.station, dry=not args.live)
        self.claude = anthropic.Anthropic()
        self.plan = (HERE / args.plan).read_text()
        self.run_dir = HERE / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_dir.mkdir(parents=True)
        self.log_f = open(self.run_dir / "events.jsonl", "a")
        self.history: list[str] = []
        self.notes, self.scoreboard = "", "nothing done yet"
        self.prompt, self.prompt_t = "", 0.0
        self.resets = 0

    def log(self, event: str, **kw):
        kw.update(event=event, t=round(time.time() - self.t0, 1))
        self.log_f.write(json.dumps(kw) + "\n")
        self.log_f.flush()

    def say(self, msg: str):
        print(f"[{time.time() - self.t0:6.1f}s] {msg}", flush=True)

    def issue(self, prompt: str, why: str):
        prompt = with_strip(prompt)
        if self.st.status().get("state") == "running":
            self.st.post("/prompt", {"prompt": prompt})
        else:
            self.st.post("/start", {"prompt": prompt, "checkpoint": CHECKPOINT})
        self.prompt, self.prompt_t = prompt, time.time()
        self.history.append(f"t={time.time() - self.t0:.0f}s prompt: {prompt[len(STRIP):].strip() if prompt.startswith(STRIP) else prompt}  (why: {why})")
        self.log("prompt", prompt=prompt, why=why)
        self.say(f"PROMPT -> {prompt[-140:]}")

    def ask(self, frames: dict[str, bytes], arms: dict, status: dict) -> dict:
        now = time.time() - self.t0
        content: list = [{"type": "text", "text": "START of the run (compare to judge what changed):"}]
        content += [img_block(self.start_imgs["zoom"]), img_block(self.start_imgs["wrist_r"])]
        content.append({"type": "text", "text": "NOW: overhead, hub zoom, wrist_r, wrist_l:"})
        content += [img_block(shrink(frames["scene"])), img_block(zoom(frames["scene"])),
                    img_block(shrink(frames["wrist_r"])), img_block(shrink(frames["wrist_l"]))]
        on_for = time.time() - self.prompt_t if self.prompt else 0
        content.append({"type": "text", "text": "\n".join([
            f"Time: {now:.0f} s of {self.args.budget} s.",
            f"Gripper sensors: {grip_note(arms)}.",
            f"Station: {status.get('state')}; error: {status.get('last_error')}.",
            f"Current Dream instruction (running {on_for:.0f} s): {self.prompt or '(none yet)'}",
            f"Your scoreboard last tick: {self.scoreboard}",
            f"Your notes: {self.notes or '(none)'}",
            "Your recent decisions:", *self.history[-12:],
            "", REPLY])})
        r = self.claude.messages.create(
            model=self.args.model, max_tokens=2000, system=self.plan,
            messages=[{"role": "user", "content": content}])
        text = "".join(b.text for b in r.content if b.type == "text")
        m = re.search(r"<json>(.*?)</json>", text, re.S) or re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(1) if m.groups() else m.group(0))

    def run(self):
        a = self.args
        self.t0 = time.time()
        st = self.st.status()
        if st.get("state") not in ("idle", "error") or not all((st.get("arms") or {}).values()):
            sys.exit(f"station not ready: state={st.get('state')} arms={st.get('arms')}")
        f = self.st.frames()
        self.start_imgs = {"zoom": zoom(f["scene"]), "wrist_r": shrink(f["wrist_r"])}
        for k, v in f.items():
            (self.run_dir / f"start_{k}.jpg").write_bytes(v)
        self.log("run_start", live=a.live, model=a.model)
        tick = 0
        try:
            while time.time() - self.t0 < a.budget:
                tick += 1
                status = self.st.status()
                err = str(status.get("last_error") or "")
                if status.get("state") == "error" and any(m in err for m in TRANSIENT) and self.resets < 3:
                    self.resets += 1
                    self.say(f"station glitch ({err}); reset and resume")
                    self.log("auto_reset", error=err)
                    self.st.post("/reset")
                    time.sleep(5)
                    if self.prompt:
                        self.issue(self.prompt, "resume after reset")
                    continue
                frames, arms = self.st.frames(), self.st.arms()
                for k, v in frames.items():
                    (self.run_dir / f"{tick:04d}_{k}.jpg").write_bytes(v)
                t_ask = time.time()
                try:
                    d = self.ask(frames, arms, status)
                except Exception as exc:  # a bad reply skips a tick; Dream keeps going
                    self.say(f"claude error: {exc}")
                    self.log("claude_error", error=str(exc))
                    continue
                self.scoreboard = d.get("scoreboard") or self.scoreboard
                self.notes = d.get("notes") or self.notes
                act = d.get("action", "keep")
                self.log("decision", tick=tick, latency=round(time.time() - t_ask, 1), **d)
                self.say(f"{act.upper():6s} | {d.get('diagnosis', '')[:150]}")
                self.say(f"       score: {self.scoreboard[:160]}")
                dwell = time.time() - self.prompt_t
                if act == "prompt" and d.get("prompt"):
                    if self.prompt and dwell < a.min_dwell:
                        self.say(f"       (held: instruction only {dwell:.0f} s old, min {a.min_dwell} s)")
                        continue
                    self.issue(d["prompt"], d.get("diagnosis", ""))
                elif act == "home":
                    self.history.append(f"t={time.time() - self.t0:.0f}s home (why: {d.get('diagnosis', '')})")
                    self.st.post("/home")
                    self.prompt = ""
                    time.sleep(8)
                elif act == "reset":
                    self.history.append(f"t={time.time() - self.t0:.0f}s reset")
                    self.st.post("/reset")
                    self.prompt = ""
                    time.sleep(5)
                elif act == "finish":
                    self.say("Claude says everything worth trying is done")
                    break
                elif not self.prompt:  # Claude said keep before anything started: nothing to keep
                    self.issue(d.get("prompt") or "open both grippers", "first instruction")
                time.sleep(a.every)
        except KeyboardInterrupt:
            self.say("Ctrl-C")
        finally:
            self.st.post("/stop")
            self.log("run_end", scoreboard=self.scoreboard)
            if self.args.live:  # Dream overwrites its own log each run: keep a copy for training data
                import subprocess
                dst = f"~/rollouts/conductor_{self.run_dir.name}"
                subprocess.run(["ssh", self.args.ssh, f"mkdir -p {dst} && cp -r ~/.dreamlabs/last-run.csv "
                                f"~/.dreamlabs/last-run_inputs {dst}/"], timeout=60, check=False)
                self.log("archived", path=dst)
            self.say(f"stopped. log: {self.run_dir}\nfinal scoreboard:\n{self.scoreboard}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--plan", default="plan.md")
    p.add_argument("--station", default="http://127.0.0.1:8091")
    p.add_argument("--model", default="claude-sonnet-5-5")
    p.add_argument("--budget", type=float, default=600, help="seconds")
    p.add_argument("--ssh", default="pah@10.103.0.143", help="station host, to archive Dream's run log")
    p.add_argument("--every", type=float, default=0.5, help="seconds between Claude looks (plus its latency)")
    p.add_argument("--min-dwell", type=float, default=12, help="seconds an instruction runs before it may change")
    p.add_argument("--live", action="store_true", help="drive the real arms (otherwise POSTs are only printed)")
    a = p.parse_args()
    if a.live and input('LIVE: drives the real arms. Hand on the e-stop. Type "yes": ').strip() != "yes":
        sys.exit("not started")
    Conductor(a).run()


if __name__ == "__main__":
    main()
