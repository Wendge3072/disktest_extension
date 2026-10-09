#!/usr/bin/env bash
# Generate a fio profile and validated protocol metadata; never issue IO.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
die() { echo "make_job.sh: $*" >&2; exit 1; }
integer() {
    local value=$1 label=$2 minimum=${3:-0}
    [[ $value =~ ^[0-9]{1,18}$ ]] || die "$label must be an integer (at most 18 digits)"
    value=$((10#$value))
    (( value >= minimum )) || die "$label must be >= $minimum"
    printf '%s' "$value"
}
bytes() {
    local value=$1 label=$2 number suffix factor
    if [[ $value =~ ^([0-9]{1,18})([KkMmGgTt]?)([iI]?[bB])?$ ]]; then
        number=$(integer "${BASH_REMATCH[1]}" "$label")
        # Save the suffix before integer() can perform a regex match.
        suffix=${value//[0-9]/}
        case $suffix in
            ''|B|b) factor=1 ;;
            K|k|KB|kb|KiB|Kib|kiB|kib) factor=1024 ;;
            M|m|MB|mb|MiB|Mib|miB|mib) factor=1048576 ;;
            G|g|GB|gb|GiB|Gib|giB|gib) factor=1073741824 ;;
            T|t|TB|tb|TiB|Tib|tiB|tib) factor=1099511627776 ;;
            *) die "$label has an unsupported binary unit: $suffix" ;;
        esac
        (( number <= 9223372036854775807 / factor )) || die "$label overflows 64-bit bytes"
        printf '%s' "$((number * factor))"
    else
        die "$label must be bytes or a binary K/M/G/T size"
    fi
}
offset_bytes() {
    local value=$1 label=$2 alignment=$3 result percent
    if [[ $value =~ ^([0-9]{1,3})%$ ]]; then
        percent=$(integer "${BASH_REMATCH[1]}" "$label percent")
        (( percent <= 100 )) || die "$label percent must be <= 100"
        result=$((DEVICE_BYTES / 100 * percent + DEVICE_BYTES % 100 * percent / 100))
        result=$((result / alignment * alignment))
    else
        result=$(bytes "$value" "$label")
        (( result % alignment == 0 )) || die "$label must align to $alignment bytes"
    fi
    printf '%s' "$result"
}

DEVICE=${DEVICE:-/dev/vda}
[[ $DEVICE == /* && $DEVICE != *$'\n'* && $DEVICE != *$'\r'* && $DEVICE != *$'\t'* ]] || die 'DEVICE must be an absolute, single-line path'
RUN_ID=${RUN_ID:-profile}
[[ $RUN_ID =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die 'RUN_ID must contain only letters, digits, dot, underscore and dash'
READ_IOPS=$(integer "${READ_IOPS:?Set READ_IOPS to a positive target IOPS}" READ_IOPS 1)
WRITE_MIB=$(integer "${WRITE_MIB:-512}" WRITE_MIB 1)
RUNTIME=$(integer "${RUNTIME:-600}" RUNTIME 1)
GC_START=$(integer "${GC_START:-240}" GC_START 1)
GC_THRESHOLD=$(integer "${GC_THRESHOLD:-920}" GC_THRESHOLD 1)
READY_TIMEOUT=$(integer "${READY_TIMEOUT:-900}" READY_TIMEOUT 1)
READ_QD=$(integer "${READ_QD:-32}" READ_QD 1)
WRITE_QD=$(integer "${WRITE_QD:-128}" WRITE_QD 1)
READ_SEED=$(integer "${READ_SEED:-2252}" READ_SEED 1)
WRITE_SEED=$(integer "${WRITE_SEED:-9001}" WRITE_SEED 1)
(( GC_START < RUNTIME )) || die 'GC_START must be less than RUNTIME'
if [[ -z ${DEVICE_BYTES:-} ]]; then
    [[ -b $DEVICE ]] || die 'Set DEVICE_BYTES for configuration-only or regular-file testing'
    DEVICE_BYTES=$(blockdev --getsize64 "$DEVICE")
fi
DEVICE_BYTES=$(integer "$DEVICE_BYTES" DEVICE_BYTES 1)
READ_SIZE_BYTES=$(bytes "${READ_SIZE:-1024G}" READ_SIZE)
WRITE_SIZE_BYTES=$(bytes "${WRITE_SIZE:-320G}" WRITE_SIZE)
READ_OFFSET_BYTES=$(offset_bytes "${READ_OFFSET:-2560G}" READ_OFFSET 4096)
WRITE_OFFSET_BYTES=$(offset_bytes "${WRITE_OFFSET:-97%}" WRITE_OFFSET 65536)
(( READ_SIZE_BYTES > 0 && READ_SIZE_BYTES % 4096 == 0 )) || die 'READ_SIZE must be positive and 4KiB-aligned'
(( WRITE_SIZE_BYTES > 0 && WRITE_SIZE_BYTES % 65536 == 0 )) || die 'WRITE_SIZE must be positive and 64KiB-aligned'
(( READ_SIZE_BYTES <= DEVICE_BYTES && READ_OFFSET_BYTES <= DEVICE_BYTES - READ_SIZE_BYTES )) || die 'Read range exceeds device capacity'
(( WRITE_SIZE_BYTES <= DEVICE_BYTES && WRITE_OFFSET_BYTES <= DEVICE_BYTES - WRITE_SIZE_BYTES )) || die 'Write range exceeds device capacity'
(( READ_OFFSET_BYTES >= WRITE_OFFSET_BYTES + WRITE_SIZE_BYTES || WRITE_OFFSET_BYTES >= READ_OFFSET_BYTES + READ_SIZE_BYTES )) || die 'Read and write ranges overlap'
# Division avoids overflow for unreasonable user-supplied runtime/rate values.
(( WRITE_MIB <= WRITE_SIZE_BYTES / 1048576 / RUNTIME )) || die 'Write budget exceeds WRITE_SIZE: enlarge the write range or shorten runtime'

JOB_FILE=${JOB_FILE:-${1:--}}
[[ $# -le 1 ]] || die 'Usage: make_job.sh [OUTPUT_JOB]'
if [[ -z ${LOG_PREFIX:-} ]]; then
    if [[ $JOB_FILE == - ]]; then LOG_PREFIX=$PWD/profile
    else LOG_PREFIX=$(cd -- "$(dirname -- "$JOB_FILE")" && pwd)/profile; fi
fi
[[ $LOG_PREFIX == /* && $LOG_PREFIX != *$'\n'* && $LOG_PREFIX != *$'\r'* && $LOG_PREFIX != *$'\t'* ]] || die 'LOG_PREFIX must be an absolute, single-line path'

render() {
    local line
    while IFS= read -r line || [[ -n $line ]]; do
        line=${line//@@DEVICE@@/$DEVICE}
        line=${line//@@RUNTIME@@/$RUNTIME}
        line=${line//@@WRITE_MIB@@/$WRITE_MIB}
        line=${line//@@READ_IOPS@@/$READ_IOPS}
        line=${line//@@WRITE_OFFSET@@/$WRITE_OFFSET_BYTES}
        line=${line//@@WRITE_SIZE@@/$WRITE_SIZE_BYTES}
        line=${line//@@READ_OFFSET@@/$READ_OFFSET_BYTES}
        line=${line//@@READ_SIZE@@/$READ_SIZE_BYTES}
        line=${line//@@WRITE_QD@@/$WRITE_QD}
        line=${line//@@READ_QD@@/$READ_QD}
        line=${line//@@WRITE_SEED@@/$WRITE_SEED}
        line=${line//@@READ_SEED@@/$READ_SEED}
        line=${line//@@LOG_PREFIX@@/$LOG_PREFIX}
        printf '%s\n' "$line"
    done < "$HERE/templates/profile.fio.in"
}
metadata() {
    printf 'schema\t1\nrun_id\t%s\ndevice\t%s\nwrite_mib\t%s\nread_iops\t%s\nruntime\t%s\ngc_start\t%s\ngc_threshold\t%s\nread_offset_bytes\t%s\nread_size_bytes\t%s\nwrite_offset_bytes\t%s\nwrite_size_bytes\t%s\n' \
        "$RUN_ID" "$DEVICE" "$WRITE_MIB" "$READ_IOPS" "$RUNTIME" "$GC_START" "$GC_THRESHOLD" \
        "$READ_OFFSET_BYTES" "$READ_SIZE_BYTES" "$WRITE_OFFSET_BYTES" "$WRITE_SIZE_BYTES"
}
if [[ $JOB_FILE == - ]]; then render
else
    [[ ! -e $JOB_FILE ]] || die "Refusing to overwrite $JOB_FILE"
    render > "$JOB_FILE"
fi
if [[ -n ${METADATA_FILE:-} ]]; then
    [[ ! -e $METADATA_FILE ]] || die "Refusing to overwrite $METADATA_FILE"
    metadata > "$METADATA_FILE"
fi
