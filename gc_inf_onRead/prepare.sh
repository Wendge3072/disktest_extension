#!/usr/bin/env bash
# Guest: one full sequential cover, then a prefix cover, after random conditioning.
# Completion proves writes; verify_qlc_range.py separately proves QLC residency.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
die() { echo "prepare.sh: $*" >&2; exit 1; }
integer() {
    [[ $1 =~ ^[0-9]{1,18}$ ]] || die "$2 must be an integer byte/second count"
    printf '%s' "$((10#$1))"
}
DRY_RUN=${DRY_RUN:-0}
if [[ ${1:-} == --dry-run ]]; then DRY_RUN=1; shift; fi
[[ $# == 0 ]] || die 'Usage: prepare.sh [--dry-run]'
[[ $DRY_RUN == 0 || $DRY_RUN == 1 ]] || die 'DRY_RUN must be 0 or 1'
SKIP_RANDOM=${SKIP_RANDOM:-0}
[[ $SKIP_RANDOM == 0 || $SKIP_RANDOM == 1 ]] || die 'SKIP_RANDOM must be 0 or 1'
DEVICE=${DEVICE:-/dev/vda}
[[ $DEVICE == /* && $DEVICE != *$'\n'* && $DEVICE != *$'\r'* && $DEVICE != *$'\t'* ]] || die 'Invalid DEVICE path'
if [[ -b $DEVICE ]]; then
    actual=$(blockdev --getsize64 "$DEVICE")
    [[ -z ${DEVICE_BYTES:-} || $DEVICE_BYTES == "$actual" ]] || die 'DEVICE_BYTES differs from actual capacity'
    DEVICE_BYTES=$actual
elif [[ $DRY_RUN == 1 && -n ${DEVICE_BYTES:-} ]]; then
    :
elif [[ ${ALLOW_REGULAR_FILE:-0} == 1 && -f $DEVICE ]]; then
    actual=$(stat -c %s "$DEVICE" 2>/dev/null || stat -f %z "$DEVICE")
    [[ -z ${DEVICE_BYTES:-} || $DEVICE_BYTES == "$actual" ]] || die 'DEVICE_BYTES differs from test file size'
    DEVICE_BYTES=$actual
else
    die 'DEVICE must be a raw block device; configuration-only needs DRY_RUN and DEVICE_BYTES'
fi
DEVICE_BYTES=$(integer "$DEVICE_BYTES" DEVICE_BYTES)
# Exact integer GiB defaults; avoid ambiguous decimal TB and binary TiB.
PREFIX_SIZE_BYTES=$(integer "${PREFIX_SIZE_BYTES:-1825361100800}" PREFIX_SIZE_BYTES)
READ_FLOOR_BYTES=$(integer "${READ_FLOOR_BYTES:-2199023255552}" READ_FLOOR_BYTES)
PREPARE_SETTLE_SECONDS=$(integer "${PREPARE_SETTLE_SECONDS:-600}" PREPARE_SETTLE_SECONDS)
(( DEVICE_BYTES > 0 && DEVICE_BYTES % 4096 == 0 )) || die 'Capacity must be positive and 4KiB aligned'
(( PREFIX_SIZE_BYTES > 0 && PREFIX_SIZE_BYTES % 4096 == 0 )) || die 'Prefix must be positive and 4KiB aligned'
(( READ_FLOOR_BYTES % 4096 == 0 && PREFIX_SIZE_BYTES < READ_FLOOR_BYTES && READ_FLOOR_BYTES < DEVICE_BYTES )) || die 'Require prefix < read floor < capacity, all 4KiB aligned'
UPSTREAM_DIR=${UPSTREAM_DIR:-$HERE/../gc_inf_onBW}
UPSTREAM_DIR=$(cd -- "$UPSTREAM_DIR" && pwd)
if [[ $SKIP_RANDOM == 0 ]]; then
    for job in rnd.job rnd2.job rnd3.job rnd64.job; do
        [[ -f $UPSTREAM_DIR/$job ]] || die "Missing upstream job $job"
    done
fi
PREPARE_RUN_ID=${PREPARE_RUN_ID:-prepare-$(date -u +%Y%m%dT%H%M%SZ)}
[[ $PREPARE_RUN_ID =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die 'Invalid PREPARE_RUN_ID'
RESULTS_ROOT=${RESULTS_ROOT:-$HERE/results}
mkdir -p -- "$RESULTS_ROOT"
RESULTS_ROOT=$(cd -- "$RESULTS_ROOT" && pwd)
RESULT_DIR=$RESULTS_ROOT/$PREPARE_RUN_ID
mkdir -- "$RESULT_DIR" || die 'Preparation output already exists'
full_main=$((DEVICE_BYTES / 65536 * 65536))
full_tail=$((DEVICE_BYTES - full_main))
prefix_main=$((PREFIX_SIZE_BYTES / 65536 * 65536))
prefix_tail=$((PREFIX_SIZE_BYTES - prefix_main))
printf 'schema\t2\ndevice\t%s\ndevice_bytes\t%s\nprefix_size_bytes\t%s\nread_floor_bytes\t%s\nfull_main_bytes\t%s\nfull_tail_bytes\t%s\nprefix_main_bytes\t%s\nprefix_tail_bytes\t%s\nskip_random\t%s\nsettle_seconds\t%s\n' \
    "$DEVICE" "$DEVICE_BYTES" "$PREFIX_SIZE_BYTES" "$READ_FLOOR_BYTES" "$full_main" "$full_tail" "$prefix_main" "$prefix_tail" "$SKIP_RANDOM" "$PREPARE_SETTLE_SECONDS" > "$RESULT_DIR/plan.tsv"
printf 'epoch_ms\tevent\tdetail\n' > "$RESULT_DIR/events.tsv"
record() { printf '%s000\t%s\t%s\n' "$(date -u +%s)" "$1" "${2:-}" >> "$RESULT_DIR/events.tsv"; }
stages=()
budgets=()
job() {
    local name=$1 offset=$2 length=$3 block=$4
    (( length > 0 )) || return 0
    cat > "$RESULT_DIR/$name.fio" <<EOF
[global]
ioengine=libaio
direct=1
filename=$DEVICE
allow_file_create=0
numjobs=1
iodepth=128
group_reporting=1
time_based=0
loops=1
refill_buffers=1

[$name]
rw=write
bs=$block
offset=$offset
size=$length
io_size=$length
EOF
    stages+=("$name")
    budgets+=("$length")
}
job full_sequential_main 0 "$full_main" 64k
job full_sequential_tail "$full_main" "$full_tail" 4k
job prefix_sequential_main 0 "$prefix_main" 64k
job prefix_sequential_tail "$prefix_main" "$prefix_tail" 4k
FIO_BIN=${FIO_BIN:-fio}
printf '#!/usr/bin/env bash\nset -euo pipefail\n' > "$RESULT_DIR/commands.sh"
if [[ $SKIP_RANDOM == 0 ]]; then
    for upstream in rnd.job rnd2.job rnd3.job rnd64.job; do
        cp -- "$UPSTREAM_DIR/$upstream" "$RESULT_DIR/$upstream"
        printf '%q %q --filename=%q --output-format=json --output=%q\n' "$FIO_BIN" "$RESULT_DIR/$upstream" "$DEVICE" "$RESULT_DIR/$upstream.json" >> "$RESULT_DIR/commands.sh"
    done
fi
for stage in "${stages[@]}"; do
    printf '%q %q --filename=%q --output-format=json --output=%q\n' "$FIO_BIN" "$RESULT_DIR/$stage.fio" "$DEVICE" "$RESULT_DIR/$stage.fio.json" >> "$RESULT_DIR/commands.sh"
done
record generated 'full logical cover followed by low-address prefix cover'
if [[ $DRY_RUN == 1 ]]; then
    printf '1\n' > "$RESULT_DIR/DRY_RUN"
    record dry_run 'No fio or sleep; no QLC proof'
    printf '%s\n' "$RESULT_DIR"
    exit 0
fi
command -v "$FIO_BIN" >/dev/null || die 'fio not installed'
if [[ -n ${PYTHON_BIN:-} ]]; then command -v "$PYTHON_BIN" >/dev/null || die 'PYTHON_BIN not found'
elif command -v python3 >/dev/null; then PYTHON_BIN=python3
elif command -v python >/dev/null; then PYTHON_BIN=python
else die 'Python 2.7 or 3 is required to validate fio JSON'; fi
if command -v pgrep >/dev/null && pgrep -x fio >/dev/null; then die 'Another fio is running'; fi
if [[ -b $DEVICE ]]; then
    command -v lsblk >/dev/null || die 'lsblk is required to check mounts'
    [[ -z $(lsblk -nr -o MOUNTPOINT "$DEVICE" | tr -d '[:space:]') ]] || die 'Target or a partition is mounted'
fi
"$FIO_BIN" --version > "$RESULT_DIR/fio.version"
check_result() {
    "$PYTHON_BIN" - "$1" "$2" <<'PY'
from __future__ import print_function
import json, sys
report = json.load(open(sys.argv[1]))
jobs = report.get('jobs')
if not isinstance(jobs, list) or not jobs:
    sys.exit('Missing fio jobs')
total = 0
for job in jobs:
    if job.get('error') != 0 or job.get('read', {}).get('io_bytes') != 0:
        sys.exit('fio error or unexpected reads')
    count = job.get('write', {}).get('io_bytes')
    if isinstance(count, bool) or not isinstance(count, (int, type(2**64))) or count < 0:
        sys.exit('Missing/invalid completed write bytes')
    total += count
expected = int(sys.argv[2])
if (expected >= 0 and (len(jobs) != 1 or total != expected)) or (expected < 0 and total == 0):
    sys.exit('fio incomplete write: expected %s, completed %s' % (expected, total))
PY
}
run_job() {
    local path=$1 expected=$2 name
    name=$(basename -- "$path")
    record job_start "$name"
    "$FIO_BIN" "$path" --filename="$DEVICE" --output-format=json --output="$path.json" > "$path.log" 2>&1
    check_result "$path.json" "$expected"
    record job_end "$name; completed_bytes=$expected"
}
if [[ $SKIP_RANDOM == 0 ]]; then
    for upstream in rnd.job rnd2.job rnd3.job rnd64.job; do run_job "$RESULT_DIR/$upstream" -1; done
else record random_skipped 'Operator declares prior random conditioning already complete'; fi
for index in "${!stages[@]}"; do
    run_job "$RESULT_DIR/${stages[$index]}.fio" "${budgets[$index]}"
done
record settle_wait "$PREPARE_SETTLE_SECONDS seconds; time alone does not prove compaction completion"
sleep "$PREPARE_SETTLE_SECONDS"
"$PYTHON_BIN" - "$RESULT_DIR/completed.json" "$DEVICE" "$DEVICE_BYTES" "$PREFIX_SIZE_BYTES" "$READ_FLOOR_BYTES" <<'PY'
from __future__ import print_function
import json, sys
json.dump({'schema': 2, 'writes_completed': True, 'qlc_verified': False,
           'device': sys.argv[2], 'device_bytes': int(sys.argv[3]),
           'prefix_size_bytes': int(sys.argv[4]), 'read_floor_bytes': int(sys.argv[5]),
           'next_step': 'Host verify_qlc_range.py scans the complete selected read range'},
          open(sys.argv[1], 'w'), indent=2)
PY
record complete 'Writes completed; host full-read-range QLC verification still required'
printf '%s\n' "$RESULT_DIR"
