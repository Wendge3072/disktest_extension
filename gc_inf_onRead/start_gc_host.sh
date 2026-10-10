#!/usr/bin/env bash
# Host: start guest fio over SSH, switch GC at 240 s, restore it at 600 s.
set -euo pipefail
RUN_ID=${1:-${RUN_ID:-read-gc-$(date -u +%Y%m%dT%H%M%SZ)}}
READ_IOPS=${2:-${READ_IOPS:-25000}}
WRITE_MIB=${3:-${WRITE_MIB:-512}}
RUNTIME=${RUNTIME:-600}
GC_START=${GC_START:-240}
GC_THRESHOLD=${GC_THRESHOLD:-920}
SPDK_DIR=${SPDK_DIR:-/home/nly/csal/git/spdk}
SPDK_LOG=${SPDK_LOG:-$SPDK_DIR/log/smoke_test}
FTL_NAME=${FTL_NAME:-FTL0}
GUEST_DIR=${GUEST_DIR:-/root/disktest_extension/gc_inf_onRead}
GUEST_RESULTS_ROOT=${GUEST_RESULTS_ROOT:-$GUEST_DIR/results}
SSH_TARGET=${SSH_TARGET:-root@127.0.0.1}
SSH_PORT=${SSH_PORT:-32001}
[[ $# -le 3 && $RUN_ID =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || {
    echo 'Usage: sudo bash start_gc_host.sh [RUN_ID [READ_IOPS [WRITE_MIB]]]' >&2; exit 2;
}
for value in "$READ_IOPS" "$WRITE_MIB" "$RUNTIME" "$GC_START" "$GC_THRESHOLD"; do
    [[ $value =~ ^[1-9][0-9]{0,8}$ ]] || { echo 'Rates and times must be positive integers' >&2; exit 2; }
done
(( GC_START < RUNTIME )) || { echo 'GC_START must be less than RUNTIME' >&2; exit 2; }
run_dir=${HOST_RESULTS_ROOT:-$SPDK_DIR/log/read_gc_results}/$RUN_ID
mkdir -p -- "$(dirname -- "$run_dir")"
mkdir -- "$run_dir"
printf 'epoch_ms,event,threshold,detail\n' > "$run_dir/control.csv"
now_ms() { date +%s%3N; }
record() { printf '%s,%s,%s,%s\n' "$(now_ms)" "$1" "${2:-}" "${3:-}" >> "$run_dir/control.csv"; }
rpc=("${PYTHON_BIN:-python3}" "$SPDK_DIR/scripts/rpc.py")
[[ -z ${RPC_BIN:-} ]] || rpc=("$RPC_BIN")
set_threshold() {
    record rpc_begin "$1" "$2"
    "${rpc[@]}" bdev_ftl_set_bggc_comp_threshold -b "$FTL_NAME" -t "$1" > "$run_dir/rpc-$2.log" 2>&1 || return $?
    record rpc_success "$1" "$2"
}
ssh_pid=''; tail_pid=''; gc_active=0
cleanup() {
    rc=$?
    trap - EXIT
    if (( gc_active )); then set_threshold 0 cleanup || rc=1; fi
    for pid in "$ssh_pid" "$tail_pid"; do
        [[ -z $pid ]] || { kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; }
    done
    if (( rc == 0 )); then touch "$run_dir/controller.complete"
    else touch "$run_dir/controller.aborted"; fi
    echo "Host result: $run_dir (exit=$rc)"
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
[[ -r $SPDK_LOG ]] || { echo "Cannot read SPDK log: $SPDK_LOG" >&2; exit 1; }
tail -n 0 -F -- "$SPDK_LOG" > "$run_dir/spdk.log" 2> "$run_dir/tail.stderr" &
tail_pid=$!
set_threshold 0 baseline
echo 'GC threshold=0; settling before guest fio starts.'
sleep "${BASELINE_SETTLE:-60}"

ssh_options=(-n -p "$SSH_PORT" -o StrictHostKeyChecking=yes)
[[ -z ${KNOWN_HOSTS:-} ]] || ssh_options+=(-o "UserKnownHostsFile=$KNOWN_HOSTS")
printf -v remote 'cd %q && env DEVICE=%q RUNTIME=%q GC_START=%q GC_THRESHOLD=%q RESULTS_ROOT=%q bash start.sh %q %q %q' \
    "$GUEST_DIR" "${DEVICE:-/dev/vda}" "$RUNTIME" "$GC_START" "$GC_THRESHOLD" "$GUEST_RESULTS_ROOT" "$RUN_ID" "$READ_IOPS" "$WRITE_MIB"
# One SSH connection: receive the start time, then let guest fio keep running.
exec 3< <(ssh "${ssh_options[@]}" "$SSH_TARGET" "$remote")
ssh_pid=$!
IFS=$'\t' read -r -t "${START_TIMEOUT:-120}" tag guest_id started_ms <&3
[[ $tag == STARTED && $guest_id == "$RUN_ID" && $started_ms =~ ^[0-9]{13}$ ]] || {
    echo 'Guest did not return a valid fio start time' >&2; exit 1;
}
skew=$(( $(now_ms) - started_ms ))
(( skew >= -5000 && skew <= 5000 )) || { echo 'Check host/guest clock alignment' >&2; exit 1; }
printf '%s\t%s\n' "$RUN_ID" "$started_ms" > "$run_dir/started.tsv"
echo "Guest fio started: $RUN_ID"
while (( $(now_ms) - started_ms < RUNTIME * 1000 )); do
    kill -0 "$ssh_pid" 2>/dev/null || { echo 'Guest fio/SSH ended early' >&2; exit 1; }
    if (( ! gc_active && $(now_ms) - started_ms >= GC_START * 1000 )); then
        gc_active=1 # A failed RPC response may still mean the change was applied.
        set_threshold "$GC_THRESHOLD" gc_on
        echo "GC threshold=$GC_THRESHOLD"
    fi
    sleep "${POLL_INTERVAL:-1}"
done
set_threshold 0 gc_off
gc_active=0
wait "$ssh_pid"
ssh_pid=''
