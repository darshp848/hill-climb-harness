"""One HTML page for every live run on the bot, from any harness checkout: what ran, what passed, what failed and why.

    python tools/bot_report.py                       # writes runs/bot_report.html
    python tools/bot_report.py --open

Run folders come from RUN_DIRS (each harness checkout's runs/bot). Hand-written diagnosis and what the operator saw
at the bot go in lessons/bot_notes.json, keyed by run stamp: {"20261108T182057Z": {"seen": "...", "notes": ["..."]}}.
The rest of each card is computed from the run's events.jsonl and manifest.json. Thumbnails are embedded, so the page
opens on its own.
"""
from __future__ import annotations

import argparse
import base64
import html
import io
import json
import statistics
import subprocess
import webbrowser
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parents[1]
WT = HERE.parent
RUN_DIRS = {
    "Darsh (cloud-sims)": WT / "darsh-cloud-sims" / "runs" / "bot",
    "Ours (hybrid + sim fixes)": WT / "ours-bot" / "runs" / "bot",
}
NOTES = HERE / "lessons" / "bot_notes.json"
OUT = HERE / "runs" / "bot_report.html"
BASE = "dreamlabs/dream-1-beta"
PREFIX = "The power strip is unplugged from the wall"


def thumb(path: Path, w: int = 220) -> str:
    try:
        im = Image.open(path).convert("RGB")
    except OSError:
        return ""
    im.thumbnail((w, w))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=65)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def git_head(run_root: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(run_root), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def load_run(harness: str, run: Path) -> dict | None:
    if not (run / "events.jsonl").exists():
        return None
    ev = [json.loads(l) for l in open(run / "events.jsonl") if l.strip()]
    if not ev:
        return None
    t0 = ev[0]["t"]
    for e in ev:
        e["_s"] = e["t"] - t0
    man = json.loads((run / "manifest.json").read_text()) if (run / "manifest.json").exists() else {}
    args = man.get("args") or {}
    ckpt = next((e.get("checkpoint") for e in ev if e["event"] == "start"), None) or BASE
    verdicts = [e for e in ev if e["event"] == "verdict"]
    lat = [e["latency_s"] for e in verdicts if e.get("latency_s")]
    backend = args.get("claude_backend") or "api"
    return {
        "harness": harness, "run": run, "stamp": run.name, "ev": ev, "args": args,
        "plan": Path(args.get("plan") or man.get("plan_file") or "?").name,
        "ckpt": ckpt, "ft": ckpt != BASE,
        "monitor": "OpenRouter (" + (man.get("openrouter_models") or "gemini-3.1-flash-lite") + ")"
                   if backend == "openrouter" else "Claude Haiku 5.5",
        "dur": ev[-1]["_s"],
        "end": next((e.get("reason") for e in ev if e["event"] == "run_end"), "killed (no run_end)"),
        "verdicts": verdicts, "lat": lat,
        "prompts": [e for e in ev if e["event"] == "prompt_change"],
        "first_prompt": next((e.get("prompt") for e in ev if e["event"] == "prompt_change"), ""),
        "attempts": [e for e in ev if e["event"] == "attempt_end"],
        "skipped": [e for e in ev if e["event"] == "stage_skipped"],
        "skip_args": args.get("skip") or [],
    }


def diagnose(r: dict) -> list[str]:
    """Things the events show without anyone looking at the bot."""
    out = []
    ev = r["ev"]
    refusals = [e for e in ev if e["event"] == "station_restart" and "refus" in str(e.get("error", ""))]
    for e in refusals:
        out.append(f"Dream refused the instruction at {e['_s']:.0f} s: {str(e.get('error'))[-140:]}")
    if r["ft"] and r["first_prompt"].startswith(PREFIX):
        out.append("Fine-tune given base-style prompts (power-strip prefix, coaching text): the fine-tune only knows its "
                   "recorded task texts.")
    layered = [e for e in ev if e["event"] == "prompt_layered"]
    if layered:
        out.append(f"Prompt layering added operator coaching: first prompt is {len(r['first_prompt'].split())} words.")
    mins = max(r["dur"] / 60, 0.1)
    n = len(r["prompts"])
    if n / mins > 2:
        out.append(f"Dream's instruction changed {n} times in {r['dur']:.0f} s ({n / mins:.1f}/min): each change resets "
                   "its motion.")
    rew = sum(e["event"] == "prompt_rewrite" for e in ev)
    if rew:
        out.append(f"Monitor rewrote the prompt {rew} times.")
    rec = [e.get("name") for e in ev if e["event"] == "recovery"]
    if rec:
        out.append("Recoveries: " + ", ".join(f"{x}×{rec.count(x)}" for x in dict.fromkeys(rec)) + ".")
    # a done verdict that was not followed by a pass: the monitor flip-flopped
    flips = 0
    for i, e in enumerate(r["verdicts"]):
        if e.get("done") is True:
            nxt = next((x for x in r["verdicts"][i + 1:] if x.get("done") is not None or x.get("progress") is not None), None)
            if nxt and nxt.get("done") is not True and not any(a["_s"] >= e["_s"] and a["_s"] <= nxt["_s"] and a.get("ok")
                                                                for a in r["attempts"]):
                flips += 1
    if flips:
        out.append(f"Monitor said done {flips} time(s) and took it back on the next look (unconfirmed dones).")
    fl = sum("in flight" in str(e.get("reason")) for e in r["verdicts"])
    if r["verdicts"]:
        p50 = f"{statistics.median(r['lat']):.1f} s" if r["lat"] else "n/a"
        out.append(f"Monitor: {len(r['verdicts'])} looks, reply p50 {p50}, {fl} skipped as still in flight.")
    if "killed" in r["end"] or "operator" in r["end"]:
        out.append("Stopped by hand before the budget ran out.")
    grip_none = [e for e in ev if e["event"] == "prompt_rewrite" and ("shut on nothing" in str(e.get("reason"))
                                                                       or "on the cord" in str(e.get("reason"))
                                                                       or "closed on" in str(e.get("reason")))]
    if grip_none:
        out.append(f"Gripper closed on the cord or on nothing {len(grip_none)} time(s) (monitor rewrites).")
    return out


def stage_rows(r: dict) -> str:
    by = {}
    for a in r["attempts"]:
        s = by.setdefault(a["stage"], {"n": 0, "ok": False, "t": a["_s"], "why": ""})
        s["n"] += 1
        s["ok"] = s["ok"] or bool(a.get("ok"))
        s["t"] = a["_s"]
        s["why"] = str(a.get("reason", ""))
    for sk in r["skipped"]:
        by.setdefault(sk["stage"], {"n": 0, "ok": False, "t": sk["_s"], "why": ""})["why"] = "skipped: " + str(sk.get("reason"))
    if not by:
        return "<tr><td colspan=4 class=m>no stage finished</td></tr>"
    return "".join(f"<tr><td>{html.escape(k)}</td><td class={'ok' if v['ok'] else 'bad'}>{'pass' if v['ok'] else 'fail'}"
                   f"</td><td>{v['n']} · {v['t']:.0f} s</td><td>{html.escape(v['why'][:180])}</td></tr>"
                   for k, v in by.items())


def timeline(r: dict) -> str:
    out, last_img = [], -99.0
    keep = {"stage_start", "attempt_end", "prompt_change", "prompt_rewrite", "recovery", "stage_skipped",
            "scene_check", "confirm_check", "done_without_grip", "guard", "stop", "run_end", "station_restart"}
    for e in r["ev"]:
        k, t = e["event"], f"{e['_s']:5.0f}s"
        if k == "verdict":
            if "in flight" in str(e.get("reason")):
                continue
            done = e.get("done")
            show = done is not None or e["_s"] - last_img > 20
            imgs = ""
            if show:
                fr = e.get("frames") or []
                imgs = "".join(f'<img loading=lazy src="{thumb(r["run"] / f)}">' for f in fr
                               if ("scene" in f or "wrist_r" in f))
                last_img = e["_s"]
            cls = {True: "ok", False: "bad"}.get(done, "")
            lab = {True: "DONE", False: "FAILING"}.get(done, "look")
            lat = f" · {e['latency_s']:.1f}s" if e.get("latency_s") else ""
            out.append(f'<div class="row {cls}"><span class=t>{t}</span><div><b>monitor {lab}</b>'
                       f'<span class=m> p={e.get("progress", "")}{lat}</span> {html.escape(str(e.get("reason")))}'
                       f'<div class=imgs>{imgs}</div></div></div>')
        elif k in keep:
            txt = e.get("prompt") or e.get("new") or e.get("reason") or e.get("error") or e.get("name") or ""
            cls = {"prompt_change": "pr", "prompt_rewrite": "rw", "attempt_end": "ok" if e.get("ok") else "bad",
                   "station_restart": "bad"}.get(k, "")
            lab = {"prompt_change": "→ Dream", "prompt_rewrite": "monitor rewrites"}.get(k, k.replace("_", " "))
            out.append(f'<div class="row {cls}"><span class=t>{t}</span><div><b>{lab}</b> '
                       f'<span class=m>{html.escape(str(e.get("stage", "")))}</span> {html.escape(str(txt)[:400])}</div></div>')
    return "\n".join(out)


CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--mut:#6b6b66;--line:#e4e4df;--card:#fff;--ok:#2f7d4f;--bad:#b3261e;--pr:#1f5fa8;--rw:#8a5a00;--rwbg:#fff4dc}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecece8;--mut:#9b9b95;--line:#30302c;--card:#1e1e1c;--ok:#5cc28a;--bad:#ff6b5e;--pr:#7fb0ff;--rw:#f0b54a;--rwbg:#2e2412}}
*{box-sizing:border-box}body{margin:0;padding:20px 16px 60px;background:var(--bg);color:var(--fg);font:13px/1.45 system-ui,-apple-system,sans-serif}
main{max-width:1150px;margin:0 auto}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 4px}.m{color:var(--mut)}
table{border-collapse:collapse;width:100%;margin:8px 0}td,th{border-bottom:1px solid var(--line);padding:4px 7px;text-align:left;vertical-align:top}
th{font-weight:600;color:var(--mut);font-size:12px}.ok{color:var(--ok)}.bad{color:var(--bad)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;margin:16px 0}
.tags span{display:inline-block;border:1px solid var(--line);border-radius:10px;padding:1px 8px;margin:2px 4px 2px 0;font-size:12px}
.tags .ft{border-color:var(--rw);color:var(--rw)}ul{margin:6px 0;padding-left:18px}.seen{border-left:3px solid var(--pr);padding:4px 10px;margin:8px 0}
.prompt{font-family:ui-monospace,monospace;font-size:12px;background:var(--bg);padding:8px;border-radius:6px;white-space:pre-wrap}
details{margin-top:8px}summary{cursor:pointer;color:var(--pr)}
.row{display:grid;grid-template-columns:48px 1fr;gap:8px;padding:5px 0;border-bottom:1px solid var(--line)}
.row.pr b{color:var(--pr)}.row.rw{background:var(--rwbg)}.row.rw b{color:var(--rw)}.row.ok b{color:var(--ok)}.row.bad b{color:var(--bad)}
.t{color:var(--mut);font-variant-numeric:tabular-nums}.imgs img{width:220px;max-width:48%;margin:4px 4px 0 0;border-radius:4px}
a{color:var(--pr)}.wrap{overflow-x:auto}
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args()
    notes = json.loads(NOTES.read_text()) if NOTES.exists() else {}
    runs = []
    for harness, d in RUN_DIRS.items():
        head = git_head(d.parent.parent)
        for run in sorted(d.glob("2026*")):
            r = load_run(f"{harness} @{head}" if head else harness, run)
            if r:
                runs.append(r)
    runs.sort(key=lambda r: r["stamp"], reverse=True)

    idx = []
    cards = []
    for r in runs:
        n = notes.get(r["stamp"], {})
        passed = sorted({a["stage"] for a in r["attempts"] if a.get("ok")})
        idx.append(f"<tr><td><a href='#{r['stamp']}'>{r['stamp'][9:15]}</a></td><td>{html.escape(r['harness'])}</td>"
                   f"<td>{html.escape(r['plan'])}{' (skip ' + ', '.join(r['skip_args']) + ')' if r['skip_args'] else ''}</td>"
                   f"<td class={'bad' if r['ft'] else ''}>{'fine-tune' if r['ft'] else 'base'}</td><td>{html.escape(r['monitor'])}</td>"
                   f"<td class={'ok' if passed else 'bad'}>{html.escape(', '.join(passed) or 'nothing')}</td>"
                   f"<td>{html.escape(n.get('seen', ''))}</td></tr>")
        diag = [html.escape(x) for x in n.get("notes", [])] + [html.escape(x) for x in diagnose(r)]
        seen = f"<div class=seen><b>At the bot:</b> {html.escape(n['seen'])}</div>" if n.get("seen") else ""
        cards.append(
            f"<div class=card id='{r['stamp']}'><h2>{r['stamp']} · {html.escape(r['harness'])}</h2>"
            f"<div class=tags><span>{html.escape(r['plan'])}</span><span class={'ft' if r['ft'] else ''}>{html.escape(r['ckpt'])}</span>"
            f"<span>monitor: {html.escape(r['monitor'])}</span><span>{r['dur']:.0f} s</span>"
            f"<span>{'match' if r['args'].get('match') else 'no-operator' if r['args'].get('no_operator') else 'practice'}</span>"
            f"<span>ended: {html.escape(str(r['end']))}</span></div>{seen}"
            f"<div class=wrap><table><tr><th>Stage</th><th>Result</th><th>Tries · at</th><th>Why it ended</th></tr>{stage_rows(r)}</table></div>"
            f"<b>Diagnosis</b><ul>{''.join(f'<li>{x}</li>' for x in diag)}</ul>"
            f"<b>First instruction to Dream</b> <span class=m>({len(r['first_prompt'].split())} words)</span>"
            f"<div class=prompt>{html.escape(r['first_prompt'])}</div>"
            f"<details><summary>Full trace ({len(r['prompts'])} prompts, {len(r['verdicts'])} monitor looks)</summary>{timeline(r)}</details></div>")
    page = (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Bot run report</title><style>{CSS}</style></head><body><main><h1>Bot run report</h1>"
            f"<div class=m>Every live run on the real station, newest first. Built by tools/bot_report.py; hand notes from "
            f"lessons/bot_notes.json.</div><div class=wrap><table><tr><th>Run</th><th>Harness</th><th>Plan</th><th>Dream</th>"
            f"<th>Monitor</th><th>Passed</th><th>Seen at the bot</th></tr>{''.join(idx)}</table></div>{''.join(cards)}</main></body></html>")
    OUT.write_text(page)
    print(f"{OUT} ({OUT.stat().st_size // 1024} KB, {len(runs)} runs)")
    if a.open:
        webbrowser.open(OUT.as_uri())


if __name__ == "__main__":
    main()
