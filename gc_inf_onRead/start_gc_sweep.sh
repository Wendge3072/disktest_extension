#!/usr/bin/env bash
# Host: recovery, paired GC control, one log per IOPS point. No guest SSH.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$HERE/sweep_config.sh"
SPDK_DIR=${SPDK_DIR:-/home/nly/csal/git/spdk}
SPDK_LOG=${SPDK_LOG:-$SPDK_DIR/log/gc_influnce_on_read}
FTL_NAME=${FTL_NAME:-FTL0}
PYTHON_BIN=${PYTHON_BIN:-python3}
(( START_AT-$(date +%s)>=RECOVER_SECONDS )) || { echo 'START_AT leaves too little recovery time' >&2;exit 2; }
[[ -r $SPDK_LOG ]] || { echo "Cannot read $SPDK_LOG" >&2;exit 1; }
root=${HOST_RESULTS_ROOT:-$SPDK_DIR/log/read_gc_results}/$SWEEP_ID
mkdir -p -- "$(dirname -- "$root")";mkdir -- "$root"
# Only one new sweep controller may operate FTL0 at a time.
exec 9>"${CONTROL_LOCK_DIR:-/var/tmp}/read-gc-$FTL_NAME.lock"
flock -n 9 || { echo 'Another read-GC sweep controller is running' >&2;exit 1; }
write_plan > "$root/plan.tsv";write_settings > "$root/settings.tsv"
printf 'epoch_ms,event,threshold,detail\n' > "$root/control.csv"
rpc=("$PYTHON_BIN" "$SPDK_DIR/scripts/rpc.py");[[ -z ${RPC_BIN:-} ]] || rpc=("$RPC_BIN")
point_dir='';tail_pid='';armed=0
set_threshold() {
    local stamp
    stamp=$(date +%s%3N)
    printf '%s,rpc_begin,%s,%s\n' "$stamp" "$1" "$2" >> "$root/control.csv"
    armed=1
    "${rpc[@]}" bdev_ftl_set_bggc_comp_threshold -b "$FTL_NAME" -t "$1" > "$root/rpc-$stamp-$2.log" 2>&1
    stamp=$(date +%s%3N)
    printf '%s,rpc_success,%s,%s\n' "$stamp" "$1" "$2" >> "$root/control.csv"
    [[ -z $point_dir ]] || printf '%s,rpc_success,%s,%s\n' "$stamp" "$1" "$2" >> "$point_dir/control.csv"
}
cleanup() {
    rc=$?;trap - EXIT
    if ((armed));then set_threshold 0 cleanup || rc=1;fi
    if [[ -n $tail_pid ]];then kill "$tail_pid" 2>/dev/null || true;wait "$tail_pid" 2>/dev/null || true;fi
    printf '%s\t%s\n' "$SWEEP_ID" "$rc" > "$root/sweep.finished.tsv"
    ((rc==0)) || echo 'Host aborted: stop the guest sweep in the other terminal.' >&2
    exit "$rc"
}
trap cleanup EXIT;trap 'exit 130' INT;trap 'exit 143' TERM HUP
# Print all shared settings so the two terminals cannot silently use different schedules.
printf 'Guest command (copy this entire line):\n'
printf 'env '
for key in START_AT READ_IOPS_LIST REPEATS WRITE_MIB MEASURE_SECONDS RAMP_SECONDS RECOVER_SECONDS GC_SETTLE_SECONDS BASELINE_SETTLE GC_THRESHOLD MIN_FREE_BANDS TIMING_MARGIN;do printf '%s=%q ' "$key" "${!key}";done
for key in DEVICE DEVICE_BYTES PREPARE_END_BYTES CACHE_GUARD_BYTES READ_OFFSET READ_SIZE WRITE_OFFSET WRITE_SIZE WRITE_RW READ_QD WRITE_QD READ_SEED WRITE_SEED FTL_NAME;do [[ -z ${!key:-} ]] || printf '%s=%q ' "$key" "${!key}";done
printf 'bash %q/start_sweep.sh %q\n' "${GUEST_DIR:-/root/disktest_extension/gc_inf_onRead}" "$SWEEP_ID"
while IFS=$'\t' read -r point repeat rate baseline gc end baseline_write gc_write;do
    [[ $point != point_id ]] || continue
    point_dir=$root/$point;mkdir -- "$point_dir"
    printf 'epoch_ms,event,threshold,detail\n' > "$point_dir/control.csv"
    tail -n 0 -F -- "$SPDK_LOG" > "$point_dir/spdk.log" 2> "$point_dir/tail.stderr" & tail_pid=$!
    # Stop recovery as soon as its reserve is reached, preserving GC candidates.
    set_threshold 0 recovery_check
    sleep "${TELEMETRY_WAIT_SECONDS:-2}"
    if ! "$PYTHON_BIN" "$HERE/check_free_bands.py" "$point_dir/spdk.log" "$FTL_NAME" "$MIN_FREE_BANDS" > "$point_dir/free-bands-before-recovery.txt" 2>&1; then
        set_threshold "$GC_THRESHOLD" recovery
        while ! "$PYTHON_BIN" "$HERE/check_free_bands.py" "$point_dir/spdk.log" "$FTL_NAME" "$MIN_FREE_BANDS" > "$point_dir/free-bands-recovery.txt" 2>&1; do
            (( $(date +%s%3N)<(baseline-BASELINE_SETTLE)*1000 )) || { echo 'Recovery could not establish the space reserve; stop the guest' >&2;exit 1; }
            sleep 2
        done
        set_threshold 0 recovery_done
    fi
    wait_until "$((baseline-BASELINE_SETTLE))"
    "$PYTHON_BIN" "$HERE/check_free_bands.py" "$point_dir/spdk.log" "$FTL_NAME" "$MIN_FREE_BANDS" > "$point_dir/free-bands-before-baseline.txt"
    set_threshold 0 baseline
    # Leave the same timing margin used for fio startup/GC switching.
    preflight_lead=$TIMING_MARGIN;((preflight_lead<=BASELINE_SETTLE)) || preflight_lead=$BASELINE_SETTLE
    wait_until "$((baseline-preflight_lead))"
    quiet_samples=$BASELINE_SETTLE;((quiet_samples<=10)) || quiet_samples=10
    "$PYTHON_BIN" "$HERE/check_free_bands.py" "$point_dir/spdk.log" "$FTL_NAME" "$MIN_FREE_BANDS" --quiet-samples "$quiet_samples" > "$point_dir/baseline-ready.txt"
    (( $(date +%s%3N)<baseline*1000 )) || { echo 'Recovery/check missed baseline deadline' >&2;exit 1; }
    wait_until "$((baseline+PHASE_SECONDS+TIMING_MARGIN))"
    # Keep the inter-phase gap at T=0. Starting GC 60 s before reads would
    # consume candidates in an idle period and change the treatment itself.
    wait_until "$((gc-TIMING_MARGIN))"
    set_threshold "$GC_THRESHOLD" gc_on
    (( $(date +%s%3N)<gc*1000 )) || { echo 'GC RPC missed the second fio start' >&2;exit 1; }
    if ((RAMP_SECONDS>0)); then
        wait_until "$((gc+RAMP_SECONDS-1))"
        active_samples=$RAMP_SECONDS;((active_samples<=10)) || active_samples=10
        "$PYTHON_BIN" "$HERE/check_free_bands.py" "$point_dir/spdk.log" "$FTL_NAME" 6 --active-samples "$active_samples" > "$point_dir/gc-ready.txt"
    fi
    wait_until "$end"
    set_threshold 0 gc_off
    kill "$tail_pid";wait "$tail_pid" 2>/dev/null || true;tail_pid=''
    touch "$point_dir/controller.complete"
    echo "$point finished (Read=$rate IOPS)"
    point_dir=''
done < "$root/plan.tsv"
echo "Host sweep complete: $root"
