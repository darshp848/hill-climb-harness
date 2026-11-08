"""gcloud-only cloud operations. Dry-run never invokes gcloud or reads credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BILLING = os.environ.get("HILL_CLIMB_BILLING_ACCOUNT", "")  # set it before setup


def action(message):
    raise SystemExit("USER ACTION NEEDED: " + message)


class Gcloud:
    def __init__(self, args):
        self.args = args
        self.exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
        if args.execute and not self.exe:
            action("Install Google Cloud CLI and run gcloud auth login with your personal Gmail.")

    def call(self, *argv, optional=False, data=None):
        command = [self.exe or "gcloud", *argv, "--project", self.args.project, "--quiet"]
        if not self.args.execute:
            print("DRY RUN: " + shlex.join(["gcloud", *command[1:]]))
            return ""
        result = subprocess.run(command, input=data, capture_output=True)
        if result.returncode:
            err = result.stderr.decode(errors="replace")
            if optional and any(x in err.lower() for x in ("not_found", "not found", "does not exist", "404")):
                return None
            # Do not forward upstream stderr, especially on credential-upload failures.
            action(f"gcloud {' '.join(argv[:3])} failed. Check login, billing link to {BILLING}, "
                   "API terms, IAM permissions, regional Spot CPU quota and capacity in your own gcloud console.")
        return result.stdout.decode().strip()

    def ensure(self, describe, create):
        if not self.args.execute:
            print("If missing:")
            self.call(*create)
        elif self.call(*describe, optional=True) is None:
            self.call(*create)


def bindings(gc, resource_args, account, roles):
    for role in roles:
        gc.call(*resource_args, "--member", "serviceAccount:" + account, "--role", role)


def setup(args, gc):
    p, r, bucket = args.project, args.region, "gs://" + args.bucket
    worker = f"hill-climb-worker@{p}.iam.gserviceaccount.com"
    builder = f"hill-climb-builder@{p}.iam.gserviceaccount.com"
    print(f"Will configure project {p}, region {r}, repository hill-climb, bucket {bucket}, "
          f"worker/builder accounts, Dream/Claude secret containers and a {args.budget} USD budget alert.")
    print("Project creation and billing linking are manual. Existing secret versions are never rotated.")
    if args.execute:
        account = gc.call("auth", "list", "--filter=status:ACTIVE", "--format=value(account)")
        if not account:
            action("Run gcloud auth login with the Gmail owning the billing account.")
        gc.call("projects", "describe", p)
        billing = json.loads(gc.call("billing", "projects", "describe", p, "--format=json"))
        if not billing.get("billingEnabled") or not billing.get("billingAccountName", "").endswith(BILLING):
            action(f"Link project {p} to billing account {BILLING} in the console, then rerun setup.")
    gc.call("services", "enable", "batch.googleapis.com", "compute.googleapis.com",
            "artifactregistry.googleapis.com", "cloudbuild.googleapis.com", "secretmanager.googleapis.com",
            "storage.googleapis.com", "billingbudgets.googleapis.com", "logging.googleapis.com",
            "iam.googleapis.com")
    gc.ensure(["artifacts", "repositories", "describe", "hill-climb", "--location", r],
              ["artifacts", "repositories", "create", "hill-climb", "--location", r, "--repository-format=docker"])
    gc.ensure(["storage", "buckets", "describe", bucket],
              ["storage", "buckets", "create", bucket, "--location", r,
               "--uniform-bucket-level-access", "--public-access-prevention"])
    for name, email in [("hill-climb-worker", worker), ("hill-climb-builder", builder)]:
        gc.ensure(["iam", "service-accounts", "describe", email],
                  ["iam", "service-accounts", "create", name])
    bindings(gc, ["projects", "add-iam-policy-binding", p], worker,
             ["roles/batch.agentReporter", "roles/logging.logWriter"])
    bindings(gc, ["projects", "add-iam-policy-binding", p], builder, ["roles/logging.logWriter"])
    bindings(gc, ["artifacts", "repositories", "add-iam-policy-binding", "hill-climb", "--location", r],
             worker, ["roles/artifactregistry.reader"])
    bindings(gc, ["artifacts", "repositories", "add-iam-policy-binding", "hill-climb", "--location", r],
             builder, ["roles/artifactregistry.writer"])
    bindings(gc, ["storage", "buckets", "add-iam-policy-binding", bucket], worker,
             ["roles/storage.objectCreator", "roles/storage.objectViewer"])   # gcloud storage cp needs objects.get
    bindings(gc, ["storage", "buckets", "add-iam-policy-binding", bucket], builder,
             ["roles/storage.objectViewer"])
    for secret in ("dream-api-key", "anthropic-api-key", "openrouter-api-key"):
        gc.ensure(["secrets", "describe", secret],
                  ["secrets", "create", secret, "--replication-policy=automatic"])
        bindings(gc, ["secrets", "add-iam-policy-binding", secret], worker, ["roles/secretmanager.secretAccessor"])
    budget_name = "hill-climb-sims-monthly"
    existing = gc.call("billing", "budgets", "list", "--billing-account", BILLING,
                       "--filter=displayName=" + budget_name, "--format=value(name)")
    if not existing:
        gc.call("billing", "budgets", "create", "--billing-account", BILLING,
                "--display-name", budget_name, "--budget-amount", f"{args.budget}USD",
                "--filter-projects", "projects/" + p,
                "--threshold-rule=percent=0.5", "--threshold-rule=percent=0.9",
                "--threshold-rule=percent=1.0")
    # Upload only through the user's authenticated gcloud. Values never enter argv or a temp file.
    if args.execute:
        versions = gc.call("secrets", "versions", "list", "dream-api-key", "--filter=state:ENABLED",
                           "--format=value(name)", "--limit=1")
        if not versions:
            credentials = Path.home() / ".dreamlabs/credentials"
            if not credentials.is_file():
                action("Save the Dream setup block in ~/.dreamlabs/credentials, then rerun setup.")
            key = None
            for line in credentials.read_text(encoding="utf-8").splitlines():
                name, sep, value = line.removeprefix("export ").partition("=")
                if sep and name.strip() == "DREAM_API_KEY":
                    try:
                        key = " ".join(shlex.split(value, comments=True))
                    except ValueError:
                        action("Fix DREAM_API_KEY quoting in ~/.dreamlabs/credentials.")
            if not key:
                action("Add DREAM_API_KEY to ~/.dreamlabs/credentials, then rerun setup.")
            gc.call("secrets", "versions", "add", "dream-api-key", "--data-file=-", data=key.encode())
            key = None
        for email in (worker, builder):
            gc.call("iam", "service-accounts", "add-iam-policy-binding", email,
                    "--member", "user:" + account, "--role", "roles/iam.serviceAccountUser")
    else:
        print("DRY RUN: upload Dream key via stdin only if no enabled version; grant current user actAs on both accounts.")
    print("Budget alerts notify billing IAM recipients; they do not cap spending.")


def tasks(matrix, pilot=False):
    import yaml
    raw = yaml.safe_load(Path(matrix).read_text(encoding="utf-8"))
    if raw.get("scene_randomisation") != "none":
        raise ValueError("the current scene supports only scene_randomisation: none")
    groups = []
    for config in raw["configs"]:
        if not re.fullmatch(r"[a-z0-9_-]+", config["name"]):
            raise ValueError("invalid config name")
        if not {"--match", "--detect", "--live-guard"} <= set(config["args"]):
            raise ValueError("matrix config must enable match, detect and live guard")
        seeds = config["seeds"]
        if seeds["count"] < 1:
            raise ValueError("seed count must be positive")
        groups.append([{**config, "seed": s} for s in range(seeds["start"], seeds["start"] + seeds["count"])])
    # Interleave configs so a ten-match pilot covers both plans.
    result = [g[i] for i in range(max(map(len, groups))) for g in groups if i < len(g)]
    if pilot and len(result) < 10:
        raise ValueError("pilot requires at least ten matrix entries")
    return result[:10] if pilot else result


def job_config(args, entries):
    identity = hashlib.sha256(json.dumps({"tasks": entries, "judge": args.judge, "image": args.image,
                                         "group": args.matches_per_task, "judge_models": args.judge_models,
                                         "judge_mode": args.judge_mode, "judge_hedge": args.judge_hedge,
                                         "dream_hedge": args.dream_hedge, "machine": args.machine_type},
                                        sort_keys=True).encode()).hexdigest()[:32]
    count = (len(entries) + args.matches_per_task - 1) // args.matches_per_task
    secret = {"DREAM_API_KEY": f"projects/{args.project}/secrets/dream-api-key/versions/latest"}
    if args.judge == "api":
        secret["ANTHROPIC_API_KEY"] = f"projects/{args.project}/secrets/anthropic-api-key/versions/latest"
    if args.judge == "openrouter":
        secret["OPENROUTER_API_KEY"] = f"projects/{args.project}/secrets/openrouter-api-key/versions/latest"
    network = ({"networkInterfaces": [{"network": "global/networks/default",
                                       "subnetwork": f"regions/{args.region}/subnetworks/default",
                                       "noExternalIpAddress": True}]} if args.private else None)
    policy = {"instances": [{"policy": {"machineType": args.machine_type,
              "provisioningModel": "SPOT", "bootDisk": {"sizeGb": 30, "type": "pd-standard"}}}],
              "location": {"allowedLocations": ["regions/" + args.region]},
              "serviceAccount": {"email": f"hill-climb-worker@{args.project}.iam.gserviceaccount.com"}}
    if network:
        policy["network"] = network
    return {"taskGroups": [{"taskCount": count, "parallelism": min(args.parallelism, count),
                            "taskCountPerNode": 1,
                            "taskSpec": {"runnables": [{"container": {"imageUri": args.image,
                                "commands": ["--matrix", "/app/cloud/matrix.yaml", "--judge", args.judge,
                                             "--judge-models", args.judge_models, "--judge-mode", args.judge_mode,
                                             "--judge-hedge", str(args.judge_hedge),
                                             "--dream-hedge", str(args.dream_hedge),
                                             "--matches-per-task", str(args.matches_per_task),
                                             "--limit", str(len(entries))]}}],
                                "environment": {"variables": {"OUTPUT_PREFIX": f"gs://{args.bucket}/runs/{args.job}",
                                                                "RUN_ID": args.job,
                                                                "MATRIX_SHA256": hashlib.sha256((ROOT / "cloud/matrix.yaml").read_bytes()).hexdigest(),
                                                                "LP_NUM_THREADS": "2", "OMP_NUM_THREADS": "2"},
                                                "secretVariables": secret},
                                "computeResource": {"cpuMilli": 2000, "memoryMib": 1280},
                                "maxRetryCount": 2,
                                "maxRunDuration": str(900 * args.matches_per_task) + "s"}}],
            "allocationPolicy": policy,
            "logsPolicy": {"destination": "CLOUD_LOGGING"}, "labels": {"manifest": identity}}


def build(args, gc):
    print(f"Will submit Cloud Build from the allowlisted checkout and push {args.image}. No local Docker.")
    if args.execute:
        if gc.call("artifacts", "docker", "images", "describe", args.image, optional=True):
            print("Image tag already exists; use a new --tag after changing code.")
            return
    digest = hashlib.sha256()
    source = [ROOT / "sim_station.py", ROOT / "experiments/sim_match.py"]
    for directory in ("cloud", "harness", "sim_scenes", "plans"):
        source.extend(path for path in (ROOT / directory).rglob("*") if path.is_file()
                      and not {"evidence", "generated", "__pycache__"}.intersection(path.relative_to(ROOT).parts)
                      and path.name not in ("credentials", ".env"))
    for path in sorted(source):
        digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes())
    config = {"steps": [{"name": "gcr.io/cloud-builders/docker", "args": ["build", "-f", "cloud/Dockerfile",
                       "--build-arg", "SOURCE_REVISION=" + digest.hexdigest(), "-t", args.image, "."]}],
              "images": [args.image], "serviceAccount": f"projects/{args.project}/serviceAccounts/"
                         f"hill-climb-builder@{args.project}.iam.gserviceaccount.com",
              "options": {"logging": "CLOUD_LOGGING_ONLY"}, "timeout": "1800s"}
    with tempfile.TemporaryDirectory(prefix="hill-climb-build-") as folder:
        path = Path(folder) / "build.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        gc.call("builds", "submit", str(ROOT), "--config", str(path),
                "--ignore-file", str(ROOT / "cloud/gcloudignore"),
                "--gcs-source-staging-dir", f"gs://{args.bucket}/build-source")


def submit(args, gc):
    entries = tasks(ROOT / "cloud/matrix.yaml", args.pilot)
    config = job_config(args, entries)
    print(f"Will submit Spot job {args.job}: {len(entries)} matches, "
          f"{config['taskGroups'][0]['taskCount']} tasks, parallelism {args.parallelism}, judge {args.judge}.")
    print(f"Each task runs {args.matches_per_task} sequential matches on {args.machine_type}; "
          "Spot retries may repeat paid Dream inference.")
    if args.execute:
        old = gc.call("batch", "jobs", "describe", args.job, "--location", args.region,
                      "--format=json", optional=True)
        if old:
            if json.loads(old).get("labels", {}).get("manifest") != config["labels"]["manifest"]:
                action("Job name exists with a different matrix/image/judge. Choose another --job name.")
            print("Job already exists with this manifest; nothing submitted.")
            return
        for key in ["dream-api-key"] + (["anthropic-api-key"] if args.judge == "api" else []) + (
                ["openrouter-api-key"] if args.judge == "openrouter" else []):
            if not gc.call("secrets", "versions", "list", key, "--filter=state:ENABLED", "--format=value(name)", "--limit=1"):
                action(f"Upload {key} through Secret Manager in your own console before submitting.")
        gc.call("artifacts", "docker", "images", "describe", args.image)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print("Job config: " + str(target))
    gc.call("batch", "jobs", "submit", args.job, "--location", args.region, "--config", str(target))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation", choices=["setup", "build", "submit"])
    p.add_argument("--execute", action="store_true", help="perform cloud changes; default is offline dry-run")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--project", default="hill-climb-sims")
    p.add_argument("--region", default="us-central1")
    p.add_argument("--bucket", default="hill-climb-sims-results")
    p.add_argument("--budget", type=int, default=50)
    p.add_argument("--tag", default="cloud-v1")
    p.add_argument("--job", default="hill-climb-pilot-v1")
    p.add_argument("--pilot", action="store_true")
    p.add_argument("--judge", choices=["oracle", "api", "openrouter"], default="oracle")
    p.add_argument("--judge-models", default="google/gemini-3.1-flash-lite")
    p.add_argument("--judge-mode", choices=["race", "alternate"], default="race")
    p.add_argument("--judge-hedge", type=int, default=1)
    p.add_argument("--dream-hedge", type=int, default=1)
    p.add_argument("--private", action="store_true", help="no external IPs (needs Cloud NAT; lifts the IP quota)")
    p.add_argument("--parallelism", type=int, default=8)
    p.add_argument("--matches-per-task", type=int, default=1)
    p.add_argument("--machine-type", default="e2-highcpu-2")
    p.add_argument("--output", default="cloud/generated/job.json")
    args = p.parse_args()
    if args.execute and args.dry_run:
        p.error("choose --execute or --dry-run")
    if min(args.parallelism, args.matches_per_task, args.budget) < 1:
        p.error("parallelism, matches-per-task and budget must be positive")
    args.image = f"{args.region}-docker.pkg.dev/{args.project}/hill-climb/sim:{args.tag}"
    try:
        globals()[args.operation](args, Gcloud(args))
    except (OSError, ValueError) as exc:
        action(str(exc))


if __name__ == "__main__":
    main()
