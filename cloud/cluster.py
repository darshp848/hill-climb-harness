"""Run the cloud matches on the home cluster too (the Inspiron head node), with the same image and entrypoint.

    python -m cloud.cluster push                    # ship HEAD's sources, build hill-climb-sim:<rev> on the node
    python -m cloud.cluster keys                    # Secret Manager -> ~/.hill-climb/keys.env (mode 600), never printed
    python -m cloud.cluster run --job J --config hybrid_v4 --seed-start 0 --matches 8 --containers 4 -- <entrypoint args>
    python -m cloud.cluster status --job J
    python -m cloud.cluster pull --job J            # results -> cloud/generated/cluster/<J>/ for cloud.collect
    python -m cloud.cluster stop --job J

Each container runs `cloud.entrypoint --config C --seed-start S --matches M`, sequential matches on its own seed
range, 2 CPUs and 2 GB like a Batch task, writing to the cluster's shared folder (OUTPUT_PREFIX is a local path, so
no cloud credentials are needed on the node). Live snapshots land there too, under each match's live/.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = "inspiron"
BASE = "/cluster/shared/dev/darsh/projects/hill_climb_sims"
SOURCES = ["cloud", "harness", "sim_scenes", "plans", "lessons", "experiments/sim_match.py", "sim_station.py"]


def ssh(command: str, *, stdin: bytes | None = None, check: bool = True) -> str:
    result = subprocess.run(["ssh", HOST, command], input=stdin, capture_output=True)
    if check and result.returncode:
        raise SystemExit(f"ssh {HOST} failed ({result.returncode}): {result.stderr.decode(errors='replace')[-600:]}")
    return result.stdout.decode(errors="replace")


def revision() -> str:
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                          text=True, check=True).stdout.strip()


def push(_args) -> None:
    rev = revision()
    src = f"{BASE}/src-{rev}"
    archive = subprocess.run(["git", "archive", "--format=tar", "HEAD", *SOURCES], cwd=ROOT, capture_output=True,
                             check=True).stdout
    # Readable by everyone: the containers run as the node's user, and COPY keeps the files' modes.
    ssh(f"mkdir -p {src} && tar -x -C {src} && chmod -R a+rX {src}", stdin=archive)
    print(f"sources at {HOST}:{src}; building hill-climb-sim:{rev} (first build pulls the base images)", flush=True)
    out = ssh(f"cd {src} && docker build -q -f cloud/Dockerfile --build-arg SOURCE_REVISION={rev} "
              f"-t hill-climb-sim:{rev} -t hill-climb-sim:latest .")
    print(out.strip()[-200:])


def keys(args) -> None:
    import shutil

    gcloud = shutil.which(args.gcloud) or args.gcloud      # gcloud.cmd on Windows: run directly, no shell quoting
    lines = []
    for env, secret in (("DREAM_API_KEY", "dream-api-key"), ("OPENROUTER_API_KEY", "openrouter-api-key")):
        value = subprocess.run([gcloud, "secrets", "versions", "access", "latest", f"--secret={secret}",
                                f"--project={args.project}"], capture_output=True, text=True)
        if value.returncode or not value.stdout.strip():
            raise SystemExit(f"could not read secret {secret} (is gcloud logged in?)")
        lines.append(f"{env}={value.stdout.strip()}")
    ssh("umask 077 && mkdir -p ~/.hill-climb && cat > ~/.hill-climb/keys.env", stdin=("\n".join(lines) + "\n").encode())
    print(f"keys written to {HOST}:~/.hill-climb/keys.env (mode 600); not printed")


def run(args) -> None:
    out = f"{BASE}/results/{args.job}"
    per = -(-args.matches // args.containers)
    starts = [(args.seed_start + i * per, min(per, args.matches - i * per)) for i in range(args.containers)
              if args.matches - i * per > 0]
    extra = " ".join(args.entrypoint_args)
    lines = [f"mkdir -p {out}"]
    for i, (start, n) in enumerate(starts):
        name = f"hc-{args.job}-{i}"
        lines.append(f"docker run -d --name {name} --cpus 2 --memory 2g --env-file ~/.hill-climb/keys.env "
                     f"--user $(id -u):$(id -g) -e HOME=/tmp "
                     # The node's Intel GPU through EGL: ~20 ms per capture instead of ~320 ms in software.
                     f"--device /dev/dri --group-add $(getent group render | cut -d: -f3) "
                     f"--group-add $(getent group video | cut -d: -f3) -e MUJOCO_GL=egl -e PYOPENGL_PLATFORM=egl "
                     f"-e OUTPUT_PREFIX=/results/{args.job} -e RUN_ID={args.job} -v {BASE}/results:/results "
                     f"--label hill-climb-job={args.job} hill-climb-sim:{args.tag} "
                     f"--config {args.config} --seed-start {start} --matches {n} --out /tmp/out {extra}")
    print(ssh(" && ".join(lines)).strip())
    print(f"{len(starts)} containers on {HOST}; results in {out}")


def status(args) -> None:
    print(ssh(f"docker ps -a --filter label=hill-climb-job={args.job} --format '{{{{.Names}}}} {{{{.Status}}}}'; "
              f"echo matches: $(find {BASE}/results/{args.job} -name match.json 2>/dev/null | wc -l) "
              f"complete: $(find {BASE}/results/{args.job} -name complete.json 2>/dev/null | wc -l)", check=False))


def pull(args) -> None:
    target = ROOT / "cloud/generated/cluster" / args.job
    target.mkdir(parents=True, exist_ok=True)
    data = subprocess.run(["ssh", HOST, f"tar -c -C {BASE}/results {args.job}"], capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(target.parent)], input=data, check=True)
    print(f"results in {target}")


def stop(args) -> None:
    print(ssh(f"docker rm -f $(docker ps -aq --filter label=hill-climb-job={args.job}) 2>/dev/null; true",
              check=False).strip() or "nothing to stop")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("op", choices=["push", "keys", "run", "status", "pull", "stop"])
    p.add_argument("--job")
    p.add_argument("--config", default="hybrid_v4")
    p.add_argument("--seed-start", type=int, default=0)
    p.add_argument("--matches", type=int, default=4)
    p.add_argument("--containers", type=int, default=4)
    p.add_argument("--tag", default="latest")
    p.add_argument("--project", default="hill-climb-sims")
    p.add_argument("--gcloud", default="gcloud")
    argv = sys.argv[1:]
    passthrough = argv[argv.index("--") + 1:] if "--" in argv else []     # after --: for cloud.entrypoint
    args = p.parse_args(argv[:argv.index("--")] if "--" in argv else argv)
    args.entrypoint_args = passthrough
    if args.op in ("run", "status", "pull", "stop") and not args.job:
        p.error("--job is required")
    globals()[args.op](args)


if __name__ == "__main__":
    main()
