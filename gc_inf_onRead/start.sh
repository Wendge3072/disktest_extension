#!/usr/bin/env bash
# Guest-side single profile. The host controller must acknowledge this RUN_ID.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
die() { echo "start.sh: $*" >&2; exit 1; }
epoch_ms() {
    local value
    value=$(date -u +%s%3N)
    if [[ $value =~ ^[0-9]+$ ]]; then printf '%s' "$value"
    else printf '%s' "$(( $(date -u +%s) * 1000 ))"; fi
}
DRY_RUN=${DRY_RUN:-0}
if [[ ${1:-} == --dry-run ]]; then DRY_RUN=1; shift; fi
[[ $# == 0 ]] || die 'Usage: start.sh [--dry-run]'
[[ $DRY_RUN == 0 || $DRY_RUN == 1 ]] || die 'DRY_RUN must be 0 or 1'
RUN_ID=${RUN_ID:-read-gc-$(date -u +%Y%m%dT%H%M%SZ)}
[[ $RUN_ID =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die 'Invalid RUN_ID (maximum 64 characters)'
DEVICE=${DEVICE:-/dev/vda}
RESULTS_ROOT=${RESULTS_ROOT:-$HERE/results}
mkdir -p -- "$RESULTS_ROOT"
RESULTS_ROOT=$(cd -- "$RESULTS_ROOT" && pwd)
RESULT_DIR=$RESULTS_ROOT/$RUN_ID
mkdir -- "$RESULT_DIR" || die "Result directory already exists: $RESULT_DIR"
FIO_PID=''
FIO_SIGNAL_TARGET=''
FINISHED=0
record() { printf '%s\t%s\t%s\n' "$(epoch_ms)" "$1" "${2:-}" >> "$RESULT_DIR/events.tsv"; }
finish() {
    local rc=$1
    (( FINISHED == 0 )) || return 0
    FINISHED=1
    printf '%s\t%s\t%s\n' "$RUN_ID" "$rc" "$(epoch_ms)" > "$RESULT_DIR/.finished.tsv.tmp"
    mv -- "$RESULT_DIR/.finished.tsv.tmp" "$RESULT_DIR/finished.tsv"
    record finished "fio_exit=$rc"
}
stop_fio() {
    local attempt
    [[ -n $FIO_PID ]] || return 0
    if kill -0 -- "$FIO_SIGNAL_TARGET" 2>/dev/null; then
        kill -TERM -- "$FIO_SIGNAL_TARGET" 2>/dev/null || true
        for attempt in {1..50}; do
            kill -0 -- "$FIO_SIGNAL_TARGET" 2>/dev/null || break
            sleep 0.1
        done
        if kill -0 -- "$FIO_SIGNAL_TARGET" 2>/dev/null; then kill -KILL -- "$FIO_SIGNAL_TARGET" 2>/dev/null || true; fi
    fi
    wait "$FIO_PID" 2>/dev/null || true
    FIO_PID=''
    FIO_SIGNAL_TARGET=''
}
on_exit() {
    local rc=$?
    trap - EXIT HUP INT TERM
    if (( FINISHED == 0 )) && [[ $DRY_RUN != 1 ]]; then
        stop_fio
        finish "$rc"
    fi
    exit "$rc"
}
on_signal() {
    local rc=$1
    trap - HUP INT TERM
    record interrupted "exit=$rc"
    stop_fio
    finish "$rc"
    exit "$rc"
}
trap on_exit EXIT
trap 'on_signal 129' HUP
trap 'on_signal 130' INT
trap 'on_signal 143' TERM
printf 'epoch_ms\tevent\tdetail\n' > "$RESULT_DIR/events.tsv"
record created "$RUN_ID"

if [[ -b $DEVICE ]]; then
    ACTUAL_BYTES=$(blockdev --getsize64 "$DEVICE")
    if [[ -n ${DEVICE_BYTES:-} && $DEVICE_BYTES != "$ACTUAL_BYTES" ]]; then die 'DEVICE_BYTES must equal actual blockdev capacity'; fi
    DEVICE_BYTES=$ACTUAL_BYTES
    record device_class block_device
elif [[ $DRY_RUN == 1 && -n ${DEVICE_BYTES:-} ]]; then
    record device_class 'configuration_only_capacity_override; not QLC evidence'
elif [[ ${ALLOW_REGULAR_FILE:-0} == 1 && -f $DEVICE ]]; then
    ACTUAL_BYTES=$(stat -c %s "$DEVICE" 2>/dev/null || stat -f %z "$DEVICE")
    [[ -z ${DEVICE_BYTES:-} || $DEVICE_BYTES == "$ACTUAL_BYTES" ]] || die 'DEVICE_BYTES must equal actual test file size'
    DEVICE_BYTES=$ACTUAL_BYTES
    record device_class 'regular_file_test; not QLC evidence'
else
    die 'DEVICE must be a block device; regular-file tests require ALLOW_REGULAR_FILE=1'
fi
if [[ $DRY_RUN != 1 ]]; then
    FIO_BIN=${FIO_BIN:-fio}
    command -v "$FIO_BIN" >/dev/null || die "fio executable not found: $FIO_BIN"
    "$FIO_BIN" --version > "$RESULT_DIR/fio.version"
    if command -v pgrep >/dev/null && pgrep -x fio >/dev/null; then
        die 'Another fio process is running; refusing a concurrent profile'
    fi
    if [[ -b $DEVICE ]]; then
        command -v lsblk >/dev/null || die 'lsblk is required to check whether the target is mounted'
        MOUNTS=$(lsblk -nr -o MOUNTPOINT "$DEVICE" | tr -d '[:space:]')
        [[ -z $MOUNTS ]] || die 'The target or one of its partitions is mounted; use the raw FTL device'
    fi
fi
export RUN_ID DEVICE DEVICE_BYTES
export LOG_PREFIX=${LOG_PREFIX:-$RESULT_DIR/profile}
export JOB_FILE=$RESULT_DIR/profile.fio
export METADATA_FILE=$RESULT_DIR/.request.tsv.tmp
bash "$HERE/make_job.sh"
mv -- "$METADATA_FILE" "$RESULT_DIR/request.tsv"
printf 'device_bytes\t%s\n' "$DEVICE_BYTES" > "$RESULT_DIR/capacity.tsv"
record device_capacity "$DEVICE_BYTES"
record configuration_validated "request=$RESULT_DIR/request.tsv"
if [[ $DRY_RUN == 1 ]]; then
    printf '1\n' > "$RESULT_DIR/DRY_RUN"
    record dry_run 'No fio process or controller wait'
    printf '%s\n' "$RESULT_DIR"
    exit 0
fi
READY_TIMEOUT=${READY_TIMEOUT:-900}
[[ $READY_TIMEOUT =~ ^[0-9]{1,9}$ ]] && (( 10#$READY_TIMEOUT > 0 )) || die 'READY_TIMEOUT must be a positive integer'
READY_TIMEOUT=$((10#$READY_TIMEOUT))
DEADLINE=$((SECONDS + READY_TIMEOUT))
record waiting_for_controller "timeout=$READY_TIMEOUT"
while true; do
    [[ ! -e $RESULT_DIR/controller.failed ]] || die 'Host controller reported failure before fio start'
    if [[ -f $RESULT_DIR/controller.ready ]]; then
        READY_ID=$(cat -- "$RESULT_DIR/controller.ready")
        [[ $READY_ID == "$RUN_ID" ]] || die 'controller.ready does not match RUN_ID'
        break
    fi
    (( SECONDS < DEADLINE )) || die 'Timed out waiting for controller.ready'
    sleep 0.2
done
record controller_ready "$RUN_ID"
START_EPOCH=$(epoch_ms)
if command -v setsid >/dev/null; then
    # CentOS util-linux provides setsid: isolate only our fio and its workers.
    setsid "$FIO_BIN" "$JOB_FILE" --output-format=json --output="$RESULT_DIR/result.json" > "$RESULT_DIR/fio.log" 2>&1 &
    FIO_PID=$!
    FIO_SIGNAL_TARGET=-$FIO_PID
else
    # Non-Linux configuration/mock tests; fio normally forwards termination.
    "$FIO_BIN" "$JOB_FILE" --output-format=json --output="$RESULT_DIR/result.json" > "$RESULT_DIR/fio.log" 2>&1 &
    FIO_PID=$!
    FIO_SIGNAL_TARGET=$FIO_PID
fi
printf '%s\t%s\n' "$RUN_ID" "$START_EPOCH" > "$RESULT_DIR/.started.tsv.tmp"
mv -- "$RESULT_DIR/.started.tsv.tmp" "$RESULT_DIR/started.tsv"
record fio_started "pid=$FIO_PID"
while kill -0 "$FIO_PID" 2>/dev/null; do
    [[ ! -e $RESULT_DIR/controller.failed ]] || die 'Host controller reported failure during fio'
    sleep 0.2
done
if wait "$FIO_PID"; then FIO_RC=0; else FIO_RC=$?; fi
FIO_PID=''
FIO_SIGNAL_TARGET=''
# A controller failure racing with fio completion also invalidates the run.
if [[ -e $RESULT_DIR/controller.failed && $FIO_RC == 0 ]]; then FIO_RC=1; fi
finish "$FIO_RC"
printf '%s\n' "$RESULT_DIR"
exit "$FIO_RC"
