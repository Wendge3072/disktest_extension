#!/usr/bin/env python3
"""Read every block in the selected formal read range once and verify QLC routing.

This host-side check uses only read RPCs and a guest fio --readonly scan. Exact
assignment of L2P reads to TLC depends on a separately audited, pinned vhost
build. A matching hash verifies build identity; it does not audit routing code.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

import sample_qlc_full_device as sampler
from verify_read_path import read_profile


MIB = 1024 ** 2
AUDITED_VHOST_SHA256 = '5f5ab19671476a73d1d8fb2ea3a31b55d1bcb0971eec7d671b1f8566d5e3c2d4'
SCOPE = 'complete selected range at verification time, not all disk'
AUDIT_BASIS = {
    'claim': 'In the audited build, FTL_STATS_TYPE_L2P reads use the TLC nv_cache bdev.',
    'source_evidence': 'gc_inf_onRead/L2P_ROUTING_AUDIT.md',
    'method': 'Independent source/build audit plus disk and live /proc/PID/exe hash pin.',
    'limitation': 'A SHA256 match establishes binary identity; routing is an externally audited assumption.',
}


def audit_basis(expected_sha256):
    result = dict(AUDIT_BASIS, expected_vhost_sha256=expected_sha256)
    if expected_sha256 != AUDITED_VHOST_SHA256:
        result['source_evidence'] = None
        result['audit_selection'] = 'Operator asserts an independent audit for the supplied build; this tool does not validate that audit.'
    return result


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def live_vhost_identity(spdk_dir, expected_sha256):
    """Pin both the saved binary and the live executable; require one vhost."""
    binary = (Path(spdk_dir) / 'build/bin/vhost').resolve(strict=True)
    saved_hash = sha256_file(binary)
    matches = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        executable = entry / 'exe'
        try:
            target = os.readlink(executable)
        except OSError:
            continue
        # A replaced or deleted binary cannot masquerade as the saved build.
        if target == str(binary) or target == str(binary) + ' (deleted)':
            matches.append({'pid': int(entry.name), 'executable_path': target,
                            'sha256': sha256_file(executable)})
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one live vhost for this SPDK directory; found %s. Run as the vhost owner/root.' % len(matches))
    live = matches[0]
    if saved_hash != expected_sha256 or live['sha256'] != expected_sha256 or live['executable_path'] != str(binary):
        raise RuntimeError('Vhost binary differs from the independently audited SHA256; re-audit its L2P routing before choosing a new --expected-vhost-sha256.')
    return {'saved_binary': str(binary), 'saved_sha256': saved_hash,
            'running_vhost': live, 'expected_sha256': expected_sha256,
            'uses_default_audited_build': expected_sha256 == AUDITED_VHOST_SHA256,
            'audit_selection': ('bundled_existing_audit' if expected_sha256 == AUDITED_VHOST_SHA256
                               else 'operator_supplied_independently_audited_build')}


def full_scan_job(device, read):
    return ('[global]\nioengine=libaio\ndirect=1\nnumjobs=1\n'
            'allow_file_create=0\nfilename=%s\ntime_based=0\nloops=1\n'
            '[complete_qlc_range_scan]\nrw=read\nbs=1M\niodepth=%s\n'
            'offset=%s\nsize=%s\nio_size=%s\n') % (
                device, read['iodepth'], read['offset'], read['size'], read['size'])


def exact_range_result(report, read, binary_pinned, expected_sha256=AUDITED_VHOST_SHA256):
    """Keep conservative evidence, then add strict source-backed routing checks."""
    metrics = report.get('metrics', {})
    delta = metrics.get('probe_delta', {})
    groups = delta.get('groups', {})
    l2p = groups.get('l2p', {}).get('read', {}).get('blocks')
    l2p_bytes = l2p * sampler.BLOCK if type(l2p) is int else None
    other_reads = {
        name: counters['read'] for name, counters in groups.items()
        if name not in ('user', 'l2p')
        and (counters['read']['blocks'] or counters['read']['ios'])
    }
    total = metrics.get('fio_read_bytes')
    user = metrics.get('user_physical_read_bytes')
    qlc = metrics.get('qlc_total_read_bytes')
    tlc = metrics.get('tlc_total_read_bytes')
    logical = metrics.get('ftl_logical_read_bytes')
    exact = {
        'independently_audited_binary_pinned': bool(binary_pinned),
        'conservative_evidence_valid_except_target_coverage': bool(report.get('evidence_valid')),
        'complete_selected_range_read': total == read['size'] and logical == read['size'],
        'every_requested_byte_has_mapped_user_data': user == total and type(user) is int and total > 0,
        'only_l2p_non_user_reads': bool(groups) and not other_reads and type(l2p_bytes) is int,
        'all_user_read_bytes_on_qlc': qlc == total and type(qlc) is int and total > 0,
        'tlc_reads_exactly_equal_l2p_metadata': tlc == l2p_bytes and type(tlc) is int,
    }
    return {'passed': all(exact.values()), 'exact_checks': exact,
            'scan_metrics': dict(metrics, l2p_read_bytes=l2p_bytes,
                                 other_non_user_reads=other_reads),
            'conservative_evidence': report,
            'exact_routing_basis': audit_basis(expected_sha256)}


class RangeVerifier(sampler.Sampler):
    def __init__(self, args, output, read, invocation):
        self.read = read
        super().__init__(args, output)
        command = [sys.executable, str(Path(__file__).resolve()), *invocation]
        # Replace only --output-dir for a repeat invocation into a new directory.
        rerun = []
        iterator = iter(command)
        for value in iterator:
            if value == '--output-dir':
                next(iterator, None)
            elif value.startswith('--output-dir='):
                continue
            else:
                rerun.append(value)
        self.command_log.write_text('#!/usr/bin/env bash\nset -euo pipefail\n'
                                    '# Reproduce into a NEW directory with the same verified SSH identity.\n'
                                    + sampler.shell_join(rerun)
                                    + ' --output-dir "${1:?Supply a NEW output directory}"\n'
                                    '# Exact RPC/guest commands from this invocation follow as comments.\n')

    def make_job(self, sample):
        return full_scan_job(self.args.device, self.read)

    def wait_until_idle(self):
        directory = self.output / 'wait_for_idle'
        directory.mkdir()
        deadline = time.monotonic() + self.args.wait_timeout
        index = 0
        before = self.snapshot(directory, '0000.before')
        while True:
            self.guard()
            self.verify_inventory(directory, '%04d.before' % index)
            time.sleep(2)
            after = self.snapshot(directory, '%04d.after' % index)
            self.verify_inventory(directory, '%04d.after' % index)
            delta = sampler.snapshot_delta(before, after, self.args, 'wait_for_idle')
            errors = sum(value for group in delta['groups'].values()
                         for counters in group.values() for key, value in counters.items()
                         if key.startswith('errors.'))
            if errors:
                raise RuntimeError('FTL errors increased while waiting for idle')
            if any(counters.get(key, 0) for counters in delta['bdevs'].values()
                   for key in ('bytes_unmapped', 'num_unmap_ops')):
                raise RuntimeError('Unmap activity appeared while waiting for idle')
            logical = delta['bdevs'][self.args.ftl_name]
            busy = any(logical[key] for key in ('bytes_read', 'num_read_ops', 'bytes_written', 'num_write_ops'))
            busy = busy or any(delta['groups'][name][direction][key]
                               for name in ('user', 'cmp', 'gc')
                               for direction in ('read', 'write') for key in ('blocks', 'ios'))
            if not busy:
                # Unknown IO, unmaps and non-closing metadata accounting fail;
                # only explicitly identified background activity is retryable.
                sampler.assert_idle(before, after, self.args)
                return
            if time.monotonic() >= deadline:
                raise RuntimeError('USER/CMP/GC activity did not drain within --wait-timeout; scan not started')
            index += 1
            before = after


def parser():
    result = sampler.parser()
    result.description = __doc__
    result.add_argument('--profile', required=True, type=Path, help='Generated formal read profile, copied to host')
    result.add_argument('--expected-vhost-sha256', default=AUDITED_VHOST_SHA256,
                        help='An independently audited vhost build; changing this hash requires a new routing audit')
    result.add_argument('--scan-timeout', type=int, default=7200, help='Guest fio timeout in seconds')
    result.add_argument('--wait-timeout', type=int, default=600, help='Wait for background IO to drain, seconds')
    result.add_argument('--read-floor-bytes', type=int, default=2048 * sampler.GIB,
                        help='Formal read addresses must be at or above this boundary')
    result.add_argument('--prefix-size-bytes', type=int, default=1700 * sampler.GIB,
                        help='Reserved low-address preparation/write region, recorded for the formal-test gate')
    result.add_argument('--prepare-end-bytes', type=int,
                        help='Use partial-sequential layout: read end must precede this minus --cache-guard-bytes')
    result.add_argument('--cache-guard-bytes', type=int, default=1700 * sampler.GIB)
    return result


def main(argv=None):
    invocation = list(sys.argv[1:] if argv is None else argv)
    arg_parser = parser()
    args = arg_parser.parse_args(invocation)
    if not re.fullmatch(r'[0-9a-fA-F]{64}', args.expected_vhost_sha256):
        arg_parser.error('--expected-vhost-sha256 must be a SHA256 digest')
    args.expected_vhost_sha256 = args.expected_vhost_sha256.lower()
    if args.scan_timeout <= 10 or args.wait_timeout < 2:
        arg_parser.error('scan-timeout must exceed 10 seconds and wait-timeout must be at least 2 seconds')
    if not 1 <= args.ssh_port <= 65535 or not re.fullmatch(r'[A-Za-z0-9_.:@\[\]-]+', args.ssh_target):
        arg_parser.error('Invalid SSH destination')
    if not 1 <= args.idle_seconds <= 60 or not .2 <= args.settle_seconds <= 10:
        arg_parser.error('Invalid observation durations')
    if not 0 < args.minimum_qlc_fraction <= 1:
        arg_parser.error('Invalid conservative evidence threshold')
    read = read_profile(args.profile)
    if args.prepare_end_bytes is not None:
        if (args.prepare_end_bytes % sampler.BLOCK or args.cache_guard_bytes % sampler.BLOCK
                or not 0 < args.cache_guard_bytes < args.prepare_end_bytes < args.expected_capacity_bytes
                or read['offset']+read['size'] > args.prepare_end_bytes-args.cache_guard_bytes):
            arg_parser.error('Read range must fit before partial prepare end minus the positive TLC guard')
    elif (args.read_floor_bytes <= 0 or args.read_floor_bytes % MIB
            or args.prefix_size_bytes <= 0 or args.prefix_size_bytes % MIB
            or args.prefix_size_bytes >= args.read_floor_bytes
            or read['offset'] < args.read_floor_bytes):
        arg_parser.error('Positive 1MiB-aligned prefix/read-floor boundaries are required; prefix < read floor <= read offset')
    if read['offset'] % MIB or read['size'] % MIB:
        arg_parser.error('The formal read offset and size must both be 1MiB aligned')
    if (type(args.expected_capacity_bytes) is not int or args.expected_capacity_bytes <= 0
            or args.expected_capacity_bytes % sampler.BLOCK
            or read['offset'] + read['size'] > args.expected_capacity_bytes):
        arg_parser.error('The read range must lie inside the expected FTL capacity')
    args.device = read['device']
    args.probe_bytes = read['size']
    if not (Path(args.spdk_dir) / 'scripts/rpc.py').is_file():
        raise RuntimeError('SPDK scripts/rpc.py is missing')
    if args.known_hosts and not args.known_hosts.is_file():
        raise RuntimeError('Verified known_hosts file does not exist')
    if args.ssh_control_path and not args.ssh_control_path.exists():
        raise RuntimeError('Supplied SSH control socket does not exist')
    output = (args.output_dir or Path(args.spdk_dir) / 'log/qlc_range_verification' / (
        time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:8])).resolve()
    output.mkdir(parents=True, exist_ok=False)
    runner = RangeVerifier(args, output, read, invocation)
    result = {'schema': 2, 'complete': False, 'passed': False, 'error': None,
              'scope': SCOPE, 'read_profile': read, 'ftl_name': args.ftl_name,
              'read_floor_bytes': args.read_floor_bytes,
              'prefix_size_bytes': args.prefix_size_bytes,
              'range_protocol': 'partial_sequential' if args.prepare_end_bytes is not None else 'full_cover_prefix',
              'prepare_end_bytes': args.prepare_end_bytes,
              'cache_guard_bytes': args.cache_guard_bytes if args.prepare_end_bytes is not None else None,
              'source_profile_sha256': sha256_file(args.profile),
              'capacity_bytes': None, 'bdev_identity': None,
              'binary_sha256': None, 'scan_metrics': None, 'exact_checks': {},
              'exact_routing_basis': audit_basis(args.expected_vhost_sha256),
              'limitations': ['The verification concerns this complete selected range at this time.',
                              'Later writes or unmaps in the read range invalidate its residency guarantee.',
                              'A passing scan does not establish the GC preparation state or future latency.']}
    previous_term = signal.getsignal(signal.SIGTERM)
    def terminate(signum, frame):
        raise RuntimeError('Range verification interrupted by SIGTERM')
    signal.signal(signal.SIGTERM, terminate)
    try:
        runner.save(output / 'request.json', {
            'argv': invocation,
            'resolved_arguments': {key: str(value) if isinstance(value, Path) else value
                                   for key, value in vars(args).items()},
            'read_profile': read})
        pin = live_vhost_identity(args.spdk_dir, args.expected_vhost_sha256)
        result['binary_sha256'] = pin['saved_sha256']
        result['binary_identity_before'] = pin
        runner.save(output / 'binary.before.json', pin)
        runner.connect()
        runner.guard()
        models, capacity = runner.verify_inventory(output, 'initial')
        result.update(capacity_bytes=capacity, bdev_identity=runner.inventory, models=models)
        guest_capacity = int(runner.guest_checked('blockdev --getsize64 ' + sampler.shell_join([args.device])).strip())
        if guest_capacity != capacity:
            raise RuntimeError('Guest device capacity differs from the host FTL capacity')
        runner.save(output / 'identity.json', {'capacity_bytes': capacity, 'guest_capacity_bytes': guest_capacity,
                                              'models': models, 'bdev_identity': runner.inventory})
        (output / 'fio.version').write_text(runner.guest_checked('fio --version'))
        runner.wait_until_idle()
        sample = {'name': 'complete-selected-range', 'kind': 'complete_range', 'index': 0,
                  'offset_bytes': read['offset'], 'size_bytes': read['size'], 'seed': read['randseed']}
        report = runner.attempt(output / 'complete_scan', sample, args.scan_timeout + 10)
        after_pin = live_vhost_identity(args.spdk_dir, args.expected_vhost_sha256)
        runner.save(output / 'binary.after.json', after_pin)
        if after_pin != pin:
            raise RuntimeError('The live vhost process/build changed during range verification')
        result['binary_identity_after'] = after_pin
        result.update(exact_range_result(report, read, binary_pinned=True,
                                         expected_sha256=args.expected_vhost_sha256))
        result['complete'] = True
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        result['error'] = str(error) or type(error).__name__
        result['passed'] = False
    finally:
        try:
            runner.close()
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            runner.save(output / 'verification.json', result)
    print(json.dumps({'passed': result['passed'], 'complete': result['complete'],
                      'error': result['error'], 'verification': str(output / 'verification.json')}))
    return 0 if result['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
