# 4. The decision model

[Back to the index](../TECHNICAL_REPORT.md)

The runner's rules are hand-set constants. To make them tunable, Darsh (with Claude) wrote them as a model in which
today's rules are a special case: design in `docs/DECISION_MODEL.md` (branch `final-harness` and the cloud-sims
family; it is the committed form of the local `HARNESS_DECISION_MODEL_2026-11-07.md`), code in `harness/decide.py`
(337 lines) and `harness/statespace.py`, state-space write-up in `STATE_SPACE.md` (cloud-sims). **None of it made a
match decision**: it ran in shadow mode only.

## 4.1 Runtime model of a stage

```math
T_\text{stage} = T_\text{start} + \frac{T_\text{robot}}{u} + T_\text{detect} + T_\text{decide}
```

with $u$ the share of time the arms actually move. On the station $u \approx 0.70$ because 2/3 of chunks were
stop-and-wait (F1). The levers ranked by expected gain (`DECISION_MODEL.md` §1): L1 get Dream into async (network,
light frames), L2 event-driven stage-done from local sensors (4–7 s per boundary), L3 never `/start` mid-match
(2.9 s each), L4 skip VLM checks the gripper sensors can answer (~4 s per skippable stage), L5 faster judge.

## 4.2 Done evidence as recursive log-odds

State $x_t$: stage $s$, elapsed $\tau$, time left $L=T-t$, evidence features $\phi(t)$, attempt count $n$.

```math
\ell_t = \gamma\,\ell_{t-1} + \sum_i w_i\,\phi_i(t), \qquad \text{done} \iff \ell_t \ge \theta_\text{done}(s)
```

| $\phi_i$ | Source | Weight today |
|---|---|---|
| monitor done (+1) / not, × confidence | VLM | $w_\text{monitor}=1$; a no resets to 0 |
| progress $p_t \in [0,1]$ | VLM | stall and regression only |
| gripper closed-on-object → open near the target | `/arms` | 0 (`w_sensor_release`) |
| object at goal (detector box in the bin / on the tower) | detector | 0 (`w_goal_box`) |
| plug count fell (cable-end boxes) | cable-end CNN | 0 (`w_plug_count`) |
| same frames as last time | SHA-1 | $\phi=0$ |

Today's rule is the special case $\gamma=1$ on agreement and $0$ on disagreement, unit weight on the judge,
$\theta_\text{done}=\texttt{confirm}=2$, plus the 15 s "sticky done" window. Code: `DoneWeights`, `done_step`,
`run_looks` (`harness/decide.py:38-102` on cloud-sims).

## 4.3 The fail hazard

```math
h_t = \sigma\!\Big(b + w_f\,n_\text{fail} + w_r\max(0,\,p_\text{max}-p_t-\delta) + w_s\frac{\tau-\tau_\text{rise}}{\text{stall}_s} + w_T\frac{\tau}{\text{timeout}_s} - w_\text{on}\,\mathbb{1}[\text{on target}]\Big)
```

The attempt ends when $h_t > \theta_\text{fail}$. In code (`FailWeights`, `fail_terms`, `fail_score`), each term
$g_k$ is a ratio that reaches 1 exactly when today's hard rule fires (consecutive failing looks / confirm,
consecutive stalled looks / confirm, time since progress rose / `stall_s`, elapsed / `timeout_s`) and they are
combined by `max` (today's tree: any rule fires) or `sum` (weak signals add up, for tuning).

## 4.4 Choosing an action: the Q-value and the value of time left

At a decision point, with $a \in$ {continue, re-prompt/rewrite, reset prompt, home + retry, skip, abort}:

```math
Q(a \mid x) = P_a(\text{succ within } c_a)\,V_s \;+\; W(L - c_a) \;-\; \lambda_\text{risk}\,R_a
```

- $V_s$: the stage's points (first cable +10, extra +5, tower pass +20, +3/inch, ball +5) plus what it unlocks.
- $c_a$: time cost (re-prompt ~1 s; reset 5–20 s; home ~10 s + 2.9 s `/start`).
- $R_a$: risk (DQ from a false done or out-of-order touch, knocked tower, illegal ball). Default $\lambda=50$.
- $W(L)$: expected points of the remaining stages with $L$ seconds left, by backward dynamic programming on a 1 s grid
  over each stage's success-vs-time curve $F_s$:

```math
W_i(L) = \max_{T \le L}\Big[ F_i(T)\big(V_i + W_{i+1}(L - \bar t_i(T))\big) + (1-F_i(T))\cdot
\begin{cases} 0 & \text{gate} \\ W_{i+1}(L-T) & \text{otherwise}\end{cases}\Big]
```

with $\bar t_i(T)$ the mean success time given success by $T$ (`StageModel.mean_success_time`), and the argmax $T$
being that stage's timeout for $L$ seconds left (`plan_timeouts`, `decide.py:213-232`). The implemented action value
is `action_value` (`decide.py:244-253`): $Q(a)=p(V_i+W_{i+1}(L-c)) + (1-p)W_i(L-c) - \lambda R_a$.

**Stage success curve** (`StageModel.F`, `decide.py:165-181`):

```math
F_s(\tau) = p_\text{max}\left(1 - e^{-\max(0,\,\tau - t_\text{min})/\tau_c}\right)
```

### 4.4.1 The optimal-timeout condition

Keep going on stage $s$ while its hazard of success, times its value, beats the marginal value of time elsewhere:

```math
\frac{f_s(\tau)}{1 - F_s(\tau)}\,V_s \;\ge\; \frac{dW}{dL}
```

So each stage gets a timeout tied to its own success curve and to how much match is left: optional stages get short
timeouts late in a match and longer ones early. This replaces a fixed `timeout_s`.

### 4.4.2 Fitted curves (simulation only)

`tools/fit_stage_curves.py` fits $F_s$ by censored maximum likelihood over attempts; `plans/stage_curves_sim.json`
(cloud-sims family) holds the fit from **Dream's YAM MuJoCo sim only**:

| Stage | $p_\text{max}$ | $t_\text{min}$ (s) | $\tau_c$ (s) | Episodes | Successes |
|---|---|---|---|---|---|
| unplug_cable | 0.64 | 22.84 | 105.6 | 35 | 19 |
| put_cable_down, release_before_stack, stack_two, plug_loose_cable | 0.02 (floor) | 0 | 292.84 | 19 / 9 / 8 / 5 | 0 |
| watcher_into_place | 1.0 | 5.71 | 2.81 | — | — |

The commit message (c2098dc) and the paper say "unplug p_max 0.64, none before ~23 s, **median 56 s**".
**Not reproducible from the JSON:** with these parameters the time to half the ceiling is
$t_\text{min} + \tau_c\ln 2 \approx 96$ s (88 s if conditioned on success within 292 s). The 56 s may be the
empirical median of the 19 successful attempts; the attempt-level data is not in the repo. Flagged in §13.

## 4.5 The pass-bar constraint: the $\alpha^k$ bound

A false done on a pass bar is a DQ. If each look has false-positive rate $\alpha$ and $k$ independent looks are
required:

```math
P(\text{false done}) \approx \alpha^k
```

Choose $\theta_\text{done}(s)$ per pass bar so that $P(\text{false done}) \le \varepsilon$ (e.g. 1 %), then minimise
the expected detection delay. Example: $\alpha = 0.10$, $k=2$ gives 1 %; $k=3$ gives 0.1 %.

**The independence assumption is the weak point.** The stress sim found two DQs (`false_done_unplug` and
`false_done_all`, same seed) where two false "cable out" verdicts from **two calls in flight** landed seconds apart
on near-identical frames and passed `confirm: 2` (`FINAL_HARNESS.md`). Correlated looks make the effective $k$ close
to 1. The fixes are a minimum gap between confirming verdicts (`confirm_gap_s`, 3 s default in `statespace.py`, 0 on
cloud-sims' runner per audit A03) and a confirming look from a different model (Sonnet `confirm_check` on the bot
line). Audit A03 also notes that the 15 s sticky-done window lets a tower that falls and is re-placed pass.

## 4.6 Tuning objective (planned, not run)

```math
J(\theta) = \mathbb{E}[\text{score}] - \mu\,P(\text{DQ}) - \nu\,P(\text{illegal ball})
```

over $\theta = \{w_i, \gamma, \theta_\text{done}(s), w_f, w_r, w_s, w_T, w_\text{on}, \theta_\text{fail}, \lambda,
\theta_\text{rw}, \dots\}$, estimated in the stress sim with common random numbers (~200 sim matches per candidate),
searched by CMA-ES or coordinate hill-climbing, then confirmed on Metal (best 2 vs today, 5 supervised matches each).
Step 1 — calibrate the sim's evidence model from real logs — was never done, so this was never run.

The rewrite gate in the same formalism (`DECISION_MODEL.md` §4):

```math
g_\text{rw} = w_\text{wrong}\mathbb{1}[\text{wrong object}] + w_\text{stall}\frac{t - t_\text{rise}}{\text{stall}_s} + w_\text{risk}P(\text{knock}) - w_\text{churn}\,n_\text{rewrites} > \theta_\text{rw}, \quad t - t_\text{last prompt} \ge \tau_\text{cool}
```

Arm choice from layout (`DECISION_MODEL.md` §3), never built:

```math
c_a(o) = w_\text{reach}\frac{\lVert p_o-b_a\rVert}{R_a} + w_\text{cross}\mathbb{1}[\text{past midline}] + w_\text{clear}\sum_k e^{-d_k^2/\sigma^2} + w_\text{busy}\mathbb{1}[\text{busy}] + w_\text{skill}(1-s_a)
```

## 4.7 State-space form (`harness/statespace.py`, `STATE_SPACE.md`)

State $x \in \mathbb{R}^{K\times 9}$ (`done, fail, stalled, guard_m, guard_l, pmax, since_rise, t, since_done`), input
$u \in \mathbb{R}^{1\times 12}$ (`dt, v_new, v_done, v_same, v_failing, v_stalled, v_rise, v_guard_yes, v_guard_no,
l_guard_yes, l_guard_no, obs_release`), $K$ weight sets evaluated at once:

```math
X_{k+1} = \big(\mathbf 1_K \otimes (1 - \mathrm{clip}(u_k R, 0, 1))\big) \odot (X_k A^\top) + \mathbf 1_K (u_k B)
```

with $A=I_9$, $B$ integrating `dt` into the clocks and counting guard yeses, $R$ resetting on `v_rise` and guard
noes, plus a switched jump map on verdict ticks (done/failing/blank) and a fixed-gain observer on the task estimate
$\hat z \in [0,1]^5$ (progress, holding, at_goal, illegal_grip, cable_out):

```math
\hat z \leftarrow \tfrac12\mathbf 1 + \rho(\hat z - \tfrac12\mathbf 1),\quad
\hat z \leftarrow \hat z + L_f H_f (y_f - \hat z),\quad
\hat z \leftarrow \hat z + L_s H_s (y_s - \hat z)
```

$\rho=0.98$, $L_f=0.25$ (local detector), $L_s=0.8$ (VLM). With defaults it makes the same pass decision as
`decide.py`'s reference on **3,000** random verdict sequences (`tests/test_statespace.py`); one tick over 1,000 weight
sets takes < 5 ms (46 µs after the mac-arm64 optimisation for K = 1000).

## 4.8 What shadow mode showed

`Shadow` (`decide.py:287-337`) is fed every runner look and logs `shadow_*` events. The tests prove agreement with
the runner on 5,000 random look sequences (`tests/test_decide.py:48`) and on a real `Runner` with a scripted monitor.
No station run used shadow mode (the bot line on main does not contain `decide.py`), so there is **no field evidence**
for or against it.
