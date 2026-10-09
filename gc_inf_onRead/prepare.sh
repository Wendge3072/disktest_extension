#!/usr/bin/env bash
# Explicit opt-in only. Reuses upstream conditioning, never the 16-point scan.
# Conditioning does NOT prove that the cold read range is written or in QLC.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
UPSTREAM_DIR=${UPSTREAM_DIR:-$HERE/../gc_inf_onBW}
DEVICE=${DEVICE:-/dev/vda}
[[ $DEVICE == /dev/vda ]] || { echo 'prepare.sh only supports the unchanged upstream /dev/vda jobs' >&2; exit 1; }
[[ -b $DEVICE ]] || { echo 'prepare.sh requires /dev/vda to be a block device' >&2; exit 1; }
FIO_BIN=${FIO_BIN:-fio}
command -v "$FIO_BIN" >/dev/null || { echo 'fio not installed' >&2; exit 1; }
UPSTREAM_DIR=$(cd -- "$UPSTREAM_DIR" && pwd)
for job in rnd.job rnd2.job rnd3.job rnd64.job; do
    [[ -f $UPSTREAM_DIR/$job ]] || { echo "Missing upstream job $job" >&2; exit 1; }
done
PREPARE_RUN_ID=${PREPARE_RUN_ID:-prepare-$(date -u +%Y%m%dT%H%M%SZ)}
[[ $PREPARE_RUN_ID =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { echo 'Invalid PREPARE_RUN_ID' >&2; exit 1; }
RESULTS_ROOT=${RESULTS_ROOT:-$HERE/results}
mkdir -p -- "$RESULTS_ROOT"
RESULTS_ROOT=$(cd -- "$RESULTS_ROOT" && pwd)
RESULT_DIR=$RESULTS_ROOT/$PREPARE_RUN_ID
mkdir -- "$RESULT_DIR" || { echo 'Preparation output already exists' >&2; exit 1; }
cd -- "$UPSTREAM_DIR"
printf 'epoch_ms\tevent\tdetail\n' > "$RESULT_DIR/events.tsv"
for job in rnd.job rnd2.job rnd3.job rnd64.job; do
    printf '%s000\tjob_start\t%s\n' "$(date -u +%s)" "$job" >> "$RESULT_DIR/events.tsv"
    "$FIO_BIN" "$job" --output-format=json --output="$RESULT_DIR/$job.json" > "$RESULT_DIR/$job.log" 2>&1
    printf '%s000\tjob_end\t%s\n' "$(date -u +%s)" "$job" >> "$RESULT_DIR/events.tsv"
done
printf '%s000\tdrain_wait\t600s\n' "$(date -u +%s)" >> "$RESULT_DIR/events.tsv"
sleep 600
printf '%s000\tcomplete\tConditioning only; QLC residency unverified\n' "$(date -u +%s)" >> "$RESULT_DIR/events.tsv"
printf '%s\n' "$RESULT_DIR"
