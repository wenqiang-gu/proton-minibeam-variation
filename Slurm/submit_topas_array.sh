#!/usr/bin/env bash
# Build an immutable input manifest and submit one generic TOPAS array task per file.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: Slurm/submit_topas_array.sh [options] TOPAS_INPUT.txt [...]
       Slurm/submit_topas_array.sh [options] --manifest INPUTS.txt

The shell expands globs before this helper runs. Inputs are validated, sorted,
and frozen in a checksummed manifest. One Slurm array task runs each input.

Options:
  --throttle N       Maximum simultaneous array tasks (default: 5).
  --time LIMIT       Override the worker's 48:00:00 time limit.
  --mem SIZE         Request memory, for example 12G or 32000M.
  --partition NAME   Select a Slurm partition.
  --account NAME     Select a Slurm account.
  --qos NAME         Select a Slurm quality of service.
  --exclude NODES    Exclude a Slurm node list, for example rohpc[9003-9005].
  --job-name NAME    Override the default job name topas_general.
  --topas-env FILE   OpenTOPAS environment script. If omitted, use TOPAS_ENV.
  --cpus-per-task N  Require N CPUs per task; N must match NumberOfThreads in
                     every selected TOPAS input (default: infer from inputs).
  --manifest FILE    Read repository-relative TOPAS inputs from FILE.
  --dry-run          Validate and show the planned submission without writing
                     a manifest or calling sbatch.
  -h, --help         Show this help.

Every input must directly define one positive i:Ts/NumberOfThreads value. All
selected inputs must use the same value. The TOPAS environment must be supplied
explicitly with --topas-env or TOPAS_ENV; no cluster-specific path is assumed.
EOF
}

throttle=5
time_limit=
memory=
partition=
account=
qos=
excluded_nodes=
job_name=
requested_cpus=
requested_topas_env=
dry_run=false
input_manifest=
inputs=()

while (( $# > 0 )); do
    case "$1" in
        --throttle)
            [[ $# -ge 2 ]] || { echo "--throttle requires a value" >&2; exit 2; }
            throttle=$2
            shift 2
            ;;
        --time)
            [[ $# -ge 2 ]] || { echo "--time requires a value" >&2; exit 2; }
            time_limit=$2
            shift 2
            ;;
        --mem)
            [[ $# -ge 2 ]] || { echo "--mem requires a value" >&2; exit 2; }
            memory=$2
            shift 2
            ;;
        --partition)
            [[ $# -ge 2 ]] || { echo "--partition requires a value" >&2; exit 2; }
            partition=$2
            shift 2
            ;;
        --account)
            [[ $# -ge 2 ]] || { echo "--account requires a value" >&2; exit 2; }
            account=$2
            shift 2
            ;;
        --qos)
            [[ $# -ge 2 ]] || { echo "--qos requires a value" >&2; exit 2; }
            qos=$2
            shift 2
            ;;
        --exclude)
            [[ $# -ge 2 ]] || { echo "--exclude requires a value" >&2; exit 2; }
            excluded_nodes=$2
            shift 2
            ;;
        --job-name)
            [[ $# -ge 2 ]] || { echo "--job-name requires a value" >&2; exit 2; }
            job_name=$2
            shift 2
            ;;
        --cpus-per-task)
            [[ $# -ge 2 ]] || { echo "--cpus-per-task requires a value" >&2; exit 2; }
            requested_cpus=$2
            shift 2
            ;;
        --topas-env)
            [[ $# -ge 2 ]] || { echo "--topas-env requires a file" >&2; exit 2; }
            requested_topas_env=$2
            shift 2
            ;;
        --manifest)
            [[ $# -ge 2 ]] || { echo "--manifest requires a value" >&2; exit 2; }
            input_manifest=$2
            shift 2
            ;;
        --dry-run)
            dry_run=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            inputs+=("$@")
            break
            ;;
        -*)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            inputs+=("$1")
            shift
            ;;
    esac
done

if [[ -n $input_manifest ]]; then
    (( ${#inputs[@]} == 0 )) || { echo "--manifest cannot be combined with positional inputs" >&2; exit 2; }
    [[ -s $input_manifest ]] || { echo "Input manifest is missing or empty: $input_manifest" >&2; exit 2; }
    while IFS= read -r input || [[ -n $input ]]; do
        [[ -z $input ]] || inputs+=("$input")
    done < "$input_manifest"
fi

if [[ ! $throttle =~ ^[1-9][0-9]*$ ]]; then
    echo "--throttle must be a positive integer" >&2
    exit 2
fi
if [[ -n $requested_cpus && ! $requested_cpus =~ ^[1-9][0-9]*$ ]]; then
    echo "--cpus-per-task must be a positive integer" >&2
    exit 2
fi
topas_env=${requested_topas_env:-${TOPAS_ENV:-}}
if [[ -z $topas_env ]]; then
    echo "OpenTOPAS environment is required; use --topas-env FILE or set TOPAS_ENV." >&2
    exit 2
fi
if [[ $topas_env != /* ]]; then
    echo "TOPAS environment path must be absolute: $topas_env" >&2
    exit 2
fi
if [[ $topas_env == *','* || $topas_env == *$'\n'* || $topas_env == *$'\r'* ]]; then
    echo "TOPAS environment path may not contain commas or newlines: $topas_env" >&2
    exit 2
fi
if [[ ! -f $topas_env || ! -r $topas_env || ! -s $topas_env ]]; then
    echo "TOPAS environment script is missing, unreadable, or empty: $topas_env" >&2
    exit 2
fi
if (( ${#inputs[@]} == 0 )); then
    echo "At least one TOPAS input file is required" >&2
    usage >&2
    exit 2
fi

for command_name in awk mktemp realpath sha256sum sort; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Required command is not available: $command_name" >&2
        exit 2
    fi
done
if [[ $dry_run != true ]] && ! command -v sbatch >/dev/null 2>&1; then
    echo "Required command is not available: sbatch" >&2
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
project_root=$(cd -- "$script_dir/.." && pwd -P)
worker=$project_root/Slurm/topas_general_array.sbatch
if [[ ! -f $worker ]] || [[ ! -f $project_root/study.toml ]]; then
    echo "Could not identify the proton-minibeam-variation project root" >&2
    exit 2
fi
topas_env=$(realpath "$topas_env")
if [[ $topas_env == *','* || $topas_env == *$'\n'* || $topas_env == *$'\r'* ]]; then
    echo "Resolved TOPAS environment path is unsafe for Slurm --export: $topas_env" >&2
    exit 2
fi

raw_list=$(mktemp "${TMPDIR:-/tmp}/topas_inputs_raw.XXXXXX")
sorted_list=$(mktemp "${TMPDIR:-/tmp}/topas_inputs_sorted.XXXXXX")
cleanup() {
    rm -f "$raw_list" "$sorted_list"
}
trap cleanup EXIT

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

inferred_threads=

for input in "${inputs[@]}"; do
    if [[ ! -f $input ]]; then
        echo "TOPAS input does not exist: $input" >&2
        exit 2
    fi
    if [[ ! -s $input ]]; then
        echo "TOPAS input is empty: $input" >&2
        exit 2
    fi
    if [[ $input != *.txt ]]; then
        echo "TOPAS input must have a .txt extension: $input" >&2
        exit 2
    fi

    absolute_input=$(realpath "$input")
    case "$absolute_input" in
        "$project_root"/*) ;;
        *)
            echo "TOPAS input must be inside the project root: $absolute_input" >&2
            exit 2
            ;;
    esac

    input_threads=$(extract_thread_count "$absolute_input") || exit $?
    if [[ -z $inferred_threads ]]; then
        inferred_threads=$input_threads
    elif [[ $input_threads != "$inferred_threads" ]]; then
        echo "Selected TOPAS inputs use mixed NumberOfThreads values: " \
             "$inferred_threads and $input_threads" >&2
        echo "Conflicting input: $absolute_input" >&2
        exit 2
    fi

    relative_input=${absolute_input#"$project_root"/}
    if [[ $relative_input == *$'\n'* ]]; then
        echo "TOPAS input paths may not contain newlines: $relative_input" >&2
        exit 2
    fi
    printf '%s\n' "$relative_input" >> "$raw_list"
done

LC_ALL=C sort "$raw_list" > "$sorted_list"
duplicate=$(uniq -d "$sorted_list" | head -n 1 || true)
if [[ -n $duplicate ]]; then
    echo "Duplicate TOPAS input supplied: $duplicate" >&2
    exit 2
fi

task_count=$(wc -l < "$sorted_list")
task_count=${task_count//[[:space:]]/}
array_spec="1-${task_count}%${throttle}"

if [[ -n $requested_cpus && $requested_cpus != "$inferred_threads" ]]; then
    echo "--cpus-per-task $requested_cpus does not match TOPAS NumberOfThreads " \
         "$inferred_threads" >&2
    exit 2
fi

echo "TOPAS inputs: $task_count"
echo "Array specification: $array_spec"
echo "CPUs per task (from TOPAS inputs): $inferred_threads"
echo "TOPAS environment: $topas_env"
echo "First input: $(head -n 1 "$sorted_list")"
echo "Last input: $(tail -n 1 "$sorted_list")"

sbatch_args=(--array="$array_spec" --cpus-per-task="$inferred_threads")
[[ -z $time_limit ]] || sbatch_args+=(--time="$time_limit")
[[ -z $memory ]] || sbatch_args+=(--mem="$memory")
[[ -z $partition ]] || sbatch_args+=(--partition="$partition")
[[ -z $account ]] || sbatch_args+=(--account="$account")
[[ -z $qos ]] || sbatch_args+=(--qos="$qos")
[[ -z $excluded_nodes ]] || sbatch_args+=(--exclude="$excluded_nodes")
[[ -z $job_name ]] || sbatch_args+=(--job-name="$job_name")

if [[ $dry_run == true ]]; then
    printf 'Dry run command: sbatch'
    printf ' %q' "${sbatch_args[@]}"
    printf ' --export=%q %q\n' \
        "ALL,TOPAS_ENV=$topas_env,TOPAS_MANIFEST=<manifest>,TOPAS_MANIFEST_SHA256=<sha256>,TOPAS_TASK_COUNT=$task_count" \
        "$worker"
    echo "Dry run only; no manifest was written and no job was submitted."
    exit 0
fi

mkdir -p "$project_root/Slurm/logs"
manifest=$project_root/Slurm/logs/topas_manifest_$(date +%Y%m%dT%H%M%S)_$$.txt
cp "$sorted_list" "$manifest"
chmod 0444 "$manifest"
manifest_hash=$(sha256sum "$manifest" | awk '{print $1}')
export_spec="ALL,TOPAS_ENV=$topas_env,TOPAS_MANIFEST=$manifest,TOPAS_MANIFEST_SHA256=$manifest_hash,TOPAS_TASK_COUNT=$task_count"

echo "Manifest: $manifest"
echo "Manifest SHA-256: $manifest_hash"
printf 'Command: sbatch'
printf ' %q' "${sbatch_args[@]}"
printf ' --export=%q %q\n' "$export_spec" "$worker"

cd "$project_root"
if ! submission=$(sbatch --parsable "${sbatch_args[@]}" --export="$export_spec" "$worker"); then
    chmod u+w "$manifest"
    rm -f "$manifest"
    echo "Submission failed; the unused manifest was removed" >&2
    exit 1
fi

job_id=${submission%%;*}
echo "Submitted array job: $job_id"
echo "Keep the manifest for retries; task N always maps to manifest line N."
