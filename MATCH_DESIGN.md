# Five-minute Hill Climb supervisor

## Usage

Run the sprint plan for unplug and stack, or the full plan to exercise the optional plug and balls. Use a mock station and a scripted monitor for scheduling tests.

```python
plan = load_plan("plans/hill_climb_full.yaml")
result = Runner(plan, station, frames, {"monitor": monitor}, log).run()
abandoned = [stage.name for stage in result.stages if stage.abandoned]
```

The match plans use 300 seconds, with the last five reserved for requesting stop. The runner starts its clock after preflight. Match procedure still needs to align this start with the organizer's go signal. A designated team member can call stop.

## Shape

`Plan.match: MatchRules | None` enables the match clock and validates the named unplug and stack gates. Required stages run in plan order and halt on exhaustion. `Stage.stage_budget_s` caps total work, including retries and resets. `on_fail: abandon` marks an optional stage. Its `reserve_after_s` protects time for later stages. A dependent optional stage only runs after its prerequisite passes. The runner records abandonment separately from success and continues to the next eligible stage.

The runner checks the remaining budget before commands, when it sleeps or polls, and after a verifier returns. A timed-out command is ambiguous and terminates the run. The station's reply is separate from the stop request. HTTP timeouts are socket bounds. Physical stop and command ordering after a network failure depend on the station.

The unplug gate needs visual evidence of a cable clear of the hub. Rubric v4 requires a stack at least four inches tall that stands freely for three seconds after release. Current camera evidence and monitor prompts are proxies for the judge. Hardware validation needs a height reference and a check that the release and three-second dwell are visible.

## Design selection

The design review preferred a separate hierarchy with typed proof and action leases for a later version. The first slice keeps the existing runner because it already owns prompt changes, resets, and stop. The separate controller would need those paths extracted before it could run a match. This slice borrows explicit gate roles, an absolute clock, and separate abandonment and stop outcomes. It keeps generic plans and the legacy immediate `skip` behavior.

## Evidence and next tests

REFLECT uses observations around a failed subgoal to choose a bounded correction. Inner Monologue shows how false success can lead planning astray. LIBERO-RECOVER separates a retry from restoring a disrupted scene. Those distinctions map to stage verdicts, the authored recovery menu, and bounded abandonment. [REFLECT](https://proceedings.mlr.press/v229/liu23g.html) · [Inner Monologue](https://proceedings.mlr.press/v205/huang23c.html) · [LIBERO-RECOVER](https://arxiv.org/abs/2609.05178)

IntervenGen supports labeling mistakes and their corrections separately in future demonstrations. FORGE uses estimated Cartesian contact force and an impedance controller. Gripper motor torque is a separate observation and needs a bench calibration before it can support any insertion judgement. [IntervenGen](https://doi.org/10.1109/IROS58592.2024.10801523) · [FORGE](https://doi.org/10.1109/LRA.2025.3551637)

Sentinel supports comparing timestamped observations asynchronously. Hi Robot separates short task commands from the controller that executes them. Use a fake clock and scripted station to check that late verdicts never unlock another task, recovery consumes the stage budget, and optional failure preserves verified core progress. [Sentinel](https://proceedings.mlr.press/v270/agia25a.html) · [Hi Robot](https://www.pi.website/research/hirobot)

## LiveKit

Rubric v4 awards 10 points when the team shows its use of LiveKit to the referee before the match. The same rubric restricts human input after go, including voice. Keep tele-op handoffs in practice and demonstration collection. [Event page](https://luma.com/pah)

For training, Portal can record synchronized observations, executed actions, and which operator supplied each action. Use the capture timeline to label the policy mistake, human correction, and recovery result. Verify the event's Metal/Dream adapter, feature schema, and Dream import against a recorded sample before relying on this path. The public Portal HITL tutorial uses a different arm and an ACT policy. [LiveKit Portal](https://github.com/livekit/portal) · [HITL reference](https://github.com/livekit-examples/portal-hitl)

For detection, the optional LiveKit observer subscribes to named camera tracks and can run the existing monitor for a selected stage. It logs locally received frames and advisory verdicts. Comparing view coverage and detection latency with Dream debug trips requires paired recordings with a shared capture clock. The runner retains control and task advancement. [Robot camera tracks](https://docs.livekit.io/robotics/media/video/video-tracks/)
