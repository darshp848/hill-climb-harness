"""Dashboard gripper control for the follower in leader/follower mode: a slider that overrides the leader's gripper."""
import pathlib

home = pathlib.Path.home()
D = home / "physical-ai-hacks/src/dream_hackathon"

d = (D / "dashboard.py").read_text()
assert "GRIP_OVERRIDE" not in d
d = d.replace('''def take_presses() -> frozenset[str]:''', '''## The follower gripper's opening set on the page (0 shut .. 1 open), or None: follow the leader's gripper.
GRIP_OVERRIDE: list = [None]


def take_presses() -> frozenset[str]:''', 1)
d = d.replace('''            if path in ("/api/press", "/api/mode"):''', '''            if path in ("/api/press", "/api/mode", "/api/grip"):''', 1)
d = d.replace('''                elif path == "/api/mode":''', '''                elif path == "/api/grip":
                    value = body.get("value")
                    if value is None:
                        GRIP_OVERRIDE[0] = None
                        ok, message = True, "gripper follows the leader again"
                    else:
                        GRIP_OVERRIDE[0] = min(1.0, max(0.0, float(value)))
                        ok, message = True, f"gripper set to {GRIP_OVERRIDE[0]:.2f} (0 shut, 1 open)"
                elif path == "/api/mode":''', 1)
d = d.replace('''                "teleop_mode": current_mode(),''', '''                "teleop_mode": current_mode(),
                "grip_override": GRIP_OVERRIDE[0],''', 1)
d = d.replace('''  <div id="controlresult" class="actionresult"></div>
</section>''', '''  <div id="gripbox" style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px">
    <span>Follower gripper:</span>
    <button class="action" type="button" data-grip="1">Open</button>
    <input id="gripslider" type="range" min="0" max="1" step="0.02" value="1" style="flex:1;min-width:160px">
    <button class="action" type="button" data-grip="0">Close</button>
    <b id="gripvalue" style="min-width:3em">leader</b>
    <button class="action" type="button" data-grip="leader">Follow leader gripper</button>
  </div>
  <div id="controlresult" class="actionresult"></div>
</section>''', 1)
d = d.replace('''function renderControls(s) {''', '''async function setGrip(value) {
  const r = await postAction("/api/grip", {value}); $("controlresult").textContent = r.message;
}
let gripTimer = null;
document.addEventListener("input", (e) => {
  if (e.target.id !== "gripslider") return;
  $("gripvalue").textContent = Number(e.target.value).toFixed(2);
  clearTimeout(gripTimer); gripTimer = setTimeout(() => setGrip(Number(e.target.value)), 60);
});
function renderControls(s) {
  $("gripbox").hidden = s.teleop_mode !== "leader";
  if (document.activeElement !== $("gripslider")) {
    $("gripvalue").textContent = s.grip_override == null ? "leader" : s.grip_override.toFixed(2);
    if (s.grip_override != null) $("gripslider").value = s.grip_override;
  }''', 1)
d = d.replace('''  else if (t.dataset && t.dataset.mode) switchMode(t.dataset.mode);''', '''  else if (t.dataset && t.dataset.mode) switchMode(t.dataset.mode);
  else if (t.dataset && t.dataset.grip) {
    const v = t.dataset.grip === "leader" ? null : Number(t.dataset.grip);
    if (v != null) $("gripslider").value = v;
    setGrip(v);
  }''', 1)
for n in ('"/api/grip"', 'id="gripslider"', "setGrip(v)", "grip_override"):
    assert n in d, n
(D / "dashboard.py").write_text(d)

L = home / "follower_code/metal/leader_collect.py"
s = L.read_text()
old = '''        self._command.grips[side] = robot.leader_grip'''
assert s.count(old) == 1
s = s.replace(old, '''        override = dashboard.GRIP_OVERRIDE[0]  # the dashboard's gripper slider, when it is set
        self._command.grips[side] = robot.leader_grip if override is None else override''', 1)
assert "from dream_hackathon import arms, collect, config, dashboard" in s
L.write_text(s)
print("patched")
