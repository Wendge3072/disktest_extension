#!/usr/bin/env bash
# Host-only controller for the two-terminal experiment. No SSH or guest launch.
set -euo pipefail
RUN_ID=${1:-read-gc-$(date -u +%Y%m%dT%H%M%SZ)}
READ_IOPS=${2:-25000}
WRITE_MIB=${3:-512}
RUNTIME=${RUNTIME:-600}
GC_START=${GC_START:-240}
GC_THRESHOLD=${GC_THRESHOLD:-920}
BASELINE_SETTLE=${BASELINE_SETTLE:-60}
START_AT=${START_AT:-$(( $(date +%s) + 120 ))}
SPDK_DIR=${SPDK_DIR:-/home/nly/csal/git/spdk}
SPDK_LOG=${SPDK_LOG:-$SPDK_DIR/log/smoke_test}
[[ $# -le 3 && $RUN_ID =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || exit 2
for value in "$READ_IOPS" "$WRITE_MIB" "$RUNTIME" "$GC_START" "$GC_THRESHOLD"; do
    [[ $value =~ ^[1-9][0-9]{0,8}$ ]] || { echo 'Invalid rate or time' >&2; exit 2; }
done
[[ $START_AT =~ ^[1-9][0-9]{9}$ ]] && (( GC_START < RUNTIME )) || exit 2
[[ $BASELINE_SETTLE =~ ^[0-9]{1,8}$ ]] || exit 2
(( START_AT * 1000 - $(date +%s%3N) >= BASELINE_SETTLE * 1000 )) || {
    echo 'Start time must leave at least BASELINE_SETTLE seconds for settling' >&2; exit 2;
}
[[ -r $SPDK_LOG ]] || { echo "Cannot read SPDK log: $SPDK_LOG" >&2; exit 1; }
run_dir=${HOST_RESULTS_ROOT:-$SPDK_DIR/log/read_gc_results}/$RUN_ID
mkdir -p -- "$(dirname -- "$run_dir")"
mkdir -- "$run_dir"
printf 'epoch_ms,event,threshold,detail\n' > "$run_dir/control.csv"
printf '%s\t%s000\n' "$RUN_ID" "$START_AT" > "$run_dir/scheduled.tsv"
rpc=("${PYTHON_BIN:-python3}" "$SPDK_DIR/scripts/rpc.py")
[[ -z ${RPC_BIN:-} ]] || rpc=("$RPC_BIN")
set_threshold() {
    printf '%s,rpc_begin,%s,%s\n' "$(date +%s%3N)" "$1" "$2" >> "$run_dir/control.csv"
    "${rpc[@]}" bdev_ftl_set_bggc_comp_threshold -b "${FTL_NAME:-FTL0}" -t "$1" > "$run_dir/rpc-$2.log" 2>&1 || return $?
    printf '%s,rpc_success,%s,%s\n' "$(date +%s%3N)" "$1" "$2" >> "$run_dir/control.csv"
}
gc_active=0; tail_pid=''
cleanup() {
    rc=$?; trap - EXIT
    if (( gc_active )); then set_threshold 0 cleanup || rc=1; fi
    if [[ -n $tail_pid ]]; then kill "$tail_pid" 2>/dev/null || true; wait "$tail_pid" 2>/dev/null || true; fi
    if (( rc == 0 )); then touch "$run_dir/controller.complete"
    else touch "$run_dir/controller.aborted"; fi
    echo "Host result: $run_dir (exit=$rc)"
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
tail -n 0 -F -- "$SPDK_LOG" > "$run_dir/spdk.log" 2> "$run_dir/tail.stderr" &
tail_pid=$!
set_threshold 0 baseline
(( START_AT * 1000 - $(date +%s%3N) >= BASELINE_SETTLE * 1000 )) || {
    echo 'Baseline RPC took too long; choose a later start time' >&2; exit 1;
}
echo 'Run the following command in the guest terminal before the scheduled time:'
printf 'START_AT=%s RUNTIME=%s GC_START=%s GC_THRESHOLD=%s bash /root/disktest_extension/gc_inf_onRead/start.sh %s %s %s\n' \
    "$START_AT" "$RUNTIME" "$GC_START" "$GC_THRESHOLD" "$RUN_ID" "$READ_IOPS" "$WRITE_MIB"
wait_until() { while (( $(date +%s%3N) < $1 * 1000 )); do sleep "${POLL_INTERVAL:-0.1}"; done; }
wait_until "$(( START_AT + GC_START ))"
gc_active=1
set_threshold "$GC_THRESHOLD" gc_on
echo "GC threshold=$GC_THRESHOLD"
wait_until "$(( START_AT + RUNTIME ))"
set_threshold 0 gc_off
gc_active=0
