#!/usr/bin/env bash
# Read-only status. The only external executable invoked is gcloud.
set -uo pipefail

project=hill-climb-sims
account=${HILL_CLIMB_GCLOUD_ACCOUNT:-$(gcloud config get-value account 2>/dev/null)}
region=us-central1
bucket=gs://hill-climb-sims-results
gcloud_bin=${GCLOUD:-gcloud}
if ! command -v "$gcloud_bin" >/dev/null 2>&1; then
    gcloud_bin='/c/Users/darsh/AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin/gcloud'
fi
if ! command -v "$gcloud_bin" >/dev/null 2>&1; then
    printf 'gcloud is unavailable. Put it on PATH or set GCLOUD to its executable.\n' >&2
    exit 1
fi

gc() {
    "$gcloud_bin" "$@" --project="$project" --account="$account" --quiet
}

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    printf 'Usage: bash cloud/status.sh [JOB ...]\n'
    printf 'Defaults to the three protected pilot2 jobs. Reads us-central1 only.\n'
    exit 0
fi
jobs=("$@")
if ((${#jobs[@]} == 0)); then
    jobs=(pilot2-oracle pilot2-gemini-h2 pilot2-alt-gemini-haiku)
fi
for job in "${jobs[@]}"; do
    if [[ ! $job =~ ^[a-z][a-z0-9-]{0,62}$ ]]; then
        printf 'Invalid job ID: %s\n' "$job" >&2
        exit 2
    fi
done

failed=0
printf 'Project=%s account=%s region=%s\n' "$project" "$account" "$region"
printf 'Reads are sequential snapshots. Object counts include all retry attempts.\n'
printf 'QUEUED/SCHEDULED are job states; PENDING/ASSIGNED are task states.\n'
for job in "${jobs[@]}"; do
    printf '\n%s\n' "$job"
    if ! description=$(gc batch jobs describe "$job" --location="$region" \
            '--format=value(status.state,uid)' 2>/dev/null); then
        printf '  ERROR: job read failed; check identity, job ID, API and IAM.\n'
        failed=1
        continue
    fi
    description=${description//$'\r'/}
    IFS=$'\t' read -r job_state uid <<< "$description"
    if [[ -z $job_state || -z $uid ]]; then
        printf '  ERROR: incomplete job metadata.\n'
        failed=1
        continue
    fi
    printf '  state=%s\n' "$job_state"
    if states=$(gc batch tasks list --job="$job" --location="$region" \
            '--format=value(status.state)' 2>/dev/null); then
        declare -A counts=([QUEUED]=0 [SCHEDULED]=0 [RUNNING]=0 [SUCCEEDED]=0 \
            [FAILED]=0 [PENDING]=0 [ASSIGNED]=0 [UNEXECUTED]=0 [OTHER]=0)
        total=0
        while IFS= read -r state; do
            state=${state//$'\r'/}
            [[ -z $state ]] && continue
            case "$state" in
                QUEUED|SCHEDULED|RUNNING|SUCCEEDED|FAILED|PENDING|ASSIGNED|UNEXECUTED)
                    counts[$state]=$((counts[$state] + 1)) ;;
                *) counts[OTHER]=$((counts[OTHER] + 1)) ;;
            esac
            total=$((total + 1))
        done <<< "$states"
        printf '  tasks=%s' "$total"
        for state in QUEUED SCHEDULED RUNNING SUCCEEDED FAILED PENDING ASSIGNED UNEXECUTED OTHER; do
            printf ' %s=%s' "$state" "${counts[$state]}"
        done
        printf '\n'
        unset counts
    else
        printf '  tasks=UNKNOWN (read failed)\n'
        failed=1
    fi

    if vms=$(gc compute instances list --filter="labels.batch-job-uid=$uid" \
            '--format=value(status)' 2>/dev/null); then
        vm_count=0
        vm_running=0
        while IFS= read -r state; do
            state=${state//$'\r'/}
            [[ -z $state ]] && continue
            vm_count=$((vm_count + 1))
            [[ $state == RUNNING ]] && vm_running=$((vm_running + 1))
        done <<< "$vms"
        printf '  VMs=%s RUNNING=%s\n' "$vm_count" "$vm_running"
    else
        printf '  VMs=UNKNOWN (read failed)\n'
        failed=1
    fi

    objects_ok=1
    prefix="$bucket/runs/$job/"
    if ! objects=$(gc storage ls --recursive "$prefix" 2>&1); then
        case "$objects" in
            *'matched no objects'*|*'No URLs matched'*) objects='' ;;
            *) objects_ok=0 ;;
        esac
    fi
    if ((objects_ok)); then
        matches=0
        completes=0
        while IFS= read -r object; do
            object=${object//$'\r'/}
            case "$object" in
                "$prefix"*/match.json) matches=$((matches + 1)) ;;
                "$prefix"*/complete.json) completes=$((completes + 1)) ;;
            esac
        done <<< "$objects"
        printf '  uploaded match.json=%s complete.json=%s\n' "$matches" "$completes"
    else
        printf '  uploaded match.json=UNKNOWN complete.json=UNKNOWN (list failed)\n'
        failed=1
    fi

    filter="logName=\"projects/$project/logs/batch_task_logs\" AND labels.job_uid=\"$uid\" AND (severity>=ERROR OR textPayload=~\"(?i)(error|exception|traceback|failed)\" OR jsonPayload.message=~\"(?i)(error|exception|traceback|failed)\")"
    printf '  last 5 task error entries (newest first, last 7 days):\n'
    if errors=$(gc logging read "$filter" --freshness=7d --limit=5 --order=desc \
            '--format=value(timestamp,severity,textPayload,jsonPayload.message)' 2>/dev/null); then
        if [[ -z $errors ]]; then
            printf '    none\n'
        else
            while IFS= read -r line; do
                # Never print credential-like lines, even if an upstream logger regresses.
                case "${line,,}" in
                    *api_key*|*api-key*|*authorization*|*bearer*|*password*|*access_token*|*sk-*|*aiza*)
                        printf '    [REDACTED credential-like log line]\n' ;;
                    *) printf '    %s\n' "$line" ;;
                esac
            done <<< "$errors"
        fi
    else
        printf '    UNKNOWN (log read failed)\n'
        failed=1
    fi
done
exit "$failed"
