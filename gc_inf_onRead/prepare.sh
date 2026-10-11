#!/usr/bin/env bash
# Guest: original random conditioning, then ONE partial sequential pass.
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
source "$HERE/layout.sh"
# The suffix is allowed to remain in TLC. No full-device or second-prefix pass.
PREPARE_END_BYTES=$(integer "$PREPARE_END_BYTES" PREPARE_END_BYTES)
CACHE_GUARD_BYTES=$(integer "$CACHE_GUARD_BYTES" CACHE_GUARD_BYTES)
PREPARE_SETTLE_SECONDS=$(integer "${PREPARE_SETTLE_SECONDS:-600}" PREPARE_SETTLE_SECONDS)
(( DEVICE_BYTES > 0 && DEVICE_BYTES % 4096 == 0 )) || die 'Capacity must be positive and 4KiB aligned'
(( PREPARE_END_BYTES > CACHE_GUARD_BYTES && PREPARE_END_BYTES < DEVICE_BYTES && PREPARE_END_BYTES % 4096 == 0 && CACHE_GUARD_BYTES > 0 && CACHE_GUARD_BYTES % 4096 == 0 )) || die 'Require 0 < cache guard < partial write end < capacity, all 4KiB aligned'
read_start=$(layout_bytes "$READ_OFFSET")
read_length=$(layout_bytes "$READ_SIZE")
(( read_start%4096==0 && read_length>0 && read_length%4096==0 && read_start+read_length<=PREPARE_END_BYTES-CACHE_GUARD_BYTES )) || die 'Read range does not fit before partial write end minus TLC guard'
UPSTREAM_DIR=${UPSTREAM_DIR:-$HERE/../gc_inf_onBW}
UPSTREAM_DIR=$(cd -- "$UPSTREAM_DIR" && pwd)
if [[ $SKIP_RANDOM == 0 ]]; then
    for job in rnd.job rnd2.job rnd3.job; do
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
seq_main=$((PREPARE_END_BYTES / 65536 * 65536))
seq_tail=$((PREPARE_END_BYTES - seq_main))
printf 'schema\t3\ndevice\t%s\ndevice_bytes\t%s\nprepare_end_bytes\t%s\ncache_guard_bytes\t%s\nqlc_candidate_end_bytes\t%s\nsequential_main_bytes\t%s\nsequential_tail_bytes\t%s\nskip_random\t%s\nsettle_seconds\t%s\n' \
    "$DEVICE" "$DEVICE_BYTES" "$PREPARE_END_BYTES" "$CACHE_GUARD_BYTES" "$((PREPARE_END_BYTES-CACHE_GUARD_BYTES))" "$seq_main" "$seq_tail" "$SKIP_RANDOM" "$PREPARE_SETTLE_SECONDS" > "$RESULT_DIR/plan.tsv"
printf 'read_offset_bytes\t%s\nread_size_bytes\t%s\n' "$read_start" "$read_length" >> "$RESULT_DIR/plan.tsv"
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
job partial_sequential_main 0 "$seq_main" 64k
job partial_sequential_tail "$seq_main" "$seq_tail" 4k
FIO_BIN=${FIO_BIN:-fio}
printf '#!/usr/bin/env bash\nset -euo pipefail\n' > "$RESULT_DIR/commands.sh"
if [[ $SKIP_RANDOM == 0 ]]; then
    for upstream in rnd.job rnd2.job rnd3.job; do
        cp -- "$UPSTREAM_DIR/$upstream" "$RESULT_DIR/$upstream"
        printf '%q %q --filename=%q --output-format=json --output=%q\n' "$FIO_BIN" "$RESULT_DIR/$upstream" "$DEVICE" "$RESULT_DIR/$upstream.json" >> "$RESULT_DIR/commands.sh"
    done
fi
for stage in "${stages[@]}"; do
    printf '%q %q --filename=%q --output-format=json --output=%q\n' "$FIO_BIN" "$RESULT_DIR/$stage.fio" "$DEVICE" "$RESULT_DIR/$stage.fio.json" >> "$RESULT_DIR/commands.sh"
done
record generated 'original random conditioning, then one partial sequential pass; no full cover or prefix rewrite'
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
    for upstream in rnd.job rnd2.job rnd3.job; do run_job "$RESULT_DIR/$upstream" -1; done
else record random_skipped 'Operator declares prior random conditioning already complete'; fi
for index in "${!stages[@]}"; do
    run_job "$RESULT_DIR/${stages[$index]}.fio" "${budgets[$index]}"
done
record settle_wait "$PREPARE_SETTLE_SECONDS seconds; time alone does not prove compaction completion"
sleep "$PREPARE_SETTLE_SECONDS"
"$PYTHON_BIN" - "$RESULT_DIR/completed.json" "$DEVICE" "$DEVICE_BYTES" "$PREPARE_END_BYTES" "$CACHE_GUARD_BYTES" <<'PY'
from __future__ import print_function
import json, sys
json.dump({'schema': 3, 'writes_completed': True, 'qlc_verified': False,
           'device': sys.argv[2], 'device_bytes': int(sys.argv[3]),
           'prepare_end_bytes': int(sys.argv[4]), 'cache_guard_bytes': int(sys.argv[5]),
           'qlc_candidate_end_bytes': int(sys.argv[4])-int(sys.argv[5]),
           'next_step': 'Host verify_qlc_range.py scans the complete selected read range'},
          open(sys.argv[1], 'w'), indent=2)
PY
record complete 'Writes completed; host full-read-range QLC verification still required'
printf '%s\n' "$RESULT_DIR"
