#!/usr/bin/env python3
"""Sequential read-only QLC routing samples across the entire FTL address space.

This is a distribution sample, not a throughput benchmark or proof that every
LBA is mapped. It never sets GC thresholds, resets counters, submits foreground
writes, or terminates an existing workload. Normal SPDK metadata persistence
caused by reads is separately recorded and must be fully accounted for. Only
this invocation's own probe can be stopped. Passwords are not accepted/stored.
"""
import argparse
import csv
import json
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from read_path_evidence import _snapshot, _subtract, summarize
from verify_read_path import check_controllers, inventory_roles, json_output


BLOCK = 4096
GIB = 1024 ** 3
EXPECTED_CAPACITY = 14541900267520
TARGET_ONLY_CHECKS = {'physical_user_read_coverage', 'sample_qlc_lower_bound_sufficient'}
TARGET_ONLY_REASONS = {
    'user_read_coverage_below_minimum_possible_unmapped_lbas',
    'sample_qlc_lower_bound_below_minimum',
}


def shell_join(arguments):
    # shlex.join is only available in Python 3.8 and newer.
    return ' '.join(shlex.quote(str(value)) for value in arguments)


def partition_ranges(capacity, segments):
    """Partition all blocks; lengths differ by at most one 4KiB block."""
    if type(capacity) is not int or capacity <= 0 or capacity % BLOCK:
        raise ValueError('Capacity must be positive and 4KiB aligned')
    blocks = capacity // BLOCK
    if type(segments) is not int or not 1 <= segments <= blocks:
        raise ValueError('Invalid segment count')
    result = []
    for index in range(segments):
        first, last = index * blocks // segments, (index + 1) * blocks // segments
        result.append({'name': 'segment-%03d' % (index + 1), 'kind': 'full_device',
                       'index': index + 1, 'offset_bytes': first * BLOCK,
                       'size_bytes': (last - first) * BLOCK, 'seed': 310001 + index})
    validate_partition(result, capacity)
    return result


def validate_partition(ranges, capacity):
    cursor = 0
    for item in ranges:
        if item['offset_bytes'] != cursor or item['size_bytes'] <= 0:
            raise ValueError('Partition has a gap, overlap, or empty range')
        if item['offset_bytes'] % BLOCK or item['size_bytes'] % BLOCK:
            raise ValueError('Partition is not 4KiB aligned')
        cursor += item['size_bytes']
    if cursor != capacity:
        raise ValueError('Partition does not cover the full FTL capacity')


def make_job(device, sample, probe_bytes, probe_iops):
    return ('[global]\nioengine=libaio\ndirect=1\nnumjobs=1\n'
            'allow_file_create=0\nfilename=%s\n'
            '[read_path_probe]\nrw=randread\nbs=4k\niodepth=32\n'
            'offset=%s\nsize=%s\nrandseed=%s\n'
            'io_size=%s\nrate_iops=%s\n') % (
                device, sample['offset_bytes'], sample['size_bytes'], sample['seed'],
                probe_bytes, probe_iops)


def snapshot_delta(before, after, args, label):
    names = (args.ftl_name, args.tlc_name, args.qlc_name)
    return _subtract(_snapshot(before, names, args.ftl_name, label + '.before'),
                     _snapshot(after, names, args.ftl_name, label + '.after'), label)


def assert_no_writes(delta, label, args):
    """Allow only fully classified normal SPDK metadata persistence."""
    metadata_classes = {'md_base', 'md_nv_cache', 'l2p'}
    for name, counters in delta['bdevs'].items():
        if any(counters.get(key, 0) for key in ('bytes_unmapped', 'num_unmap_ops')):
            raise RuntimeError('%s: unmap activity on %s' % (label, name))
        if name == args.ftl_name and (counters['bytes_written'] or counters['num_write_ops']):
            raise RuntimeError('%s: foreground logical write activity' % label)
        if bool(counters['bytes_written']) != bool(counters['num_write_ops']):
            raise RuntimeError('%s: physical write bytes/operations do not agree on %s' % (label, name))
    for name, directions in delta['groups'].items():
        if name not in metadata_classes and (directions['write']['blocks'] or directions['write']['ios']):
            raise RuntimeError('%s: FTL %s foreground/maintenance write activity' % (label, name))
        if any(value for counters in directions.values()
               for key, value in counters.items() if key.startswith('errors.')):
            raise RuntimeError('%s: FTL error counter increased' % label)
    metadata_bytes = sum(delta['groups'][name]['write']['blocks'] * BLOCK for name in metadata_classes)
    physical_bytes = sum(delta['bdevs'][name]['bytes_written'] for name in (args.tlc_name, args.qlc_name))
    if physical_bytes != metadata_bytes:
        raise RuntimeError('%s: physical writes do not close against classified metadata writes' % label)
    return metadata_bytes


def assert_idle(before, after, args):
    delta = snapshot_delta(before, after, args, 'idle')
    assert_no_writes(delta, 'idle', args)
    logical = delta['bdevs'][args.ftl_name]
    if logical['bytes_read'] or logical['num_read_ops']:
        raise RuntimeError('idle: another foreground reader is active')
    for name in ('user', 'cmp', 'gc'):
        if any(delta['groups'][name][direction][key]
               for direction in ('read', 'write') for key in ('blocks', 'ios')):
            raise RuntimeError('idle: USER/CMP/GC activity; wait for it to drain')
    physical = sum(delta['bdevs'][name]['bytes_read'] for name in (args.tlc_name, args.qlc_name))
    classified = sum(group['read']['blocks'] * BLOCK for group in delta['groups'].values())
    if physical != classified:
        raise RuntimeError('idle: physical reads are not fully accounted for')


def assert_stable(before, after, args):
    delta = snapshot_delta(before, after, args, 'after_stable')
    if any(value for counters in delta['bdevs'].values() for value in counters.values()):
        raise RuntimeError('Probe counters did not become stable, or another workload appeared')
    if any(value for group in delta['groups'].values()
           for counters in group.values() for value in counters.values()):
        raise RuntimeError('FTL counters did not become stable, or another workload appeared')


def classify_report(report):
    """Low mapped/QLC coverage is a finding; all other failures invalidate it."""
    checks = report.get('checks', {})
    failed = {name for name, passed in checks.items() if not passed}
    fatal = [reason for reason in report.get('reasons', []) if reason not in TARGET_ONLY_REASONS]
    metrics = report.get('metrics', {})
    required = ('fio_read_bytes', 'user_physical_read_bytes', 'qlc_total_read_bytes',
                'tlc_total_read_bytes', 'non_user_read_bytes_all_devices')
    valid = bool(checks) and not (failed - TARGET_ONLY_CHECKS) and not fatal
    valid = valid and all(type(metrics.get(key)) is int for key in required)
    report['evidence_valid'] = bool(valid)
    if not valid:
        report['classification'] = 'invalid_evidence'
        report['mapped_residency'] = 'invalid_evidence'
        return report
    total = metrics['fio_read_bytes']
    user = metrics['user_physical_read_bytes']
    qlc, tlc = metrics['qlc_total_read_bytes'], metrics['tlc_total_read_bytes']
    non_user = metrics['non_user_read_bytes_all_devices']
    if total <= 0 or not 0 <= user <= total:
        report['evidence_valid'] = False
        report['classification'] = 'invalid_evidence'
        report['mapped_residency'] = 'invalid_evidence'
        return report
    metrics.update({
        'qlc_foreground_lower_bound_bytes': max(0, qlc - non_user),
        'qlc_foreground_upper_bound_bytes': min(qlc, user),
        'tlc_foreground_lower_bound_bytes': max(0, tlc - non_user),
        'tlc_foreground_upper_bound_bytes': min(tlc, user),
        'unmapped_return_zero_bytes': total - user,
        'unmapped_return_zero_fraction': (total - user) / total,
    })
    for device in ('qlc', 'tlc'):
        for bound in ('lower', 'upper'):
            key = device + '_foreground_' + bound + '_bound'
            metrics[key + '_fraction'] = metrics[key + '_bytes'] / total
            metrics[device + '_mapped_data_' + bound + '_bound_fraction'] = (
                metrics[key + '_bytes'] / user if user else None)
    if not user:
        report['mapped_residency'] = 'no_mapped_data'
    elif metrics['qlc_mapped_data_lower_bound_fraction'] >= metrics['minimum_qlc_fraction']:
        report['mapped_residency'] = 'qlc_dominant_mapped_data'
    elif metrics['tlc_mapped_data_lower_bound_fraction'] >= metrics['minimum_qlc_fraction']:
        report['mapped_residency'] = 'tlc_dominant_mapped_data'
    elif (metrics['qlc_mapped_data_lower_bound_fraction'] > 0
          and metrics['tlc_mapped_data_lower_bound_fraction'] > 0):
        report['mapped_residency'] = 'mixed_mapped_data'
    else:
        report['mapped_residency'] = 'uncertain_mapped_data'
    if report['passed']:
        report['classification'] = 'qlc_target_met'
    elif metrics['user_physical_to_logical_fraction'] < .99:
        report['classification'] = 'unmapped_or_partly_unmapped'
    elif metrics['qlc_foreground_upper_bound_fraction'] >= metrics['minimum_qlc_fraction']:
        report['classification'] = 'metadata_limits_qlc_lower_bound'
    else:
        report['classification'] = 'tlc_or_mixed_residency'
    return report


def needs_warm_retry(report):
    if not report.get('evidence_valid'):
        return False
    m = report['metrics']
    return (m['user_physical_read_bytes'] > 0
            and (m['qlc_foreground_upper_bound_bytes'] - m['qlc_foreground_lower_bound_bytes'])
            / m['fio_read_bytes'] > .01)


def check_warm_consistency(first, warm):
    """A same-seed retry must have the same mapped/zero-return byte total."""
    if not first.get('evidence_valid') or not warm.get('evidence_valid'):
        return
    old = first['metrics']['user_physical_read_bytes']
    new = warm['metrics']['user_physical_read_bytes']
    warm['checks']['warm_user_read_bytes_matches_first'] = old == new
    warm['warm_comparison'] = {'first_user_physical_read_bytes': old,
                               'warm_user_physical_read_bytes': new, 'equal': old == new}
    if old != new:
        warm['reasons'].append('warm_user_read_bytes_changed_mapping_or_zero_return_sample')
        warm['passed'] = False
        warm['evidence_valid'] = False
        warm['classification'] = 'invalid_evidence'
        warm['mapped_residency'] = 'invalid_evidence'


def aggregate(records):
    selected = [item['attempts'][item['selected_attempt']]['evidence']
                for item in records if item.get('completed')]
    total = sum(item['metrics']['fio_read_bytes'] for item in selected)
    mapped = sum(item['metrics']['user_physical_read_bytes'] for item in selected)
    result = {'completed_samples': len(selected), 'logical_read_bytes': total,
              'mapped_data_read_bytes': mapped,
              'mapped_data_fraction_of_logical_reads': mapped / total if total else None,
              'passed_samples': sum(bool(item['passed']) for item in selected),
              'weighting': 'Selected-attempt fio logical read bytes; not address-range size.',
              'scope': 'Conservative bounds for random samples, not exact whole-device residency.'}
    for key in ('qlc_foreground_lower_bound', 'qlc_foreground_upper_bound',
                'tlc_foreground_lower_bound', 'tlc_foreground_upper_bound', 'unmapped_return_zero'):
        count = sum(item['metrics'][key + '_bytes'] for item in selected)
        result[key + '_bytes'] = count
        result[key + '_fraction'] = count / total if total else None
        if key != 'unmapped_return_zero':
            device, _, bound, _ = key.split('_')
            result[device + '_mapped_data_' + bound + '_bound_fraction'] = count / mapped if mapped else None
    return result


class Sampler:
    def __init__(self, args, output):
        self.args, self.output = args, output
        self.rpc = [sys.executable, str(Path(args.spdk_dir) / 'scripts/rpc.py'),
                    '-s', args.rpc_socket, '-t', '10']
        self.temp = tempfile.TemporaryDirectory(prefix='ada-full-read-')
        self.owned_master = args.ssh_control_path is None
        control = str(args.ssh_control_path) if args.ssh_control_path else self.temp.name + '/socket'
        self.ssh_opts = ['-p', str(args.ssh_port), '-oStrictHostKeyChecking=yes',
                         '-oConnectTimeout=10', '-oServerAliveInterval=5',
                         '-oServerAliveCountMax=2', '-oControlPath=' + control]
        if args.known_hosts:
            self.ssh_opts += ['-oUserKnownHostsFile=' + str(args.known_hosts)]
        self.active_tag = None
        self.active_pid_file = None
        self.inventory = None
        self.command_log = output / 'commands.sh'
        rerun = [sys.executable, str(Path(__file__).resolve()), '--spdk-dir', args.spdk_dir,
                 '--rpc-socket', args.rpc_socket, '--ssh-target', args.ssh_target,
                 '--ssh-port', str(args.ssh_port), '--device', args.device,
                 '--ftl-name', args.ftl_name, '--tlc-name', args.tlc_name, '--qlc-name', args.qlc_name,
                 '--expected-capacity-bytes', str(args.expected_capacity_bytes),
                 '--segments', str(args.segments), '--probe-bytes', str(args.probe_bytes),
                 '--probe-iops', str(args.probe_iops), '--idle-seconds', str(args.idle_seconds),
                 '--settle-seconds', str(args.settle_seconds),
                 '--minimum-qlc-fraction', str(args.minimum_qlc_fraction)]
        if args.known_hosts:
            rerun += ['--known-hosts', str(args.known_hosts)]
        if args.ssh_control_path:
            rerun += ['--ssh-control-path', str(args.ssh_control_path)]
        self.command_log.write_text('#!/usr/bin/env bash\nset -euo pipefail\n'
                                    '# Reproduce into a NEW directory; use the same verified SSH identity/master.\n'
                                    + shell_join(rerun) + ' --output-dir "${1:?Supply a NEW output directory}"\n'
                                    '# Below: exact commands from the recorded invocation, as comments.\n')

    def log_command(self, command, stdin_file=None):
        text = shell_join(command)
        if stdin_file is not None:
            text += ' < ' + shlex.quote(str(stdin_file))
        # Remote scripts contain newlines; every log line stays a comment.
        with self.command_log.open('a') as handle:
            for line in text.splitlines():
                handle.write('# ' + line + '\n')

    def save(self, path, value):
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')

    def call(self, command):
        argv = self.rpc + command
        self.log_command(argv)
        result = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError('Read-only RPC failed: %s: %s' % (' '.join(command), result.stderr + result.stdout))
        return json_output(result.stdout)

    def snapshot(self, directory, name):
        value = {'epoch_ms': int(time.time() * 1000), 'bdev': self.call(['bdev_get_iostat']),
                 'ftl': self.call(['bdev_ftl_get_stats', '-b', self.args.ftl_name])}
        self.save(directory / (name + '.json'), value)
        return value

    def guest(self, command, data=None, limit=15, stdin_file=None):
        argv = ['ssh', *self.ssh_opts, '-oBatchMode=yes', self.args.ssh_target, command]
        self.log_command(argv, stdin_file)
        return subprocess.run(argv, input=data, capture_output=True, text=True, timeout=limit)

    def guest_checked(self, command, limit=15):
        result = self.guest(command, limit=limit)
        if result.returncode:
            raise RuntimeError('Guest command failed (%s): %s' % (result.returncode, result.stderr))
        return result.stdout

    def connect(self):
        if self.owned_master:
            command = ['ssh', *self.ssh_opts, '-MNf', self.args.ssh_target]
            self.log_command(command)
            subprocess.run(command, check=True, timeout=60)
        else:
            command = ['ssh', *self.ssh_opts, '-oBatchMode=yes', '-O', 'check', self.args.ssh_target]
            self.log_command(command)
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise RuntimeError('The supplied SSH master is not available: ' + result.stderr)

    def guard(self):
        check_controllers()
        command = ('set -e; if pgrep -x fio >/dev/null; then '
                   'echo "Existing fio; sampling refused" >&2; exit 30; fi; '
                   'command -v fio >/dev/null; command -v timeout >/dev/null; '
                   'command -v setsid >/dev/null; test -b ' + shlex.quote(self.args.device))
        self.guest_checked(command)

    def verify_inventory(self, directory, label):
        inventory = self.call(['bdev_get_bdevs'])
        self.save(directory / ('bdevs.' + label + '.json'), inventory)
        devices, models, capacity = inventory_roles(inventory, self.args)
        if capacity != self.args.expected_capacity_bytes:
            raise RuntimeError('FTL capacity does not match --expected-capacity-bytes')
        identity = {name: {'uuid': devices[name].get('uuid'), 'block_size': devices[name]['block_size'],
                           'num_blocks': devices[name]['num_blocks'],
                           'driver_specific': devices[name].get('driver_specific')}
                    for name in (self.args.ftl_name, self.args.tlc_name, self.args.qlc_name)}
        if self.inventory is not None and identity != self.inventory:
            raise RuntimeError('Bdev identity, geometry, or FTL mapping changed')
        self.inventory = identity
        return models, capacity

    def probe(self, directory, sample, timeout):
        tag = uuid.uuid4().hex
        pid_file = '/tmp/ada-full-sample-' + tag + '.pid'
        self.active_tag, self.active_pid_file = tag, pid_file
        command = r'''set -euo pipefail
if pgrep -x fio >/dev/null; then echo 'Existing fio; sampling refused' >&2; exit 30; fi
probe_dir=$(mktemp -d /tmp/ada-full-sample-@@TAG@@.XXXXXX)
pid_file=@@PID_FILE@@
probe_pid=''
cleanup() {
    trap - EXIT HUP INT TERM
    if [[ -n $probe_pid ]] && kill -0 "$probe_pid" 2>/dev/null; then
        if test -r "/proc/$probe_pid/cmdline" && grep -a -F -q -- @@TAG@@ "/proc/$probe_pid/cmdline"; then
            kill -TERM -- "-$probe_pid" 2>/dev/null || true
            for attempt in {1..10}; do kill -0 "$probe_pid" 2>/dev/null || break; sleep .1; done
            if test -r "/proc/$probe_pid/cmdline" && grep -a -F -q -- @@TAG@@ "/proc/$probe_pid/cmdline"; then
                kill -KILL -- "-$probe_pid" 2>/dev/null || true
            fi
        fi
        wait "$probe_pid" 2>/dev/null || true
    fi
    rm -f -- "$pid_file"
    rm -rf -- "$probe_dir"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
cat > "$probe_dir/probe.fio"
setsid timeout --signal=TERM --kill-after=5s @@TIMEOUT@@s fio --readonly --output-format=json --output="$probe_dir/fio.json" "$probe_dir/probe.fio" &
probe_pid=$!
printf '%s\n' "$probe_pid" > "$pid_file"
status=0
wait "$probe_pid" || status=$?
probe_pid=''
rm -f -- "$pid_file"
if test -f "$probe_dir/fio.json"; then cat "$probe_dir/fio.json"; fi
exit "$status"
'''.replace('@@TAG@@', tag).replace('@@PID_FILE@@', shlex.quote(pid_file)).replace('@@TIMEOUT@@', str(timeout - 10))
        job_path = directory / 'probe.fio'
        job = self.make_job(sample)
        job_path.write_text(job)
        (directory / 'guest-command.sh').write_text(command)
        try:
            result = self.guest('bash -c ' + shlex.quote(command), data=job,
                                limit=timeout, stdin_file=job_path)
            (directory / 'fio.raw.json').write_text(result.stdout)
            (directory / 'guest.stderr.txt').write_text(result.stderr)
            if result.returncode:
                raise RuntimeError('Read-only fio/SSH failed (exit %s); see %s' % (result.returncode, directory))
            fio = json_output(result.stdout)
            self.save(directory / 'fio.json', fio)
            self.active_tag, self.active_pid_file = None, None
            return fio
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout or ''
            stderr = error.stderr or ''
            (directory / 'fio.raw.json').write_text(stdout.decode() if isinstance(stdout, bytes) else stdout)
            (directory / 'guest.stderr.txt').write_text(stderr.decode() if isinstance(stderr, bytes) else stderr)
            raise

    def make_job(self, sample):
        return make_job(self.args.device, sample, self.args.probe_bytes, self.args.probe_iops)

    def attempt(self, directory, sample, timeout):
        directory.mkdir()
        self.guard()
        self.verify_inventory(directory, 'before')
        idle_before = self.snapshot(directory, 'idle.before')
        time.sleep(self.args.idle_seconds)
        idle_after = self.snapshot(directory, 'idle.after')
        assert_idle(idle_before, idle_after, self.args)
        self.guard()
        before = self.snapshot(directory, 'probe.before')
        fio = self.probe(directory, sample, timeout)
        time.sleep(self.args.settle_seconds)
        after = self.snapshot(directory, 'probe.after')
        time.sleep(self.args.settle_seconds)
        stable = self.snapshot(directory, 'probe.after.stable')
        assert_stable(after, stable, self.args)
        self.verify_inventory(directory, 'after')
        report = summarize(before, after, fio, idle_before, idle_after,
                           ftl_name=self.args.ftl_name, tlc_name=self.args.tlc_name,
                           qlc_name=self.args.qlc_name, expected_io_bytes=self.args.probe_bytes,
                           minimum_qlc_fraction=self.args.minimum_qlc_fraction)
        # Cold read page-in can evict a previously dirty L2P page. This normal
        # persistence is allowed only with exact physical/classified closure.
        try:
            metadata_bytes = assert_no_writes(snapshot_delta(before, after, self.args, 'probe'),
                                             'probe', self.args)
            idle_metadata_bytes = assert_no_writes(snapshot_delta(idle_before, idle_after, self.args, 'idle'),
                                                  'idle', self.args)
        except (ValueError, RuntimeError) as error:
            report['checks']['only_closed_metadata_writes_no_unmap'] = False
            report['reasons'].append(str(error))
            report['passed'] = False
        else:
            report['checks']['only_closed_metadata_writes_no_unmap'] = True
            report['metrics']['metadata_written_bytes'] = metadata_bytes
            report['metrics']['idle_metadata_written_bytes'] = idle_metadata_bytes
        classify_report(report)
        report['sample'] = sample
        self.save(directory / 'evidence.json', report)
        return report

    def close(self):
        if self.active_tag is not None:
            command = r'''set -e
path=@@PID_FILE@@
if test -f "$path"; then
    read -r pid < "$path"
    if [[ $pid =~ ^[0-9]+$ ]] && test -r "/proc/$pid/cmdline" && grep -a -F -q -- @@TAG@@ "/proc/$pid/cmdline"; then
        kill -TERM -- "-$pid" 2>/dev/null || true
        sleep 1
        if test -r "/proc/$pid/cmdline" && grep -a -F -q -- @@TAG@@ "/proc/$pid/cmdline"; then
            kill -KILL -- "-$pid" 2>/dev/null || true
        fi
    fi
    rm -f -- "$path"
fi
'''.replace('@@PID_FILE@@', shlex.quote(self.active_pid_file)).replace('@@TAG@@', shlex.quote(self.active_tag))
            try:
                self.guest('bash -c ' + shlex.quote(command), limit=10)
            except (OSError, subprocess.SubprocessError):
                print('Own-probe cleanup SSH unavailable; its guest timeout remains active.', file=sys.stderr)
        if self.owned_master:
            try:
                subprocess.run(['ssh', *self.ssh_opts, '-O', 'exit', self.args.ssh_target],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        self.temp.cleanup()


def save_summary(output, records, capacity, args, complete, error=None):
    full = [item for item in records if item['kind'] == 'full_device']
    candidate = [item for item in records if item['kind'] == 'candidate']
    partition = partition_ranges(capacity, args.segments) if capacity is not None else []
    actual_bytes = sum(attempt.get('evidence', {}).get('metrics', {}).get('fio_read_bytes', 0)
                       for item in records for attempt in item['attempts'])
    summary = {'schema': 1, 'complete': complete,
               'passed': complete and all(item['attempts'][item['selected_attempt']]['evidence']['passed']
                                          for item in records),
               'error': error, 'capacity_bytes': capacity, 'segments_requested': args.segments,
               'partition_complete_no_overlap': bool(partition), 'partition': partition,
               'full_device_samples': aggregate(full), 'candidate_samples': aggregate(candidate),
               'all_selected_samples': aggregate(records), 'classified_fio_read_bytes_all_attempts': actual_bytes,
               'records': records,
               'limitations': [
                   'Uniform random samples inside a complete address-space partition; not an exhaustive scan.',
                   'QLC/TLC intervals are conservative accounting bounds, not exact residency percentages.',
                   'An unmapped fraction describes zero-return samples, not the unsampled part of the disk.',
                   'At most one same-seed metadata warm retry; selected retry is conditional on the first cold read.',
                   'QLC/TLC bounds are given separately per all requested read bytes and per mapped USER read bytes.',
                   'The same-seed retry must preserve the first mapped USER byte total; otherwise evidence is rejected.',
                   'The fixed candidate sample is reported separately from the full-device aggregate.',
                   'No counter resets, threshold changes, foreground/fio writes, or termination of existing workloads.',
                   'Normal SPDK metadata persistence may occur on reads; its physical/classified write bytes must close and are recorded.',
               ]}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    fields = ['name', 'kind', 'offset_bytes', 'size_bytes', 'seed', 'completed', 'selected_attempt',
              'attempt_count', 'classification', 'mapped_residency', 'passed', 'evidence_valid', 'fio_read_bytes',
              'metadata_written_bytes',
              'qlc_foreground_lower_bound_fraction', 'qlc_foreground_upper_bound_fraction',
              'tlc_foreground_lower_bound_fraction', 'tlc_foreground_upper_bound_fraction',
              'qlc_mapped_data_lower_bound_fraction', 'qlc_mapped_data_upper_bound_fraction',
              'tlc_mapped_data_lower_bound_fraction', 'tlc_mapped_data_upper_bound_fraction',
              'unmapped_return_zero_fraction']
    with (output / 'summary.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in records:
            row = {key: item.get(key) for key in fields}
            row['attempt_count'] = len(item['attempts'])
            if item.get('selected_attempt') is not None:
                report = item['attempts'][item['selected_attempt']]['evidence']
                for key in ('classification', 'mapped_residency', 'passed', 'evidence_valid'):
                    row[key] = report.get(key)
                for key in fields:
                    if key in report.get('metrics', {}):
                        row[key] = report['metrics'][key]
            writer.writerow(row)
    return summary


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--spdk-dir', default='/home/nly/csal/git/spdk')
    result.add_argument('--rpc-socket', default='/var/tmp/spdk.sock')
    result.add_argument('--ssh-target', default='root@127.0.0.1')
    result.add_argument('--ssh-port', type=int, default=32001)
    result.add_argument('--known-hosts', type=Path)
    result.add_argument('--ssh-control-path', type=Path, help='Existing SSH master; this script never closes it')
    result.add_argument('--output-dir', type=Path, help='New directory; never overwritten')
    result.add_argument('--device', default='/dev/vda')
    result.add_argument('--ftl-name', default='FTL0')
    result.add_argument('--tlc-name', default='nvme0n1')
    result.add_argument('--qlc-name', default='nvme1n1')
    result.add_argument('--expected-capacity-bytes', type=int, default=EXPECTED_CAPACITY)
    result.add_argument('--segments', type=int, default=32)
    result.add_argument('--probe-bytes', type=int, default=64 * 1024 * 1024)
    result.add_argument('--probe-iops', type=int, default=2000)
    result.add_argument('--minimum-qlc-fraction', type=float, default=.95)
    result.add_argument('--idle-seconds', type=float, default=2)
    result.add_argument('--settle-seconds', type=float, default=.5)
    return result


def main(argv=None):
    arg_parser = parser()
    args = arg_parser.parse_args(argv)
    if not 1 <= args.ssh_port <= 65535 or not re.fullmatch(r'[A-Za-z0-9_.:@\[\]-]+', args.ssh_target):
        arg_parser.error('Invalid SSH destination')
    if not re.fullmatch(r'/dev/[A-Za-z0-9/_-]+', args.device):
        arg_parser.error('Expected a guest block device path')
    if not 1 <= args.segments <= 4096 or args.probe_bytes <= 0 or args.probe_bytes % BLOCK or args.probe_iops <= 0:
        arg_parser.error('Positive segment/IOPS counts and a 4KiB aligned probe budget are required')
    if not 0 < args.minimum_qlc_fraction <= 1 or not 1 <= args.idle_seconds <= 60 or not .2 <= args.settle_seconds <= 10:
        arg_parser.error('Invalid evidence threshold or observation durations')
    # Validate geometry and budget before any SSH/RPC/IO.
    ranges = partition_ranges(args.expected_capacity_bytes, args.segments)
    candidate = {'name': 'candidate', 'kind': 'candidate', 'index': 0,
                 'offset_bytes': 2048 * GIB, 'size_bytes': 1024 * GIB, 'seed': 2252}
    if candidate['offset_bytes'] + candidate['size_bytes'] > args.expected_capacity_bytes:
        arg_parser.error('The fixed candidate read range exceeds the expected capacity')
    if args.probe_bytes > min(item['size_bytes'] for item in ranges + [candidate]):
        arg_parser.error('Probe budget exceeds a sample range')
    expected_seconds = args.probe_bytes / BLOCK / args.probe_iops
    timeout = max(30, int(expected_seconds * 3 + 20))
    if timeout > 300:
        arg_parser.error('Each probe would be too long; increase IOPS or reduce bytes')
    check_controllers()
    if not (Path(args.spdk_dir) / 'scripts/rpc.py').is_file():
        raise RuntimeError('SPDK scripts/rpc.py is missing')
    if args.known_hosts and not args.known_hosts.is_file():
        raise RuntimeError('Verified known_hosts file does not exist')
    if args.ssh_control_path and not args.ssh_control_path.exists():
        raise RuntimeError('Supplied SSH control socket does not exist')
    output = args.output_dir or Path(args.spdk_dir) / 'log/qlc_full_device_samples' / (
        time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:8])
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    runner = Sampler(args, output)
    records, capacity, complete, error = [], None, False, None
    previous_term = signal.getsignal(signal.SIGTERM)
    def terminate(signum, frame):
        raise RuntimeError('Sampling interrupted by SIGTERM')
    signal.signal(signal.SIGTERM, terminate)
    try:
        runner.save(output / 'request.json', {key: str(value) if isinstance(value, Path) else value
                                             for key, value in vars(args).items()})
        runner.connect()
        runner.guard()
        models, capacity = runner.verify_inventory(output, 'initial')
        runner.save(output / 'identity.json', {'capacity_bytes': capacity, 'models': models,
                                              'bdev_identity': runner.inventory})
        actual = int(runner.guest_checked('blockdev --getsize64 ' + shlex.quote(args.device)).strip())
        if actual != capacity:
            raise RuntimeError('Guest device capacity does not match this FTL bdev')
        (output / 'fio.version').write_text(runner.guest_checked('fio --version'))
        for index, sample in enumerate(ranges + [candidate], 1):
            print('[%s/%s] %s offset=%s size=%s seed=%s' % (
                index, len(ranges) + 1, sample['name'], sample['offset_bytes'], sample['size_bytes'], sample['seed']), flush=True)
            directory = output / sample['name']
            directory.mkdir()
            record = dict(sample, completed=False, selected_attempt=None, attempts=[])
            records.append(record)
            for attempt_index, label in enumerate(('first_cold_read', 'warm_retry')):
                report = runner.attempt(directory / label, sample, timeout)
                if attempt_index:
                    check_warm_consistency(record['attempts'][0]['evidence'], report)
                    runner.save(directory / label / 'evidence.json', report)
                record['attempts'].append({'label': label, 'path': str(directory / label), 'evidence': report})
                record['selected_attempt'] = attempt_index
                save_summary(output, records, capacity, args, False)
                if not report['evidence_valid']:
                    raise RuntimeError('%s/%s has contaminated or invalid evidence: %s' % (
                        sample['name'], label, ', '.join(report['reasons'])))
                m = report['metrics']
                print('  %s: %s; QLC lower/upper %.2f%%/%.2f%%; zero-return %.2f%%' % (
                    label, report['classification'], 100 * m['qlc_foreground_lower_bound_fraction'],
                    100 * m['qlc_foreground_upper_bound_fraction'],
                    100 * m['unmapped_return_zero_fraction']), flush=True)
                if m['user_physical_read_bytes']:
                    print('  Mapped data: %s; QLC lower/upper %.2f%%/%.2f%% of USER bytes' % (
                        report['mapped_residency'], 100 * m['qlc_mapped_data_lower_bound_fraction'],
                        100 * m['qlc_mapped_data_upper_bound_fraction']), flush=True)
                if attempt_index or not needs_warm_retry(report):
                    break
                print('  One same-seed read retry to reduce cold metadata uncertainty.', flush=True)
            record['completed'] = True
            record['precondition'] = ('Selected warm_retry follows the retained first_cold_read.'
                                      if len(record['attempts']) > 1 else 'Selected first_cold_read; no retry.')
            save_summary(output, records, capacity, args, False)
        complete = True
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        error = str(exc) or type(exc).__name__
        print('Sampling aborted: ' + error, file=sys.stderr, flush=True)
    finally:
        runner.close()
        signal.signal(signal.SIGTERM, previous_term)
    summary = save_summary(output, records, capacity, args, complete, error)
    print('Evidence directory: ' + str(output), flush=True)
    print('Completed=%s; passed=%s; completed full-device samples=%s/%s' % (
        complete, summary['passed'], summary['full_device_samples']['completed_samples'], args.segments), flush=True)
    return 0 if summary['passed'] else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print('Sampling NOT started: ' + str(exc), file=sys.stderr)
        sys.exit(2)
