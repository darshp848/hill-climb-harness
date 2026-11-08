"""A local control panel for the harness: start a run (base Dream-1 or the fine-tune, which stages), watch the cameras,
every prompt sent to Dream and why, everything the harness itself did (grip assist, locks, retries, guard), the
monitor's looks, and type to it: override Dream's prompt, skip the stage, talk to the operator assistant, stop.

    doppler run -p nous -c dev -- .venv/bin/python -m harness.panel        # then open http://127.0.0.1:8099

Practice only: a run started here takes typed input (a DQ in a match), unless "match mode" is ticked. The panel runs
the normal CLI (`python -m harness ... --live`) as a child process and answers its "yes" for you, so the CLI's own
checks (live guard required, preflight) all apply. Binds 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PLANS = {"base": "plans/bot_v1.yaml", "ft": "plans/bot_v1_ft.yaml"}
GROUPS = {"cable": ["cable_", "arms_"], "stack": ["stack", "tower"], "ball": ["ball_"]}


class Run:
    """The one harness child process, its stdin (for yes / s / operator lines) and its output."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.cmd: list[str] = []
        self.run_dir: Path | None = None
        self.out: deque[str] = deque(maxlen=400)
        self.lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, model: str, stages: list[str], match: bool, monitor_model: str, station: str) -> str | None:
        with self.lock:
            if self.running():
                return "a run is already going: stop it first"
            plan = PLANS.get(model)
            if plan is None:
                return f"unknown model {model!r}"
            cmd = [sys.executable, "-m", "harness", plan, "--live", "--station", station,
                   "--monitor-model", monitor_model, "--detect", "plans/table_colors.yaml", "--live-guard"]
            for group, prefixes in GROUPS.items():
                if group not in stages:
                    for p in prefixes:
                        cmd += ["--skip", p]
            if match:
                cmd.append("--no-operator")
            self.cmd, self.run_dir = cmd, None
            self.out.clear()
            self.proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, bufsize=1)
            threading.Thread(target=self._pump, daemon=True).start()
            self.send("yes")   # the person clicked Start (with a confirm dialog) at the bot
            return None

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            self.out.append(line)
            if line.startswith("logging to "):
                self.run_dir = ROOT / line[len("logging to "):].strip()

    def send(self, line: str) -> str | None:
        if not self.running() or self.proc is None or self.proc.stdin is None:
            return "no run is going"
        try:
            self.proc.stdin.write(line.replace("\n", " ") + "\n")
            self.proc.stdin.flush()
        except OSError as exc:
            return str(exc)
        return None


def latest_run_dir() -> Path | None:
    runs = sorted((ROOT / "runs").glob("2*"), reverse=True)
    return next((r for r in runs if (r / "events.jsonl").exists()), None)


def read_events(run_dir: Path | None, since: int) -> tuple[list[dict[str, Any]], int]:
    if run_dir is None or not (run_dir / "events.jsonl").exists():
        return [], since
    lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[since:]:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "dry_run" or (e.get("event") == "verdict" and e.get("status") is None):
            continue   # "monitor call in flight" placeholders
        e.pop("frames", None)
        if e.get("event") == "preflight":
            e.pop("status", None)
        out.append(e)
    return out, len(lines)


def get(url: str, timeout: float = 2.0) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except Exception:  # noqa: BLE001 - the panel shows "station unreachable"
        return None


def make_handler(run: Run, station: str, monitor_model: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode())

        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            if path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if path == "/api/state":
                raw = get(station + "/status")
                st = json.loads(raw) if raw else None
                arms = get(station + "/arms")
                rd = run.run_dir or latest_run_dir()
                return self._json({
                    "running": run.running(), "cmd": " ".join(run.cmd[2:]) if run.cmd else "",
                    "run_dir": rd.name if rd else None, "output": list(run.out)[-40:],
                    "station": None if st is None else {
                        k: st.get(k) for k in ("state", "prompt", "checkpoint", "chunks", "clamps", "run_elapsed_s",
                                               "action_space")} | {"timing": st.get("timing", {}),
                                                                   "last_error": st.get("last_error")},
                    "arms": json.loads(arms) if arms else None, "monitor_model": monitor_model})
            if path == "/api/events":
                rd = run.run_dir or latest_run_dir()
                if q.get("run") and rd is not None and q["run"] != rd.name:
                    return self._json({"run": rd.name, "events": [], "next": 0, "reset": True})
                events, nxt = read_events(rd, int(q.get("since", "0") or 0))
                return self._json({"run": rd.name if rd else None, "events": events, "next": nxt})
            if path.startswith("/api/frame/"):
                cam = path.rsplit("/", 1)[-1].removesuffix(".jpg")
                if not cam.replace("_", "").isalnum():
                    return self._send(400, b"{}")
                jpeg = get(f"{station}/frame/{cam}.jpg?w=480")
                return self._send(200, jpeg, "image/jpeg") if jpeg else self._send(404, b"{}")
            self._send(404, b"{}")

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json({"error": "bad JSON"}, 400)
            text = str(body.get("text", "")).strip()
            if self.path == "/api/start":
                err = run.start(str(body.get("model", "base")), list(body.get("stages") or []),
                                bool(body.get("match")), monitor_model, station)
            elif self.path == "/api/stop":
                err = run.send("s")
                if err is None:
                    threading.Timer(8.0, lambda: run.running() and run.proc.terminate()).start()
            elif self.path == "/api/prompt":
                err = run.send(f"!prompt {text}") if text else "empty prompt"
            elif self.path == "/api/skip":
                err = run.send("!skip")
            elif self.path == "/api/say":
                err = run.send(text) if text and text.lower() not in ("y", "n", "s", "yes") else "type a sentence"
            else:
                return self._json({"error": "no such route"}, 404)
            self._json({"ok": err is None, "error": err}, 200 if err is None else 409)

    return Handler


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Harness Panel</title>
<style>
:root{--bg:#f6f5f2;--panel:#fff;--ink:#1d1d1b;--muted:#6b6a65;--line:#e3e1db;--dream:#2f6fd6;--harness:#b0620a;
--guard:#c7302b;--op:#7a3fc2;--ok:#2c8a4b;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--panel:#1d1d1b;--ink:#ecebe6;--muted:#9a988f;--line:#33322e;
--dream:#6ea2ff;--harness:#f0a24a;--guard:#ff6b63;--op:#b78cff;--ok:#5ccf83}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.4 system-ui,-apple-system,sans-serif}
header{display:flex;gap:16px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
header h1{font-size:16px;margin:0}.pill{padding:2px 8px;border-radius:99px;border:1px solid var(--line);font-size:12px}
.grid{display:grid;grid-template-columns:320px 1fr 460px;gap:12px;padding:12px;height:calc(100vh - 50px)}
@media (max-width:1100px){.grid{grid-template-columns:1fr;height:auto}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;overflow:auto}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 8px}
label{display:block;margin:4px 0}button{font:inherit;padding:6px 12px;border-radius:8px;border:1px solid var(--line);
background:var(--panel);color:var(--ink);cursor:pointer}button.primary{background:var(--dream);color:#fff;border-color:var(--dream)}
button.danger{background:var(--guard);color:#fff;border-color:var(--guard)}textarea,input[type=text]{width:100%;font:inherit;
padding:6px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
.row{display:flex;gap:8px;margin:8px 0;flex-wrap:wrap}.cams{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.cams img{width:100%;border-radius:8px;background:#000;aspect-ratio:16/9;object-fit:cover}.cams div{font-size:12px;color:var(--muted)}
.now{font-size:16px;padding:8px 10px;border-left:4px solid var(--dream);background:var(--bg);border-radius:6px;margin:6px 0}
.ev{padding:6px 8px;border-bottom:1px solid var(--line);display:grid;grid-template-columns:54px 1fr;gap:8px}
.ev .t{font-family:var(--mono);color:var(--muted);font-size:12px}.tag{font-size:11px;font-weight:600;margin-right:6px}
.dream .tag{color:var(--dream)}.harness .tag{color:var(--harness)}.guard .tag{color:var(--guard)}.op .tag{color:var(--op)}
.stage{background:var(--bg)}.stage .tag{color:var(--ok)}.look .tag{color:var(--muted)}.look{color:var(--muted)}
.why{color:var(--muted);font-size:12px}pre{font-family:var(--mono);font-size:11px;white-space:pre-wrap;margin:0;color:var(--muted)}
.kv{font-family:var(--mono);font-size:12px}
</style></head><body>
<header><h1>Harness panel</h1><span id="station" class="pill">station …</span><span id="model" class="pill"></span>
<span id="timing" class="pill"></span><span id="grip" class="pill"></span><span id="runp" class="pill"></span></header>
<div class="grid">
<div class="card">
<h2>Run</h2>
<label><input type="radio" name="model" value="base" checked> Base Dream-1 (plans/bot_v1.yaml)</label>
<label><input type="radio" name="model" value="ft"> Fine-tune relay-final (plans/bot_v1_ft.yaml, trained texts only)</label>
<div class="row"><label><input type="checkbox" class="stg" value="cable" checked> Cables</label>
<label><input type="checkbox" class="stg" value="stack" checked> Stack</label>
<label><input type="checkbox" class="stg" value="ball" checked> Balls</label></div>
<label><input type="checkbox" id="match"> Match mode (no typed input: overrides below are off)</label>
<div class="row"><button class="primary" onclick="start()">Start</button><button class="danger" onclick="post('/api/stop')">Stop</button></div>
<h2 style="margin-top:16px">Override Dream's prompt</h2>
<textarea id="prompt" rows="3" placeholder="sent to Dream exactly as typed, right now"></textarea>
<div class="row"><button onclick="sendPrompt()">Send to Dream</button><button onclick="post('/api/skip')">Skip this stage</button></div>
<h2 style="margin-top:16px">Talk to the harness</h2>
<input type="text" id="say" placeholder="e.g. skip the balls / why did it grab the pink one?">
<div class="row"><button onclick="say()">Send</button></div>
<h2 style="margin-top:16px">Harness output</h2><pre id="out"></pre>
</div>
<div class="card">
<h2>Cameras</h2>
<div class="cams"><div><img id="c_scene"><div>scene</div></div><div><img id="c_wrist_l"><div>wrist_l</div></div><div><img id="c_wrist_r"><div>wrist_r</div></div></div>
<h2 style="margin-top:12px">Dream is running</h2><div class="now" id="now">—</div>
<h2 style="margin-top:12px">Latest monitor look</h2><div id="look" class="kv">—</div>
<h2 style="margin-top:12px">Stages</h2><div id="stages" class="kv"></div>
</div>
<div class="card"><h2>Timeline <label style="display:inline;font-weight:400;text-transform:none">
<input type="checkbox" id="showlooks"> monitor looks</label> <span id="rundir" class="why"></span></h2><div id="tl"></div></div>
</div>
<script>
const HARNESS=new Set(["grip_assist","grip_regrip","grip_pull","grip_slipped","grip_too_low","grip_put_down","grip_assist_error",
"stage_locked","recovery","retry","prompt_rewrite","prompt_written","auto_reset","reset","reset_skipped","pull_pass_check",
"confirm_check","regression","attempt_end","stop","scene_check","tower_prompt","station_restart","timeout_extended","entry_check","precondition"]);
const GUARD=new Set(["guard_yes","guard_tripped","guard_blind","live_guard_armed","live_guard_disarmed"]);
const OP=new Set(["operator_input","operator_reply","operator_action"]);
let run=null,next=0,t0=null,stages={};
const $=id=>document.getElementById(id);
function esc(s){return String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}
async function post(url,body){const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})});
const j=await r.json();if(!j.ok)alert(j.error||"failed");return j}
function start(){const model=document.querySelector("input[name=model]:checked").value;
const st=[...document.querySelectorAll(".stg:checked")].map(x=>x.value);
if(!confirm(`Start a LIVE run on ${model==="ft"?"the fine-tune":"base Dream-1"} (${st.join(", ")})?\nSomeone must be at the e-stop.`))return;
run=null;next=0;$("tl").innerHTML="";stages={};post("/api/start",{model,stages:st,match:$("match").checked})}
function sendPrompt(){const t=$("prompt").value.trim();if(t)post("/api/prompt",{text:t}).then(j=>{if(j.ok)$("prompt").value=""})}
function say(){const t=$("say").value.trim();if(t)post("/api/say",{text:t}).then(j=>{if(j.ok)$("say").value=""})}
$("say").addEventListener("keydown",e=>{if(e.key==="Enter")say()});
function line(e){const ev=e.event;let cls="harness",tag=ev,txt="",why="";
 if(ev==="prompt_change"){cls="dream";tag="→ DREAM";txt=e.prompt;why=`${e.how}: ${e.why}`}
 else if(ev==="start"||ev==="set_prompt")return null;
 else if(ev==="stage_start"){cls="stage";tag="STAGE";txt=e.stage;stages[e.stage]="running"}
 else if(ev==="attempt_end"){tag=e.ok?"PASS":"attempt ended";cls=e.ok?"stage":"harness";txt=`${e.stage} #${e.attempt}`;why=e.reason;
  if(e.ok)stages[e.stage]="passed"}
 else if(ev==="stage_skipped"){cls="stage";tag="SKIPPED";txt=e.stage;why=e.reason;stages[e.stage]="skipped"}
 else if(ev==="verdict"){cls="look";tag=`look ${e.done?"DONE":e.status||""}`;txt=`p=${e.progress??"?"} ${e.reason||""}`;if(!$("showlooks").checked)return "skip"}
 else if(GUARD.has(ev)){cls="guard";txt=e.reason||e.stage||"";}
 else if(OP.has(ev)){cls="op";txt=e.text||e.note||e.action||""}
 else if(ev==="run_end"){cls="stage";tag="RUN END";txt=e.reason}
 else if(ev==="run_start"){cls="stage";tag="RUN START";txt=(e.stages||[]).join(", ")}
 else{const k={...e};delete k.event;delete k.t;delete k.stage;delete k.attempt;txt=Object.entries(k).map(([a,b])=>`${a}=${typeof b==="object"?JSON.stringify(b):b}`).join("  ").slice(0,300)}
 if(!HARNESS.has(ev)&&cls==="harness"&&!["stage_locked"].includes(ev)&&ev!=="attempt_end")cls="harness";
 return {cls,tag,txt,why}}
async function poll(){
 try{const s=await (await fetch("/api/state")).json();const st=s.station;
  $("station").textContent=st?`station ${st.state}`:"station unreachable";
  $("model").textContent=st?(st.checkpoint||"no model yet")+" · "+(st.action_space||""):"";
  const t=st&&st.timing||{};$("timing").textContent=t.p50_ms?`Dream round trip p50 ${Math.round(t.p50_ms)} ms · p95 ${Math.round(t.p95_ms)} ms`:"";
  const a=s.arms;$("grip").textContent=a?`grip R ${a.right?.gripper?.toFixed(2)} · L ${a.left?.gripper?.toFixed(2)}`:"";
  $("runp").textContent=s.running?"harness RUNNING":"harness idle";$("now").textContent=st&&st.prompt||"—";
  $("out").textContent=(s.output||[]).slice(-14).join("\n");$("rundir").textContent=s.run_dir||"";
 }catch(e){$("station").textContent="panel error"}
 try{const r=await (await fetch(`/api/events?since=${next}&run=${run||""}`)).json();
  if(r.reset||r.run!==run){run=r.run;next=0;$("tl").innerHTML="";stages={};if(r.reset)return}
  next=r.next;for(const e of r.events){if(t0===null||e.event==="run_start")t0=e.t;
   if(e.event==="verdict")$("look").innerHTML=`${esc(e.stage)} · ${e.done?"DONE":esc(e.status)} · p=${e.progress??"?"}<br>${esc(e.reason)}`;
   const l=line(e);if(!l||l==="skip")continue;const d=document.createElement("div");d.className="ev "+l.cls;
   d.innerHTML=`<div class="t">${(e.t-t0).toFixed(1)}s</div><div><span class="tag">${esc(l.tag)}</span>${esc(l.txt)}${l.why?`<div class="why">${esc(l.why)}</div>`:""}</div>`;
   $("tl").prepend(d)}
  $("stages").innerHTML=Object.entries(stages).map(([k,v])=>`${esc(k)}: ${esc(v)}`).join("<br>")||"—";
 }catch(e){}}
function cams(){for(const c of ["scene","wrist_l","wrist_r"]){const i=new Image();i.onload=()=>{$("c_"+c).src=i.src};i.src=`/api/frame/${c}.jpg?${Date.now()}`}}
setInterval(poll,1000);setInterval(cams,700);poll();cams();
</script></body></html>
"""


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m harness.panel")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--station", default="http://127.0.0.1:8091")
    ap.add_argument("--monitor-model", default="google/gemini-3.1-flash-lite")
    a = ap.parse_args(argv)
    run = Run()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(run, a.station.rstrip("/"), a.monitor_model))
    print(f"harness panel on http://127.0.0.1:{a.port}  (station {a.station})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        if run.running():
            run.send("s")
            time.sleep(3)


if __name__ == "__main__":
    main()
