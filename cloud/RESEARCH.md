# Research and validation record

Research date: 2026-11-08. No cloud resources were queried or changed.

Local baseline: `cluster/Dockerfile`, `cluster/run_matches.sh`, `FINAL_HARNESS.md`,
`HARNESS_GUIDE.md`, `experiments/sim_match.py`, the referee, station add-on,
frame client, runner and monitor were inspected. No capsule configuration exists.
The sibling venv has dreamlabs 1.0.37, MuJoCo 4.0.0, NumPy 2.2.6 and Pillow 13.0.0.
Its installed `dream_examples/yam_mujoco/yam_env.py` is the reference implementation.
`YamEnv.state()` returns seven measured values per arm, with normalized opening at
index 6. Finger actuator controls use meters, 0.0475 per finger at full opening.
`images()` returns top/left/right RGB arrays. Only the simulation thread may read
MuJoCo state or render; HTTP handlers must use snapshots.

Supported workflows and primary sources:

- [Dream package 1.0.37](https://pypi.org/project/dreamlabs/1.0.37/).
- [MuJoCo changelog](https://mujoco.readthedocs.io/en/stable/changelog.html),
  4.0.0 released November 6. Pin only after build, step and offscreen-render checks.
- [Batch jobs](https://docs.cloud.google.com/batch/docs/create-run-basic-job):
  taskCount, parallelism, container runnable and allocationPolicy.
- [Batch secrets](https://docs.cloud.google.com/batch/docs/create-run-job-secret-manager):
  environment.secretVariables maps names to secret version resource paths.
- [Custom job account](https://docs.cloud.google.com/batch/docs/create-run-job-custom-service-account):
  dedicated VM identity, resource-scoped secret, image and output permissions.
- [Budget CLI](https://docs.cloud.google.com/sdk/gcloud/reference/billing/budgets/create):
  project-filtered monthly amount and threshold rules, default billing recipients.
- [Spot pricing](https://cloud.google.com/products/compute/pricing): variable pricing,
  interruption possible; prices are assumptions until verified for the chosen region.

Plausible failure causes: missing GL libraries, incompatible model/compiler API,
wrong camera aliases, stale render snapshots, real-table colour roles applied to
the white sim balls, synchronous oracle blocking health checks, real-table prompts
contradicting sim colours, missing ADC/secret permissions, Spot quota or capacity.

First diagnostic: build the installed example with the relay-table patch, step
without policy/network, save three offscreen frames and inspect state and pixel
statistics. Success requires finite state, nonuniform images and six white/two
black ball geoms. Then test HTTP snapshots, asynchronous judge scheduling and job
JSON locally. Only then run one 300-second scripted match and measure process CPU,
wall time and peak RSS. Linux OSMesa is separately checked during Cloud Build.
No Dream calls, cloud build, job submission or downstream training in this session.

Results: the installed original example and the patched relay scene both build,
step and render with MuJoCo 4.0.0. All three offscreen camera images have nonzero
contrast. `/frame` and `/arms` integration checks passed. The scripted full match
ran for 300 wall seconds, with 301.156 s total wall, 136.531 s CPU and 689.34 MiB
sampled peak RSS. The referee advanced 194.05 physics seconds. See COST.md.

A supplemental Linux/Python 3.12 wheel-only dependency dry-run did not finish:
the sibling venv has no pip, so the system pip was used without installation;
its first target-tag list omitted JAX's manylinux_2_27 wheel. Official PyPI's
[JAX 0.7.1 files](https://pypi.org/project/jaxlib/0.7.1/#files) established the tag.
With all supported glibc tags, the next check reached Dream's source-only
`portal==3.7.3`. Its [official release](https://pypi.org/project/portal/3.7.3/#files)
contains only a source archive. The known-good installed Portal contains Python
files and no native extensions, depending on cloudpickle/msgpack/numpy/psutil.
The container uses ordinary pip, which permits this source package; no pins were
changed to force a wheel-only check through. Linux installation and OSMesa remain
Cloud Build gates, unexecuted under the brief's cloud-change prohibition.
