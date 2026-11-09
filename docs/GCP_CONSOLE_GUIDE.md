# Watching Hill Climb simulations on Google Cloud

Use **<your-gmail>**, project **hill-climb-sims**, region
**us-central1**. The project number is `14336004864`. The links below select
Chrome account slot 1 with `authuser=1`. Check the profile email and project
header after opening each link. Leave the default tamu.edu account alone.

## Console links

| Page | What to select |
| --- | --- |
| [Batch jobs](https://console.cloud.google.com/batch/jobs?project=hill-climb-sims&authuser=1) | Region us-central1. Open a pilot job, then Tasks and Logs. Clear filters that hide completed jobs. |
| [Compute Engine VMs](https://console.cloud.google.com/compute/instances?project=hill-climb-sims&authuser=1) | Filter by label `batch-job-id` or `batch-job-uid`. The pilots ran in us-central1-f. Finished Batch VMs disappear automatically. |
| [Results bucket](https://console.cloud.google.com/storage/browser/hill-climb-sims-results?project=hill-climb-sims&authuser=1) | Objects, then `runs/JOB/ATTEMPT/CONFIG-SEED/`. A run with no successful uploads has no folder to open. |
| [Logs Explorer with the task filter](https://console.cloud.google.com/logs/query;query=logName%3D%22projects%2Fhill-climb-sims%2Flogs%2Fbatch_task_logs%22;duration=PT24H?project=hill-climb-sims&authuser=1) | Query library, Saved, **hill-climb tasks**, then run. Use a time range containing November 8, 2026, 10:15-10:30 UTC for these pilots. |
| [Hill Climb sims dashboard](https://console.cloud.google.com/monitoring/dashboards/builder/3e8d98be-176f-48e2-99e5-fad5884ca504?project=hill-climb-sims&authuser=1) | Select a range covering the pilot. Blank recent VM charts after cancellation are expected. |
| [Quotas and system limits](https://console.cloud.google.com/iam-admin/quotas?project=hill-climb-sims&authuser=1) | Service Compute Engine API. Inspect CPUs all regions and E2 CPUs in us-central1. Also inspect quota requests. |
| [Billing reports](https://console.cloud.google.com/billing/<BILLING_ACCOUNT_ID>/reports?project=hill-climb-sims&authuser=1) | Billing account My Billing Account, project hill-climb-sims. Group by Service, then SKU for details. |
| [$50 budget](https://console.cloud.google.com/billing/<BILLING_ACCOUNT_ID>/budgets/c8164699-01cd-4096-962c-d32c51d8d5d2?project=hill-climb-sims&authuser=1) | Budget hill-climb-sims-monthly. Keep the amount at $50. |
| [Budgets list](https://console.cloud.google.com/billing/<BILLING_ACCOUNT_ID>/budgets?project=hill-climb-sims&authuser=1) | Open hill-climb-sims-monthly if the individual-budget route changes. |
| [Billing account management](https://console.cloud.google.com/billing/<BILLING_ACCOUNT_ID>/manage?project=hill-climb-sims&authuser=1) | View billing IAM and email recipients. No permission change is needed. |
| [Enabled APIs](https://console.cloud.google.com/apis/dashboard?project=hill-climb-sims&authuser=1) | Verify the services listed in the audit below. |
| [Artifact Registry](https://console.cloud.google.com/artifacts?project=hill-climb-sims&authuser=1) | us-central1, hill-climb, sim, tags cloud-v1 and cloud-v2. |
| [Service accounts](https://console.cloud.google.com/iam-admin/serviceaccounts?project=hill-climb-sims&authuser=1) | hill-climb-worker and hill-climb-builder. |
| [Secret Manager metadata](https://console.cloud.google.com/security/secret-manager?project=hill-climb-sims&authuser=1) | Inspect names and version states only. Never open secret values. |
| [Cloud NAT](https://console.cloud.google.com/net-services/nat/list?project=hill-climb-sims&authuser=1) | hc-nat, hc-router, us-central1. |
| [VPC networks](https://console.cloud.google.com/networking/networks/list?project=hill-climb-sims&authuser=1) | default, subnet default in us-central1, Private Google Access on. |

The Logs Explorer URL preloads the saved query's exact filter. It does not depend
on an undocumented saved-query URL parameter. The saved query itself is private
to the Gmail identity and lives at
`projects/hill-climb-sims/locations/global/savedQueries/hill-climb-tasks`.

**USER ACTION NEEDED:** reconnect the Chrome browser plugin and grant this
session access to Google Chrome, then check these console pages. The browser
tool reported `The browser access host is unavailable`; the native app route
reported `Computer Use was not approved to use Google Chrome`. No console page
was verified or changed through Chrome. CLI and API verification below succeeded.
If any page requests sign-in, phone verification, payment details, terms, or a
user decision, complete that step yourself. Do not use the tamu.edu account.

## Audit on November 8, 2026

The audit used gcloud 592.0.0 with explicit Gmail account and project flags.
The CLI's billing IAM command returned INVALID_ARGUMENT. The documented
read-only Cloud Billing REST endpoint succeeded with the same gcloud identity.
Access tokens stayed in process memory and were never printed or saved.

| Item | Result and changes |
| --- | --- |
| Project and billing link | VERIFIED. Active project 14336004864, billing enabled on <BILLING_ACCOUNT_ID>. No change. |
| Required APIs | VERIFIED. batch, compute, artifactregistry, cloudbuild, secretmanager, storage, billingbudgets, logging and iam are enabled. Monitoring is also enabled. No API enabled by this work. |
| Artifact Registry | VERIFIED. Docker repository hill-climb in us-central1. Both sim:cloud-v1 and sim:cloud-v2 exist. No rebuild or image change. |
| Results bucket | VERIFIED. Regional STANDARD storage in US-CENTRAL1, uniform access true, public access prevention enforced. No change. |
| Worker and builder identities | VERIFIED. Both exist and are enabled. No service-account change. |
| Worker bucket IAM | VERIFIED. Bucket-scoped objectCreator and objectViewer. No change. |
| Worker secret IAM | VERIFIED. secretAccessor on all three named secret containers. No change. |
| Worker project/repository IAM | VERIFIED. batch.agentReporter and logging.logWriter at project scope, artifactregistry.reader on hill-climb. No change. |
| Builder IAM | VERIFIED. Repository writer, bucket objectViewer and project logWriter. No change. |
| Secret versions | VERIFIED from metadata only. dream-api-key and openrouter-api-key each have enabled version 1. anthropic-api-key is empty. No values accessed or versions added. |
| Budget | VERIFIED. $50 USD monthly, project filter 14336004864, actual-spend alerts at 50%, 90%, 100%. No change; etag 1794130969301254 remained unchanged. |
| Budget recipients | VERIFIED. Default IAM recipients are enabled, with no custom channel. Billing IAM lists <your-gmail> as roles/billing.admin. This Gmail is therefore a configured recipient. Actual inbox delivery was not tested. |
| Router and NAT | VERIFIED. hc-router on default in us-central1; hc-nat is AUTO_ONLY, all subnet IP ranges, minimum 256 ports per VM. No change. |
| Private Google Access | VERIFIED. Enabled on default subnet in us-central1. Pilot VMs had no external IPs. No change. |
| Pilot jobs | VERIFIED initially RUNNING, 10 tasks and parallelism 4 each, 12 Spot e2-highcpu-2 VMs total in us-central1-f. Later observed CANCELLED as detailed below. No operation on the jobs or their VMs was issued. |
| CPU requests | SUBMITTED, then DENIED, both with message Quota request denied. Details below. |
| Dashboard | CREATED and read back. Hill Climb sims, ID 3e8d98be-176f-48e2-99e5-fad5884ca504. Server-side validation passed before creation. |
| Saved query | CREATED and read back. hill-climb tasks, ID hill-climb-tasks, location global, private to Gmail. Filter is `logName=projects/hill-climb-sims/logs/batch_task_logs`. |
| Status script | CREATED, syntax-checked, and run against all three pilots. Only Bash builtins and gcloud are used. Additional offline checks covered partial attempts, unknown task states, read failures and credential-line redaction. |
| Console verification | USER ACTION NEEDED. Chrome access is blocked. Use the links and selectors above to verify the UI. |

**IAM changes: none.** The only cloud writes were the two requested quota
preferences, the dashboard and the private saved query. No VMs, jobs, buckets,
secrets, routers or projects were created, edited or deleted. Billing account,
payment methods, budget amount, logs and result objects were left untouched.

## Quota requests and Spot capacity

Both requests used the Gmail contact and the requested justification:

> Batch robotics simulation (MuJoCo) on Spot VMs, ~100 concurrent 2-vCPU tasks for short runs.

| Request | State | Evidence |
| --- | --- | --- |
| CPUs all regions, 32 to 200 | DENIED | hill-climb-cpus-all-regions-200. Quota ID CPUS-ALL-REGIONS-per-project. Message Quota request denied. Trace 847cee18-bb23-4cd2-9c7a-2c02c725c40e. |
| E2 CPUs, us-central1, 24 to 200 | DENIED | hill-climb-e2-us-central1-200. Current API quota ID CPUS-PER-VM-FAMILY-per-project-region, dimensions region=us-central1,vm_family=E2. Message Quota request denied. Trace 99c88133-28d7-4aca-8680-a3d1ecdfcc1b. |

The Cloud Quotas API uses the unified VM-family quota for E2; the old
`E2-CPUS-per-project-region` ID is not present. After denial, Compute Engine
still reported **CPUS_ALL_REGIONS 32**, **E2_CPUS 24**, regional **CPUS 200**,
**INSTANCES 24**, and **PREEMPTIBLE_CPUS 0**. The preference response's E2
`grantedValue=0` is not the effective Compute Engine E2 limit of 24. No request
was retried or broadened. Neither request required verification or payment.

**USER ACTION NEEDED:** review the denied requests on the Quotas page or discuss
these trace IDs with Google Cloud support if larger runs are still needed.

The read-only Compute capacity adviser, queried for 100 e2-highcpu-2 Spot VMs
with distribution ANY, returned **obtainability 0.9**, **estimated uptime 3600s**,
and a recommendation for all 100 in **us-central1-f**. This is a changing
capacity estimate, not a reservation or a guarantee, and it does not override
quota limits. No capacity test VMs or reservations were created.

No second region was selected because the brief made that decision conditional
on quota approval, and both requests were denied. Before selecting one, compare
current regional E2 Spot prices plus inter-region result/image transfer and a
second NAT gateway's charges. The $0.030/VM-hour in cloud/COST.md is a planning
assumption, not a verified current quote.

## Dashboard contents

The dashboard has six widgets:

1. Running Batch tasks, with links to authoritative Batch task counts and the
   status command. The project has no native Batch task-count metric. This
   widget explicitly explains that limitation.
2. Reporting simulation VMs, using reserved-core metric series counted in
   60-second intervals.
3. CPU utilization per simulation VM.
4. hc-nat allocation failures, using the BOOL allocation-failure metric aligned
   as true-sample counts per minute.
5. Batch agent task-state reports.
6. Task logs and errors.

VM charts select names starting with `pilot2-` or `hill-climb-`. Use that naming
convention for future jobs or revise the dashboard filter. The queries were
tested against real Monitoring data: the VM series showed 12 reporting VMs,
CPU series contained pilot history, and hc-nat returned zero allocation failure
samples. Metrics can lag several minutes. Historical series can outlive VMs.
A blank chart is not an authoritative zero task count.

A live numeric RUNNING-task chart would require a task-state metric exporter
or other additional data source. None was created under the brief's resource
restrictions. Use the linked Batch Tasks tab or status.sh for exact task counts.

## Healthy runs and stalled runs

Run this in Git Bash from this checkout:

```bash
bash cloud/status.sh
# Or select a specific existing job:
bash cloud/status.sh pilot2-oracle
```

The script pins the Gmail account, project and us-central1 on every gcloud call.
It discovers the existing Windows SDK path if gcloud is absent from PATH; an
explicit executable can also be supplied through `GCLOUD`. It performs no cloud
writes. Read failures print UNKNOWN and return exit code 1 instead of reporting
false zero counts. A healthy read returns 0 even if a job itself failed.

The script reports the job state, task-state counts, current VM counts, uploaded
match.json and complete.json counts, and the latest five task error entries per
job from the last seven days. QUEUED and SCHEDULED are job states. Batch tasks
normally use PENDING, ASSIGNED, RUNNING, SUCCEEDED, FAILED or UNEXECUTED, so the
requested QUEUED/SCHEDULED columns are retained alongside the actual API states.

Object counts cover every attempt, including retries, and are not deduplicated
match counts. Only complete.json marks a fully uploaded, complete match. A
match.json without that marker is not ready for collection. The entrypoint
writes worker.log locally while a match runs, then uploads the output and the
marker last. Quiet task logs during the roughly 300-second match can be normal.

A healthy job moves through QUEUED, SCHEDULED and RUNNING, gains successful tasks,
and uploads completion markers. Compare successive snapshots. CPU activity,
recent Batch agent reports and increasing completed uploads support health.
Check a job that remains queued without VMs for quota or capacity errors. Check
repeated failures, upload errors, stale agent reports, or absent completion
markers after expected match/retry durations. A RUNNING VM alone does not prove
the simulation is healthy. Use the configured 900-second task timeout and status
events when judging whether a task is stuck; do not automatically cancel it.

## What happened to these pilots

Initially all three protected pilots were RUNNING, each with four VMs. During
this audit the service reported transitions to CANCELLATION_IN_PROGRESS:

| Job | Transition time UTC |
| --- | --- |
| pilot2-oracle | 2026-11-08 10:27:55.461665405 |
| pilot2-gemini-h2 | 2026-11-08 10:27:58.367976028 |
| pilot2-alt-gemini-haiku | 2026-11-08 10:28:01.508344286 |

Subsequent reads showed CANCELLED and zero remaining VMs. This session issued
no cancel/delete/edit/resubmit call. Read-only Batch audit-log queries returned
no entries, so this audit cannot identify the caller responsible for the
cancellation. Do not infer the caller from the active account alone.

All three jobs reported `output upload failed; check bucket IAM and network`.
The final reads found zero match.json and complete.json objects for every pilot.
The IAM and network configuration matched the brief, so these messages alone do
not establish the root cause. No corrective change or rerun was attempted.

**USER ACTION NEEDED:** review the cancellation and upload failures before
authorizing any later run. Keep the existing job history, logs and bucket
objects. Do not interpret leftover task RUNNING counts on a CANCELLED job as
live work; task states can remain recorded after cancellation. The current job
state and VM inventory take precedence for whether compute is still running.

## Cancel a future job safely

The three pilot2 jobs remain protected by the brief. This section describes a
manual procedure for a different job that you have chosen to stop.

1. Verify Gmail, project, region and the exact job name. Capture its status and
   uploaded-marker counts first. A cancellation can lose the current task's
   local, not-yet-uploaded output and does not refund completed API inference.
2. Use Batch cancellation for that job, rather than deleting individual managed
   VMs. In Git Bash, replace FUTURE-JOB with the intended non-protected job:

   ```bash
   gcloud batch jobs cancel FUTURE-JOB --location=us-central1 \
     --project=hill-climb-sims --account=<your-gmail>
   ```

3. Read the job until it reaches CANCELLED and verify its VMs disappear. Leave
   bucket results, logs, NAT, secrets and job history in place. Cancellation
   retains Batch job information for up to 60 days, subject to Google's normal
   retention; deleting the job removes that history sooner.

This procedure was not executed during the audit.

## Reading costs

| Service or SKU | What causes the charge |
| --- | --- |
| Batch | Orchestrates jobs; underlying compute, storage and network are billed separately. |
| Compute Engine E2 Spot CPU and RAM | Allocated VM runtime, including startup, idle periods and retries. Low measured CPU use does not reduce allocated-VM billing. |
| Persistent Disk | Each pilot VM requested a 30 GB pd-standard boot disk. Disk provisioned capacity is billed while it exists. |
| Cloud NAT gateway and data processing | Private VMs' outbound internet traffic uses hc-nat. Existing NAT and its auto-allocated IP can continue to incur charges after Batch VMs finish. |
| External IPv4 | NAT's allocated public IP can be billed separately. These pilot VMs had no individual external IP. |
| Network transfer | Internet and applicable inter-region traffic. A second region can add transfer charges to the central results bucket and registry. |
| Artifact Registry | Stored image bytes and applicable data transfer. Both version tags remain stored. |
| Cloud Build | Container build machine time, subject to billing allowances. Source staging objects also occupy storage. No build was run during this audit. |
| Cloud Storage | Result/source bytes, operations, applicable retrieval/transfer, and retained soft-deleted data. Bucket soft-delete retention is seven days. |
| Cloud Logging and Monitoring | Log ingestion/retention and any chargeable telemetry beyond allowances. This dashboard reads existing service metrics; it creates no exporter or custom metric. |
| Secret Manager | Stored active versions and access operations, subject to allowances. No secret version was read during this audit. |
| Dream and OpenRouter/Anthropic | Separate providers' inference/session/token charges. They are not covered by the GCP $50 budget. |

The $50 budget covers the selected GCP project, includes credits, and sends
actual-spend alerts at $25, $45 and $50. It is an alerts-only budget, not a hard
spending cap. Billing and alert delivery can lag usage. The recipient is already
the Gmail billing administrator; no additional role or notification channel is
needed. Do not change the budget amount or payment settings.

## Sources and validation

The research gate preceded cloud writes: local README/control/entrypoint/cost
files and live metadata were inspected; official documentation established the
quota, dashboard, saved-query and capacity-advice workflows. Read-only series
queries verified the metric types, labels, BOOL conversion and aggregations.
The dashboard was validated separately before creation and read back afterward.

- [Cloud Quotas CLI workflows](https://docs.cloud.google.com/docs/quotas/gcloud-cli-examples)
- [Dashboard create and validate-only](https://docs.cloud.google.com/sdk/gcloud/reference/monitoring/dashboards/create)
- [Saved Logging queries](https://docs.cloud.google.com/sdk/gcloud/reference/logging/saved-queries/create)
- [Batch resource metrics](https://docs.cloud.google.com/batch/docs/monitor-job-resources-using-metrics)
- [Spot capacity advice](https://docs.cloud.google.com/compute/docs/instances/view-vm-availability)
- [Billing IAM read API](https://docs.cloud.google.com/billing/docs/reference/rest/v1/billingAccounts/getIamPolicy)
- [Budget recipients](https://docs.cloud.google.com/billing/docs/how-to/budgets-notification-recipients)
- [Cancel Batch jobs](https://docs.cloud.google.com/batch/docs/cancel-job)
- [Spot pricing](https://cloud.google.com/spot-vms/pricing)

## Status script output

The following sequential snapshot was captured after cancellation. It returned
exit code 0, meaning the reads succeeded. It does not mean the jobs succeeded.

```text
Project=hill-climb-sims account=<your-gmail> region=us-central1
Reads are sequential snapshots. Object counts include all retry attempts.
QUEUED/SCHEDULED are job states; PENDING/ASSIGNED are task states.

pilot2-oracle
  state=CANCELLED
  tasks=10 QUEUED=0 SCHEDULED=0 RUNNING=3 SUCCEEDED=0 FAILED=1 PENDING=6 ASSIGNED=0 UNEXECUTED=0 OTHER=0
  VMs=0 RUNNING=0
  uploaded match.json=0 complete.json=0
  last 5 task error entries (newest first, last 7 days):
    2026-11-08T10:23:04.559531625Z	ERROR	USER ACTION NEEDED: output upload failed; check bucket IAM and network. Local logs remain in /out.
    2026-11-08T10:23:02.365343882Z	ERROR	USER ACTION NEEDED: output upload failed; check bucket IAM and network. Local logs remain in /out.
    2026-11-08T10:23:01.902323212Z	ERROR	USER ACTION NEEDED: output upload failed; check bucket IAM and network. Local logs remain in /out.
    2026-11-08T10:23:01.393496763Z	ERROR	USER ACTION NEEDED: output upload failed; check bucket IAM and network. Local logs remain in /out.

pilot2-gemini-h2
  state=CANCELLED
  tasks=10 QUEUED=0 SCHEDULED=0 RUNNING=0 SUCCEEDED=0 FAILED=4 PENDING=6 ASSIGNED=0 UNEXECUTED=0 OTHER=0
  VMs=0 RUNNING=0
  uploaded match.json=0 complete.json=0
  last 5 task error entries (newest first, last 7 days):
    2026-11-08T10:28:14.455905092Z	ERROR	time="2026-11-08T10:28:14Z" level=error msg="error waiting for container: unexpected EOF"
    2026-11-08T10:23:14.818310152Z	ERROR	USER ACTION NEEDED: output upload failed; check bucket IAM and network. Local logs remain in /out.
    2026-11-08T10:23:14.671579589Z	ERROR	USER ACTION NEEDED: output upload failed; check bucket IAM and network. Local logs remain in /out.
    2026-11-08T10:23:14.215856152Z	ERROR	USER ACTION NEEDED: output upload failed; check bucket IAM and network. Local logs remain in /out.
    2026-11-08T10:23:12.703398013Z	ERROR	USER ACTION NEEDED: output upload failed; check bucket IAM and network. Local logs remain in /out.

pilot2-alt-gemini-haiku
  state=CANCELLED
  tasks=10 QUEUED=0 SCHEDULED=0 RUNNING=4 SUCCEEDED=0 FAILED=0 PENDING=6 ASSIGNED=0 UNEXECUTED=0 OTHER=0
  VMs=0 RUNNING=0
  uploaded match.json=0 complete.json=0
  last 5 task error entries (newest first, last 7 days):
    2026-11-08T10:28:13.104394896Z	ERROR	USER ACTION NEEDED: output upload failed; check bucket IAM and network. Local logs remain in /out.
    2026-11-08T10:28:09.867262858Z	ERROR	USER ACTION NEEDED: output upload failed; check bucket IAM and network. Local logs remain in /out.
    2026-11-08T10:28:08.673180412Z	ERROR	USER ACTION NEEDED: output upload failed; check bucket IAM and network. Local logs remain in /out.
    2026-11-08T10:28:08.571905248Z	ERROR	USER ACTION NEEDED: output upload failed; check bucket IAM and network. Local logs remain in /out.
    2026-11-08T10:23:37.411127767Z	ERROR	USER ACTION NEEDED: output upload failed; check bucket IAM and network. Local logs remain in /out.
```
