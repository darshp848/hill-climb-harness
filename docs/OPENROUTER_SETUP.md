# OpenRouter judge setup

Account, credit and secret changes belong to you. Never paste a key into chat,
source files, YAML, benchmark arguments or a Git commit.

## Account and credentials

1. USER ACTION NEEDED: Create an account or sign in at <https://openrouter.ai>.
2. USER ACTION NEEDED: Open [Credits](https://openrouter.ai/settings/credits),
   buy a small starting balance, for example $5 if offered, and leave automatic
   top-ups off for the initial experiment.
3. USER ACTION NEEDED: Open [API keys](https://openrouter.ai/settings/keys),
   choose **Create API Key**, name it `hill-climb-judges`, and set **Key limit →
   Credit limit** to $1 for the first check. Leave the reset interval unset for
   a lifetime limit. Existing keys have the same fields in their edit dialog.
   OpenRouter documents these controls in [key credit limits](https://openrouter.ai/docs/guides/best-practices/capping-shared-agent-keys).
4. USER ACTION NEEDED: Store the key as a Windows *user* environment variable
   in PowerShell. This masked prompt keeps the value out of shell history:

   ```powershell
   $routerSecret = Read-Host 'Paste <OPENROUTER_API_KEY> here, never into chat' -AsSecureString
   $routerCredential = [System.Net.NetworkCredential]::new('', $routerSecret)
   [Environment]::SetEnvironmentVariable('OPENROUTER_API_KEY', $routerCredential.Password, 'User')
   $env:OPENROUTER_API_KEY = $routerCredential.Password
   Remove-Variable routerSecret, routerCredential
   ```

   Exact literal equivalent, with a placeholder only:

   ```powershell
   [Environment]::SetEnvironmentVariable('OPENROUTER_API_KEY', '<OPENROUTER_API_KEY>', 'User')
   ```

   Prefer the masked version so the real value never enters PowerShell history.
   New shells inherit the user variable. Existing shells can load it with
   `$env:OPENROUTER_API_KEY = [Environment]::GetEnvironmentVariable('OPENROUTER_API_KEY', 'User')`.
   The variable lives in your Windows profile. Do not print it.
5. USER ACTION NEEDED: For cloud runs, sign in to your Google account using
   `gcloud auth login`, then enable Secret Manager if needed:

   ```powershell
   gcloud services enable secretmanager.googleapis.com --project=hill-climb-sims
   ```

   Create `openrouter-api-key` from stdin, using the environment variable above:

   ```powershell
   $env:OPENROUTER_API_KEY | gcloud secrets create openrouter-api-key --project=hill-climb-sims --replication-policy=automatic --data-file=-
   ```

   The stdin source is `<OPENROUTER_API_KEY>`, not a repository file. PowerShell
   adds a newline; the retrieval commands below trim it. If the secret already
   exists, rotate it with
   `$env:OPENROUTER_API_KEY | gcloud secrets versions add openrouter-api-key --project=hill-climb-sims --data-file=-`.
   The [gcloud create reference](https://docs.cloud.google.com/sdk/gcloud/reference/secrets/create)
   documents `--data-file=-` and PowerShell's newline behavior.
6. USER ACTION NEEDED: Grant only the cloud runner's service account access to
   this secret. Replace the service-account placeholder with the identity used
   by the job, then run:

   ```powershell
   gcloud secrets add-iam-policy-binding openrouter-api-key --project=hill-climb-sims --member='serviceAccount:<RUNNER_SERVICE_ACCOUNT_EMAIL>' --role=roles/secretmanager.secretAccessor
   ```

   In the Linux cloud job, retrieve the value into the process environment before
   running the benchmark or harness. Keep shell tracing off:

   ```sh
   set +x
   export OPENROUTER_API_KEY="$(gcloud secrets versions access latest --secret=openrouter-api-key --project=hill-climb-sims | tr -d '\r\n')"
   export HARNESS_CLAUDE_BACKEND=openrouter
   export HARNESS_OPENROUTER_MODELS=google/gemini-3.1-flash-lite
   ```

   If the job runs in Docker, pass the existing variable by name with
   `docker run --rm -e OPENROUTER_API_KEY -e HARNESS_CLAUDE_BACKEND -e HARNESS_OPENROUTER_MODELS ...`.
   The current cluster launcher does not inject this secret automatically. Put
   retrieval in the cloud runner or job wrapper.

   The labelled station photo is tracked at `tests/data/table_c_topdown.jpg`;
   include it when packaging the checkout for a cloud job.
7. USER ACTION NEEDED: Optional, review [Privacy](https://openrouter.ai/settings/privacy)
   and [Observability](https://openrouter.ai/workspaces/default/observability).
   Choose whether providers may train on inputs, whether zero data retention is
   required, and whether prompts and images should appear in logs. Stricter
   choices can remove providers. See [data collection](https://openrouter.ai/docs/guides/privacy/data-collection)
   and [provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).
   These benchmarks use the harness's default provider routing.

## Verify and measure

8. USER ACTION NEEDED: From this worktree in PowerShell, run the offline check.
   It needs no account, key or network:

   ```powershell
   $judgePython = '..\hill_climb_harness\.venv\Scripts\python.exe'
   & $judgePython tools/bench_judges.py --dry-run --hedge 1 --hedge 2 --out runs/judges-dry
   ```

   This checks all eight singles and the Haiku/Gemini race on four photo
   questions and two MuJoCo scenes. `--hedge 2` sets `claude.hedge` to two copies.
   Every request passes through the real monitor and shared wire-body builder;
   image bytes, text, ordering, system prompt and token limit are checked.
   Dry summaries leave accuracy, latency and cost null. Add `--no-sim` for a
   photo-only check if the local `yam_mujoco` example, MuJoCo or Pillow is missing.
   Full runs require the same renderer as `tools/check_monitor.py --sim`.
9. USER ACTION NEEDED: When ready to spend credits, run one single-model check:

   ```powershell
   & $judgePython tools/bench_judges.py --judge openrouter:google/gemini-3.1-flash-lite -n 1 --max-cost-usd 0.10 --out runs/judges-first
   ```

   Look for numeric cost and parsed answers. Exit 1 means wrong answers;
   exit 2 means setup, validation, accounting, budget or interruption stopped
   the run. Partial reports are preserved. Reports omit reply text and exception
   messages. HTTP and parse errors count as incorrect stage answers.
10. USER ACTION NEEDED: Compare singles, a model race, and a hedge after the
    first check works:

    ```powershell
    & $judgePython tools/bench_judges.py -n 3 --hedge 1 --hedge 2 --max-cost-usd 1 --out runs/judges-all
    & $judgePython tools/bench_judges.py --judge openrouter:anthropic/claude-haiku-5.5,google/gemini-3.1-flash-lite -n 3 --max-cost-usd 0.50 --out runs/judges-race
    ```

    Linux cloud jobs use `python tools/bench_judges.py` with the same arguments.
    For a harness run after model selection, set `HARNESS_OPENROUTER_MODELS` to
    one ID or a comma-separated race, and pass `--claude-backend openrouter` to
    the existing harness command. A race uses the first nonempty response, even
    if it later fails parsing. A hedge uses the first parseable monitor answer.
    Neither checks correctness before choosing a winner.

`summary.csv` and `results.json` report stage and guard accuracy, parse and
transport failure rates, and latency p50, p90 and max. Guard accuracy uses only
guard-labelled cases. `accepted_done_accuracy` also applies the confidence gate.
Percentiles use linear interpolation. JSON includes each trial, plan hash,
frame digest and numeric usage. Connector close-ups have no stage/guard labels.

Winning latency ends when `check_now` returns. Accounting waits separately for
every losing HTTP call before starting the next trial. `cost_per_call_usd` is
total reported cost divided by HTTP attempts; `cost_per_trial_usd` includes
every race and hedge copy for one verdict. [Usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)
returns `usage.cost` automatically. Missing costs stay null and stop scheduling.
`--max-cost-usd` defaults to $1 and stops new trials at reported spend. It can
overshoot by the full concurrent trial already started. Zero starts no calls.
The key's server-side credit limit is the separate account control.

`--judge api:claude-haiku-5-5` and `--judge subscription:claude-haiku-5-5` also
work with their existing credentials, but these transports do not expose dollar
cost in usage. A live run stops after the first trial with unknown cost. Use
OpenRouter for the budgeted comparison. No live benchmark was run during
implementation.

## Default candidates

The unauthenticated [live model catalog](https://openrouter.ai/api/v1/models)
was checked on 2026-11-08. All eight advertise image input and text output.
Prices below are advertised USD per million input/output tokens, not measured
costs. Image tokens, provider choice, caching and reasoning change actual cost.
Speed and guard reliability are hypotheses to measure.

| OpenRouter ID | Input / output | Reason to include |
| --- | --- | --- |
| `anthropic/claude-haiku-5.5` | $0.10 / $0.50 | Existing judge family, comparison baseline |
| `google/gemini-3.1-flash-lite` | $0.25 / $1.50 | Flash-Lite alternative and default race partner |
| `qwen/qwen3.8-flash` | $0.15 / $0.47 | Current Qwen Flash with vision, inexpensive output |
| `z-ai/glm-5.3-flash` | $0.0335 / $0.083 | Cheapest advertised paid Flash candidate here |
| `meta-llama/llama-4-scout` | $0.10 / $0.30 | Independent vision model family at low cost |
| `openai/gpt-5-nano` | $0.05 / $0.40 | Low-cost small vision model; check reasoning delay |
| `mistralai/mistral-small-2603` | $0.15 / $0.60 | Current Mistral Small vision alternative |
| `google/gemma-4-26b-a4b-it` | $0.0675 / $0.225 | Low-cost sparse Gemma vision alternative |

Text-only GLM 4.7 Flash and Qwen Coder Flash are excluded. Batch endpoints,
image-generation variants and free variants are excluded from the defaults.
The six cases are a smoke test, not evidence to deploy a guard. Compare guard
false negatives and false positives separately, collect more labelled match
frames, and increase repeats before choosing a judge for a match.
