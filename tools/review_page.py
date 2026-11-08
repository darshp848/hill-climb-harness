"""Build one HTML page to review recorded episodes: the cameras side by side, the right gripper over time, and keys
to mark each episode keep or drop. It only reads the datasets and writes the page; it never changes an episode.

On the station (pyarrow comes from dream-env; without it the page just has no gripper chart):

    ~/dream-env/bin/python ~/hill-climb-harness/tools/review_page.py ~/hackathon_data/quest-split-v3 \
        ~/hackathon_data/leader-split-v1-frozen ...        # writes ~/hackathon_review/review.html
    python3 -m http.server 8097 --bind 127.0.0.1 --directory ~/hackathon_review

On the laptop:

    ssh -N -L 8097:localhost:8097 pah@<station-ip>             # then open http://localhost:8097/review.html

Keys: Left/Right previous/next, D drop, K keep, U unmark, Space play/pause, F next unmarked.
Marks stay in the browser. The Export box turns the drops into `dhk-dataset drop` commands to run by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CAMERAS = ("scene", "right_wrist", "left_wrist")
CHART_HZ = 10


def _gripper(dataset: Path, info: dict, ep: dict) -> dict | None:
    """The right gripper's measured and commanded value, sampled at CHART_HZ, or None without pyarrow."""
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return None
    names = (info["features"].get("observation.state") or {}).get("names") or []
    if "right_gripper" not in names:
        return None
    col = names.index("right_gripper")
    chunk = ep["episode_index"] // info.get("chunks_size", 1000)
    path = dataset / info["data_path"].format(episode_chunk=chunk, episode_index=ep["episode_index"])
    table = pq.read_table(path, columns=["observation.state", "action"])
    step = max(1, round(info["fps"] / CHART_HZ))
    state = [round(float(row[col]), 3) for row in table.column("observation.state").to_pylist()[::step]]
    action = [round(float(row[col]), 3) for row in table.column("action").to_pylist()[::step]]
    return {"hz": info["fps"] / step, "state": state, "action": action}


def collect(datasets: list[Path], root: Path) -> list[dict]:
    """Video paths are relative to `root`, the datasets' parent folder, which the page reaches as `data/`."""
    rows = []
    for dataset in datasets:
        info = json.loads((dataset / "meta" / "info.json").read_text())
        video_keys = {k.rsplit(".", 1)[-1]: k for k, v in info["features"].items() if v.get("dtype") == "video"}
        with (dataset / "meta" / "episodes.jsonl").open() as f:
            episodes = [json.loads(line) for line in f if line.strip()]
        for ep in episodes:
            chunk = ep["episode_index"] // info.get("chunks_size", 1000)
            videos = {}
            for cam in CAMERAS:
                key = video_keys.get(cam)
                if key:
                    rel = info["video_path"].format(episode_chunk=chunk, video_key=key, episode_index=ep["episode_index"])
                    videos[cam] = "data/" + os.path.relpath(dataset / rel, root)
            rows.append({
                "folder": dataset.name,
                "ep": ep["episode_index"],
                "task": (ep.get("tasks") or [""])[0],
                "seconds": round(ep["length"] / info["fps"], 1),
                "videos": videos,
                "gripper": _gripper(dataset, info, ep),
            })
        print(f"{dataset.name}: {len(episodes)} episodes", file=sys.stderr)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("datasets", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, default=Path("~/hackathon_review/review.html"),
                        help="the page; its folder gets a `data` link to the datasets' parent (default: %(default)s)")
    args = parser.parse_args()
    datasets = [d.expanduser().resolve() for d in args.datasets]
    root = datasets[0].parent
    if any(d.parent != root for d in datasets):
        parser.error("all datasets must be in the same folder")
    out = args.out.expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    link = out.parent / "data"
    if link.is_symlink():
        link.unlink()
    if link.exists():
        parser.error(f"{link} exists and is not a link; pick another --out folder")
    link.symlink_to(root)
    rows = collect(datasets, root)
    page = PAGE.replace("/*DATA*/null", json.dumps(rows, separators=(",", ":")))
    out.write_text(page)
    print(f"wrote {out} ({len(rows)} episodes); serve its folder and open {out.name}", file=sys.stderr)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Episode review</title>
<style>
:root { --bg:#f6f5f2; --panel:#fff; --ink:#1d1d1b; --muted:#6b6a66; --line:#e2e0da; --accent:#2f5bd3;
        --keep:#1e7a46; --keep-bg:#e3f3e9; --drop:#b42318; --drop-bg:#fbe7e5; --sel:#eef2fc; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#151514; --panel:#1f1f1d; --ink:#ecebe7; --muted:#9a9993; --line:#33322f; --accent:#7d9cf0;
          --keep:#6fcf97; --keep-bg:#183424; --drop:#f28b82; --drop-bg:#3b1d1a; --sel:#232a3d; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.4 system-ui, -apple-system, sans-serif;
       height:100vh; display:grid; grid-template-columns:340px 1fr; }
aside { border-right:1px solid var(--line); background:var(--panel); display:flex; flex-direction:column; min-height:0; }
.filters { padding:12px; border-bottom:1px solid var(--line); display:grid; gap:8px; }
.filters select, .filters label { font:inherit; }
.filters select { width:100%; padding:4px; background:var(--bg); color:var(--ink); border:1px solid var(--line); border-radius:6px; }
.counts { color:var(--muted); font-size:12px; }
ul { list-style:none; margin:0; padding:0; overflow:auto; flex:1; }
li { padding:7px 12px; border-bottom:1px solid var(--line); cursor:pointer; display:grid;
     grid-template-columns:1fr auto; gap:2px 8px; }
li.sel { background:var(--sel); box-shadow:inset 3px 0 var(--accent); }
li .name { font-weight:600; font-variant-numeric:tabular-nums; }
li .meta { color:var(--muted); font-size:12px; grid-column:1 / -1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.badge { font-size:11px; padding:1px 7px; border-radius:99px; align-self:start; }
.badge.keep { background:var(--keep-bg); color:var(--keep); } .badge.drop { background:var(--drop-bg); color:var(--drop); }
.short { color:var(--drop); font-weight:600; }
main { display:flex; flex-direction:column; min-height:0; padding:16px; gap:12px; overflow:auto; }
header { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }
header h1 { font-size:18px; margin:0; font-variant-numeric:tabular-nums; }
header .task { color:var(--muted); }
.cams { display:grid; grid-template-columns:2fr 2fr 1fr; gap:8px; }
.cam { background:#000; border-radius:8px; overflow:hidden; position:relative; }
.cam video { width:100%; display:block; aspect-ratio:16/9; }
.cam span { position:absolute; left:8px; top:6px; color:#fff; font-size:11px; background:#0008; padding:1px 6px; border-radius:4px; }
.bar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
button { font:inherit; padding:6px 12px; border-radius:6px; border:1px solid var(--line); background:var(--panel); color:var(--ink); cursor:pointer; }
button.keep { border-color:var(--keep); color:var(--keep); } button.drop { border-color:var(--drop); color:var(--drop); }
input[type=range] { flex:1; min-width:160px; }
.chart { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:8px; }
.chart svg { width:100%; height:110px; display:block; }
.legend { font-size:12px; color:var(--muted); }
.help { font-size:12px; color:var(--muted); }
kbd { font:11px ui-monospace, monospace; border:1px solid var(--line); border-radius:4px; padding:0 4px; background:var(--panel); }
textarea { width:100%; min-height:110px; font:12px ui-monospace, monospace; background:var(--panel); color:var(--ink);
           border:1px solid var(--line); border-radius:8px; padding:8px; }
@media (max-width: 900px) { body { grid-template-columns:1fr; height:auto; } aside { max-height:40vh; }
  .cams { grid-template-columns:1fr; } }
</style>
</head>
<body>
<aside>
  <div class="filters">
    <select id="fFolder"></select>
    <select id="fTask"></select>
    <select id="fMark"><option value="">all marks</option><option value="none">unmarked</option>
      <option value="keep">keep</option><option value="drop">drop</option></select>
    <label><input type="checkbox" id="fShort"> only under 3 s</label>
    <div class="counts" id="counts"></div>
  </div>
  <ul id="list"></ul>
</aside>
<main>
  <header><h1 id="title"></h1><span class="task" id="task"></span></header>
  <div class="cams" id="cams"></div>
  <div class="bar">
    <button id="play">Play</button>
    <input type="range" id="seek" min="0" max="1000" value="0">
    <span id="clock" class="legend"></span>
    <select id="rate"><option>0.5</option><option>1</option><option selected>2</option><option>4</option></select>
    <button class="keep" id="bKeep">Keep (K)</button>
    <button class="drop" id="bDrop">Drop (D)</button>
  </div>
  <div class="chart"><svg id="chart" viewBox="0 0 1000 110" preserveAspectRatio="none"></svg>
    <div class="legend">Right gripper: <b style="color:var(--accent)">measured</b>, dashed = commanded.
      Higher is more open. A good unplug closes fully and stays closed while it pulls.</div></div>
  <div class="help"><kbd>←</kbd>/<kbd>→</kbd> previous/next · <kbd>K</kbd> keep · <kbd>D</kbd> drop ·
    <kbd>U</kbd> unmark · <kbd>Space</kbd> play/pause · <kbd>F</kbd> next unmarked. Marking moves to the next episode.
    Marks are saved in this browser only.</div>
  <div>
    <div class="legend">Export: run on the station, from <code>~/physical-ai-hacks</code>. Each command asks before it deletes. Then rebuild that folder's <code>-eef</code> copy.</div>
    <textarea id="export" readonly></textarea>
  </div>
</main>
<script>
const ROWS = /*DATA*/null;
const KEY = "episode-review-marks";
let marks = {};
try { marks = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(marks)); } catch (e) {} };
const id = r => r.folder + ":" + r.ep;
const $ = s => document.getElementById(s);
const shortTask = t => t.length > 48 ? t.slice(0, 46) + "…" : t;

function options(sel, label, values) {
  sel.innerHTML = `<option value="">${label}</option>` + values.map(v => `<option>${v}</option>`).join("");
}
options($("fFolder"), "all folders", [...new Set(ROWS.map(r => r.folder))]);
options($("fTask"), "all tasks", [...new Set(ROWS.map(r => r.task))]);

let shown = [], cur = -1;
function filter() {
  const f = $("fFolder").value, t = $("fTask").value, m = $("fMark").value, s = $("fShort").checked;
  shown = ROWS.filter(r => (!f || r.folder === f) && (!t || r.task === t) && (!s || r.seconds < 3) &&
    (!m || (m === "none" ? !marks[id(r)] : marks[id(r)] === m)));
  const all = Object.values(marks);
  $("counts").textContent = `${shown.length} shown of ${ROWS.length} · ${all.filter(x => x === "keep").length} keep · ` +
    `${all.filter(x => x === "drop").length} drop · ${ROWS.length - all.length} unmarked`;
  $("list").innerHTML = shown.map((r, i) => {
    const mk = marks[id(r)];
    return `<li data-i="${i}"><span class="name">${r.folder} #${r.ep}</span>` +
      (mk ? `<span class="badge ${mk}">${mk}</span>` : "<span></span>") +
      `<span class="meta"><span class="${r.seconds < 3 ? "short" : ""}">${r.seconds}s</span> · ${shortTask(r.task)}</span></li>`;
  }).join("");
  [...$("list").children].forEach(li => li.onclick = () => select(+li.dataset.i));
  exportCmds();
}

function vids() { return [...document.querySelectorAll("#cams video")]; }
function select(i) {
  if (!shown.length) { cur = -1; return; }
  cur = Math.max(0, Math.min(i, shown.length - 1));
  const r = shown[cur];
  [...$("list").children].forEach((li, j) => li.classList.toggle("sel", j === cur));
  $("list").children[cur]?.scrollIntoView({ block: "nearest" });
  $("title").textContent = `${r.folder} #${r.ep} · ${r.seconds}s`;
  $("task").textContent = r.task;
  $("cams").innerHTML = ["scene", "right_wrist", "left_wrist"].filter(c => r.videos[c]).map(c =>
    `<div class="cam"><video src="${r.videos[c]}" muted playsinline preload="auto"></video><span>${c}</span></div>`).join("");
  const [lead, ...rest] = vids();
  if (!lead) return;
  lead.addEventListener("timeupdate", () => {
    const d = lead.duration || r.seconds;
    $("seek").value = Math.round(1000 * lead.currentTime / d);
    $("clock").textContent = `${lead.currentTime.toFixed(1)} / ${d.toFixed(1)} s`;
    rest.forEach(v => { if (Math.abs(v.currentTime - lead.currentTime) > 0.15) v.currentTime = lead.currentTime; });
    drawHead(lead.currentTime / d);
  });
  lead.addEventListener("ended", () => { $("play").textContent = "Replay"; });
  setRate(); drawChart(r); play();
}
function play() { vids().forEach(v => { if (v.ended) v.currentTime = 0; v.play().catch(() => {}); }); $("play").textContent = "Pause"; }
function pause() { vids().forEach(v => v.pause()); $("play").textContent = "Play"; }
function toggle() { const v = vids()[0]; if (!v) return; (v.paused || v.ended) ? play() : pause(); }
function setRate() { vids().forEach(v => v.playbackRate = +$("rate").value); }

function drawChart(r) {
  const svg = $("chart"), g = r.gripper;
  if (!g) { svg.innerHTML = `<text x="10" y="60" fill="currentColor" font-size="14">no gripper data (built without pyarrow)</text>`; return; }
  const vals = g.state.concat(g.action), lo = Math.min(...vals), hi = Math.max(...vals), span = (hi - lo) || 1;
  const pts = arr => arr.map((v, i) => `${(1000 * i / Math.max(arr.length - 1, 1)).toFixed(1)},${(100 - 90 * (v - lo) / span).toFixed(1)}`).join(" ");
  svg.innerHTML = `<polyline points="${pts(g.action)}" fill="none" stroke="var(--muted)" stroke-width="2" stroke-dasharray="6 5" vector-effect="non-scaling-stroke"/>` +
    `<polyline points="${pts(g.state)}" fill="none" stroke="var(--accent)" stroke-width="2.5" vector-effect="non-scaling-stroke"/>` +
    `<line id="head" x1="0" x2="0" y1="0" y2="110" stroke="var(--drop)" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`;
}
function drawHead(frac) { const h = document.getElementById("head"); if (h) { h.setAttribute("x1", 1000 * frac); h.setAttribute("x2", 1000 * frac); } }

function mark(value) {
  if (cur < 0) return;
  const r = shown[cur];
  if (value) marks[id(r)] = value; else delete marks[id(r)];
  save();
  const next = cur + 1, keepPos = $("fMark").value !== "";   // a mark filter may hide the row just marked
  filter();
  select(keepPos ? cur : next);
}
function nextUnmarked() {
  const j = shown.findIndex((r, i) => i > cur && !marks[id(r)]);
  if (j >= 0) select(j);
}
function exportCmds() {
  const byFolder = {};
  ROWS.forEach(r => { if (marks[id(r)] === "drop") (byFolder[r.folder] ||= []).push(r.ep); });
  const lines = Object.entries(byFolder).map(([f, eps]) =>
    `.venv/bin/dhk-dataset drop ~/hackathon_data/${f} ${eps.sort((a, b) => b - a).join(" ")}\n` +
    `~/dream-env/bin/python tools/add_eef.py ~/hackathon_data/${f} --arm metal --replace`);
  $("export").value = lines.length ? "cd ~/physical-ai-hacks\n" + lines.join("\n") : "(no drops marked yet)";
}

$("seek").oninput = e => { const v = vids()[0]; if (v && v.duration) vids().forEach(x => x.currentTime = v.duration * e.target.value / 1000); };
$("play").onclick = toggle;
$("rate").onchange = setRate;
$("bKeep").onclick = () => mark("keep");
$("bDrop").onclick = () => mark("drop");
["fFolder", "fTask", "fMark", "fShort"].forEach(k => $(k).onchange = () => { filter(); select(0); });
document.addEventListener("keydown", e => {
  if (e.target.tagName === "SELECT" || e.target.tagName === "TEXTAREA" || e.metaKey || e.ctrlKey) return;
  const k = e.key.toLowerCase();
  if (k === "arrowright") select(cur + 1); else if (k === "arrowleft") select(cur - 1);
  else if (k === "k") mark("keep"); else if (k === "d") mark("drop"); else if (k === "u") mark(null);
  else if (k === "f") nextUnmarked(); else if (k === " ") toggle(); else return;
  e.preventDefault();
});
filter(); select(0);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
