#!/usr/bin/env bash
# Guest: one command runs all paired IOPS points; no GC RPC and no extra prewarm.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ -n ${START_AT:-} || ${DRY_RUN:-0} == 1 ]] || { echo 'Copy the START_AT command printed by the host' >&2; exit 2; }
source "$HERE/sweep_config.sh"
root=${RESULTS_ROOT:-$HERE/results}/$SWEEP_ID
mkdir -p -- "$(dirname -- "$root")";mkdir -- "$root"
write_plan > "$root/plan.tsv";write_settings > "$root/settings.tsv"
export RESULTS_ROOT=$root RUNTIME=$MEASURE_SECONDS RAMP_TIME=$RAMP_SECONDS GC_THRESHOLD WRITE_MIB
if [[ ${DRY_RUN:-0} != 1 && -b $DEVICE ]]; then
    [[ $(blockdev --getsize64 "$DEVICE") == "$DEVICE_BYTES" ]] || { echo 'Actual FTL capacity differs from the complete sweep plan' >&2; exit 1; }
fi
# Validate the entire address/budget configuration before the first fio.
READ_IOPS=${rates[0]} PHASE=baseline LOG_PREFIX=$root/preflight bash "$HERE/make_job.sh" "$root/preflight.fio"
child=''
cleanup() {
    rc=$?;trap - EXIT
    if [[ -n $child ]];then kill -TERM "$child" 2>/dev/null || true;wait "$child" 2>/dev/null || true;fi
    printf '%s\t%s\n' "$SWEEP_ID" "$rc" > "$root/sweep.finished.tsv"
    exit "$rc"
}
trap cleanup EXIT;trap 'exit 130' INT;trap 'exit 143' TERM HUP
while IFS=$'\t' read -r point repeat rate baseline gc end baseline_write gc_write; do
    [[ $point != point_id ]] || continue
    for phase in baseline gc; do
        if [[ $phase == baseline ]]; then target=$baseline; offset=$baseline_write; else target=$gc; offset=$gc_write; fi
        echo "$point: $phase, Read=$rate IOPS, start=$target" >&2
        if [[ ${DRY_RUN:-0} == 1 ]]; then
            env -u START_AT WRITE_OFFSET=$offset PHASE=$phase bash "$HERE/start.sh" "$point-$phase" "$rate" "$WRITE_MIB"
        else
            START_AT=$target WRITE_OFFSET=$offset PHASE=$phase bash "$HERE/start.sh" "$point-$phase" "$rate" "$WRITE_MIB" & child=$!
            wait "$child";child=''
            # The next phase has an explicit deadline, not a sleep after fio.
        fi
    done
done < "$root/plan.tsv"
echo "Guest sweep complete: $root"
