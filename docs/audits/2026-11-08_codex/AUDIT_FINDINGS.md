| id | severity | branch(es) | file:line | one-line claim |
|---|---|---|---|---|
| [A01](#a01) | Critical | main, cloud-sims | `harness/harness/runner.py:256` | A logging error can leave the robot running, including when stop is requested |
| [A02](#a02) | Critical | main, cloud-sims | `harness/harness/runner.py:645` | tower_pass can pass on one image with no stability confirmation |
| [A03](#a03) | High | main, cloud-sims | `harness/harness/runner.py:498` | Success confirmation ignores intervening failure and can bypass the configured count |
| [A04](#a04) | High | main, cloud-sims | `harness/harness/runner.py:431` | The image labelled stage start is captured after the action has begun |
| [A05](#a05) | High | main | `harness/harness/runner.py:420` | Main has no fast live guard, and cached images can keep its guard falsely fresh |
| [A06](#a06) | High | main, cloud-sims | `harness-cloud-sims/harness/detect.py:342` | A healthy wrist camera masks a missing camera on the carrying arm |
| [A07](#a07) | High | main, cloud-sims | `harness/plans/table_colors.yaml:21` | The white-ball guard misses neutral white by construction |
| [A08](#a08) | High | main, cloud-sims | `harness/plans/hill_climb_v4.yaml:71` | Fine-tune defaults conflict with runtime prompt generation and the prompt audit misses it |
| [A09](#a09) | High | main, cloud-sims | `harness/harness/station_client.py:31` | Blocking I/O can carry motion far beyond the match deadline |
| [A10](#a10) | High | cloud-sims | `harness-cloud-sims/harness/cli.py:81` | --match accepts timer-only verification and removal of pass-bar stages |
| [A11](#a11) | High | main, cloud-sims | `harness/harness/runner.py:245` | Inference restarts retain old monitor results and success evidence |
| [A12](#a12) | High | main, cloud-sims | `harness/harness/runner.py:275` | The refusal restart limit does not stop stage retries from resending the same unsafe instruction |
| [A13](#a13) | High | main, cloud-sims | `harness/plans/hill_climb_v4.yaml:109` | Failed cable-release steps can lead directly into stacking with an occupied gripper |
| [A14](#a14) | High | main, cloud-sims | `harness/plans/hill_climb_v4.yaml:160` | A three-block tower is treated as a mandatory gate even when two blocks already pass |
| [A15](#a15) | High | main, cloud-sims, task_simulation | `task_simulation/sim_scenes/hill_climb_table.py:196` | The simulated tower gate does not establish a supported stable tower |
| [A16](#a16) | Medium | main, cloud-sims | `harness/plans/hill_climb_v4.yaml:219` | Cumulative ball targets carry missed stages forward as extra work |
| [A17](#a17) | Medium | main, task_simulation | `task_simulation/sim_scenes/hill_climb_table.py:7` | The older MuJoCo table treats white balls as legal and has a different object inventory |
| [A18](#a18) | Medium | main, cloud-sims, task_simulation | `task_simulation/sim_scenes/hill_climb_table.py:219` | The bin success check counts objects below the table and does not require rest/release |
| [A19](#a19) | Medium | cloud-sims, hedged-calls, mac-arm64 | `harness-cloud-sims/harness/verifiers.py:727` | A losing hedged judge reply can contain the only illegal-ball alert and be discarded |
| [A20](#a20) | Medium | cloud-sims | `harness-cloud-sims/plans/operator_notes.yaml:101` | The oracle cannot evaluate the newly inserted ball_push_white stage |
| [A21](#a21) | Medium | cloud-sims | `harness-cloud-sims/harness/verifiers.py:272` | The subscription transport drops plan-specific system instructions |
| [A22](#a22) | Medium | cloud-sims | `harness-cloud-sims/harness/frames.py:60` | The real station add-on ignores the requested 640-pixel frame limit |
| [A23](#a23) | Medium | cloud-sims | `harness-cloud-sims/tests/test_hedge_cancel.py:110` | Missing SDK dependencies silently skip otherwise dependency-free hedge tests |
| [A25](#a25) | Medium | cloud-sims | `harness-cloud-sims/harness/runner.py:972` | The start-pose gate is advisory on several failure paths despite a hard-gate claim |
| [A24](#a24) | Low | cloud-sims | `harness-cloud-sims/tests/test_async_pacing.py:8` | Pacing tests fail on missing simulator dependencies instead of following the documented skip contract |
| [A26](#a26) | Low | main | `harness/tests/test_claude_cli.py:36` | Two subscription-backend tests use a non-executable Windows shebang stub |

## Scope and evidence

Audited 8 Nov 2026, offline, before the 15:00 PT demos. Candidate heads are main `b1f6e4b` and cloud-sims `c6a3255`. task_simulation is inspected as supplied. Severity prioritizes live motion left running, DQ exposure, and lost match time. Simulator-only and opt-in issues are labelled explicitly. No source was modified, no robot/model/service was called, and no credential file was read. The original rubric PDF and installed station SDK are not available in these clones, so rule/API claims use the supplied handoff, explainer, local design contract, and code. White-ball illegality and colour-order irrelevance are treated as settled supplied decisions, not challenged.

The research record is `raw/RESEARCH_GATE.md`. `raw/docs_<branch>.txt` contains branch README/TELEOP/design/strategy context. The companion PDF was extracted to `raw/explained_pdf.txt`. `head_diff_*` are direct head-to-head diffs; `diff_*` are merge-base diffs and should not be confused with direct comparisons. Branch references in this report mean local origin/<branch> refs, never fetched during this audit.

Offline test outcome: main 239 passed, 2 Windows-stub failures, 4 skipped. Cloud full collection stopped on missing cv2. The explicitly excluded-module subset had 376 passed, 6 failed, 11 skipped; five failures were missing Dream example dependencies and one timing assertion passed on isolated rerun. The two cloud CLI tests passed. Historical 407/4/5 results in LAST_NIGHT are not current test results. Initial sandbox/wrapper setup failures are preserved separately and are not charged to the product. See the raw test files and A23, A24, A26.

<a id="a01"></a>
## A01 Critical: A logging error can leave the robot running, including when stop is requested

Branches: main, cloud-sims. Confidence: High.

Evidence:

`harness/harness/runner.py:256`

```python
256:     def _safe_stop(self, why: str) -> None:
257:         self.log.event("stop", why=why)
258:         try:
259:             self.station.stop()
260:         except (StationError, OSError) as exc:
261:             self.log.event("stop_failed", error=str(exc))
262: 
```

`harness-cloud-sims/harness/runner.py:326`

```python
326:     def _safe_stop(self, why: str) -> None:
327:         self.log.event("stop", why=why)
328:         try:
329:             self.station.stop()
330:         except (StationError, OSError) as exc:
331:             self.log.event("stop_failed", error=str(exc))
332: 
```

`harness-cloud-sims/harness/log.py:64`

```python
64:             p.write_bytes(jpeg)
65:             paths.append(p)
66:         return paths
```

Why it is wrong: Runner.run catches a fixed set of exceptions, with no unconditional stop in finally. OSError from saving an image or writing an event escapes. Worse, _safe_stop writes a log event before entering the try that sends /stop, so a full disk can prevent even the error handler from stopping the station. The CLI finally blocks close helpers, not the station.

Demo failure scenario: Saved frames fill the laptop disk during a live stage. The harness exits with an OSError while the Dream station keeps executing its last prompt. A later timeout cannot stop it because the supervisor has already crashed.

Suggested fix: Guarantee a best-effort station.stop in an outer finally after motion has been requested. Send stop before logging, isolate logging errors, and report an unconfirmed stop without relying on the failed log writer.

Verification: raw/probes_main.json and raw/probes_cloud.json: both disk_error and stop_log_error have stops=0 and station_running=true.

<a id="a02"></a>
## A02 Critical: tower_pass can pass on one image with no stability confirmation

Branches: main, cloud-sims. Confidence: High.

Evidence:

`harness/harness/runner.py:645`

```python
645:     def _entry_check(self, stage: Stage, verifier: Verifier) -> bool:
646:         """skip_if_done: one synchronous look before the prompt is sent."""
647:         frames = self._frames_for_check(stage)
648:         verdict = self._ask(verifier, stage, frames, 0.0, sync=True)
649:         done = verdict.done is True and (verdict.immediate or (frames and verdict.confidence >= SKIP_MIN_CONFIDENCE))
650:         self.log.event("entry_check", stage=stage.name, done=verdict.done, confidence=verdict.confidence,
651:                        reason=verdict.reason, skipped=bool(done))
652:         return bool(done)
653: 
```

`harness-cloud-sims/harness/runner.py:894`

```python
894:     def _entry_check(self, stage: Stage, verifier: Verifier) -> bool:
895:         """skip_if_done: one synchronous look before the prompt is sent."""
896:         frames = self._frames_for_check(stage)
897:         verdict = self._ask(verifier, stage, frames, 0.0, sync=True)
898:         done = verdict.done is True and (verdict.immediate or (frames and verdict.confidence >= SKIP_MIN_CONFIDENCE))
899:         self.log.event("entry_check", stage=stage.name, done=verdict.done, confidence=verdict.confidence,
900:                        reason=verdict.reason, skipped=bool(done))
901:         return bool(done)
902: 
```

`harness/plans/hill_climb_v4.yaml:176`

```text
176:   - name: tower_pass
177:     prompt: "put a block on top of the stack"
178:     success: "Is the tower more than 4 inches (10 cm) tall, standing on its own, with both grippers open and clear of it?"
179:     expect: "The arm adds blocks to the tower until it is over 4 inches tall, then lifts clear; the tower stays standing."
180:     camera_notes:
181:       "{stack_cam}": "view from the gripper doing the stacking"
182:     timeout_s: 40
183:     max_attempts: 99    # pass bar: nothing later may be touched, so keep trying until the clock runs out
184:     min_run_s: 4
185:     stall_s: 20
186:     skip_if_done: true        # stack_3 may already be over 4 in: then no extra block is risked here
187:     recovery:
188:       - {name: keep_going, kind: retry, when: "the tower is standing and shorter than 4 inches", max_uses: 4}
189:       - {name: let_go_and_lift, kind: reset, prompt: "open both grippers and move the arms up",
```

Why it is wrong: skip_if_done uses one synchronous verdict with confidence >=0.8 and returns a successful StageResult immediately. It never applies confirm=2, distinct-frame checks, or the required three seconds after release. tower_pass enables this shortcut. A high confidence number is not evidence of a three-second dwell.

Demo failure scenario: The earlier stack_3 check has established a block count, but not a calibrated height pass bar. One false height-positive at entry skips tower_pass. Higher stages finish or fail, and the robot begins handling balls without a valid tower pass bar. A scene change or a recent release also receives no independent dwell check on this path. That can lose the match to DQ.

Suggested fix: Route entry checks through the same fresh-frame confirmation and release/stability timer used for normal pass-bar verification. Until that exists, disable this shortcut on tower_pass and use a deliberate observe-only confirmation step.

Verification: Both probe files: entry_confirm_2 passes after one check, with zero starts and zero elapsed seconds.

<a id="a03"></a>
## A03 High: Success confirmation ignores intervening failure and can bypass the configured count

Branches: main, cloud-sims. Confidence: High.

Evidence:

`harness/harness/runner.py:498`

```python
498:                         consec_done += 1
499:                         recent = last_done_t is not None and elapsed - last_done_t <= DONE_WINDOW_S
500:                         last_done_t, last_done_sig = elapsed, sig
501:                         if verdict.immediate or consec_done >= confirm or recent:
502:                             return _Attempt(True, verdict.reason or "verified", max_p, hint)
503:                     else:
504:                         consec_done = 0
505:                         self.log.event("no_frames", stage=stage.name, attempt=attempt,
506:                                        note="done verdict ignored: no frames to back it")
507:                 elif failing and last_done_t is not None and elapsed - last_done_t <= DONE_WINDOW_S:
508:                     pass                           # one flicker after a done does not undo it
509:                 elif failing:
510:                     consec_done = 0
511:                     consec_fail += 1
```

`harness-cloud-sims/harness/runner.py:718`

```python
718:                         consec_done += 1
719:                         recent = last_done_t is not None and elapsed - last_done_t <= DONE_WINDOW_S
720:                         last_done_t, last_done_sig = elapsed, sig
721:                         if verdict.immediate or consec_done >= confirm or recent:
722:                             return _Attempt(True, verdict.reason or "verified", max_p, hint)
723:                     else:
724:                         consec_done = 0
725:                         self.log.event("no_frames", stage=stage.name, attempt=attempt,
726:                                        note="done verdict ignored: no frames to back it")
727:                 elif failing and last_done_t is not None and elapsed - last_done_t <= DONE_WINDOW_S:
728:                     pass                           # one flicker after a done does not undo it
729:                 elif failing:
730:                     consec_done = 0
731:                     consec_fail += 1
```

`harness-cloud-sims/harness/plan.py:140`

```python
140:     confirm_gap_s: float = 0.0   # min seconds between two "done" verdicts that count toward confirm
141:     prompt_level: int = 1        # harness.promptgen level for base Dream-1 (1 = the plan's prompt as written)
```

`harness/tests/test_v2.py:345`

```python
345: def test_confirm_two_needs_consecutive_done(fake, write_plan, tmp_path):
346:     path = write_plan([stage("a")], confirm=2)
347:     sv = Script([mv(True), mv(None, "ok"), mv(True), mv(True)], repeat=False)
348:     result = build(fake, path, tmp_path, sv).run()
349:     assert result.ok, result.reason
350:     assert len(sv.calls) == 3          # kaushik-fixes: a second done within DONE_WINDOW_S passes despite the flicker
351: 
```

Why it is wrong: The return condition includes `or recent`, independent of consec_done >= confirm. A failing verdict inside DONE_WINDOW_S does not clear last_done_t. Thus a true/false/true sequence passes even with confirm=3. The test whose name promises consecutive confirmations explicitly expects this behavior. Cloud-sims has a minimum-gap feature, but the match plans omit it and default to 0.0. With the default 3 s poll, polling itself spaces deliveries; zero becomes an extra hazard when the poll interval is reduced. Receipt spacing also does not establish image capture spacing.

Demo failure scenario: A tower is reported standing, falls, then is rebuilt and has only just been released. The old positive plus the new positive satisfy the gate despite the intervening failure and without a new stability interval. A false cable-out result can similarly survive contradictory evidence.

Suggested fix: Reset success evidence on a definite contradiction and on scene/prompt epochs; require the configured number of independent observations. Implement dwell using capture times and release time. Explicitly set and validate a safe confirm_gap_s on cloud-sims, including fast-poll configurations.

Verification: Both probe files: confirm=3 with [true,false,true] returns ok=true after only two positive answers. The separate entry bypass is A02.

<a id="a04"></a>
## A04 High: The image labelled stage start is captured after the action has begun

Branches: main, cloud-sims. Confidence: High.

Evidence:

`harness/harness/runner.py:431`

```python
431:             if elapsed >= gate:
432:                 frames = self._frames_for_check(stage)
433:                 verdict = self._ask(verifier, stage, frames, elapsed)
434:                 self._last_verdict = verdict
435:                 saved = [p.name for p in self.log.save_frames(f"{stage.name}_a{attempt}", frames)]
```

`harness/harness/verifiers.py:455`

```python
455:             if role not in self._first:
456:                 self._first[role] = (elapsed_s, frames[role])
457:             else:
458:                 self._history[role] = (self._history.get(role, []) + [(elapsed_s, frames[role])])[-self.history_frames:]
459:         images += [(label(elapsed_s, r, "newest"), j) for r, j in wrists.items() if r not in tracked_roles]
```

`harness-cloud-sims/harness/verifiers.py:697`

```python
697:             if role not in self._first:
698:                 self._first[role] = (elapsed_s, frames[role])
699:             else:
700:                 self._history[role] = (self._history.get(role, []) + [(elapsed_s, frames[role])])[-self.history_frames:]
701:         images += [(label(elapsed_s, r, "newest"), j) for r, j in wrists.items() if r not in tracked_roles]
```

`harness/plans/hill_climb_v4.yaml:80`

```text
80:     success: "Compared with the stage-start overhead image, is at least one cable that was plugged into the hub at the start now fully out of its port (lying on the table or held in a gripper)?"
81:     expect: "The right gripper closes fully on the plug at the end of the hub nearest it and pulls it straight out along the port until the plug is clear."
82:     timeout_s: 60
```

Why it is wrong: For relative stages without an entry probe, the runner issues the prompt before asking the monitor. MonitorVerifier creates _first from the first eventual check, not a pre-action snapshot. With min_run_s=4 and a 3 s poll, unplug normally first checks at 6 s, and it can be later while a previous call occupies the monitor. This image is then repeatedly labelled stage start. The MuJoCo oracle also sets its baseline on first check.

Demo failure scenario: The fine-tune pulls the first cable out in under six seconds. Its first monitor image already shows that cable out. Every relative comparison now asks for an additional unplug, so the harness keeps pulling instead of moving to the put-down. Fast relative ball or higher stages have the same problem.

Suggested fix: Capture and timestamp a stage baseline before sending any motion prompt. Give both the monitor and oracle an explicit begin_stage baseline. Keep it across retries of that logical stage and reset it only on a genuine stage change.

Verification: Both probe files: a first build at elapsed=6 becomes _first=6 and the next payload labels it `t=+6s overhead (stage start)`.

<a id="a05"></a>
## A05 High: Main has no fast live guard, and cached images can keep its guard falsely fresh

Branches: main. Confidence: High for code; real pick-to-bin margin is unmeasured here.

Evidence:

`harness/harness/runner.py:420`

```python
420:                 local, why = self.fast_guard(self.frames.latest())
421:                 if isinstance(local, bool):
422:                     self._guard_answer_t = self._clock()
423:                 if local is True:
424:                     consec_guard += 1
425:                     self.log.event("guard_yes", stage=stage.name, attempt=attempt, count=consec_guard,
426:                                    question=guard.question, reason=why, source="local")
```

`harness/harness/frames.py:45`

```python
45:                 if n == self._cache_n and self._cache:
46:                     self.last_error = None
47:                     return dict(self._cache)
48:                 try:
49:                     trip = self._get_json(f"/trip/{n}")
```

`harness/harness/cli.py:39`

```python
39:     p.add_argument("--detect", default=None, metavar="COLORS.yaml",
40:                    help="local ball/block detector (e.g. plans/table_colors.yaml): fast black-ball guard + boxes on "
41:                         "the monitor's frames")
42:     p.add_argument("--verifier", choices=["manual", "claude", "time", "claude+manual", "monitor", "monitor+manual"], default=None,
```

`harness/plans/hill_climb_v4.yaml:55`

```text
55:   max_blind_s: 20
56: 
```

Why it is wrong: Main polls the local detector only every check_every_s and reads debug-view trips. There is no LiveGuard or --live-guard option on this branch. The detector is opt-in and the documented simple match command omits it. A cached, frozen clear wrist image can be rejudged on every poll and refresh _guard_answer_t; freshness is a time of judgment, not capture. The slow model guard cannot reliably beat pick-to-bin motion. Cloud-sims contains the independent live-frame stop loop that main lacks.

Demo failure scenario: A ball is picked and released between monitor replies. The newest debug trip still shows an empty gripper. Main either sees the illegal ball too late or keeps treating a frozen clear view as a fresh all-clear, so max_blind_s does not protect the run.

Suggested fix: Before selecting main for a ball-capable match, port the reviewed independent live-frame guard and motion interlocks, together with per-camera capture freshness. Treat stale guard inputs as unknown, and verify the detector on station wrist frames. Account for A06 and A07 as well.

Verification: The handoff reports illegal-ball losses with lagged debug frames. head_diff_main_to_start-pose-live-guard.txt contains the unmerged guard implementation.

<a id="a06"></a>
## A06 High: A healthy wrist camera masks a missing camera on the carrying arm

Branches: main, cloud-sims. Confidence: High.

Evidence:

`harness-cloud-sims/harness/detect.py:342`

```python
342:         looked = False
343:         avoid = [c for c in self.cfg.classes if "avoid" in c.kinds.values()]   # only the colours that can trip it
344:         for role, jpeg in frames.items():
345:             if not any(w in role for w in self.cfg.wrist_roles):
346:                 continue
347:             im = _load(jpeg, WORK_W)
348:             if im is None:
349:                 continue
350:             looked = True
351:             b = held(detect_image(im, self.cfg, classes=avoid), self.cfg)
352:             if b is not None:
353:                 return True, f"local detector: {b.cls} between the fingers in {role} ({b.frac:.1%} of the view)"
354:         return (False, "local detector: none held") if looked else (None, "local detector: no wrist view")
355: 
356:     def annotate_frames(self, frames: dict[str, bytes]) -> dict[str, bytes]:
```

`harness-cloud-sims/harness/live_guard.py:165`

```python
165:                 self._sig[role] = sig
166:                 if answer is None:
167:                     continue
168:                 self.last_answer_t = self._clock()
169:                 if answer is False:
170:                     self._count[role] = 0
171:                     continue
```

`harness-cloud-sims/harness/runner.py:584`

```python
584:                     return _Attempt(False, "fast loop: " + ("failing" if fd.get("fail") else "stalled"), max_p, hint)
585:             if guard is not None and self.live_guard is not None and self.live_guard.last_answer_t is not None:
586:                 self._guard_answer_t = max(self._guard_answer_t or 0.0, self.live_guard.last_answer_t)
```

Why it is wrong: TableDetector.guard returns False once any readable wrist shows no avoid ball, even if the other wrist is absent. LiveGuard and Runner use one shared last-answer time. LiveFrames keeps trying missing cameras, but a stream from the other arm continuously satisfies the blind timer. Runtime health checks do not enforce freshness for both cameras. Main has the same any-wrist detector and shared blind timer.

Demo failure scenario: The right wrist stream freezes or disappears during a pick. The left wrist keeps changing and shows empty space. The guard remains apparently healthy indefinitely while the right arm can carry a white ball into the bin.

Suggested fix: Track required wrist coverage and capture age per camera. Stop or hold the ball stage when the carrying arm is unobserved. A clear view from another arm must not clear its blind timer. Recheck required camera presence during the run, not just values returned by preflight.

Verification: raw/probes_cloud.json: after 30 s with wrist_l only, last_answer_t=30, tripped=null, roles_seen=[wrist_l].

<a id="a07"></a>
## A07 High: The white-ball guard misses neutral white by construction

Branches: main, cloud-sims. Confidence: High for the counterexample; medium for prevalence on the live cameras.

Evidence:

`harness/plans/table_colors.yaml:21`

```text
21:   white:  {hsv: [[135, 22, 225], [168, 70, 255]], min_frac: 0.0001, max_frac: 0.25, kinds: {ball: avoid}, small_ball: true}
22:   black:  {hsv: [[0, 0, 0], [255, 255, 55]], min_frac: 0.002, max_frac: 0.25, kinds: {ball: avoid}}
23:   green:  {hsv: [[72, 60, 120], [110, 255, 255]], min_frac: 0.0001, kinds: {ball: target}, small_ball: true}
```

`harness-cloud-sims/plans/table_colors.yaml:21`

```text
21:   white:  {hsv: [[135, 22, 225], [168, 70, 255]], min_frac: 0.0001, max_frac: 0.25, kinds: {ball: avoid}, small_ball: true}
22:   black:  {hsv: [[0, 0, 0], [255, 255, 55]], min_frac: 0.002, max_frac: 0.25, kinds: {ball: avoid}}
23:   green:  {hsv: [[72, 60, 120], [110, 255, 255]], min_frac: 0.0001, kinds: {ball: target}, small_ball: true}
```

`harness-cloud-sims/harness/detect.py:153`

```python
153: def _in_range(hsv: np.ndarray, lo: tuple[int, int, int], hi: tuple[int, int, int]) -> np.ndarray:
154:     h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
155:     hue = (h >= lo[0]) & (h <= hi[0]) if lo[0] <= hi[0] else (h >= lo[0]) | (h <= hi[0])   # red wraps around
156:     return hue & (s >= lo[1]) & (s <= hi[1]) & (v >= lo[2]) & (v <= hi[2])
```

Why it is wrong: The white class requires hue 135..168 and saturation at least 22 on PIL's 0..255 HSV scale. Neutral white has saturation near zero and fails the mask even when large, round, and centred between the fingers. These values were tuned on a tinted phone photo, not a demonstrated range of station wrist lighting. This is a reproducible coverage hole, not a measured claim about the current station failure rate.

Demo failure scenario: Auto white balance makes the held white ball look neutral rather than blue-tinted. The fast guard answers False. The cloud live guard repeats that answer at 10 Hz, while the slower visual monitor may only notice after release.

Suggested fix: Calibrate on actual held-ball wrist images under the station lighting, include low-saturation white, and use shape, position, gripper state, and bin/background rejection to control false alarms. Require held-white and held-black negative/positive examples before relying on the fast guard.

Verification: Both probe files: a generated round RGB(250,250,250) ball centred in grip_roi returns [false, "local detector: none held"].

<a id="a08"></a>
## A08 High: Fine-tune defaults conflict with runtime prompt generation and the prompt audit misses it

Branches: main, cloud-sims. Confidence: High for configuration/label drift; model performance effect requires a station test.

Evidence:

`harness/plans/hill_climb_v4.yaml:71`

```text
71: tower_prompts:
72:   cube: "put a block on top of the stack"   # the recorded text, byte for byte
73:   stick: "stand a stick upright on top of the stack"
```

`harness/harness/tasks.py:35`

```python
35: def plan_prompts(plan: Plan) -> dict[str, list[str]]:
36:     """Every prompt the plan can send -> where it is used ("stage" or "stage/recovery"), in plan order."""
37:     out: dict[str, list[str]] = {}
38:     for stage in plan.stages:
39:         out.setdefault(stage.prompt, []).append(stage.name)
40:         for rec in stage.menu():
41:             if rec.prompt:   # kind retry re-sends the stage prompt
42:                 out.setdefault(rec.prompt, []).append(f"{stage.name}/{rec.name}")
43:     return out
44: 
45: 
```

`harness-cloud-sims/plans/hybrid_v4.yaml:37`

```text
37: prompt_technique: true
38: verifier: monitor
39: confirm: 2
```

`harness-cloud-sims/plans/hybrid_v4.yaml:54`

```text
54: rewrite_prompts: true
55: prompt_prefix: "The power strip is unplugged from the wall and has no power; the orange cords are insulated low-voltage USB cables, not bare wires."
```

`harness/TELEOP_PLAN.md:245`

```text
245:    each model (`plans/hill_climb_v4.yaml`, same `--var model=...`) show whether it stacks as well as base Dream-1.
246:    If the fine-tune wins the cable but stacks worse, a per-stage model is a harness change to make then. If stacking
247:    runs on the fine-tune, delete `tower_prompts` from the plan (the fine-tune only knows "put a block on top of the
248:    stack").
```

Why it is wrong: Both v4 plans default to relay-final but still generate `stand a stick upright on top of the stack` via tower_prompts. Main's final training notes explicitly say to delete tower_prompts when using the fine-tune; the checked split labels use cylinder wording, not that stick text. plan_prompts enumerates only stage and recovery prompts, so its apparent coverage check misses the dynamic string. Cloud hybrid now also defaults to the fine-tune while keeping free-text rewrites, technique appending, and a new white-ball push stage. This violates the project's documented exact-label contract. It does not prove that the model cannot understand natural language.

Demo failure scenario: The team selects the new default checkpoint assuming the documented fine-tune-ready plan. At the stick stage, or immediately on hybrid cable stages, the harness sends texts outside the recorded labels. Behavior regresses during the match despite a passing static prompt audit.

Suggested fix: Make model mode explicit and reject incompatible rewrites, technique appending, and generated prompts for the recorded-text mode. Enumerate every possible runtime template in the prompt audit. Use the actual recorded cylinder label after checking its intended action. For a base-model hybrid run, explicitly override model=dreamlabs/dream-1-beta.

Verification: raw/probes_main.json and probes_cloud.json show the same checkpoint and generated stick prompt. raw/more_probes.json shows hybrid_ft_defaults with rewrite_prompts=true and prompt_technique=true.

<a id="a09"></a>
## A09 High: Blocking I/O can carry motion far beyond the match deadline

Branches: main, cloud-sims. Confidence: High from blocking call paths; no live delay was induced.

Evidence:

`harness/harness/station_client.py:31`

```python
31:         timeout_s: float = 70.0,
32:         dry_run: bool = False,
33:         log: Callable[[str], None] | None = None,
34:     ):
```

`harness-cloud-sims/harness/station_client.py:31`

```python
31:         timeout_s: float = 70.0,
32:         dry_run: bool = False,
33:         log: Callable[[str], None] | None = None,
34:     ):
```

`harness-cloud-sims/harness/verifiers.py:789`

```python
789:     def check_now(self, stage: Stage, frames: dict[str, bytes], elapsed_s: float) -> Verdict:
790:         """Synchronous: waits for any call in flight, then asks once and returns that answer."""
791:         self.wait_idle(self.plan.claude_user.get("timeout_s", 60.0))
792:         with self._lock:
793:             payload = self._build(stage, frames, elapsed_s)
794:             self._result = None
795:         if payload is None:
796:             return Verdict(None, 0.0, "no frames available")
797:         return self._call(payload, len(stage.menu()))
798: 
```

`harness-cloud-sims/harness/runner.py:372`

```python
372:             self._motion(self.station.start, prompt, self.plan.checkpoint)
373:             left = self.plan.time_budget_s - (self._clock() - self._t0)
374:             st = self._wait({"running", "error"}, max(5.0, min(START_TIMEOUT_S, left)), "running")
```

Why it is wrong: The 295 s deadline is checked between operations. Status, /arms, motion requests, synchronous entry/precondition checks, and monitor joins are not bounded by the remaining match budget. A station request can take 70 s; check_now can join multiple 60 s calls and then make another call. Frames passed into it were captured before the wait. The five-second margin cannot cover these waits. Cloud _motion checks before sending, but cannot interrupt a blocked call; main also lacks the before-set_prompt deadline check.

Demo failure scenario: At 292 s, the next entry check waits for a slow monitor call, or /status stalls. Dream continues its last action while the harness cannot check the deadline or send its normal stop until well after the referee cutoff. A normal operator stop also waits behind synchronous I/O.

Suggested fix: Use one monotonic work deadline and a reserved stop interval. Bound every request and synchronous wait by min(remaining, short_request_cap), check again after each call, and obtain fresh frames after waiting. Keep a deadline/stop path independent of model and camera I/O. The review-fixes branch has _in_budget and a five-second request cap worth porting selectively.

Verification: Both probe files simulate a synchronous entry check crossing the budget. Main sends a new prompt at t=61 despite a 60 s budget; cloud blocks that particular late send but still waits to t=61. raw/head_diff_main_to_codex__hill-climb-review-fixes.txt contains request caps and stop reserve absent in both candidates.

<a id="a10"></a>
## A10 High: --match accepts timer-only verification and removal of pass-bar stages

Branches: cloud-sims. Confidence: High.

Evidence:

`harness-cloud-sims/harness/cli.py:81`

```python
81: def match_problems(args: argparse.Namespace, plan) -> list[str]:
82:     """Why these args and this plan can't run as a match (empty list = fine). Any human input after go is a DQ."""
83:     problems = []
84:     if args.budget:
85:         problems.append("--budget: a match runs on the plan's own time_budget_s")
86:     if args.verifier in MANUAL_VERIFIERS:
87:         problems.append(f"--verifier {args.verifier}: a typed y/n is human input")
88:     manual = sorted({st.name for st in plan.stages if st.verifier in MANUAL_VERIFIERS})
89:     if manual and args.verifier not in ("monitor", "claude", "time"):
90:         problems.append(f"stages answered by a person: {', '.join(manual)} (use --verifier monitor)")
91:     return problems
92: 
93: 
94: def main(argv: list[str] | None = None) -> int:
```

`harness-cloud-sims/harness/cli.py:112`

```python
112:     if args.skip:
113:         plan.stages = [st for st in plan.stages if not any(st.name.startswith(x) for x in args.skip)]
114:         if not plan.stages:
115:             print("plan error: --skip removed every stage", file=sys.stderr)
116:             return 2
117:     if args.budget:
118:         plan.time_budget_s = args.budget
```

`harness-cloud-sims/harness/verifiers.py:800`

```python
800: class TimeVerifier:
801:     """For stages with no real check: done once 90% of the stage timeout has passed."""
802: 
803:     def check(self, stage: Stage, frames: dict[str, bytes], elapsed_s: float) -> Verdict:
804:         if elapsed_s >= stage.timeout_s * 0.9:
805:             return Verdict(True, 0.5, "time elapsed", immediate=True)
806:         return Verdict(None, 0.0, "waiting")
807: 
```

Why it is wrong: match_problems rejects manual verifiers but explicitly treats time as an allowed override. TimeVerifier declares success solely after 90% of the timeout. The CLI removes --skip stages before match validation, and validation does not require cable and tower gates to remain or require dependencies for all ball stages. --match therefore does not enforce the DQ rules it is advertised to check.

Demo failure scenario: A practice invocation retains --verifier time or --skip unplug/stack/tower while adding --match for the demo. The harness accepts it and issues block or ball prompts without visual proof of prerequisites. A failed cable action becomes a match-losing DQ.

Suggested fix: Reject time as well as human verifiers in match mode. Validate a required pass-bar dependency graph after filtering, prohibit skipping those gates, and require guarded ball stages to depend on the verified tower gate. Keep practice overrides available only outside match mode.

Verification: raw/more_probes.json: match_accepts_time_and_removed_gates has problems=[] while unplug, stack_, and tower_pass are absent.

<a id="a11"></a>
## A11 High: Inference restarts retain old monitor results and success evidence

Branches: main, cloud-sims. Confidence: High.

Evidence:

`harness/harness/runner.py:245`

```python
245:     def _poll_health(self) -> dict[str, Any]:
246:         self._check_global()
247:         st = self.station.status()
248:         if st.get("state") == "error":
249:             self._on_station_error(st.get("last_error"))   # raises unless a restart is allowed
250:             return self._start_run(self._prompt)
251:         dead = [a for a, alive in (st.get("arms") or {}).items() if not alive]
252:         if dead:
253:             raise _Fail(f"arm(s) not alive: {', '.join(dead)}")
254:         return st
```

`harness-cloud-sims/harness/runner.py:299`

```python
299:     def _poll_health(self) -> dict[str, Any]:
300:         self._check_global()
301:         st = self.station.status()
302:         if st.get("state") == "error":
303:             self._on_station_error(st.get("last_error"))   # raises unless a restart is allowed
304:             self._maybe_reset(st.get("last_error"))
305:             self._nap(ARCHIVE_WAIT_S)
306:             return self._start_run(self._prompt)
307:         dead = [a for a, alive in (st.get("arms") or {}).items() if not alive]
308:         if dead:
309:             raise _Fail(f"arm(s) not alive: {', '.join(dead)}")
```

`harness-cloud-sims/harness/verifiers.py:584`

```python
584:     def notify_prompt_change(self, prompt: str) -> None:
585:         with self._lock:
586:             self._last_prompt = prompt
587:             self._gen += 1
588:             self._result = None
589: 
```

Why it is wrong: Normal _issue invalidates monitor generations, but _poll_health directly calls _start_run after a transient error. _start_run does not notify verifiers. The current _watch keeps last_done_t, signatures, and counters across that restart too. Cloud can additionally /reset after an IK failure, moving the arms and changing the scene, while still accepting an answer computed before the reset.

Demo failure scenario: One done verdict or an in-flight image was taken before an SVD failure. A reset releases the object or knocks the tower, the station restarts, and the old positive is consumed as current evidence. The next stage can be reached without a new confirmed physical success.

Suggested fix: Treat every start/reset/recovery that changes execution continuity as a new observation epoch. Invalidate pending monitor results, clear success/stall counters, restart the settle gate, and require post-restart capture timestamps before counting success.

Verification: raw/more_probes.json: restart_generation remains 0 before and after restart and old_result_still_available=true.

<a id="a12"></a>
## A12 High: The refusal restart limit does not stop stage retries from resending the same unsafe instruction

Branches: main, cloud-sims. Confidence: High.

Evidence:

`harness/harness/runner.py:275`

```python
275:         if kind == "refusal":
276:             n = self._refusals[self._prompt] = self._refusals.get(self._prompt, 0) + 1
277:             if n > REFUSAL_RESTARTS:
278:                 self.log.event("prompt_refused", prompt=self._prompt, refusals=n, error=_err(error))
279:                 raise _Refused(f"Dream refused the prompt ({n}x): {_err(error)}")
280:         elif kind == "transient" and self._restarts < MAX_STATION_RESTARTS:
281:             self._restarts += 1   # one gateway timeout must not end a 5-minute match
282:         else:
```

`harness-cloud-sims/harness/runner.py:356`

```python
356:         if kind == "refusal":
357:             n = self._refusals[self._prompt] = self._refusals.get(self._prompt, 0) + 1
358:             if n > REFUSAL_RESTARTS:
359:                 self.log.event("prompt_refused", prompt=self._prompt, refusals=n, error=_err(error))
360:                 raise _Refused(f"Dream refused the prompt ({n}x): {_err(error)}")
361:         elif kind == "transient" and self._restarts < MAX_STATION_RESTARTS:
362:             self._restarts += 1   # one gateway timeout must not end a 5-minute match
363:         else:
```

`harness/plans/hill_climb_v4.yaml:83`

```text
83:     max_attempts: 99    # pass bar: nothing later may be touched, so keep trying until the clock runs out
84:     min_run_s: 4
```

Why it is wrong: REFUSAL_RESTARTS=1 ends an attempt, not the logical prompt. The retry loop then sends the identical prompt before checking the accumulated refusal count again. Even though the counter increases, there is no quarantine before the next /start. Required stages retry up to 99 attempts. Refusals can describe a human hand in view or an unsafe scene, not a transient server failure.

Demo failure scenario: Dream refuses unplug because a person is holding the plug. The harness sends that same request repeatedly through recoveries and retries, consuming the clock and reattempting motion if a later answer changes. The documented one-restart policy does not bound these sends.

Suggested fix: Enforce a prompt/scene refusal budget before issuing a command. After a repeated refusal, safely stop a required gate or skip an optional task; retry only after a verified scene change or a truthful, separately allowed prompt revision. Do not treat a safety refusal as an inference outage.

Verification: Both probe files: max_attempts=3 sends four /start requests with the same refused prompt; final reason records four refusals.

<a id="a13"></a>
## A13 High: Failed cable-release steps can lead directly into stacking with an occupied gripper

Branches: main, cloud-sims. Confidence: High for missing precondition; exact robot behavior is model-dependent.

Evidence:

`harness/plans/hill_climb_v4.yaml:109`

```text
109:   - name: release_cable
110:     prompt: "open both grippers and move the arms up"
111:     success: "Are both grippers open and empty, with no cable held?"
112:     timeout_s: 10
113:     min_run_s: 2
114:     confirm: 1
115:     on_fail: skip
116: 
117:   # One extra unplug, +5. Same prompt as unplug_cable: once the end port is empty, the end cable nearest the right arm
118:   # is the next one along (TELEOP_PLAN: record some T1 with the end port already empty). Then put that cable down too.
119:   - name: unplug_more
120:     prompt: "unplug the USB cable at the end of the hub nearest the right arm"
121:     success: "Compared with the stage-start overhead image, is one more cable now fully out of the hub than at the start of this stage?"
```

`harness/plans/hill_climb_v4.yaml:144`

```text
144:   - name: stack_2
145:     prompt: "put a block on top of the stack"
146:     success: "Is one block resting on top of another block, standing on its own, with no gripper touching either?"
147:     expect: "An arm picks up a block, sets it squarely on another block, opens and lifts clear."
148:     camera_notes:
149:       "{stack_cam}": "view from the gripper doing the stacking"
150:     timeout_s: 40
151:     max_attempts: 99    # pass bar: nothing later may be touched, so keep trying until the clock runs out
152:     min_run_s: 4
153:     stall_s: 20
```

`harness-cloud-sims/harness/runner.py:1049`

```python
1049:             if stage.on_fail == "skip":
1050:                 self.log.event("stage_skipped", stage=stage.name, reason=reason)
1051:                 return StageResult(stage.name, False, attempts, self._clock() - t0, reason)
1052:             while True:   # choose a recovery; a reset that leaves the scene broken burns an attempt
```

Why it is wrong: Putting the cable down and opening/raising the arms are optional skip stages. After these fail, neither candidate establishes empty grippers before later cable work or stack_2. The stack prompt contains only the block task. The review-fixes/sim-table branches added an explicit drop-held-cable instruction; that safeguard is absent from the candidate v4 plans. Skipping unsuccessful housekeeping is useful only if the next action has a safe start state.

Demo failure scenario: The policy successfully unplugs but keeps gripping the cable, a failure seen throughout the supplied simulation runs. Release checks time out, then the harness tells the occupied gripper to pick up a block. It wastes the stack attempts or drags the cable/strip through later-stage objects.

Suggested fix: Before stacking, require an observed empty, clear work gripper and run a bounded recorded-text release action when needed. If it cannot clear, stop without risking later-stage contact. Port the other branch's intent while preserving the fine-tune label contract rather than blindly appending free text.

Verification: raw/head_diff_main_to_codex__hill-climb-review-fixes.txt and head_diff_main_to_codex__hill-climb-sim-table.txt contain the held-cable-first stack prompt. The handoff reports 0/54 simulated put-down tries.

<a id="a14"></a>
## A14 High: A three-block tower is treated as a mandatory gate even when two blocks already pass

Branches: main, cloud-sims. Confidence: High for shipped scenes and plan logic; conditional on live block height.

Evidence:

`harness/plans/hill_climb_v4.yaml:160`

```text
160:   - name: stack_3
161:     prompt: "put a block on top of the stack"
162:     success: "Is the tower three blocks tall, standing on its own, with no gripper touching it?"
163:     expect: "An arm picks up a third block and places it on the two-block tower without knocking it, opens and lifts clear."
164:     camera_notes:
165:       "{stack_cam}": "view from the gripper doing the stacking"
166:     timeout_s: 40
167:     max_attempts: 99    # pass bar: nothing later may be touched, so keep trying until the clock runs out
168:     min_run_s: 4
169:     stall_s: 20
170:     recovery:
171:       - {name: keep_going, kind: retry, when: "the tower is standing and a block is held or near its top", max_uses: 3}
172:       - {name: let_go_and_lift, kind: reset, prompt: "open both grippers and move the arms up",
173:          when: "the gripper is stuck on a block or pressing on the tower", settle_s: 5, max_uses: 2}
174:     on_fail: retry_in_place
175: 
176:   - name: tower_pass
```

`harness-cloud-sims/plans/hill_climb_v4.yaml:182`

```text
182:   - name: stack_3
183:     prompt: "put a block on top of the stack"
184:     success: "Is the tower three blocks tall, standing on its own, with no gripper touching it?"
185:     expect: "An arm picks up a third block and places it on the two-block tower without knocking it, opens and lifts clear."
186:     camera_notes:
187:       "{stack_cam}": "view from the gripper doing the stacking"
188:     timeout_s: 40
189:     max_attempts: 99    # pass bar: nothing later may be touched, so keep trying until the clock runs out
190:     min_run_s: 4
191:     stall_s: 20
192:     recovery:
193:       - {name: keep_going, kind: retry, when: "the tower is standing and a block is held or near its top", max_uses: 3}
194:       - {name: let_go_and_lift, kind: reset, prompt: "open both grippers and move the arms up",
195:          when: "the gripper is stuck on a block or pressing on the tower", settle_s: 5, max_uses: 2}
196:     on_fail: retry_in_place
197: 
198:   - name: tower_pass
```

`harness-cloud-sims/tests/test_hill_climb_scene.py:60`

```python
60:     # Stage 2: two cubes stacked is 12 cm, over the 4-inch bar; free-standing for 3 s.
61:     place(env, "cube_1", (*h.CUBES[0], h.TABLE_Z + 3 * h.CUBE_HALF + 0.001))   # on top of cube_0
62:     settle(env, ref, h.FREE_STANDING_S + 1)
63:     assert ref.stage2_at is not None
64:     # Stage 3: two golf balls in the bin.
```

Why it is wrong: The actual pass bar is a stable tower over four inches, not exactly three cubes. Both plans must complete stack_3 before checking tower_pass, and stack_3 retries to the clock. In the shipped scenes, two 6 cm cubes make 12 cm, already over 10.16 cm; the scene test explicitly confirms that this passes. Real block dimensions have not been measured in the handoff, so a fixed cube-count prerequisite is unjustified there too.

Demo failure scenario: A valid two-block tower stands, but the third-block pickup repeatedly fails. The harness spends the rest of the 295 s on an unnecessary required stage, may knock over the existing tower, and never reaches the balls.

Suggested fix: Check the actual height/release/stability pass bar after each successful stack increment. Treat extra pieces as optional scoring work once it passes, with a bounded allocation for balls. Measure the real blocks and calibrate visual height against a reference.

<a id="a15"></a>
## A15 High: The simulated tower gate does not establish a supported stable tower

Branches: main, cloud-sims, task_simulation. Confidence: High.

Evidence:

`task_simulation/sim_scenes/hill_climb_table.py:196`

```python
196:     def _tower_m(self, data: Any) -> float:
197:         top = TABLE_Z
198:         for b in self.groups[2]:
199:             # A block's highest point, from its geom's world AABB (cheap and orientation-proof).
200:             gid = self._geom_of(b)
201:             half = self._half_height(data, gid)
202:             top = max(top, float(data.geom_xpos[gid][2]) + half)
203:         return top - TABLE_Z
204: 
```

`harness-cloud-sims/sim_scenes/hill_climb_table.py:370`

```python
370:         if (self.stage1_at is not None and self.stage2_at is None and self._tower_m(data) >= TOWER_BAR_M
371:                 and self._t - self._last_block_touch >= FREE_STANDING_S):
372:             self.stage2_at = self._t
373: 
```

`harness-cloud-sims/experiments/sim_match.py:97`

```python
97:         elif name == "tower_pass":
98:             done = tower > h.TOWER_BAR_M and self._tower_clear(snap)
99:         elif name.startswith("higher_"):
```

Why it is wrong: The referee uses the highest AABB point of any block, then checks only time since a listed gripper touched a block. It does not require a block-on-block support chain, a table base, low velocity, or separation from other robot links. The oracle is weaker still: height plus open/distant grippers, without requiring referee.stage2_at or a three-second stability interval. An entry shortcut can therefore pass the harness tower gate while the referee has not passed it. Height alone is not the claimed task success.

Demo failure scenario: A cube rests on the basket or a forearm, or a just-released tower is still wobbling. The oracle says tower_pass is met; subsequent ball contact can be DQ in the referee. Even where the referee accepts the scene, the result does not demonstrate a valid tower on the station.

Suggested fix: Derive tower success from a supported stack rooted on the table, measure stability after full release for three seconds, and make the oracle consume that same gate. Add counterexamples for a supported single block on the bin, airborne/held blocks, forearm support, and a collapsing stack.

Verification: raw/more_probes.json: a single block 0.2 m above table on unspecified support is reported as tower height 0.23 m. This is a method-level proof; no MuJoCo solve was run.

<a id="a16"></a>
## A16 Medium: Cumulative ball targets carry missed stages forward as extra work

Branches: main, cloud-sims. Confidence: High for target drift; time/score impact depends on placement rate.

Evidence:

`harness/plans/hill_climb_v4.yaml:219`

```text
219:     name: ball_2
220:     success: "Are at least 2 golf balls resting inside the bin, with no white or black ball in the bin and the tower still standing?"
221:   - <<: *ball
222:     name: ball_3
223:     success: "Are at least 3 golf balls resting inside the bin, with no white or black ball in the bin and the tower still standing?"
```

`harness-cloud-sims/plans/hill_climb_v4.yaml:241`

```text
241:     name: ball_2
242:     success: "Are at least 2 golf balls resting inside the bin, with no white or black ball in the bin and the tower still standing?"
243:   - <<: *ball
244:     name: ball_3
245:     success: "Are at least 3 golf balls resting inside the bin, with no white or black ball in the bin and the tower still standing?"
```

`harness-cloud-sims/plans/hybrid_v4.yaml:268`

```text
268:   # Each ball stage asks for ONE more ball than at its own start (8 Nov run 2: with cumulative counts, a missed first
269:   # ball made every later stage impossible). 40 s each: 25 s cut zero-shot picks short.
270:   - &ball
271:     name: ball_1
```

Why it is wrong: Both candidate v4 plans still ask for absolute totals 1,2,...,8. If an early ball stage times out without a ball, a later short attempt needs two or more placements to catch up. The underlying scoring is independent per legal ball. The hybrid branch already fixes this by asking for one more than its own stage-start count; the review-fixes branch also uses independent ball stages. That fix was not applied to v4.

Demo failure scenario: ball_1 fails. ball_2 places one valid ball, earning five points, but the monitor must say incomplete because the plan demands two. Further time is spent chasing a historical target rather than treating that placement as one completed attempt.

Suggested fix: Make each ball attempt incremental from a pre-action baseline and log the actual total separately. Fix A04 before switching to relative checks, so fast successes are not erased by a late baseline.

<a id="a17"></a>
## A17 Medium: The older MuJoCo table treats white balls as legal and has a different object inventory

Branches: main, task_simulation. Confidence: High for source mismatch; historical result provenance varies by run.

Evidence:

`task_simulation/sim_scenes/hill_climb_table.py:7`

```python
7:   Stage 1  a 4-port USB hub (ports on top) with 2 cables plugged in and 2 loose cables on the table
8:   Stage 2  10 foam blocks: 6 cubes of 6 cm and 4 cylinders of 2.5 cm x 10 cm (one alone is under 4 in)
9:   Stage 3  6 white golf balls, 2 black pool 8-balls and a wire bin by the right arm
```

`task_simulation/sim_scenes/hill_climb_table.py:263`

```python
263:             points += 20 + 3 * int(tower / INCH)
264:         points += 5 * golf
265:         return {
266:             "t": round(self._t, 2), "points": 0 if self.dq else points, "dq": self.dq,
267:             "loses": bool(self.dq or eight), "unplugged": unplugged, "seated": seated,
```

`harness-cloud-sims/sim_scenes/hill_climb_table.py:78`

```python
78: GOLF_RGBA = (".15 .7 .3 1", ".9 .9 .15 1", "1 .45 .05 1")       # green, yellow, orange
79: EIGHT = ((0.34, -0.12),) + tuple(((0.08 + 0.06 * i, -0.30) for i in range(int(os.environ.get("SIM_BLACK_8BALLS", "0")))))
80: ILLEGAL = ((GOLF_R, "0.0459", ".97 .97 .97 1"),) + tuple(
81:     (EIGHT_R, "0.16", ".03 .03 .03 1") for _ in range(len(EIGHT) - 1))   # eight_0 white golf ball, then black 8-balls
82: 
```

Why it is wrong: task_simulation and main ship the older six-white-golf-ball scene, where golf_* balls score and only eight_* balls lose. The supplied station decision makes white illegal and coloured balls legal. Cloud-sims has three coloured legal balls and a white eight_0, plus the three-cube/two-stick table instead of six cubes/four cylinders. Old result sets therefore do not validate legal-ball selection, the new inventory, or achievable heights on the station. The simulator also uses YAM arms, while the station uses Metal.

Demo failure scenario: The team chooses main based on a high simulated score obtained by binning white balls or stacking extra pieces unavailable on the real table. The same selection on the demo table can lose the match, and the extra-piece strategy can run out of objects.

Suggested fix: Use one versioned station specification and legality mapping across all scenes, plans, detectors, and result manifests. Re-label historical results by scene/version and do not pool them with station-matched evaluations. Treat arm-embodiment transfer as unverified.

<a id="a18"></a>
## A18 Medium: The bin success check counts objects below the table and does not require rest/release

Branches: main, cloud-sims, task_simulation. Confidence: High.

Evidence:

`task_simulation/sim_scenes/hill_climb_table.py:219`

```python
219:     def _in_bin(self, data: Any, prefix: str) -> int:
220:         bx, by, bh = BIN_HALF
221:         count = 0
222:         for name in self.id:
223:             if name.startswith(prefix):
224:                 x, y, z = self._pos(data, name)
225:                 if abs(x - BIN_POS[0]) < bx and abs(y - BIN_POS[1]) < by and z < TABLE_Z + 2 * bh:
226:                     count += 1
227:         return count
```

`harness-cloud-sims/sim_scenes/hill_climb_table.py:339`

```python
339:     def _in_bin(self, data: Any, prefix: str) -> int:
340:         bx, by, bh = BIN_HALF
341:         count = 0
342:         for name in self.id:
343:             if name.startswith(prefix) and "_" in name and name.split("_", 1)[1].isdigit():
344:                 x, y, z = self._pos(data, name)
345:                 if abs(x - BIN_POS[0]) < bx and abs(y - BIN_POS[1]) < by and z < TABLE_Z + 2 * bh:
346:                     count += 1
347:         return count
```

Why it is wrong: The referee checks x/y inside bin bounds and only an upper z bound. There is no lower floor bound, containment of the whole sphere, low velocity, or release test. An object under the table with the same x/y is counted, and a held or fast-moving object below the rim can count before a valid deposit.

Demo failure scenario: A ball falls through/outside a basket geometry but keeps x/y under the opening, or is briefly carried through the bin. The simulator awards five points and the oracle can end the ball stage even though no ball is resting in the bin.

Suggested fix: Require the ball volume to be inside the bin floor/walls, released and resting for a short dwell. Check the lower z bound and exclude attached/held objects. Add under-table, rim-crossing, and bouncing-ball counterexamples.

Verification: raw/more_probes.json: a golf ball placed one metre below TABLE_Z at bin x/y returns underground_ball_in_bin=1.

<a id="a19"></a>
## A19 Medium: A losing hedged judge reply can contain the only illegal-ball alert and be discarded

Branches: cloud-sims, hedged-calls, mac-arm64. Confidence: High.

Evidence:

`harness-cloud-sims/harness/verifiers.py:727`

```python
727:             res = first_of(lambda: self._call_once(payload, menu_len), self.hedge,
728:                            timeout_s=float(self.plan.claude_user.get("timeout_s", 60.0)),
729:                            good=lambda v: not v.reason.startswith("monitor error"))
730:             verdict = res.value if res.value is not None else MonitorVerdict(
731:                 None, 0.0, f"monitor error: all {self.hedge} hedged calls failed ({'; '.join(res.errors)[:200]})")
732:             if res.winner is not None:
733:                 self.hedge_wins.append(res.winner)
734:         else:
```

`harness-cloud-sims/harness/verifiers.py:755`

```python
755:             if isinstance(verdict.guard, bool):
756:                 self.guard_answers += 1
757:             if gen == self._gen and seq > self._result_seq:
758:                 self._result, self._result_seq = verdict, seq
759:             elif verdict.guard is True:
760:                 self._guard_alert = True   # dropped (stale, or older than a delivered one), but a yes still counts
761:             self._inflight -= 1
```

`harness-cloud-sims/harness/hedge.py:63`

```python
63:             elif not ok and state["fallback"] is None:
64:                 state["fallback"] = v
65:             if ok and state["winner"] is None:
66:                 state["winner"], state["value"] = k, v
67:             cond.notify_all()
```

Why it is wrong: With claude.hedge>1, _call exposes only the first parseable reply. Only that reply reaches _run_async and its guard accounting. A later losing reply that correctly says guard=true never reaches the out-of-band safety latch. This differs from ordinary stale asynchronous results, whose guard=true is deliberately preserved. The default v4 plan has hedge=1, so this is an opt-in risk.

Demo failure scenario: The fastest model copy misses a white ball and answers guard=false. A slower copy sees the illegal grip, but its yes is silently discarded while the robot continues to the bin. Enabling hedging to reduce latency has removed an available safety signal.

Suggested fix: Use first-result-wins for progress only. Route every valid guard=true from every copy to a shared alert latch, including late losers and cross-model races, while retaining bounded request/cancellation behavior.

Verification: raw/more_probes.json: both fake copies finish, the losing copy says illegal held, but winning_guard=false, guard_alert=false, guard_answers=1.

<a id="a20"></a>
## A20 Medium: The oracle cannot evaluate the newly inserted ball_push_white stage

Branches: cloud-sims. Confidence: High.

Evidence:

`harness-cloud-sims/plans/operator_notes.yaml:101`

```text
101:     - name: ball_push_white   # ball_ prefix: the black/white-ball guard covers it
102:       prompt: "push the white ball off the edge of the table, away from the bin and the tower"
103:       success: "Is the white ball off the table top (it has fallen off the edge)?"
104:       skip_unless: "Is the white ball on the table with a clear path to the nearest table edge that does not pass the tower, the bin or any coloured golf ball?"
105:       expect: "One arm comes down beside the white ball and sweeps it in a straight line to the table edge and off, without picking it up."
106:       technique: "Use the side of the open gripper to push; do not pick the ball up."
107:       watch_for: "the gripper closes on the white ball; the ball rolls toward the bin or the tower; the arm sweeps through other balls."
108:       timeout_s: 40
109:       max_attempts: 1
110:       min_run_s: 4
```

`harness-cloud-sims/experiments/sim_match.py:101`

```python
101:         elif name.startswith("ball_"):
102:             relative = "one more" in stage.success.lower()
103:             need = base["golf_in_bin"] + 1 if relative else int(name.split("_")[1])
104:             done = ref["golf_in_bin"] >= need and not ref["eight_in_bin"]
105:         else:
```

Why it is wrong: The default hybrid plan inserts ball_push_white. OracleVerifier routes every ball_* name to int(name.split("_")[1]) unless it is a relative-count question. The push stage asks whether white is off the table, so this evaluates int("push") and raises ValueError. Runner._ask turns it into an unknown verdict, which masks the exception as repeated non-completion. Its skip_unless probe also checks generic empty grippers, not the stated clear path to the edge.

Demo failure scenario: A supposedly oracle-validated hybrid run reaches the white-ball push. The oracle cannot confirm it, spends up to 40 s on the stage, and may authorize it without checking the path. Comparison arms with operator notes are penalized for a verifier bug rather than robot capability.

Suggested fix: Add an explicit push-white oracle rule and a geometry-based precondition, or disable that stage for oracle evaluation until implemented. Fail setup on unsupported stage semantics instead of converting implementation errors into ordinary unknown observations.

Verification: raw/more_probes.json: push_white_oracle_exception is ValueError: invalid literal for int() with base 10: push.

<a id="a21"></a>
## A21 Medium: The subscription transport drops plan-specific system instructions

Branches: cloud-sims. Confidence: High.

Evidence:

`harness-cloud-sims/harness/verifiers.py:272`

```python
272:         cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
273:                "--model", str(payload["model"]), "--tools", "", "--strict-mcp-config", "--no-session-persistence",
274:                "--system-prompt", CLI_SYSTEM_PROMPT]
275:         effort = (payload.get("output_config") or {}).get("effort")
276:         if effort:
277:             cmd += ["--effort", str(effort)]
278:         lines = "".join(json.dumps({"type": "user", "message": {"role": m["role"], "content": m["content"]}}) + "\n"
279:                         for m in payload["messages"] if m.get("role") == "user")
280:         proc = subprocess.run(cmd, input=lines, capture_output=True, text=True, timeout=timeout_s,
281:                               cwd=tempfile.gettempdir(), env=env)
```

`harness-cloud-sims/harness/verifiers.py:718`

```python
718:         if self.plan.system_prompt:
719:             payload["system"] = self.plan.system_prompt
```

`harness/harness/verifiers.py:211`

```python
211:         cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
212:                "--model", str(payload["model"]), "--tools", "", "--strict-mcp-config", "--no-session-persistence",
213:                "--system-prompt", CLI_SYSTEM_PROMPT]
214:         effort = (payload.get("output_config") or {}).get("effort")
215:         if effort:
216:             cmd += ["--effort", str(effort)]
217:         lines = "".join(json.dumps({"type": "user", "message": {"role": m["role"], "content": m["content"]}}) + "\n"
218:                         for m in payload["messages"] if m.get("role") == "user")
219:         proc = subprocess.run(cmd, input=lines, capture_output=True, text=True, timeout=timeout_s,
```

Why it is wrong: Cloud MonitorVerifier puts plan.system_prompt and practice lessons in payload["system"]. claude_cli_transport ignores that field and supplies only the fixed CLI_SYSTEM_PROMPT. The user-message conversion does not carry system text either. Thus changing from API to subscription changes the monitor instructions. Main has the same transport implementation, but lacks this active plan-system producer, so the live finding is scoped to cloud-sims.

Demo failure scenario: The team switches a hybrid run to the subscription backend. The monitor loses station lessons about cable geometry, gripper readings, safe instruction prefixes, and operator lessons while all flags and plan files appear unchanged.

Suggested fix: Combine the fixed monitor role with payload.system when constructing --system-prompt, preserving intentional instruction order. Add an offline transport test that supplies a unique system sentinel and verifies it reaches the generated command.

Verification: raw/more_probes.json: a plan-system sentinel is present in neither command nor stdin of the stubbed subscription transport.

<a id="a22"></a>
## A22 Medium: The real station add-on ignores the requested 640-pixel frame limit

Branches: cloud-sims. Confidence: High for ignored query; medium for the station performance effect.

Evidence:

`harness-cloud-sims/harness/frames.py:60`

```python
60:                 try:
61:                     with urllib.request.urlopen(f"{self.station_url}/frame/{cam}.jpg?w={FRAME_WIDTH}",
62:                                                 timeout=self.timeout_s) as resp:
63:                         out[cam] = resp.read()
```

`harness-cloud-sims/station_addons/sitecustomize.py:20`

```python
20:         m = _re.match(r"^/frame/([A-Za-z0-9_]+)\.jpg$", self.path.split("?", 1)[0])
21:         if not m:
22:             return _orig_get(self)
23:         frame = self.server.api.controller.frames.get(m.group(1))
24:         if frame is None:
25:             return self._reply(404, {"error": f"no frame from camera {m.group(1)!r}"})
26:         ok, buf = _cv2.imencode(".jpg", _cv2.cvtColor(frame.data, _cv2.COLOR_RGB2BGR), [_cv2.IMWRITE_JPEG_QUALITY, 85])
27:         body = buf.tobytes()
28:         self.send_response(200)
29:         self.send_header("Content-Type", "image/jpeg")
30:         self.send_header("Content-Length", str(len(body)))
31:         self.end_headers()
32:         self.wfile.write(body)
```

Why it is wrong: FrameSource requests ?w=640 and the CLI/report describe lightweight resized images. The add-on strips the query to match the route, but never reads w or resizes frame.data before JPEG encoding. The simulator does implement a width option, so simulator tests can pass while the station still sends its full native frames. The actual bandwidth penalty depends on the station resolution.

Demo failure scenario: The team assumes the cloud branch eliminated image traffic overhead. At a high native camera resolution each poll and the 10 Hz live guard still encode/transfer full images, adding CPU and Wi-Fi load and delaying Dream replies during the run.

Suggested fix: Implement bounded resize in the actual add-on before encoding, preserve aspect ratio, and test that add-on handler with an oversized synthetic frame and ?w=640. Verify received dimensions and bytes in preflight rather than trusting the URL.

<a id="a23"></a>
## A23 Medium: Missing SDK dependencies silently skip otherwise dependency-free hedge tests

Branches: cloud-sims. Confidence: High.

Evidence:

`harness-cloud-sims/tests/test_hedge_cancel.py:110`

```python
110: transport = pytest.importorskip("dream.transport")
111: 
112: 
```

`harness-cloud-sims/tests/test_hedge_cancel.py:52`

```python
52: def test_first_reply_wins_and_the_loser_is_cancelled_and_logged():
53:     hp = HedgedPolicy([Fake("slow", 0.4), Fake("fast", 0.02)])
54:     assert hp.get_action()["from"] == "fast" and hp.last_winner == 1
55:     time.sleep(0.5)
```

`harness-cloud-sims/tests/test_sim_station.py:46`

```python
46:     except Exception as exc:  # no usable GL, etc.
47:         pytest.skip(f"simulator could not start here: {exc}")
48:     yield station
```

Why it is wrong: pytest.importorskip at module scope skips the entire hedge-cancel module, including six fake-session tests defined above it that do not need dream.transport. The simulator fixture also turns every startup Exception into a skip, including code regressions unrelated to GL availability. A green offline suite can therefore omit important async coverage without identifying which independent checks were lost.

Demo failure scenario: A developer runs pytest without the Dream SDK and sees one module skip, assuming only the SDK gateway test was skipped. Cancellation/cap behavior has not been exercised. On a configured machine, a simulator constructor bug can also be reported as a harmless skip.

Suggested fix: Move SDK-only tests to a separate module or put importorskip inside an SDK fixture. Keep fake-session tests always collectable. Narrow simulator skips to known dependency/graphics failures and fail on unexpected exceptions.

Verification: The cloud subset reports test_hedge_cancel.py:110 as one skip. raw/pytest_hedge_fake_only.txt separately runs the extracted six independent tests.

<a id="a25"></a>
## A25 Medium: The start-pose gate is advisory on several failure paths despite a hard-gate claim

Branches: cloud-sims. Confidence: High for code/documentation mismatch; live effect depends on pose coverage.

Evidence:

`harness-cloud-sims/harness/runner.py:972`

```python
972:         if chk.status != "far" or not cfg.homes(stage.name):
973:             return False
974:         if self._homes.get(stage.name, 0) >= cfg.max_homes:
975:             self.log.event("start_pose_home", stage=stage.name, done=False, reason=f"max_homes {cfg.max_homes} used")
976:             return False
977:         home = self.start_poses.check(prompt, cfg.home_pose, cfg.tolerance_rad, "home_pose")
978:         if home.status != "ok":
979:             self.log.event("start_pose_home", stage=stage.name, done=False,
980:                            reason="the home pose is not a training start for this prompt either", **home.as_event())
981:             return False
982:         left = self.plan.time_budget_s - (self._clock() - self._t0)
983:         if left < HOME_RESERVE_S:
984:             self.log.event("start_pose_home", stage=stage.name, done=False,
```

`harness-cloud-sims/harness/runner.py:1022`

```python
1022:         homed = self._start_pose_gate(stage, prompt, "stage start")
1023:         refused = self._try_issue(prompt, "stage", force_start=first or homed)
1024:         attempts = 0
```

`harness-cloud-sims/harness/start_pose.py:76`

```python
76: def _linf(a: list[float], b: list[float]) -> float:
77:     n = min(len(a), len(b))
78:     return max((abs(float(a[i]) - float(b[i])) for i in range(n)), default=0.0)
79: 
```

`harness-cloud-sims/FINAL_HARNESS.md:36`

```text
36: | Start-pose gate (`start_pose:` + `plans/start_poses.json`) | start-pose-live-guard | It won't send a prompt from a pose the policy never started from. |
```

Why it is wrong: FINAL_HARNESS says an unfamiliar start will not be sent. In fact the gate returns False for no state/reference, a far pose after max_homes, an unsuitable home reference, or insufficient time to home; the caller interprets False as simply no home needed and proceeds. Only the initial unplug prefix is configured to home. nearest also compares only arms present in both records, and _linf compares only the shorter joint-vector length. This is an advisory diagnostic with conditional homing, not a validated full-pose safety gate.

Demo failure scenario: The /arms route is missing or returns one truncated arm, or the arm remains far after the one allowed home. The harness can start the fine-tune from an unverified pose while the operator believes the advertised guard blocks it.

Suggested fix: Either document and display the advisory behavior accurately, or make match-mode eligibility explicit: require both arms, exactly six finite joint angles each, a fresh post-home recheck, and a defined stop/skip policy when a required start cannot be established. Keep homing limited to geometrically safe stages.

Verification: raw/probes_cloud.json confirms the configured zero home is near an unplug reference, so that default was checked and is not itself reported as a bug.

<a id="a24"></a>
## A24 Low: Pacing tests fail on missing simulator dependencies instead of following the documented skip contract

Branches: cloud-sims. Confidence: High.

Evidence:

`harness-cloud-sims/tests/test_async_pacing.py:8`

```python
8: from sim_station import SimStation
9: 
10: 
11: def call(sim, method, path, body=None):
```

`harness-cloud-sims/tests/test_async_pacing.py:22`

```python
22:     sim = SimStation(policy="scripted", scene="hill_climb", realtime=True, api_port=0, view_port=0,
23:                      scripted_latency_s=latency, execution_horizon=10)
24:     sim.dream_pipeline = pipeline
25:     sim = sim.start()
```

`harness-cloud-sims/tests/test_sim_station.py:17`

```python
17: pytest.importorskip("mujoco")
18: pytest.importorskip("PIL")
19: np = pytest.importorskip("numpy")
```

Why it is wrong: The supplied explainer says tests needing MuJoCo or dreamlabs skip when absent. Unlike the neighboring simulator tests, test_async_pacing has no capability fixture. Five cases fail during SimStation construction because the Dream example is absent, before any pacing assertion. Separately, this host lacks the documented OpenCV test dependency, so full collection could not run; that host limitation is not itself a product defect. None of these failures establishes that the two historical installed-simulator timing failures were fixed or reproduced.

Demo failure scenario: The pre-demo core check reports a red pacing integration suite without ever exercising pacing, mixing missing capabilities with behavioral failures and obscuring what was actually validated.

Suggested fix: Define an explicit offline-core suite and a focused simulator-capability fixture consistent with the other tests. Require the pacing integration suite in the fully provisioned environment; report it as unexecuted here rather than inferring control-loop results.

Verification: raw/pytest_cloud-sims.txt: cv2 collection limit. raw/pytest_cloud_subset.txt: 376 passed, six failed, eleven skipped; five failures are missing Dream example, one timing test passed in isolation.

<a id="a26"></a>
## A26 Low: Two subscription-backend tests use a non-executable Windows shebang stub

Branches: main. Confidence: High.

Evidence:

`harness/tests/test_claude_cli.py:36`

```python
36:     exe = tmp_path / "claude"
37:     exe.write_text(FAKE.format(python=sys.executable))
38:     exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
39:     log = tmp_path / "log.json"
40:     monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
41:     monkeypatch.setenv("HARNESS_CLAUDE_BIN", str(exe))
42:     return log
```

`harness-cloud-sims/tests/test_claude_cli.py:39`

```python
39:     if os.name == "nt":      # Windows can't run a shebang script: launch it through a .cmd next to it
40:         wrapper = tmp_path / "claude.cmd"
41:         wrapper.write_text(f'@"{sys.executable}" "{exe}" %*' + chr(10))
42:         exe = wrapper
43:     log = tmp_path / "log.json"
```

Why it is wrong: Main creates a Unix shebang script and executes it directly. Windows CreateProcess cannot launch it. Cloud-sims already adds a .cmd wrapper for the same fixture. This makes the two main test failures here test-fixture portability failures, not evidence that a real installed Claude CLI rejected the monitor payload.

Demo failure scenario: The team runs the main suite on this Windows laptop and gets WinError 193, then spends scarce pre-demo time debugging model integration that was never invoked.

Suggested fix: Port the local-stub Windows wrapper from cloud-sims, or invoke the fake script with sys.executable through a platform-neutral test transport. Keep the production CLI path separate from the test stub.

Verification: raw/pytest_main_retry.txt: 239 passed, 2 failed, 4 skipped. raw/pytest_main_cli_recheck.txt reproduces WinError 193; raw/pytest_cloud_cli_recheck.txt passes both corresponding tests.

## Branch notes

All 14 origin branches were inventoried. README and TELEOP_PLAN were read where present, along with their design/strategy documents and diffs. The code comparisons use both main and cloud-sims as bases. `raw/branch_runtime_summary.json` lists runtime file changes and commits absent from both candidates. Branch documentation is frequently older than its code; the notes below distinguish intent from observed implementation.

### cloud-sims (c6a3255)

Candidate c6a3255. Includes hybrid, live guard/start pose, operator notes, light-frame path, parallel monitor, hedge/pipeline simulator work, telemetry and matched table. v4 keeps recorded texts and technique off but still generates the unrecorded stick prompt. Hybrid now selects the fine-tune while free-text features remain on. confirm_gap_s exists but is zero in loaded candidate match plans. Retry caps for skip stages are now applied, while required gates deliberately remain 99. Cloud CLI portability fixes pass here. No claim that its simulator pacing config changes the real station agent: the live harness sends ordinary /start and /prompt only.

### codex/dual-arm-experiment (4219317)

4219317. Separate older decision-tree implementation plus simulation-only /objects and sequential/combined arm benchmark. Its ancestors add bounded match work, required camera evidence, and LiveKit. These paths are not wholesale merged into either candidate. Useful ideas: request deadlines and evidence validation. Do not replace candidate plans with its older full/sprint schema without reconciling white-ball and fine-tune behavior.

### codex/hill-climb-review-fixes (283b379)

283b379. Unmerged bounded-match design: _in_budget request cap, stop reserve, optional-stage budgets, gate dependencies, required camera/trip checks, strict consecutive success, incremental balls, and drop-held-cable-before-stack prompt. Those are relevant to A03, A09, A13, A16. Its remove-binned-8-ball recovery and other prompt changes need rule/model reconciliation before use. Its _safe_stop still logs first, so it does not fix A01.

### codex/hill-climb-sim-table (b372601)

b372601. Carries the review-fixes lineage, a relay table/referee, oracle evaluation, cluster plan selection, and LF shell-script handling. The explicit stack-drop workaround is retained in its match plans after removing a temporary variant. Some similar sim code reached main separately, but the bounded/evidence runner and independent ball targets remain unmerged. Older table legality/inventory must be reconciled.

### codex/watcher-dimos (89c9621)

89c9621. Adds canonical camera alias matching, required-camera/new-trip evidence and prompt provenance checks for the watcher. Main has watch_ok repositioning but not that complete evidence gate; cloud has the observer port but its final notes explicitly left required_cameras out. It is an older ancestor with different plans, not a drop-in match branch.

### docs/how-it-works (7f0daad)

7f0daad. Required explainer and drawn PDF, based on earlier main 9db93f6. Useful architectural map, not authoritative evidence of current defaults. It predates the final default-checkpoint and latest detector changes. Read the raw markdown and PDF extraction; no unique runtime fix was found.

### final-harness (777e389)

777e389. Merge of live guard and hybrid, motion checks around /reset, --match, LiveKit observer, and handwritten operator decisions. Most implementation is already in cloud-sims; main lacks it. Its FINAL_HARNESS statement that start pose refuses unfamiliar starts overstates the advisory code, and its run commands/base defaults predate later model-default changes. The handwritten decisions commit itself is not an implementation fix.

### hedged-calls (bf33014)

bf33014. Fast/state-space loop, layered prompts, hedged monitor and OpenRouter judge transport; most runtime work is in cloud-sims. Its unique latest commit corrects subscription/billed-call cost accounting in bench_judges, not live-run control. Keep its optional hedging safety loss A19 in mind; v4 defaults do not enable hedge>1.

### hybrid-harness (3ff5c06)

3ff5c06. Relative ball stages, put-down prerequisites, stitched/parallel judge, prompt rewrites, reset/restart delay, and recovery-done second look. Present in cloud-sims, mostly absent in main. Relative balls were not backported into candidate v4. Long hybrid plan was documented as losing more matches and is not a proven match choice.

### mac-arm64 (ad2a55f)

ad2a55f. Unmerged uint8-plane detector optimization, cropped morphology, equivalence tests, timing instrumentation and macOS CI/benchmark instructions. Relevant to requested 10 Hz guard throughput. Do not copy the whole detector: its head predates main/cloud hole-axis work and would remove it. Benchmark/equivalence-test the narrow speed changes on actual camera frames.

### main (b1f6e4b)

Candidate b1f6e4b. Recorded-text v4, single monitor call, six history frames, no independent live guard, no start-pose module, no operator retry policy. Shares the critical runner/confirmation issues. New hole-axis annotations are diagnostic only. Model default matches cloud-sims; do not confuse that with matching runtime behavior.

### start-pose-live-guard (c2098dc)

c2098dc. Independent stop thread, motion interlocks, per-camera duplicate-frame checks, safe-homing reserve and decision shadow. These are already in cloud-sims but absent in main. They do not solve shared any-camera blindness A06 or white threshold A07. Shadow equations are not authoritative live decisions by default.

### stress-test-fixes (d938644)

d938644. Refusal/transient classification, release/skip changes, corrupt-frame filtering, duplicate success-frame check and local guard. These fixes are broadly present in both candidates, so not a missing-fix branch. Its guard is still limited by debug trips; its simulated success probabilities are assumptions.

### tools/episode-review-page (e5bf84f)

e5bf84f. Offline recording reviewer, partial episode selection, carry-marks and label/cut corrections. No unique live runner fix beyond the older stress lineage. Current main has newer final dataset splits/checker/watcher, while cloud TELEOP_PLAN still describes older training preparation. Training-data differences and labels matter for A08, but no training job or upload was attempted.

## Things I checked and found OK

- Both candidate v4 plans load and resolve checkpoint to dream-1-ft:bb4b9e5f-relay-final. There is no remaining mismatch in that exact default ID. The hybrid prompt-mode conflict is A08.
- Colour order is intentionally not a gate. tower_matches compares shape order and logs the result; it does not reject a valid height because pink/blue order differs. No colour-order bug is asserted.
- Non-skip stages fail the run on exhausted attempts instead of silently touching the next stage. Required-stage retry-to-clock is a deliberate DQ exception to the operator retry note. A14 concerns an incorrectly designated prerequisite, not the existence of that exception.
- Cloud operator_notes applies two attempts to skip stages and three for configured high-value stages, using retry_skip so the loop really retries them. Inserted white-push stages retain their one-attempt cap. Main does not implement that operator policy.
- Normal _issue prompt changes invalidate monitor generations; stale monitor guard=yes is preserved through the normal late-result alert path. A11 concerns direct restart paths and A19 losing hedge copies.
- Byte-identical success frames do not confirm twice in the normal watch loop. Cloud additionally de-duplicates local guard frames per camera; its live confirmation counters are per camera. These protections do not imply complete camera coverage or correct white classification.
- Cloud checks live-guard trips before and after movement commands, blocks homing while that guard is armed, and routes reset through motion checks. The earlier reset/interlock fixes are present. No downstream motor or SDK command was exercised.
- Cloud start_poses.json loads, and the configured zero home is within the unplug tolerance of an actual reference. The guard only homes the first-unplug prefix by design. A25 concerns fail-open eligibility and the stronger documentation claim.
- Unknown hardware/motor errors do not take the recognized transient restart path; the existing arm-fault and refusal-classification tests pass. No claim is made that broad string classification proves every possible vendor error is safe to restart.
- StationClient dry_run routes POST intentions through local simulation rather than sending them. The audit never used a real client endpoint; test sockets were limited to ephemeral local fake servers. No credential-bearing files were opened.
- Scripted stress tests are explicitly modelled policies, not robot-capability measurements. The handoff correctly says real station task success is not logged, the big cloud comparison was invalidated by account failures, and no real station success-rate conclusion can be drawn. No paid rerun or model check was attempted.

## Reproduction and artifact map

All audit scripts and generated data are under out/. `probes.py` uses fake station/frame/log objects and an injected clock. `more_probes.py` uses fake model transport and synthetic referee data. No physics solve or Dream inference is hidden in those probes. `run_tests.py` disables bytecode/cache writes, redirects temporary/log output to out, blocks credential paths and real service CLI commands, and permits only ephemeral loopback test connections. It was run outside the initially failing sandbox after automatic approval, with the same guards. Raw wrapper errors remain available for transparency.

Tests: `raw/pytest_main_retry.txt`, `raw/pytest_main_cli_recheck.txt`, `raw/pytest_cloud-sims.txt`, `raw/pytest_cloud_subset.txt`, `raw/pytest_cloud_cli_recheck.txt`, `raw/pytest_cloud_timing_recheck.txt`, and `raw/pytest_hedge_fake_only.txt`. Probe results: `raw/probes_main.json`, `raw/probes_cloud.json`, `raw/more_probes.json`. Source excerpts for every finding: `raw/source_evidence.txt`. Repository status comparisons: `raw/status_before_*`, `raw/status_after_*`. No commits, branch checkouts, resets, pushes, network fetches, or source fixes were performed.
