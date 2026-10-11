#!/usr/bin/env bash
# Shared, deterministic schedule for the two terminals. This file issues no IO/RPC.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$HERE/layout.sh"
SWEEP_ID=${1:-read-sweep-$(date -u +%Y%m%dT%H%M%SZ)}
[[ $# -le 1 && $SWEEP_ID =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$ ]] || { echo 'Usage: script.sh [SWEEP_ID (<=40 characters)]' >&2; exit 2; }
READ_IOPS_LIST=${READ_IOPS_LIST:-'1000 5000 10000 25000 50000 100000'}
READ_IOPS_LIST=${READ_IOPS_LIST//,/ }
[[ $READ_IOPS_LIST != *$'\n'* && $READ_IOPS_LIST != *$'\r'* ]] || { echo 'Read IOPS list must fit on one line' >&2; exit 2; }
read -r -a rates <<< "$READ_IOPS_LIST"
REPEATS=${REPEATS:-1}
WRITE_MIB=${WRITE_MIB:-512}
MEASURE_SECONDS=${MEASURE_SECONDS:-180}
RAMP_SECONDS=${RAMP_SECONDS:-60}
RECOVER_SECONDS=${RECOVER_SECONDS:-300}
GC_SETTLE_SECONDS=${GC_SETTLE_SECONDS:-60}
BASELINE_SETTLE=${BASELINE_SETTLE:-30}
GC_THRESHOLD=${GC_THRESHOLD:-920}
MIN_FREE_BANDS=${MIN_FREE_BANDS:-2048}
TIMING_MARGIN=${TIMING_MARGIN:-5}
export DEVICE=${DEVICE:-/dev/vda} DEVICE_BYTES=${DEVICE_BYTES:-14541900267520}
export WRITE_RW=${WRITE_RW:-write}
export READ_QD=${READ_QD:-32} WRITE_QD=${WRITE_QD:-128} READ_SEED=${READ_SEED:-2252} WRITE_SEED=${WRITE_SEED:-9001}
export FTL_NAME=${FTL_NAME:-FTL0}
[[ $FTL_NAME =~ ^[A-Za-z0-9_.-]+$ ]] || { echo 'Invalid FTL_NAME' >&2; exit 2; }
for key in REPEATS WRITE_MIB MEASURE_SECONDS RAMP_SECONDS RECOVER_SECONDS GC_SETTLE_SECONDS BASELINE_SETTLE GC_THRESHOLD MIN_FREE_BANDS TIMING_MARGIN; do
    value=${!key}
    [[ $value =~ ^[0-9]{1,8}$ ]] || { echo "$key must be an integer" >&2; exit 2; }
    printf -v "$key" '%s' "$((10#$value))"
done
(( REPEATS>0 && REPEATS<=20 && WRITE_MIB>0 && MEASURE_SECONDS>0 && GC_THRESHOLD>WRITE_MIB && MIN_FREE_BANDS>5 && BASELINE_SETTLE>0 && GC_SETTLE_SECONDS>0 && TIMING_MARGIN>0 && RECOVER_SECONDS>=BASELINE_SETTLE )) || { echo 'Invalid sweep settings; positive GC threshold must exceed write rate and settling intervals must be positive' >&2; exit 2; }
(( ${#rates[@]}>0 && ${#rates[@]}<=30 )) || exit 2
seen=' '
for i in "${!rates[@]}"; do
    [[ ${rates[i]} =~ ^[1-9][0-9]{0,7}$ && $seen != *" ${rates[i]} "* ]] || { echo 'Read IOPS must be unique positive integers' >&2; exit 2; }
    seen+="${rates[i]} "
done
START_AT=${START_AT:-$(( $(date +%s) + RECOVER_SECONDS + 120 ))}
[[ $START_AT =~ ^[1-9][0-9]{9}$ ]] || { echo 'Invalid START_AT' >&2; exit 2; }
PHASE_SECONDS=$((RAMP_SECONDS+MEASURE_SECONDS))
CYCLE_SECONDS=$((RECOVER_SECONDS+2*(PHASE_SECONDS+TIMING_MARGIN)+GC_SETTLE_SECONDS))
PREPARE_END_BYTES=$(layout_bytes "$PREPARE_END_BYTES")
CACHE_GUARD_BYTES=$(layout_bytes "$CACHE_GUARD_BYTES")
WRITE_START_BYTES=$(layout_bytes "$WRITE_OFFSET")
WRITE_SLOT_BYTES=$(layout_bytes "$WRITE_SIZE")
READ_START_BYTES=$(layout_bytes "$READ_OFFSET")
READ_LENGTH_BYTES=$(layout_bytes "$READ_SIZE")
DEVICE_BYTES=$(layout_bytes "$DEVICE_BYTES")
((CACHE_GUARD_BYTES>0 && PREPARE_END_BYTES>CACHE_GUARD_BYTES && PREPARE_END_BYTES<DEVICE_BYTES && PREPARE_END_BYTES%65536==0 && CACHE_GUARD_BYTES%4096==0 && READ_START_BYTES%4096==0 && READ_LENGTH_BYTES>0 && READ_LENGTH_BYTES%4096==0 && READ_START_BYTES+READ_LENGTH_BYTES<=PREPARE_END_BYTES-CACHE_GUARD_BYTES)) || { echo 'Read region must fit before the partial-write end minus the TLC guard' >&2; exit 2; }
((WRITE_START_BYTES>=PREPARE_END_BYTES && WRITE_START_BYTES%65536==0 && WRITE_SLOT_BYTES>0 && WRITE_SLOT_BYTES%65536==0 && WRITE_MIB<=WRITE_SLOT_BYTES/1048576/PHASE_SECONDS)) || { echo 'Writes must start after preparation and each slot must cover the complete ramp+measure budget' >&2; exit 2; }
((WRITE_START_BYTES+2*${#rates[@]}*REPEATS*WRITE_SLOT_BYTES<=DEVICE_BYTES)) || { echo 'Entire sweep write slots exceed device capacity; reduce repeats/rates/slot size' >&2; exit 2; }
write_plan() {
    local round rate n=0 baseline gc end point
    printf 'point_id\trepeat\tread_iops\tbaseline_epoch\tgc_epoch\tend_epoch\tbaseline_write_offset\tgc_write_offset\n'
    for ((round=1;round<=REPEATS;round++)); do
        # Alternate order to expose drift; these are temporal repeats, not reset replicas.
        for ((i=0;i<${#rates[@]};i++)); do
            if ((round%2)); then rate=${rates[i]}; else rate=${rates[${#rates[@]}-1-i]}; fi
            printf -v point 'r%02d-iops%s' "$round" "$rate"
            baseline=$((START_AT+n*CYCLE_SECONDS));gc=$((baseline+PHASE_SECONDS+TIMING_MARGIN+GC_SETTLE_SECONDS));end=$((gc+PHASE_SECONDS+TIMING_MARGIN))
            printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$point" "$round" "$rate" "$baseline" "$gc" "$end" "$((WRITE_START_BYTES+2*n*WRITE_SLOT_BYTES))" "$((WRITE_START_BYTES+(2*n+1)*WRITE_SLOT_BYTES))"
            n=$((n+1))
        done
    done
}
write_settings() {
    for key in SWEEP_ID READ_IOPS_LIST REPEATS WRITE_MIB MEASURE_SECONDS RAMP_SECONDS RECOVER_SECONDS GC_SETTLE_SECONDS BASELINE_SETTLE GC_THRESHOLD MIN_FREE_BANDS TIMING_MARGIN START_AT DEVICE DEVICE_BYTES PREPARE_END_BYTES CACHE_GUARD_BYTES READ_OFFSET READ_SIZE WRITE_OFFSET WRITE_SIZE WRITE_RW READ_QD WRITE_QD READ_SEED WRITE_SEED FTL_NAME; do printf '%s\t%s\n' "$key" "${!key}"; done
}
wait_until() { while (( $(date +%s%3N) < $1*1000 )); do sleep "${POLL_INTERVAL:-0.1}"; done; }
