# Cost and sizing

Measured on 2026-11-08 with the existing Windows sibling venv. No Dream key,
Claude key, cloud job or cloud build was used. The command was:

```powershell
& ..\hill_climb_harness\.venv\Scripts\python.exe -m cloud.run --policy scripted --out cloud/evidence/smoke
```

The full result is in `evidence/smoke-match.json`. The match used the production
Runner, v4 plan, live image guard, measured grippers and delayed oracle.

| Measurement | Value |
|---|---:|
| Total process wall time, including setup and shutdown | 301.156 s |
| Match clock | 300.000 s |
| Process CPU time | 136.531 s |
| Consumed vCPU-minutes per match | 2.276 |
| Mean CPU use, one core = 100% | 45.3% |
| Peak process RSS, sampled every 200 ms | 689.34 MiB |
| Physics time at scoring | 194.05 s |
| Oracle calls | 43 |
| Result | 0 points, no DQ, no black ball in bin |

The sinusoidal scripted policy never unplugs a cable, so the first stage used
the whole 295-second harness budget, then the match held through 300 seconds.
This is a performance and integration check, not a policy-quality result.
CPU time includes all process threads and the guard. Windows offscreen OpenGL
can use the local graphics driver; this does not measure Linux OSMesa CPU use.
The camera render/step overhead also prevented 1:1 physics time. Compare both
wall and physics time in the pilot before interpreting scores.

## Selected shape

Use `e2-highcpu-2` Spot, two vCPUs and 2 GiB, with **N = 1 concurrent match per
VM**. The task requests 2,000 milliCPU and 1,280 MiB. Its measured RSS leaves
about 590 MiB inside that task allocation, and the VM leaves 768 MiB for the
host. Two matches on this shape would consume the measured RAM plus the OS
with little room for software rendering, and share the two renderer threads.

This is the smallest full-CPU E2 candidate that fits the local measurement.
It is the expected cheapest suitable predefined E2 shape, subject to the Linux
pilot. Shared-core E2 shapes have sustained CPU limits and are unsuitable for
assuming constant 20 Hz control. Larger shapes have no demonstrated packing
benefit. `--matches-per-task K` runs K matches sequentially to amortize VM and
image startup; it does not pack K concurrent matches on a VM.

The official [E2 shape table](https://docs.cloud.google.com/compute/docs/general-purpose-machines)
lists the shape and [Spot pricing](https://cloud.google.com/spot-vms/pricing)
explains variable regional prices. Use **$0.030 per VM-hour in us-central1 as a
planning assumption**, not a verified live quote. The official pricing page's
retrieved E2 standard-2 comparator was $0.040212/hour; a direct current price for
highcpu-2 could not be established from the retrieved table. Confirm the actual
highcpu-2 quote in the calculator before running. The scripts do not query billing.

Allocated CPU is 10 vCPU-minutes per 300-second match, regardless of the 2.276
CPU-minutes actually consumed. Formula: matches × VM seconds / 3,600 × $0.030.

| Matches | Match-only VM hours | VM cost at 300 s | VM cost with 60 s startup per task | Dream realtime usage |
|---:|---:|---:|---:|---:|
| 100 | 8.333 | $0.25 | $0.30 | 500 min / 8.333 h |
| 1,000 | 83.333 | $2.50 | $3.00 | 5,000 min / 83.333 h |
| 5,000 | 416.667 | $12.50 | $15.00 | 25,000 min / 416.667 h |

The ten-match pilot is $0.025 match-only, or $0.030 with that startup allowance.
At parallelism eight, 1,000 matches take at least 10.42 hours of match time,
plus startup and scheduling. Eight concurrent tasks means up to eight Dream
sessions and, in API judge mode, up to sixteen monitor calls in flight.

Dream usage above is the requested five-minute-per-match envelope. Actual billed
inference depends on Dream's session billing, starts/stops and retries. It is not
priced here because the account's rate is unknown. A match can create multiple
Dream sessions during recovery. Judge API mode additionally bills Claude image
and text tokens; oracle mode has no Claude API charge. The measured oracle count
was 43 per match, not a guaranteed count or API token estimate.

Build time, image storage, 30 GB boot disks, source staging, GCS results, logs,
network traffic, tax and interrupted/retried matches are excluded. Batch allows
two task retries, so budget for repeated Dream usage as well as VM time. A monthly
budget alert is not a hard spending cap. For other rates or startup times, scale
the table using the formula above. Do not scale to thousands until the pilot's
Linux RSS, camera freshness, physics/wall ratio and Dream concurrency are checked.
