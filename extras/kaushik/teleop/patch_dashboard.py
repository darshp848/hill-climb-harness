"""Dashboard controls for the kit: A/B/home buttons and a Quest / leader-follower mode switch (run by teleop_supervisor.sh)."""
import pathlib

D = pathlib.Path.home() / "physical-ai-hacks/src/dream_hackathon"

d = (D / "dashboard.py").read_text()
assert "take_presses" not in d, "already patched"

# 1. presses and the mode switch, module level
d = d.replace('''class Dashboard:
''', '''## Buttons pressed on the page (A, B, RJ), taken by the collector's next read beside the headset's own presses.
_PRESSES: "collections.deque[str]" = collections.deque(maxlen=8)
## The teleop mode teleop_supervisor.sh runs (quest or leader), and the flag that tells it to start the other one.
MODE_FILE = pathlib.Path.home() / ".cache/dream_hackathon/teleop_mode"
SWITCH_FLAG = pathlib.Path.home() / ".cache/dream_hackathon/teleop_switch"
MODES = ("quest", "leader")


def take_presses() -> frozenset[str]:
    """The page's button presses since the last call."""
    taken = set()
    while _PRESSES:
        taken.add(_PRESSES.popleft())
    return frozenset(taken)


def current_mode() -> str:
    try:
        mode = MODE_FILE.read_text().strip()
    except OSError:
        return "unknown"
    return mode if mode in MODES else "unknown"


class Dashboard:
''', 1)

# 2. status carries the mode
d = d.replace('''                "events": list(self._events),
            }''', '''                "events": list(self._events),
                "teleop_mode": current_mode(),
                "supervised": SWITCH_FLAG.parent.joinpath("teleop_supervised").exists(),
            }''', 1)

# 3. the two routes
d = d.replace('''            action = actions.get(path)
            if action is None:''', '''            if path in ("/api/press", "/api/mode"):
                actions[path] = path
            action = actions.get(path)
            if action is None:''', 1)
d = d.replace('''                if path == "/api/fix":
                    ok, message = action(str(body.get("fix", "")))''', '''                if path == "/api/press":
                    button = str(body.get("button", ""))
                    if button not in ("A", "B", "RJ"):
                        raise ValueError("button must be A, B or RJ")
                    _PRESSES.append(button)
                    ok, message = True, {"A": "A pressed", "B": "B pressed", "RJ": "home pressed"}[button]
                elif path == "/api/mode":
                    ok, message = board._switch_mode(str(body.get("mode", "")))
                elif path == "/api/fix":
                    ok, message = action(str(body.get("fix", "")))''', 1)

# 4. the switch: refused while recording; else write the mode, flag the supervisor and end this session like Ctrl+C
d = d.replace('''    def _status(self) -> dict[str, Any]:''', '''    def _switch_mode(self, mode: str) -> tuple[bool, str]:
        import os
        import signal

        if mode not in MODES:
            return False, f"mode must be one of {', '.join(MODES)}"
        if mode == current_mode():
            return True, f"already in {mode} mode"
        if self._snapshot.get("recording"):
            return False, "an episode is recording: save or discard it first"
        if not SWITCH_FLAG.parent.joinpath("teleop_supervised").exists():
            return False, "not started by teleop_supervisor.sh, so nothing would start the other mode"
        MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        MODE_FILE.write_text(mode)
        SWITCH_FLAG.write_text(mode)
        self.event(f"switching to {mode} mode: the arms go back to their start and are released, then it restarts")
        os.kill(os.getpid(), signal.SIGINT)
        return True, f"switching to {mode}: about 30 s"

    def _status(self) -> dict[str, Any]:''', 1)

# 5. the page: a Controls panel at the top of the grid
d = d.replace('''<section class="grid">
  <div class="panel">
    <h2>Session</h2>''', '''<section class="panel" id="controls" style="margin-bottom:12px">
  <h2>Controls</h2>
  <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">
    <span>Teleop: <b id="teleopmode">?</b></span>
    <button class="action" type="button" data-mode="leader">Use leader / follower</button>
    <button class="action" type="button" data-mode="quest">Use Quest</button>
    <span style="flex:1"></span>
    <button class="action" type="button" id="btnA">Open arms / Record</button>
    <button class="action danger" type="button" id="btnB">Discard</button>
    <button class="action" type="button" id="btnRJ">Home</button>
  </div>
  <div id="controlresult" class="actionresult"></div>
</section>
<section class="grid">
  <div class="panel">
    <h2>Session</h2>''', 1)
d = d.replace('''<script>
const $ = (id) => document.getElementById(id);''', '''<script>
const $ = (id) => document.getElementById(id);
async function press(button) { const r = await postAction("/api/press", {button}); $("controlresult").textContent = r.message; }
async function switchMode(mode) {
  if (!confirm(`Switch to ${mode} mode? The arms go back to their start and are released, then it restarts.`)) return;
  const r = await postAction("/api/mode", {mode}); $("controlresult").textContent = r.message;
}
function renderControls(s) {
  $("teleopmode").textContent = s.teleop_mode === "leader" ? "leader / follower" : (s.teleop_mode || "?");
  document.querySelectorAll("[data-mode]").forEach((b) => { b.disabled = !s.supervised || b.dataset.mode === s.teleop_mode; });
  const opened = s.phase === "" || s.phase === "running" || (s.snapshot && "recording" in s.snapshot && s.phase !== "ready" && s.phase !== "waiting");
  const rec = !!(s.snapshot && s.snapshot.recording);
  $("btnA").textContent = rec ? "Save episode" : (s.phase === "ready" ? "Open arms" : "Record");
  $("btnB").disabled = !rec; $("btnRJ").disabled = rec;
}
document.addEventListener("click", (e) => {
  const t = e.target;
  if (t.id === "btnA") press("A"); else if (t.id === "btnB") press("B"); else if (t.id === "btnRJ") press("RJ");
  else if (t.dataset && t.dataset.mode) switchMode(t.dataset.mode);
});''', 1)
d = d.replace('''  try { render(await (await fetch("/api/status")).json()); }''',
              '''  try { const s = await (await fetch("/api/status")).json(); render(s); renderControls(s); }''', 1)
for needle in ("take_presses", "_switch_mode", 'id="btnA"', "renderControls(s)", '"teleop_mode"'):
    assert needle in d, needle
(D / "dashboard.py").write_text(d)

c = (D / "collect.py").read_text()
c = c.replace('''        state = self._headset.read()
        self._handle_buttons(state.pressed)''', '''        state = self._headset.read()
        self._handle_buttons(state.pressed | dashboard.take_presses())''', 1)
c = c.replace('''        pressed = found.headset.read().pressed if found.headset is not None else frozenset()''',
              '''        pressed = found.headset.read().pressed if found.headset is not None else frozenset()
        pressed = pressed | dashboard.take_presses()''', 1)
assert c.count("dashboard.take_presses()") == 2
(D / "collect.py").write_text(c)
print("patched")
