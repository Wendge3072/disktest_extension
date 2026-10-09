#!/usr/bin/env bash
# Host controller. Running this file changes the selected FTL GC threshold.
set -Eeuo pipefail
umask 077

: "${RUN_ID:?Set RUN_ID to the matching guest run ID}"
[[ "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || { echo "Invalid RUN_ID" >&2; exit 2; }
SPDK_DIR=${SPDK_DIR:-/home/nly/csal/git/spdk}
FTL_NAME=${FTL_NAME:-FTL0}
SPDK_LOG=${SPDK_LOG:-$SPDK_DIR/log/smoke_test}
HOST_RESULTS_ROOT=${HOST_RESULTS_ROOT:-$SPDK_DIR/log/read_gc_results}
GUEST_RESULTS_ROOT=${GUEST_RESULTS_ROOT:-/root/disktest_extension/gc_inf_onRead/results}
SSH_TARGET=${SSH_TARGET:-root@127.0.0.1}
SSH_PORT=${SSH_PORT:-32001}
BASELINE_SETTLE=${BASELINE_SETTLE:-60}
WAIT_TIMEOUT=${WAIT_TIMEOUT:-900}
POLL_INTERVAL=${POLL_INTERVAL:-1}
PYTHON_BIN=${PYTHON_BIN:-python3}
[[ "$FTL_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || { echo "Invalid FTL_NAME" >&2; exit 2; }
[[ "$SSH_PORT" =~ ^[0-9]+$ ]] || { echo "Invalid SSH_PORT" >&2; exit 2; }
[[ "$BASELINE_SETTLE" =~ ^[0-9]+$ && "$WAIT_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || { echo "Invalid wait durations" >&2; exit 2; }
[[ "$GUEST_RESULTS_ROOT" = /* && "$GUEST_RESULTS_ROOT" != *$'\n'* ]] || { echo "GUEST_RESULTS_ROOT must be an absolute path" >&2; exit 2; }

command -v flock >/dev/null
exec 9>"${CONTROL_LOCK_DIR:-/tmp}/ada-read-gc-${FTL_NAME}.lock"
flock -n 9 || { echo "Another controller holds the lock for $FTL_NAME" >&2; exit 2; }

# Detect old controllers without printing unrelated process arguments.
"$PYTHON_BIN" - "${BASHPID:-$$}" <<'PY'
import re, subprocess, sys
rows = {}
for line in subprocess.check_output(['ps', '-eo', 'pid=,ppid=,args='], universal_newlines=True).splitlines():
    fields = line.strip().split(None, 2)
    if len(fields) == 3:
        rows[int(fields[0])] = (int(fields[1]), fields[2])
exclude = {int(sys.argv[1])}
pid = int(sys.argv[1])
while pid in rows and rows[pid][0] not in exclude:
    pid = rows[pid][0]
    exclude.add(pid)
pattern = re.compile(r'(?:^|[ /])(?:start_gc\.sh|control_gc_host\.sh|start_gc_host\.sh)(?:\s|$)')
conflicts = [p for p, (_, args) in rows.items() if p not in exclude and pattern.search(args)]
if conflicts:
    sys.exit('Other GC controller processes detected (PID %s); stop them manually before using this controller.' % ', '.join(map(str, conflicts)))
PY

run_dir=$HOST_RESULTS_ROOT/$RUN_ID
[[ ! -e "$run_dir" ]] || { echo "Host result directory already exists: $run_dir" >&2; exit 2; }
mkdir -p "$run_dir"
guest_dir=$GUEST_RESULTS_ROOT/$RUN_ID
printf 'epoch_ms,event,threshold,detail\n' > "$run_dir/control.csv"
now_ms() {
    local stamp
    stamp=$("${DATE_BIN:-date}" +%s%3N)
    if [[ "$stamp" =~ ^[0-9]+$ ]]; then
        printf '%s\n' "$stamp"
    else
        "$PYTHON_BIN" -c 'import time; print(int(time.time() * 1000))'
    fi
}
record() { printf '%s,%s,%s,%s\n' "$(now_ms)" "$1" "${2:-}" "${3:-}" >> "$run_dir/control.csv"; }
rpc_cmd=("$PYTHON_BIN" "$SPDK_DIR/scripts/rpc.py" -t "${RPC_TIMEOUT:-30}")
if [[ -n ${RPC_BIN:-} ]]; then rpc_cmd=("$RPC_BIN"); fi
rpc_seq=0
gc_may_be_positive=0
tail_pid=''
ssh_started=0
done_ok=0
failure_reason=controller_aborted
ssh_tmp=$(mktemp -d /tmp/ada-read-ssh.XXXXXX)
ssh_opts=(-p "$SSH_PORT" -o StrictHostKeyChecking=yes -o ControlPath="$ssh_tmp/socket" -o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=3)
if [[ -n ${KNOWN_HOSTS:-} ]]; then ssh_opts+=(-o UserKnownHostsFile="$KNOWN_HOSTS"); fi

remote_read() {
    local cmd path=$guest_dir/$1
    printf -v cmd 'if test -f %q; then cat -- %q; else exit 44; fi' "$path" "$path"
    ssh "${ssh_opts[@]}" -o BatchMode=yes "$SSH_TARGET" "$cmd" 9>&-
}
remote_marker() {
    local cmd path=$guest_dir/$1 value=$2 temporary
    temporary=$path.host-${BASHPID:-$$}.tmp
    printf -v cmd "printf '%%s\\\\n' %q > %q && mv -f -- %q %q" "$value" "$temporary" "$temporary" "$path"
    ssh "${ssh_opts[@]}" -o BatchMode=yes "$SSH_TARGET" "$cmd" 9>&-
}
set_threshold() {
    local threshold=$1 phase=$2 rc=0
    rpc_seq=$((rpc_seq + 1))
    record rpc_begin "$threshold" "$phase"
    if "${rpc_cmd[@]}" bdev_ftl_set_bggc_comp_threshold -b "$FTL_NAME" -t "$threshold" > "$run_dir/rpc-${rpc_seq}-${phase}.log" 2>&1 9>&-; then
        record rpc_success "$threshold" "$phase"
    else
        rc=$?
        record rpc_failure "$threshold" "${phase}_exit_${rc}"
        return "$rc"
    fi
}
cleanup() {
    local rc=$?
    trap - EXIT HUP INT TERM
    set +e
    if (( gc_may_be_positive )); then
        if set_threshold 0 cleanup; then gc_may_be_positive=0; else echo "WARNING: failed to restore threshold 0; inspect RPC cleanup log." >&2; rc=1; fi
    fi
    if (( done_ok && rc == 0 )); then
        record controller_complete 0 finished
        printf '%s\n' "$RUN_ID" > "$run_dir/controller.complete"
    else
        record controller_aborted '' "$failure_reason"
        printf '%s\t%s\n' "$RUN_ID" "$failure_reason" > "$run_dir/controller.aborted"
        if (( ssh_started )); then remote_marker controller.failed "$RUN_ID"$'\t'"$failure_reason" >/dev/null 2>&1 || true; fi
    fi
    if [[ -n "$tail_pid" ]]; then kill "$tail_pid" 2>/dev/null; wait "$tail_pid" 2>/dev/null; fi
    if (( ssh_started )); then ssh "${ssh_opts[@]}" -O exit "$SSH_TARGET" >/dev/null 2>&1 9>&-; fi
    rm -rf -- "$ssh_tmp"
    exit "$rc"
}
trap cleanup EXIT
trap 'failure_reason=signal_HUP; exit 129' HUP
trap 'failure_reason=signal_INT; exit 130' INT
trap 'failure_reason=signal_TERM; exit 143' TERM
die() { failure_reason=$1; echo "$1" >&2; exit 1; }

# One interactive authentication if needed; all later operations reuse this connection.
ssh "${ssh_opts[@]}" -M -N -f -o ControlMaster=yes -o ControlPersist=900 "$SSH_TARGET" 9>&-
ssh_started=1
record controller_connected '' "$FTL_NAME"
start_wait=$(now_ms)
while :; do
    if request=$(remote_read request.tsv); then
        printf '%s\n' "$request" > "$run_dir/request.tsv"
        break
    else
        status=$?
        [[ "$status" == 44 ]] || die request_ssh_failed
    fi
    (( $(now_ms) - start_wait < WAIT_TIMEOUT * 1000 )) || die request_timeout
    sleep "$POLL_INTERVAL"
done

"$PYTHON_BIN" - "$run_dir/request.tsv" "$RUN_ID" > "$run_dir/request.validated.tsv" <<'PY'
import math, re, sys
path, run_id = sys.argv[1:]
manifest = {}
for line in open(path):
    fields = line.rstrip('\n').split('\t')
    if len(fields) != 2 or not re.match(r'^[A-Za-z][A-Za-z0-9_]*$', fields[0]) or fields[0] in manifest or any(ord(c) < 32 for c in fields[1]):
        sys.exit('Malformed or duplicate manifest field')
    manifest[fields[0]] = fields[1]
required = ['schema', 'run_id', 'device', 'write_mib', 'read_iops', 'runtime', 'gc_start', 'gc_threshold', 'read_offset_bytes', 'read_size_bytes', 'write_offset_bytes', 'write_size_bytes']
if any(k not in manifest for k in required):
    sys.exit('Missing required manifest field')
if manifest['schema'] != '1' or manifest['run_id'] != run_id or not manifest['device'].startswith('/'):
    sys.exit('Invalid schema, run ID, or device')
for key in ['read_iops', 'runtime', 'gc_start', 'gc_threshold', 'read_offset_bytes', 'read_size_bytes', 'write_offset_bytes', 'write_size_bytes']:
    if not re.match(r'^[0-9]+$', manifest[key]):
        sys.exit('Invalid integer field: ' + key)
    manifest[key] = str(int(manifest[key]))
try:
    rate = float(manifest['write_mib'])
except ValueError:
    sys.exit('Invalid write_mib')
if not math.isfinite(rate) or rate < 0:
    sys.exit('Invalid write_mib')
if int(manifest['read_iops']) <= 0 or int(manifest['gc_threshold']) <= 0 or int(manifest['gc_threshold']) > 4294967295 or int(manifest['runtime']) <= int(manifest['gc_start']):
    sys.exit('Invalid rate, threshold, or runtime')
if int(manifest['read_size_bytes']) == 0 or (rate > 0 and int(manifest['write_size_bytes']) == 0):
    sys.exit('Empty IO range')
# Leave metadata in request.tsv; only emit validated protocol fields for shell use.
for key in required:
    print('%s\t%s' % (key, manifest[key]))
PY
while IFS=$'\t' read -r key value; do
    case "$key" in
        runtime) runtime=$value ;;
        gc_start) gc_start=$value ;;
        gc_threshold) gc_threshold=$value ;;
    esac
done < "$run_dir/request.validated.tsv"
[[ -r "$SPDK_LOG" ]] || die spdk_log_not_readable
tail -n 0 -F -- "$SPDK_LOG" > "$run_dir/spdk.log" 2> "$run_dir/tail.stderr" 9>&- &
tail_pid=$!
record spdk_capture_started '' from_current_log_position
sleep "${TAIL_START_SETTLE:-1}"
kill -0 "$tail_pid" 2>/dev/null || die spdk_capture_failed
set_threshold 0 baseline || die baseline_rpc_failed
record baseline_settle_start 0 "$BASELINE_SETTLE"
settle_until=$(( $(now_ms) + BASELINE_SETTLE * 1000 ))
while (( $(now_ms) < settle_until )); do
    if remote_read finished.tsv > "$run_dir/finished.early.tsv"; then die guest_finished_before_ready; else
        status=$?; [[ "$status" == 44 ]] || die finished_ssh_failed
    fi
    sleep "$POLL_INTERVAL"
done
remote_marker controller.ready "$RUN_ID" || die ready_ssh_failed
record controller_ready 0 "$RUN_ID"
start_wait=$(now_ms)
while :; do
    if started=$(remote_read started.tsv); then
        printf '%s\n' "$started" > "$run_dir/started.tsv"
        break
    else
        status=$?; [[ "$status" == 44 ]] || die started_ssh_failed
    fi
    if remote_read finished.tsv > "$run_dir/finished.early.tsv"; then die guest_finished_before_start; else
        status=$?; [[ "$status" == 44 ]] || die finished_ssh_failed
    fi
    (( $(now_ms) - start_wait < WAIT_TIMEOUT * 1000 )) || die started_timeout
    sleep "$POLL_INTERVAL"
done
[[ "$started" != *$'\n'* ]] || die invalid_started_marker
IFS=$'\t' read -r marker_run started_ms extra <<< "$started"
[[ "$marker_run" == "$RUN_ID" && "$started_ms" =~ ^[0-9]{13}$ && -z "${extra:-}" ]] || die invalid_started_marker
(( started_ms <= $(now_ms) + ${MAX_CLOCK_SKEW_MS:-5000} )) || die guest_clock_ahead
(( $(now_ms) - started_ms < gc_start * 1000 )) || die gc_start_already_passed
record guest_started 0 "$started_ms"

while (( $(now_ms) - started_ms < runtime * 1000 )); do
    elapsed=$(( $(now_ms) - started_ms ))
    if remote_read finished.tsv > "$run_dir/finished.early.tsv"; then
        (( $(now_ms) - started_ms >= runtime * 1000 )) || die guest_finished_early
    else
        status=$?; [[ "$status" == 44 ]] || die finished_ssh_failed
    fi
    if (( ! gc_may_be_positive && elapsed >= gc_start * 1000 )); then
        # Set before invocation: a failed transport might still have applied the RPC.
        gc_may_be_positive=1
        set_threshold "$gc_threshold" gc_on || die gc_on_rpc_failed
    fi
    kill -0 "$tail_pid" 2>/dev/null || die spdk_capture_failed
    sleep "$POLL_INTERVAL"
done
set_threshold 0 gc_off || die gc_off_rpc_failed
gc_may_be_positive=0
record runtime_elapsed 0 "$runtime"
start_wait=$(now_ms)
while :; do
    if finished=$(remote_read finished.tsv); then
        printf '%s\n' "$finished" > "$run_dir/finished.tsv"
        IFS=$'\t' read -r finished_run finished_rc finished_ms extra <<< "$finished"
        [[ "$finished" != *$'\n'* && "$finished_run" == "$RUN_ID" && "$finished_ms" =~ ^[0-9]{13}$ && "$finished_rc" =~ ^[0-9]+$ && -z "${extra:-}" ]] || die invalid_finished_marker
        [[ "$finished_rc" == 0 ]] || die guest_fio_failed
        break
    else
        status=$?; [[ "$status" == 44 ]] || die finished_ssh_failed
    fi
    (( $(now_ms) - start_wait < WAIT_TIMEOUT * 1000 )) || die finished_timeout
    sleep "$POLL_INTERVAL"
done
done_ok=1
