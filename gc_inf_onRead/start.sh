#!/usr/bin/env bash
# Guest: run the read/write profile. QLC residency is checked after preparation.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
RUN_ID=${1:-${RUN_ID:-read-gc-$(date -u +%Y%m%dT%H%M%SZ)}}
export READ_IOPS=${2:-${READ_IOPS:-25000}}
export WRITE_MIB=${3:-${WRITE_MIB:-512}}
export RUN_ID DEVICE=${DEVICE:-/dev/vda}
RESULTS_ROOT=${RESULTS_ROOT:-$HERE/results}
DRY_RUN=${DRY_RUN:-0}
FIO_BIN=${FIO_BIN:-fio}
[[ $# -le 3 && $RUN_ID =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || {
    echo 'Usage: bash start.sh [RUN_ID [READ_IOPS [WRITE_MIB]]]' >&2; exit 2;
}
[[ $DRY_RUN == 0 || $DRY_RUN == 1 ]] || { echo 'DRY_RUN must be 0 or 1' >&2; exit 2; }

if [[ -b $DEVICE ]]; then
    export DEVICE_BYTES=$(blockdev --getsize64 "$DEVICE")
    [[ -z $(lsblk -nr -o MOUNTPOINT "$DEVICE" | tr -d '[:space:]') ]] || {
        echo 'Target device is mounted' >&2; exit 1;
    }
elif [[ $DRY_RUN == 1 && -n ${DEVICE_BYTES:-} ]]; then
    : # Generate only; never open the device.
elif [[ ${ALLOW_REGULAR_FILE:-0} != 1 || ! -f $DEVICE ]]; then
    echo "Not a block device: $DEVICE" >&2; exit 1
fi
if [[ $DRY_RUN == 0 ]] && pgrep -x fio >/dev/null; then
    echo 'Another fio is running' >&2; exit 1
fi
mkdir -p -- "$RESULTS_ROOT"
RESULTS_ROOT=$(cd -- "$RESULTS_ROOT" && pwd)
RESULT_DIR=$RESULTS_ROOT/$RUN_ID
mkdir -- "$RESULT_DIR" # Refuse to overwrite an earlier result.
export LOG_PREFIX=$RESULT_DIR/profile METADATA_FILE=$RESULT_DIR/request.tsv
bash "$HERE/make_job.sh" "$RESULT_DIR/profile.fio"
# Keep all foreground writes inside the prefix prepared for this experiment.
write_end=$(awk -F '\t' '/^write_offset_bytes\t/ {o=$2} /^write_size_bytes\t/ {s=$2} END {printf "%.0f", o+s}' "$METADATA_FILE")
(( write_end <= ${PREFIX_SIZE_BYTES:-1825361100800} )) || {
    echo 'Write range must stay inside the prepared low-address prefix' >&2; exit 1;
}
if [[ $DRY_RUN == 1 ]]; then
    echo "$RESULT_DIR"
    exit 0
fi

"$FIO_BIN" --version > "$RESULT_DIR/fio.version"
fio_pid=''
signal_target=''
finish() {
    rc=$?
    trap - EXIT
    if [[ -n $fio_pid ]]; then
        kill -TERM -- "$signal_target" 2>/dev/null || true
        wait "$fio_pid" 2>/dev/null || true
    fi
    printf '%s\t%s\t%s\n' "$RUN_ID" "$rc" "$(date +%s%3N)" > "$RESULT_DIR/finished.tsv"
    echo "Guest result: $RESULT_DIR (exit=$rc)" >&2
    exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
started_ms=$(date +%s%3N)
if command -v setsid >/dev/null; then
    setsid "$FIO_BIN" "$RESULT_DIR/profile.fio" --output-format=json --output="$RESULT_DIR/result.json" > "$RESULT_DIR/fio.log" 2>&1 &
    fio_pid=$!; signal_target=-$fio_pid
else
    "$FIO_BIN" "$RESULT_DIR/profile.fio" --output-format=json --output="$RESULT_DIR/result.json" > "$RESULT_DIR/fio.log" 2>&1 &
    fio_pid=$!; signal_target=$fio_pid
fi
printf '%s\t%s\n' "$RUN_ID" "$started_ms" > "$RESULT_DIR/started.tsv"
# The host reads this one line over the same SSH connection that runs fio.
printf 'STARTED\t%s\t%s\n' "$RUN_ID" "$started_ms"
wait "$fio_pid"
fio_pid=''
