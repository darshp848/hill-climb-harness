"""Readable trace of harness runs: one page per run (what was asked, what the monitor saw, why each stage ended)
and an index of all runs.

    python tools/trace_report.py                 # every run under runs/, then opens runs/index.html
    python tools/trace_report.py runs/<stamp>    # one run

Each run page lists, in time order: every prompt sent to Dream (and why), every monitor verdict with its reason,
confidence and the frames it judged, attempt ends, skips, recoveries and the stop. Frames are the run's own files.
"""
from __future__ import annotations

import html
import json
import pathlib
import subprocess
import sys
from datetime import datetime

RUNS = pathlib.Path(__file__).resolve().parents[1] / "runs"

CSS = """
:root { --bg:#fbfbfa; --text:#1d1d1b; --muted:#6b6b66; --line:#e4e4df; --ok:#2f7d4f; --bad:#b3261e; --prompt:#1f5fa8; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --text:#ecece8; --muted:#9b9b95; --line:#30302c;
  --ok:#5cc28a; --bad:#ff6b5e; --prompt:#7fb0ff; } }
* { box-sizing:border-box; }
body { margin:0; padding:24px 16px 64px; background:var(--bg); color:var(--text);
  font:14px/1.5 Inter, system-ui, -apple-system, "Segoe UI", sans-serif; font-variant-numeric: tabular-nums; }
main { max-width:1100px; margin:0 auto; }
h1 { font-size:20px; margin:0 0 4px; } h2 { font-size:15px; margin:28px 0 8px; }
.muted { color:var(--muted); } a { color:inherit; }
table { border-collapse:collapse; width:100%; } td, th { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line);
  vertical-align:top; } th { color:var(--muted); font-weight:500; }
.ok { color:var(--ok); font-weight:600; } .bad { color:var(--bad); font-weight:600; }
.row { display:grid; grid-template-columns:64px 130px 1fr; gap:12px; padding:10px 0; border-bottom:1px solid var(--line); }
.t { color:var(--muted); } .kind { font-weight:600; }
.prompt { color:var(--prompt); } .stage { border-top:2px solid var(--text); margin-top:18px; }
.thumbs { display:flex; gap:6px; margin-top:6px; flex-wrap:wrap; }
.thumbs img { height:96px; border-radius:4px; display:block; }
.filter { margin:8px 0 0; } .filter label { margin-right:14px; }
body.hide-wait .waiting { display:none; }
"""

KINDS = {"stage_start": "stage", "start": "prompt", "set_prompt": None, "prompt_change": "prompt", "verdict": "verdict",
         "attempt_end": "attempt end", "stage_skipped": "skipped", "recovery": "recovery", "retry": "retry",
         "entry_check": "entry check", "stop": "stop", "run_end": "run end", "guard_stop": "guard stop"}


def _when(stamp: str) -> str:
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").strftime("%d %b %H:%M UTC")
    except ValueError:
        return stamp


def render(run: pathlib.Path) -> dict:
    events = [json.loads(l) for l in (run / "events.jsonl").read_text().splitlines() if l.strip()]
    if not events:
        return {}
    t0 = events[0]["t"]
    summary = json.loads((run / "summary.json").read_text()) if (run / "summary.json").exists() else {}
    end = next((e for e in reversed(events) if e["event"] in ("run_end", "stop")), {})
    task = next((e.get("task") for e in events if e["event"] == "run_start"), "")
    rows = []
    for e in events:
        kind = KINDS.get(e["event"])
        if kind is None:
            continue
        t = f"+{e['t'] - t0:.0f}s"
        stage = e.get("stage") or ""
        body = ""
        cls = "row"
        if e["event"] == "stage_start":
            cls += " stage"
            body = f"<b>{html.escape(stage)}</b> <span class='prompt'>{html.escape(e.get('prompt') or '')}</span>"
        elif e["event"] in ("start", "prompt_change"):
            why = e.get("why")
            body = f"<span class='prompt'>“{html.escape(e.get('prompt') or '')}”</span>" + (
                f" <span class='muted'>({html.escape(str(why))})</span>" if why else "")
        elif e["event"] in ("verdict", "entry_check"):
            done = e.get("done")
            if e.get("reason") == "monitor call in flight":
                cls += " waiting"
            mark = "<span class='ok'>done</span>" if done else "<span class='muted'>not yet</span>" if done is None \
                else "<span class='bad'>failing</span>"
            conf = e.get("confidence")
            body = f"{mark} <span class='muted'>{'' if conf is None else f'conf {conf:.2f}'}</span> " \
                   f"{html.escape(str(e.get('reason') or ''))}"
            frames = e.get("frames") or []
            if frames:
                body += "<div class='thumbs'>" + "".join(
                    f"<a href='{html.escape(f)}' target='_blank'><img loading='lazy' src='{html.escape(f)}' "
                    f"title='{html.escape(f)}'></a>" for f in frames) + "</div>"
        elif e["event"] == "attempt_end":
            ok = e.get("ok")
            body = ("<span class='ok'>passed</span> " if ok else "<span class='bad'>ended</span> ") + \
                html.escape(str(e.get("reason") or ""))
        else:
            body = html.escape(str(e.get("reason") or e.get("why") or e.get("prompt") or e.get("mode") or ""))
        rows.append(f"<div class='{cls}'><div class='t'>{t}</div><div class='kind'>{html.escape(kind)}"
                    f"<div class='muted'>{html.escape(stage)}</div></div><div>{body}</div></div>")
    stages = summary.get("stages") or []
    stage_rows = "".join(
        f"<tr><td>{html.escape(s['name'])}</td><td class='{'ok' if s.get('ok') else 'bad'}'>"
        f"{'passed' if s.get('ok') else 'no'}</td><td>{s.get('attempts') or 0}</td>"
        f"<td>{round(s.get('seconds') or 0)} s</td><td class='muted'>{html.escape(str(s.get('reason') or ''))[:160]}</td></tr>"
        for s in stages if s.get("attempts"))
    secs = round(end.get("seconds") or (events[-1]["t"] - t0))
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Run {run.name}</title><style>{CSS}</style></head>
<body><main><p><a href="../index.html">All runs</a></p>
<h1>Run {html.escape(run.name)}</h1>
<div class="muted">{_when(run.name)} · {secs} s · ended: {html.escape(str(end.get('reason') or end.get('why') or '?'))}</div>
<p class="muted">{html.escape(task or '')}</p>
<h2>Stages</h2><table><tr><th>Stage</th><th>Passed</th><th>Attempts</th><th>Time</th><th>How it ended</th></tr>{stage_rows}</table>
<h2>Timeline</h2>
<div class="filter muted"><label><input type="checkbox" id="w" onchange="document.body.classList.toggle('hide-wait', this.checked)">
hide "monitor call in flight"</label></div>
{''.join(rows)}
</main></body></html>"""
    (run / "trace.html").write_text(page)
    passed = [s["name"] for s in stages if s.get("ok")]
    return {"run": run.name, "secs": secs, "end": str(end.get("reason") or end.get("why") or ""), "passed": passed,
            "tried": [s["name"] for s in stages if s.get("attempts")]}


def index(items: list[dict]) -> pathlib.Path:
    rows = "".join(
        f"<tr><td><a href='{i['run']}/trace.html'>{_when(i['run'])}</a></td><td>{i['secs']} s</td>"
        f"<td class='ok'>{html.escape(', '.join(i['passed'])) or '<span class=muted>none</span>'}</td>"
        f"<td class='muted'>{html.escape(i['end'])[:120]}</td></tr>" for i in items)
    path = RUNS / "index.html"
    path.write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Harness runs</title><style>{CSS}</style></head>
<body><main><h1>Harness runs</h1><p class="muted">Newest first. Click a run for its timeline.</p>
<table><tr><th>Run</th><th>Length</th><th>Stages passed</th><th>How it ended</th></tr>{rows}</table></main></body></html>""")
    return path


if __name__ == "__main__":
    targets = [pathlib.Path(a) for a in sys.argv[1:]] or sorted(
        (p for p in RUNS.iterdir() if (p / "events.jsonl").exists()), reverse=True)
    items = [i for i in (render(t) for t in targets) if i]
    out = index(items) if not sys.argv[1:] else targets[0] / "trace.html"
    print(out)
    if sys.platform == "darwin":
        subprocess.run(["open", str(out)], check=False)
