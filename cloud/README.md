# Hill Climb on Google Cloud Batch

This directory runs real Dream-1 sessions against the local MuJoCo relay scene.
The default matrix has 1,000 matches, 500 seeds each for `hill_climb_v4` and
`hybrid_v4 --skip cable_extra`. Both use `--match`, the sim colour detector and
`--live-guard`. No cloud resources were created or changed during development.

Files:

- `run.py` wires the existing Runner, station, light frames, detector, live guard,
  start-pose gate and read-only lessons together without an operator console.
- `judge.py` uses the existing monitor's two-call asynchronous scheduler with
  referee answers captured at request time and uniform 6.5–13 s delays. Unknown
  stage questions stay unknown. Oracle mode does not invent prompt rewrites.
  `--judge api` uses the existing Claude monitor and API transport, never the
  subscription CLI. Oracle and API therefore differ in perception and rewrites,
  not just latency. Do not treat their scores as interchangeable.
- `entrypoint.py` starts a fresh process and sim for each match, writes JSON and
  logs beneath `/out`, uploads them, then writes a completion marker. Each Spot
  attempt uses a unique prefix. Only complete attempts enter collection.
- `setup.sh`, `build.sh`, `submit.sh` call `control.py`; all cloud operations use
  the user's `gcloud`. They default to offline dry-run and require `--execute`
  for changes. `collect.py` likewise defaults to a printed plan.
- `Dockerfile` extends the cluster container with CPU OSMesa, the baked checkout,
  pinned Dream 1.0.37 / MuJoCo 4.0.0 and gcloud. Cloud Build runs the original
  example and relay build/step/render/API/detector checks before publishing.
- `COST.md`, `RESEARCH.md` and `evidence/` record measurements and source checks.

The two real-table YAML plans contain incorrect ball instructions for this sim.
`run.py` adapts those texts in memory, including monitor questions and lessons.
White golf balls are targets; black 8-balls are forbidden. It records the entire
effective plan, its hash, original plan hash, versions, selected skips and colour
adaptation in the manifest. Original match plans remain available for the real
table. The sim layout does not currently support scene randomisation. Seeds
control oracle delay samples and label independent Dream repetitions; the Dream
API does not expose a deterministic policy seed through this example.

## Run commands in order

Run from this checkout in Git Bash. Use the supplied venv without changing it:

```bash
export PYTHON='../hill_climb_harness/.venv/Scripts/python.exe'
bash -n cloud/setup.sh cloud/build.sh cloud/submit.sh
bash cloud/setup.sh --dry-run
bash cloud/build.sh --tag cloud-v1 --dry-run
bash cloud/submit.sh --tag cloud-v1 --job hill-climb-pilot-v1 --pilot --parallelism 8 --dry-run
```

The same scripts work in Cloud Shell with Python 3 and PyYAML installed. They do
not need local Docker. The Windows machine currently has Git Bash but no gcloud
on PATH. Use a newly built, unique tag after any code or matrix change.

After the user actions below are complete, these commands create resources and
spend money. They are supplied for the user's execution, not run in this session:

```bash
gcloud auth login
bash cloud/setup.sh --execute
bash cloud/build.sh --tag cloud-v1 --execute
bash cloud/submit.sh --tag cloud-v1 --job hill-climb-pilot-v1 --pilot --parallelism 8 --execute
gcloud batch jobs describe hill-climb-pilot-v1 --location us-central1 --project hill-climb-sims
"$PYTHON" -m cloud.collect --prefix gs://hill-climb-sims-results/runs/hill-climb-pilot-v1 --execute
```

Inspect the pilot's task errors, manifests, metrics and referee outcomes. A normal
295-second harness timeout is a valid completed match. An interruption, init
failure or failed upload is not. Check Linux peak RSS fits the 1,280 MiB task
request, cameras stay under the runner's 2 s freshness limit, and measure physics
time relative to 300 s wall time. Verify Dream accepts eight concurrent sessions
and that recoveries are not dominated by rate limits. If it does not, submit a
new pilot job with lower `--parallelism`. No script automatically raises quotas
or switches away from Spot. `--machine-type e2-standard-2` supplies a larger-memory
fallback for a new pilot if OSMesa exceeds the selected shape's RAM.

Then submit and collect the full matrix:

```bash
bash cloud/submit.sh --tag cloud-v1 --job hill-climb-full-v1 --parallelism 8 --execute
"$PYTHON" -m cloud.collect --prefix gs://hill-climb-sims-results/runs/hill-climb-full-v1 --local cloud/generated/full --out cloud/generated/full.jsonl --execute
```

For a real Claude judge, upload the Anthropic secret first, then use a new job
name and `--judge api`. Optional `--matches-per-task 3` groups sequential matches.
Pilot mode always selects ten matches, interleaved across configurations.
For a standalone container's sequential seed range, use entrypoint arguments
`--config hybrid_v4 --seed-start 100 --matches 5 --judge oracle`, with
`OUTPUT_PREFIX` and runtime credentials supplied by the execution environment.

Setup enables APIs and ensures existing named resources. IAM additions are
idempotent. It never rotates an existing secret, edits existing budget amounts,
deletes resources or changes the global gcloud project setting. Build skips an
existing image tag. Submit skips an existing job with the same manifest and
rejects a reused name with different inputs. The matrix hash is checked inside
the image to reject a stale baked matrix. Collection downloads using gcloud,
ignores unmarked partial attempts, deduplicates completed Spot attempts by
job/config/seed, and rewrites JSONL deterministically. Retried tasks may still
incur repeated inference charges; deduplication only prevents duplicate rows.

Worker identity has project-level Batch agent reporting and log writing,
repository-scoped image reading, bucket-scoped object creation, and access only
to the two secret containers. It cannot delete or overwrite results. The separate
builder identity can write the repository, read build-source objects and write
logs; it cannot read either secret. No key file is staged or copied into the
container. The source upload uses `gcloudignore` as an explicit allowlist.
Collection uses the user's identity, which needs output object read permission.

## USER ACTION NEEDED steps

Every cloud command failure stops with `USER ACTION NEEDED:`. Resolve the named
step in your own terminal or console and rerun the same command.

1. Install Google Cloud CLI, make it available to Git Bash, and run `gcloud auth
   login` using the personal Gmail that owns billing account
   `<BILLING_ACCOUNT_ID>`, "My Billing Account".
2. Create project `hill-climb-sims` in the console if it is absent. Link that
   billing account. Setup intentionally refuses to create the project or link
   billing automatically.
3. Accept any required API terms. Ensure your user can enable APIs, administer
   project/resource IAM, create service accounts and secrets, submit Cloud Build
   and Batch jobs, and create project-filtered budgets on that billing account.
4. Save Dream's setup block locally in `~/.dreamlabs/credentials`. Do not paste
   it into chat. Setup reads only its `DREAM_API_KEY` entry, passes the value to
   your gcloud's stdin, and never prints it or stores a second local copy. If an
   enabled secret version already exists, setup does not read the local file.
5. For API judge mode only, upload the Anthropic key to `anthropic-api-key` in
   Secret Manager using your own console. Setup creates the secret container but
   leaves it empty. Oracle jobs never request its value.
6. If the global bucket name is already owned elsewhere, choose an available
   `--bucket NAME` and pass it consistently to setup, build, submit and collection.
7. Confirm the actual regional Spot price. If submission reports quota or
   capacity problems, request sufficient regional Spot/preemptible CPU quota
   and Batch quota. Default parallelism eight needs up to sixteen vCPUs. Check
   Artifact Registry and Cloud Build quotas too. The default VPC must provide
   outbound HTTPS; restore a permitted network configuration if your project has
   no default network or forbids public egress.
8. Confirm Dream account concurrency and usage limits using the ten-match pilot.
   Check Cloud Build's Linux render gate and pilot resource/timing metrics before
   submitting hundreds of matches. Verify budget-alert recipients on the billing
   account. The $50 budget produces alerts only; it does not stop jobs.

## Local validation

```powershell
& ..\hill_climb_harness\.venv\Scripts\python.exe -m pytest -q
& ..\hill_climb_harness\.venv\Scripts\python.exe -m cloud.validate
& ..\hill_climb_harness\.venv\Scripts\python.exe -m cloud.run --policy scripted --out cloud/generated/smoke
```

Local rendering is offscreen with no native viewer. The committed three PNGs
come from the real relay scene after forty physics steps. The overhead detector
finds four visible legal white balls and both black balls; other golf balls are
occluded or merged into adjacent white geometry in that view. The wrist guard
uses only forbidden balls inside the grip ROI. This colour detector remains a
heuristic, not proof that an arbitrary future pose cannot hide an 8-ball.

Collector fields match stress output: `scenario, seed, points, lost, dq,
black_in_bin, out, plugged, tower, balls, t_end, reason, stages, label`. `tower`
is metres, `dq` is a list, and `stages` contains name/ok/attempt tuples serialized
as arrays. Milestone fields add `stage1_at`, `stage2_at` and `first_touch`, in
referee physics seconds. `t_end` remains harness elapsed seconds so existing
early-finish plots retain their meaning; `match_wall_s` records the full horizon.
