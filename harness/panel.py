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
:root{--bg:#f4f3ef;--card:#fff;--ink:#1b1b19;--mute:#6d6b64;--line:#e2e0d9;--blue:#2563c9;--amber:#b45d00;--red:#c42b25;
--green:#24854a;--violet:#7339bd;--soft:#faf9f6}
@media (prefers-color-scheme:dark){:root{--bg:#121211;--card:#1c1c1a;--ink:#edece7;--mute:#9b998f;--line:#34332f;
--blue:#6fa3ff;--amber:#f5a54a;--red:#ff6a62;--green:#5fd28a;--violet:#b98dff;--soft:#232321}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,sans-serif}
.wrap{max-width:1500px;margin:0 auto;padding:16px}
.banner{display:flex;align-items:center;gap:16px;flex-wrap:wrap;background:var(--card);border:1px solid var(--line);
border-radius:14px;padding:14px 18px}
.state{font-size:22px;font-weight:700}.state.run{color:var(--green)}.state.idle{color:var(--mute)}.state.err{color:var(--red)}
.steps{display:flex;gap:6px;flex-wrap:wrap}.step{padding:4px 10px;border-radius:99px;border:1px solid var(--line);font-size:13px;color:var(--mute)}
.step.on{border-color:var(--blue);color:var(--blue);font-weight:600}.step.ok{border-color:var(--green);color:var(--green)}
.step.no{text-decoration:line-through}.meta{margin-left:auto;font-size:13px;color:var(--mute);text-align:right}
.main{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr);gap:16px;margin-top:16px}
@media (max-width:1000px){.main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
h2{font-size:12px;letter-spacing:.07em;text-transform:uppercase;color:var(--mute);margin:0 0 10px}
.cams{display:grid;grid-template-columns:2fr 1fr;gap:8px}.cams .side{display:grid;gap:8px}
.cams img{width:100%;display:block;border-radius:10px;background:#000;aspect-ratio:16/9;object-fit:cover}
.cap{font-size:12px;color:var(--mute);margin-top:2px}
.now{margin-top:16px}.bubble{border-left:4px solid var(--blue);background:var(--soft);padding:10px 14px;border-radius:8px;font-size:17px}
.who{font-size:12px;color:var(--mute);margin-top:4px}.think{border-left-color:var(--amber);font-size:15px}
.start{display:grid;grid-template-columns:1fr 1fr;gap:8px}.choice{border:2px solid var(--line);border-radius:12px;padding:12px;cursor:pointer;background:var(--card);
text-align:left;font:inherit;color:var(--ink)}.choice b{display:block;font-size:16px}.choice span{font-size:12px;color:var(--mute)}
.choice.sel{border-color:var(--blue);box-shadow:0 0 0 3px color-mix(in srgb,var(--blue) 18%,transparent)}
.toggles{display:flex;gap:16px;margin:12px 0;flex-wrap:wrap}.toggles label{display:flex;gap:6px;align-items:center}
.big{width:100%;padding:12px;font:600 16px system-ui;border-radius:12px;border:0;cursor:pointer;color:#fff}
.go{background:var(--green)}.stop{background:var(--red)}
.say{display:flex;gap:8px;margin-top:10px}.say input{flex:1;font:inherit;padding:10px 12px;border-radius:10px;border:1px solid var(--line);
background:var(--soft);color:var(--ink)}.say button{font:inherit;padding:10px 14px;border-radius:10px;border:1px solid var(--line);
background:var(--card);color:var(--ink);cursor:pointer}.hint{font-size:12px;color:var(--mute);margin-top:4px}
.tl{max-height:620px;overflow:auto}.ev{display:grid;grid-template-columns:52px 22px 1fr;gap:6px;padding:8px 2px;border-bottom:1px solid var(--line)}
.ev .t{font:12px ui-monospace,Menlo,monospace;color:var(--mute);padding-top:2px}.ev .i{font-size:15px}
.ev .d{font-size:12px;color:var(--mute);margin-top:2px}.dream b{color:var(--blue)}.harness b{color:var(--amber)}.guard b{color:var(--red)}
.you b{color:var(--violet)}.good b{color:var(--green)}.look{color:var(--mute)}.stagebar{background:var(--soft);font-weight:600}
.legend{display:flex;gap:12px;font-size:12px;color:var(--mute);flex-wrap:wrap;margin-bottom:6px}
.legend i{font-style:normal}
details summary{cursor:pointer;color:var(--mute);font-size:13px}pre{font:11px ui-monospace,Menlo,monospace;white-space:pre-wrap;color:var(--mute)}
</style></head><body><div class="wrap">
<div class="banner"><div id="state" class="state idle">Idle</div><div class="steps" id="steps"></div>
<div class="meta" id="meta"></div></div>
<div class="main">
<div>
<div class="card"><h2>What the robot sees</h2>
<div class="cams"><div><img id="c_scene"><div class="cap">overhead</div></div>
<div class="side"><div><img id="c_wrist_l"><div class="cap">left wrist</div></div><div><img id="c_wrist_r"><div class="cap">right wrist</div></div></div></div>
<div class="now"><h2>Dream is doing</h2><div class="bubble" id="now">nothing (station idle)</div><div class="who" id="nowwho"></div></div>
<div class="now"><h2>The harness thinks</h2><div class="bubble think" id="think">—</div><div class="who" id="thinkwho"></div></div>
</div>
<div class="card" style="margin-top:16px" id="controls"></div>
</div>
<div class="card"><h2>What happened, newest first</h2>
<div class="legend"><i>🤖 sent to Dream</i><i>🛠 harness stepped in</i><i>👁 monitor / check</i><i>🛑 guard</i><i>🙋 you</i></div>
<label class="hint"><input type="checkbox" id="looks"> also show every monitor look (one a second)</label>
<div class="tl" id="tl"></div></div>
</div></div>
<script>
const $=id=>document.getElementById(id);let model="base",run=null,next=0,t0=null,stageOrder=[],stageState={},running=false,lastWho="";
const esc=s=>String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const NICE={cable_unplug:"Unplug cable 1",cable_put_down:"Put cable 1 down",cable_unplug_more:"Unplug cable 2",cable_put_more_down:"Put cable 2 down",
arms_clear:"Arms clear",stack_base:"Place base cube",stack_2:"Stack a cube",tower_pass:"Tower > 4 in"};
const nice=s=>NICE[s]||(s||"").replace(/^ball_(\d)/,"Ball $1").replace(/_/g," ");
async function post(u,b){const r=await fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})});const j=await r.json();if(!j.ok)alert(j.error);return j}
function controls(){const c=$("controls");
 if(running){c.innerHTML=`<h2>Steer it</h2><button class="big stop" onclick="post('/api/stop')">■ Stop the robot</button>
 <div class="say"><input id="p" placeholder="Tell Dream exactly what to do now, e.g. put the pink cube on the blue cube"><button onclick="sp()">Send to Dream</button></div>
 <div class="hint">Replaces Dream's current instruction immediately, word for word.</div>
 <div class="say"><input id="s" placeholder="Talk to the harness, e.g. skip the balls / why did it grab the pink one?"><button onclick="ss()">Send</button></div>
 <div class="say"><button onclick="post('/api/skip')">⏭ Skip this stage</button></div>`;
 $("p").onkeydown=e=>{if(e.key==="Enter")sp()};$("s").onkeydown=e=>{if(e.key==="Enter")ss()};return}
 c.innerHTML=`<h2>Start a run</h2><div class="start">
 <button class="choice ${model==="base"?"sel":""}" onclick="model='base';controls()"><b>Base Dream-1</b><span>our prompts, writer for balls</span></button>
 <button class="choice ${model==="ft"?"sel":""}" onclick="model='ft';controls()"><b>Fine-tune (relay-final)</b><span>only the texts it was trained on</span></button></div>
 <div class="toggles"><label><input type="checkbox" class="g" value="cable" checked> Cables</label><label><input type="checkbox" class="g" value="stack" checked> Stack</label>
 <label><input type="checkbox" class="g" value="ball" checked> Balls</label><label title="no typing allowed during the run"><input type="checkbox" id="m"> Match mode</label></div>
 <button class="big go" onclick="go()">▶ Start (someone at the e-stop)</button>`}
function sp(){const t=$("p").value.trim();if(t)post("/api/prompt",{text:t}).then(j=>j.ok&&($("p").value=""))}
function ss(){const t=$("s").value.trim();if(t)post("/api/say",{text:t}).then(j=>j.ok&&($("s").value=""))}
function go(){const st=[...document.querySelectorAll(".g:checked")].map(x=>x.value);if(!st.length)return alert("pick at least one stage");
 if(!confirm(`LIVE run: ${model==="ft"?"fine-tune":"base Dream-1"}, ${st.join(" + ")}. Someone at the e-stop?`))return;
 run=null;next=0;$("tl").innerHTML="";post("/api/start",{model,stages:st,match:$("m").checked})}
function steps(){$("steps").innerHTML=stageOrder.map(s=>`<span class="step ${stageState[s]||""}">${esc(nice(s))}</span>`).join("")}
function story(e){const v=e.event,S=nice(e.stage);
 switch(v){
 case "run_start":stageOrder=e.stages||[];stageState={};steps();return["good","▶","Run started",stageOrder.map(nice).join(" → ")];
 case "reset":return["harness","🛠","Opened both grippers and moved the arms to the start pose",""];
 case "stage_start":Object.keys(stageState).forEach(k=>stageState[k]==="on"&&(stageState[k]=""));stageState[e.stage]="on";steps();return["stagebar","▸",`Stage: ${S}`,""];
 case "prompt_change":{const w=e.why||"";let who=w==="stage"?"stage prompt":w==="retry"?"retry, same stage":w.startsWith("grip assist")?"harness: after the pull":
  w.startsWith("stage")&&w.includes("lock")?"harness: stage looks done, arms lifted clear":w==="operator"?"you":w.startsWith("monitor")?"monitor rewrote it":w.startsWith("recovery")?"harness: recovery":w;
  lastWho=who;return["dream","🤖",`“${e.prompt}”`,who]}
 case "grip_assist":return["harness","🛠",e.action==="pull"?"Gripper is holding something: harness stopped Dream to pull it straight up":"Gripper closed on nothing: harness stopped Dream to re-grip",`gripper readings ${(e.readings||[]).join(", ")}`];
 case "grip_regrip":return["harness","🛠",e.held?"Re-grip worked: it holds the plug":"Re-grip closed on nothing again: Dream tries again",`gripper ${e.grip}`];
 case "grip_too_low":return["harness","🛠","Holding something at table height (a loose cable): let go, Dream tries again",`hand at ${(e.z*100).toFixed(1)} cm`];
 case "grip_slipped":return["harness","🛠","Plug slipped on the way up: back down to re-grip",`try ${e.repull}`];
 case "grip_pull":return["harness","🛠",e.outcome==="pulled"?"Pulled straight up and still holding it":e.outcome==="lost"?"Squeezed and found nothing in the gripper":"Lost it on the way up every time",
  e.z_before!=null?`hand ${(e.z_before*100).toFixed(0)} → ${(e.z_after*100).toFixed(0)} cm`:""];
 case "grip_put_down":return["harness","🛠","Put-down finished",`${e.seconds}s`];
 case "pull_pass_check":return[e.passed?"good":"harness","👁",e.passed?"Pull confirmed: the plug is out":"Sonnet says the plug is not out yet",e.reason||""];
 case "stage_locked":return["harness","🛠","Looks done: harness froze Dream so it can't undo it, checking…",""];
 case "confirm_check":return[e.yes?"good":"harness","👁",e.yes?"Sonnet confirms it's done":"Sonnet says not done yet",e.reason||""];
 case "attempt_end":if(e.ok){stageState[e.stage]="ok";steps();return["good","✓",`${S} passed`,e.reason]}return["harness","↺",`${S}: attempt ${e.attempt} ended`,e.reason];
 case "stage_skipped":stageState[e.stage]="no";steps();return["harness","⏭",`${S} skipped`,e.reason];
 case "recovery":return["harness","🛠",`Recovery: ${e.name}`,e.prompt||"same prompt again"];
 case "retry":return e.mode==="hands_off_keep_running"?["look","…","Timeout passed, Dream keeps going with the same prompt",""]:null;
 case "prompt_written":return["harness","✍",e.prompt?"Wrote a prompt for this stage from the cameras":"Prompt writer: "+e.why,""];
 case "prompt_rewrite":return["harness","✍","Monitor rewrote Dream's prompt",e.reason];
 case "verdict":if(!$("looks").checked)return null;return["look","👁",`${e.done?"DONE":e.status} · progress ${e.progress??"?"}`,e.reason];
 case "guard_yes":return["guard","🛑","Guard sees an illegal ball",e.reason||""];
 case "guard_tripped":case "guard_blind":return["guard","🛑","GUARD STOPPED THE RUN",e.reason||e.message||""];
 case "operator_input":return["you","🙋",`You: ${e.text}`,""];
 case "operator_reply":return["you","💬",e.text||"",""];
 case "operator_action":return["you","🙋",`Action: ${e.action}`,(e.stages||e.note||"")+""];
 case "station_restart":return["harness","↻","Dream errored, restarted the run",e.error];
 case "auto_reset":return["harness","↻","Dream's IK failed: reset and restart",e.error];
 case "stop":return["harness","■","Stopped Dream",e.why];
 case "run_end":Object.keys(stageState).forEach(k=>stageState[k]==="on"&&(stageState[k]=""));steps();return[e.ok?"good":"harness","■",`Run ended: ${e.reason}`,`${Math.round(e.seconds)} s`];
 case "live_guard":return["look","🛑",`Illegal-ball guard watching ${(e.answering||[]).join(" + ")} at ${e.rate_hz} Hz`,""];
 default:return null}}
async function poll(){try{const s=await(await fetch("/api/state")).json(),st=s.station;running=s.running;
 const el=$("state");if(!st){el.textContent="Station unreachable";el.className="state err"}
 else if(s.running){el.textContent="Running";el.className="state run"}else{el.textContent=st.state==="running"?"Dream running (no harness)":"Idle";el.className="state idle"}
 const t=st&&st.timing||{};$("meta").innerHTML=st?`${esc(st.checkpoint||"")}<br>Dream answers in ~${t.p50_ms?Math.round(t.p50_ms):"?"} ms · grip R ${s.arms?.right?.gripper?.toFixed(2)??"?"}`:"";
 $("now").textContent=st&&st.state==="running"&&st.prompt?st.prompt:"nothing (Dream not running)";$("nowwho").textContent=st&&st.state==="running"?("set by: "+(lastWho||"?")):"";
 if(!document.activeElement||!["p","s"].includes(document.activeElement.id)){const want=running?"stop":"start";if($("controls").dataset.m!==want){$("controls").dataset.m=want;controls()}}
}catch(e){}
try{const r=await(await fetch(`/api/events?since=${next}&run=${run||""}`)).json();if(r.reset||r.run!==run){run=r.run;next=0;$("tl").innerHTML="";t0=null;if(r.reset)return}
 next=r.next;for(const e of r.events){if(t0===null||e.event==="run_start")t0=e.t;
  if(e.event==="verdict"){$("think").textContent=`${e.done?"Looks done":e.status==="ok"?"On track":e.status==="stalled"?"Looks stuck":"Going wrong"} (progress ${e.progress??"?"}): ${e.reason}`;$("thinkwho").textContent=`monitor, ${nice(e.stage)}`}
  const x=story(e);if(!x)continue;const d=document.createElement("div");d.className="ev "+x[0];
  d.innerHTML=`<div class="t">${(e.t-t0).toFixed(0)}s</div><div class="i">${x[1]}</div><div><b>${esc(x[2])}</b>${x[3]?`<div class="d">${esc(x[3])}</div>`:""}</div>`;$("tl").prepend(d)}
}catch(e){}}
function cams(){for(const c of["scene","wrist_l","wrist_r"]){const i=new Image();i.onload=()=>$("c_"+c).src=i.src;i.src=`/api/frame/${c}.jpg?${Date.now()}`}}
controls();setInterval(poll,1000);setInterval(cams,600);poll();cams();
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
