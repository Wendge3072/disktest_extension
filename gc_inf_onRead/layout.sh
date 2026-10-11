#!/usr/bin/env bash
# Common geometry for preparation and BOTH sweep terminals; sizes are binary.
layout_bytes() {
    local value=$1 number factor
    [[ $value =~ ^([0-9]{1,18})([KkMmGgTt]?)$ ]] || { echo 'Layout sizes require integer bytes or K/M/G/T binary units' >&2; return 2; }
    number=$((10#${BASH_REMATCH[1]}))
    case ${BASH_REMATCH[2]} in
        '') factor=1;; K|k) factor=1024;; M|m) factor=1048576;; G|g) factor=1073741824;; T|t) factor=1099511627776;;
    esac
    ((number<=9223372036854775807/factor)) || return 2
    printf '%s' "$((number*factor))"
}
export DEVICE_BYTES=${DEVICE_BYTES:-14541900267520}
export CACHE_GUARD_BYTES=${CACHE_GUARD_BYTES:-1825361100800} # 1700 GiB
export WRITE_SIZE=${WRITE_SIZE:-128G}
if [[ -z ${PREPARE_END_BYTES:-} ]]; then
    _layout_capacity=$(layout_bytes "$DEVICE_BYTES")
    _layout_slot=$(layout_bytes "$WRITE_SIZE")
    _layout_list=${READ_IOPS_LIST:-'1000 5000 10000 25000 50000 100000'}
    _layout_list=${_layout_list//,/ }
    read -r -a _layout_rates <<< "$_layout_list"
    _layout_repeats=${REPEATS:-1}
    [[ $_layout_repeats =~ ^[1-9][0-9]{0,2}$ ]] || { echo 'Invalid REPEATS for preparation budget' >&2; return 2; }
    _layout_reserve=$((2*${#_layout_rates[@]}*_layout_repeats*_layout_slot))
    # 80% is a starting policy, not a measured optimum. Leave part of the
    # original random layout and spare logical space; bigger is not always better.
    PREPARE_PERCENT=${PREPARE_PERCENT:-auto}
    _layout_limit=$((_layout_capacity-_layout_reserve))
    if [[ $PREPARE_PERCENT == auto ]]; then
        _layout_end=$((_layout_capacity/100*80))
        ((_layout_end<=_layout_limit)) || _layout_end=$_layout_limit
    else
        [[ $PREPARE_PERCENT =~ ^[1-9][0-9]?$ ]] || { echo 'PREPARE_PERCENT must be auto or 1..99' >&2; return 2; }
        _layout_end=$((_layout_capacity/100*PREPARE_PERCENT))
        ((_layout_end<=_layout_limit)) || { echo 'Requested x% leaves insufficient space for the complete sweep' >&2; return 2; }
    fi
    ((_layout_end>0)) || { echo 'No address space left for sequential preparation' >&2; return 2; }
    export PREPARE_END_BYTES=$((_layout_end/65536*65536))
fi
export READ_OFFSET=${READ_OFFSET:-0}
if [[ -z ${READ_SIZE:-} ]]; then
    _layout_read_start=$(layout_bytes "$READ_OFFSET")
    _layout_read_size=$(( $(layout_bytes "$PREPARE_END_BYTES")-$(layout_bytes "$CACHE_GUARD_BYTES")-_layout_read_start ))
    ((_layout_read_size>0)) || { echo 'Partial write is too small for the TLC guard' >&2; return 2; }
    export READ_SIZE=$((_layout_read_size/1048576*1048576))
fi
export WRITE_OFFSET=${WRITE_OFFSET:-$PREPARE_END_BYTES}
