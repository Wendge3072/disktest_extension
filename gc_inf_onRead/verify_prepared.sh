#!/usr/bin/env bash
# Host: derive the SAME geometry as prepare/sweep, then verify its whole read range.
# No GC switching or writes. Existing Guest SSH authentication is required.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$HERE/sweep_config.sh"
SPDK_DIR=${SPDK_DIR:-/home/nly/csal/git/spdk}
root=${VERIFY_RESULTS_ROOT:-$SPDK_DIR/log/qlc_range_verification}/$SWEEP_ID
mkdir -p -- "$(dirname -- "$root")";mkdir -- "$root"
READ_IOPS=${rates[0]} PHASE=baseline WRITE_MIB=$WRITE_MIB RUNTIME=$MEASURE_SECONDS RAMP_TIME=$RAMP_SECONDS LOG_PREFIX=$root/profile \
    bash "$HERE/make_job.sh" "$root/profile.fio"
if [[ ${DRY_RUN:-0} == 1 ]];then echo "$root/profile.fio";exit 0;fi
"${PYTHON_BIN:-python3}" "$HERE/verify_qlc_range.py" \
    --profile "$root/profile.fio" --spdk-dir "$SPDK_DIR" \
    --prepare-end-bytes "$PREPARE_END_BYTES" --cache-guard-bytes "$CACHE_GUARD_BYTES" \
    --expected-capacity-bytes "$DEVICE_BYTES" \
    --known-hosts "${KNOWN_HOSTS:-/home/nly/csal/ada-fig5-guest-20261009.known_hosts}" \
    --output-dir "$root/verification"
