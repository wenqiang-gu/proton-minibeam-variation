#!/usr/bin/env bash
#SBATCH --job-name=topas_general
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=20
#SBATCH --time=48:00:00
#SBATCH --output=Slurm/logs/topas_general_%A_%a.out
#SBATCH --error=Slurm/logs/topas_general_%A_%a.err

set -euo pipefail

manifest_task() {
    local manifest=$1
    local task_id=$2

    if [[ ! $task_id =~ ^[1-9][0-9]*$ ]]; then
        echo "Array task ID must be a positive integer: $task_id" >&2
        return 2
    fi
    if [[ ! -s $manifest ]]; then
        echo "Manifest is missing or empty: $manifest" >&2
        return 2
    fi

    local input
    input=$(sed -n "${task_id}p" "$manifest")
    if [[ -z $input ]]; then
        echo "Manifest has no non-empty entry for task $task_id: $manifest" >&2
        return 2
    fi
    printf '%s\n' "$input"
}

extract_thread_count() {
    local input_file=$1
    local definitions
    definitions=$(
        awk '
            /^[[:space:]]*i:Ts\/NumberOfThreads[[:space:]]*=/ {
                line = $0
                sub(/#.*/, "", line)
                sub(/^[^=]*=[[:space:]]*/, "", line)
                gsub(/[[:space:]]/, "", line)
                print line
            }
        ' "$input_file"
    )

    local definition_count
    definition_count=$(printf '%s\n' "$definitions" | awk 'NF {count++} END {print count+0}')
    if [[ $definition_count != 1 ]]; then
        echo "TOPAS input must define NumberOfThreads exactly once: $input_file" >&2
        return 2
    fi
    if [[ ! $definitions =~ ^[1-9][0-9]*$ ]]; then
        echo "TOPAS input has an invalid NumberOfThreads value: $input_file" >&2
        return 2
    fi
    printf '%s\n' "$definitions"
}

if [[ ${1:-} == "--map" ]]; then
    [[ $# -eq 3 ]] || {
        echo "Usage: $0 --map MANIFEST ARRAY_TASK_ID" >&2
        exit 2
    }
    manifest_task "$2" "$3"
    exit
fi

task_start_epoch=$(date +%s)

report_runtime() {
    local task_exit_status=$?
    local task_end_epoch elapsed hours minutes seconds

    task_end_epoch=$(date +%s)
    elapsed=$((task_end_epoch - task_start_epoch))
    hours=$((elapsed / 3600))
    minutes=$(((elapsed % 3600) / 60))
    seconds=$((elapsed % 60))

    echo "Finish: $(date --iso-8601=seconds)"
    printf 'Total wall time: %02d:%02d:%02d\n' "$hours" "$minutes" "$seconds"
    echo "Exit status: $task_exit_status"

    trap - EXIT
    exit "$task_exit_status"
}

trap report_runtime EXIT

: "${SLURM_ARRAY_TASK_ID:?This script must run as a Slurm array task}"
: "${SLURM_CPUS_PER_TASK:?SLURM_CPUS_PER_TASK is not set}"
: "${SLURM_SUBMIT_DIR:?SLURM_SUBMIT_DIR is not set}"
: "${TOPAS_MANIFEST:?TOPAS_MANIFEST is not set}"
: "${TOPAS_MANIFEST_SHA256:?TOPAS_MANIFEST_SHA256 is not set}"
: "${TOPAS_TASK_COUNT:?TOPAS_TASK_COUNT is not set}"

if [[ ! $SLURM_CPUS_PER_TASK =~ ^[1-9][0-9]*$ ]]; then
    echo "SLURM_CPUS_PER_TASK must be a positive integer: $SLURM_CPUS_PER_TASK" >&2
    exit 2
fi
if [[ ! $TOPAS_TASK_COUNT =~ ^[1-9][0-9]*$ ]] || \
   (( SLURM_ARRAY_TASK_ID < 1 || SLURM_ARRAY_TASK_ID > TOPAS_TASK_COUNT )); then
    echo "Array task $SLURM_ARRAY_TASK_ID is outside manifest range 1-$TOPAS_TASK_COUNT." >&2
    exit 2
fi

project_dir=$SLURM_SUBMIT_DIR
if [[ ! -f $project_dir/study.toml ]] || \
   [[ ! -f $project_dir/Slurm/topas_general_array.sbatch ]]; then
    echo "Submit this workflow from the proton-minibeam-variation repository root." >&2
    echo "SLURM_SUBMIT_DIR was: $project_dir" >&2
    exit 2
fi
if [[ ! -s $TOPAS_MANIFEST ]]; then
    echo "Manifest is missing or empty: $TOPAS_MANIFEST" >&2
    exit 2
fi

manifest_lines=$(wc -l < "$TOPAS_MANIFEST")
manifest_lines=${manifest_lines//[[:space:]]/}
if [[ $manifest_lines != "$TOPAS_TASK_COUNT" ]]; then
    echo "Manifest changed: expected $TOPAS_TASK_COUNT lines, found $manifest_lines." >&2
    exit 2
fi
actual_hash=$(sha256sum "$TOPAS_MANIFEST" | awk '{print $1}')
if [[ $actual_hash != "$TOPAS_MANIFEST_SHA256" ]]; then
    echo "Manifest checksum changed after submission: $TOPAS_MANIFEST" >&2
    exit 2
fi

topas_input=$(manifest_task "$TOPAS_MANIFEST" "$SLURM_ARRAY_TASK_ID")
case "$topas_input" in
    /*|../*|*/../*)
        echo "Manifest input must be a repository-relative path: $topas_input" >&2
        exit 2
        ;;
esac
if [[ $topas_input != *.txt ]] || [[ ! -s $project_dir/$topas_input ]]; then
    echo "TOPAS input is missing, empty, or not a .txt file: $topas_input" >&2
    exit 2
fi

topas_threads=$(extract_thread_count "$project_dir/$topas_input") || exit $?
if [[ $topas_threads != "$SLURM_CPUS_PER_TASK" ]]; then
    echo "CPU/thread mismatch for $topas_input" >&2
    echo "TOPAS NumberOfThreads: $topas_threads" >&2
    echo "Slurm CPUs per task: $SLURM_CPUS_PER_TASK" >&2
    exit 2
fi

topas_env=${TOPAS_ENV:-"/data/maia/$USER/Applications/TOPAS/opentopas-env.sh"}
if [[ ! -f $topas_env ]]; then
    echo "TOPAS environment script not found: $topas_env" >&2
    exit 2
fi

cd "$project_dir"
mkdir -p output

output_file=$(
    awk '
        /^[[:space:]]*s:Sc\/PatientDose\/OutputFile[[:space:]]*=/ {
            line = $0
            sub(/#.*/, "", line)
            sub(/^[^=]*=[[:space:]]*/, "", line)
            gsub(/^"|"[[:space:]]*$/, "", line)
            print line
        }
    ' "$topas_input"
)
case "$output_file" in
    output/*) mkdir -p "$(dirname -- "$output_file")" ;;
    *)
        echo "Task output must be a repository-relative path below output/: $output_file" >&2
        exit 2
        ;;
esac

# shellcheck disable=SC1090
source "$topas_env"

echo "Array job: ${SLURM_ARRAY_JOB_ID:-$SLURM_JOB_ID}"
echo "Array task: $SLURM_ARRAY_TASK_ID of $TOPAS_TASK_COUNT"
echo "Manifest: $TOPAS_MANIFEST"
echo "Manifest SHA-256: $TOPAS_MANIFEST_SHA256"
echo "Node: $(hostname)"
echo "Allocated CPUs: $SLURM_CPUS_PER_TASK"
echo "TOPAS input: $topas_input"
echo "Start: $(date --iso-8601=seconds)"

srun --ntasks=1 \
     --cpus-per-task="$SLURM_CPUS_PER_TASK" \
     topas "$topas_input"
