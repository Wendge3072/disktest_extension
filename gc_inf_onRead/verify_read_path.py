#!/usr/bin/env python3
"""Host-side, read-only preflight for the exact read range of a generated profile.

Uses cumulative SPDK iostat/FTL counters without resetting them or changing GC.
SSH authentication follows the operator's existing configuration; no passwords
are accepted or stored by this script. The guest only needs Bash and fio.
"""
import argparse
import configparser
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

from read_path_evidence import summarize


def json_output(output):
    # This CSAL RPC fork prints an import path before the JSON result.
    match = re.search(r'^[\[{]', output, re.M)
    if not match:
        raise ValueError('RPC did not return JSON')
    return json.loads(output[match.start():])


def read_profile(path):
    profile = configparser.ConfigParser(interpolation=None, strict=True)
    profile.read_string(Path(path).read_text())
    global_job = dict(profile['global'])
    read = dict(global_job, **dict(profile['foreground_read']))
    if read.get('rw') != 'randread' or read.get('bs') != '4k' or read.get('direct') != '1':
        raise ValueError('Expected the generated direct=1, 4k randread profile')
    values = {}
    for key in ['offset', 'size', 'iodepth', 'randseed']:
        if not re.fullmatch(r'[0-9]{1,18}', read.get(key, '')):
            raise ValueError('Profile %s must be an integer in the generated format' % key)
        values[key] = int(read[key])
    device = read['filename']
    if not re.fullmatch(r'/dev/[A-Za-z0-9/_-]+', device):
        raise ValueError('Expected a guest block device path')
    if values['offset'] % 4096 or values['size'] <= 0 or values['size'] % 4096:
        raise ValueError('Invalid read alignment or size')
    if values['iodepth'] <= 0 or values['randseed'] <= 0:
        raise ValueError('Invalid read depth or seed')
    values['device'] = device
    return values


def check_controllers():
    rows = subprocess.check_output(['ps', '-eo', 'pid=,args='], text=True)
    pattern = r'(?:^|[ /])(?:start_gc\.sh|control_gc_host\.sh|start_gc_host\.sh)(?:\s|$)'
    conflicts = [line.split(None, 1)[0] for line in rows.splitlines() if re.search(pattern, line)]
    if conflicts:
        raise RuntimeError('Existing GC controller PID %s; finish the old experiment first' % ','.join(conflicts))


def inventory_roles(inventory, args):
    devices = {entry['name']: entry for entry in inventory}
    for name in [args.ftl_name, args.tlc_name, args.qlc_name]:
        if name not in devices or devices[name]['block_size'] != 4096:
            raise RuntimeError('Missing bdev or unsupported block size: %s' % name)
    role = devices[args.ftl_name]['driver_specific']['ftl']
    if role['base_bdev'] != args.qlc_name or role['cache'] != args.tlc_name:
        raise RuntimeError('FTL base/cache roles do not match the requested QLC/TLC bdevs')
    models = {}
    for name, expected in [(args.tlc_name, 'SSDPE2KX020T8'), (args.qlc_name, 'SSDPF2NV153TZ')]:
        model = devices[name]['driver_specific']['nvme'][0]['ctrlr_data']['model_number'].strip()
        if expected not in model:
            raise RuntimeError('Expected the current P4510/P5316 model, got %s on %s' % (model, name))
        models[name] = model
    capacity = devices[args.ftl_name]['num_blocks'] * 4096
    return devices, models, capacity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True, help='Generated profile.fio, copied to the host')
    parser.add_argument('--spdk-dir', default='/home/nly/csal/git/spdk')
    parser.add_argument('--rpc-socket', default='/var/tmp/spdk.sock')
    parser.add_argument('--ssh-target', default='root@127.0.0.1')
    parser.add_argument('--ssh-port', type=int, default=32001)
    parser.add_argument('--known-hosts', type=Path, help='Existing verified known_hosts file')
    parser.add_argument('--ftl-name', default='FTL0')
    parser.add_argument('--tlc-name', default='nvme0n1')
    parser.add_argument('--qlc-name', default='nvme1n1')
    parser.add_argument('--probe-iops', type=int, default=2000, help='Low load routing probe, not a benchmark')
    parser.add_argument('--probe-bytes', type=int, default=67108864, help='Fixed completed read budget, default 64MiB')
    parser.add_argument('--minimum-qlc-fraction', type=float, default=.95)
    parser.add_argument('--idle-seconds', type=float, default=2)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    if not 1 <= args.ssh_port <= 65535 or args.ssh_target.startswith('-'):
        parser.error('Invalid SSH destination')
    if args.probe_iops <= 0 or args.probe_bytes <= 0 or args.probe_bytes % 4096:
        parser.error('Probe requires positive IOPS and a 4KiB aligned byte budget')
    if not 0 < args.minimum_qlc_fraction <= 1 or args.idle_seconds < 1:
        parser.error('Invalid threshold or idle duration')
    read = read_profile(args.profile)
    check_controllers()  # Stop before issuing any guest read workload.
    if args.known_hosts and not args.known_hosts.is_file():
        raise RuntimeError('Verified known_hosts file does not exist')
    output = args.output_dir or Path(args.spdk_dir) / 'log/read_path_checks' / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    def save(name, value):
        (output / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    save('request.json', dict(vars(args), profile=str(Path(args.profile).resolve()), known_hosts=str(args.known_hosts) if args.known_hosts else None, output_dir=str(output), read=read))
    rpc = [sys.executable, str(Path(args.spdk_dir) / 'scripts/rpc.py'), '-s', args.rpc_socket, '-t', '10']
    def call(command):
        result = subprocess.run(rpc + command, capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError('Read-only RPC failed: %s\n%s' % (' '.join(command), result.stderr + result.stdout))
        return json_output(result.stdout)
    def snapshot(label):
        value = {'epoch_ms': int(time.time()*1000), 'bdev': call(['bdev_get_iostat']),
                 'ftl': call(['bdev_ftl_get_stats', '-b', args.ftl_name])}
        save(label + '.json', value)
        return value
    inventory = call(['bdev_get_bdevs'])
    devices, models, capacity = inventory_roles(inventory, args)
    save('bdevs.before.json', inventory)
    if read['offset'] + read['size'] > capacity or args.probe_bytes > read['size']:
        raise RuntimeError('Read range/budget exceeds the FTL capacity or profile size')
    expected_seconds = args.probe_bytes / 4096 / args.probe_iops
    timeout = max(30, int(expected_seconds * 3 + 20))
    if timeout > 300:
        raise RuntimeError('Probe is too long; increase --probe-iops or reduce --probe-bytes')
    job = '[global]\nioengine=libaio\ndirect=1\nnumjobs=1\nallow_file_create=0\nfilename={device}\n[read_path_probe]\nrw=randread\nbs=4k\niodepth={iodepth}\noffset={offset}\nsize={size}\nrandseed={randseed}\n'.format(**read)
    job += 'io_size=%s\nrate_iops=%s\n' % (args.probe_bytes, args.probe_iops)
    (output / 'probe.fio').write_text(job)
    with tempfile.TemporaryDirectory(prefix='ada-read-check-') as temp:
        ssh_opts = ['-p', str(args.ssh_port), '-oStrictHostKeyChecking=yes', '-oConnectTimeout=10',
                    '-oServerAliveInterval=5', '-oServerAliveCountMax=2', '-oControlPath=' + temp + '/socket']
        if args.known_hosts:
            ssh_opts += ['-oUserKnownHostsFile=' + str(args.known_hosts)]
        def guest(command, data=None, limit=15):
            result = subprocess.run(['ssh', *ssh_opts, '-oBatchMode=yes', args.ssh_target, command],
                                    input=data, capture_output=True, text=True, timeout=limit)
            if result.returncode:
                raise RuntimeError('Guest command failed (%s): %s' % (result.returncode, result.stderr))
            return result.stdout
        collector = None
        collector_file = None
        probe_tag = uuid.uuid4().hex
        probe_pid_file = '/tmp/ada-read-path-' + probe_tag + '.pid'
        probe_launched = False
        # A single authenticated SSH connection; later calls require no prompts.
        try:
            subprocess.run(['ssh', *ssh_opts, '-MNf', args.ssh_target], check=True, timeout=30)
            guard = 'set -e; if pgrep -x fio >/dev/null; then echo "Existing fio is still running" >&2; exit 30; fi; command -v fio >/dev/null; command -v timeout >/dev/null; test -b ' + shlex.quote(read['device'])
            guest(guard)
            actual_bytes = int(guest('blockdev --getsize64 ' + shlex.quote(read['device'])).strip())
            if actual_bytes != capacity:
                raise RuntimeError('Guest device capacity does not match this host FTL bdev')
            (output / 'fio.version').write_text(guest('fio --version'))
            idle_before = snapshot('idle.before')
            time.sleep(args.idle_seconds)
            idle_after = snapshot('idle.after')
            # Idle activity is inspected before issuing the probe. Never change GC to obtain an idle window.
            for name in [args.ftl_name]:
                start = next(b for b in idle_before['bdev']['bdevs'] if b['name'] == name)
                end = next(b for b in idle_after['bdev']['bdevs'] if b['name'] == name)
                if end['bytes_read'] != start['bytes_read'] or end['bytes_written'] != start['bytes_written']:
                    raise RuntimeError('FTL foreground activity in idle window; wait for other workloads to finish')
            for category in ['user', 'cmp', 'gc']:
                for direction in ['read', 'write']:
                    if idle_after['ftl'][category][direction]['blocks'] != idle_before['ftl'][category][direction]['blocks']:
                        raise RuntimeError('USER/CMP/GC activity in idle window; wait for it to drain')
            check_controllers()
            guest(guard)
            collector_file = (output / 'spdk.iostat.txt').open('w')
            collector = subprocess.Popen([sys.executable, str(Path(args.spdk_dir) / 'scripts/iostat.py'),
                         '-s', args.rpc_socket, '-d', '-m', '-i', '1', '-t', str(timeout)],
                         stdout=collector_file, stderr=subprocess.STDOUT)
            before = snapshot('probe.before')
            command = '''set -euo pipefail
if pgrep -x fio >/dev/null; then echo 'Existing fio appeared before probe' >&2; exit 30; fi
probe_dir=$(mktemp -d /tmp/ada-read-probe-@@PROBE_TAG@@.XXXXXX)
probe_pid_file=@@PROBE_PID_FILE@@
probe_pid=''
iostat_pid=''
cleanup() {
    trap - EXIT HUP INT TERM
    if [[ -n $probe_pid ]] && kill -0 "$probe_pid" 2>/dev/null; then
        kill -TERM -- "-$probe_pid" 2>/dev/null || kill -TERM "$probe_pid" 2>/dev/null || true
        for attempt in {1..10}; do kill -0 "$probe_pid" 2>/dev/null || break; sleep .1; done
        kill -KILL -- "-$probe_pid" 2>/dev/null || true
        wait "$probe_pid" 2>/dev/null || true
    fi
    if [[ -n $iostat_pid ]]; then kill "$iostat_pid" 2>/dev/null || true; wait "$iostat_pid" 2>/dev/null || true; fi
    rm -f -- "$probe_pid_file"
    rm -rf -- "$probe_dir"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
cat > "$probe_dir/probe.fio"
if command -v iostat >/dev/null; then iostat -dx -y 1 > "$probe_dir/iostat.txt" & iostat_pid=$!; else echo 'Guest sysstat/iostat unavailable' > "$probe_dir/iostat.txt"; fi
timeout --signal=TERM --kill-after=5s @@FIO_TIMEOUT@@s fio --readonly --output-format=json --output="$probe_dir/fio.json" "$probe_dir/probe.fio" &
probe_pid=$!
printf '%s\n' "$probe_pid" > "$probe_pid_file"
wait "$probe_pid" || { cat "$probe_dir/fio.json" >&2; exit 31; }
probe_pid=''
rm -f -- "$probe_pid_file"
if [[ -n $iostat_pid ]]; then kill "$iostat_pid" 2>/dev/null || true; wait "$iostat_pid" 2>/dev/null || true; iostat_pid=''; fi
cat "$probe_dir/iostat.txt" >&2
cat "$probe_dir/fio.json"
'''.replace('@@FIO_TIMEOUT@@', str(timeout - 10)).replace('@@PROBE_TAG@@', probe_tag).replace('@@PROBE_PID_FILE@@', shlex.quote(probe_pid_file))
            probe_launched = True
            result = subprocess.run(['ssh', *ssh_opts, '-oBatchMode=yes', args.ssh_target, 'bash -c ' + shlex.quote(command)], input=job, capture_output=True, text=True, timeout=timeout)
            (output / 'guest.iostat-and-stderr.txt').write_text(result.stderr)
            (output / 'fio.raw.json').write_text(result.stdout)
            if result.returncode:
                raise RuntimeError('Read-only probe failed (exit %s), see saved guest output' % result.returncode)
            fio = json_output(result.stdout)
            save('fio.json', fio)
            time.sleep(args.idle_seconds)
            after = snapshot('probe.after')
            time.sleep(args.idle_seconds)
            stable = snapshot('probe.after.stable')
            for name in [args.ftl_name, args.tlc_name, args.qlc_name]:
                first = next(b for b in after['bdev']['bdevs'] if b['name'] == name)
                second = next(b for b in stable['bdev']['bdevs'] if b['name'] == name)
                if first['bytes_read'] != second['bytes_read']:
                    raise RuntimeError('Physical/logical read counters have not become stable; evidence is inconclusive')
            for category in ['user', 'cmp', 'gc', 'md_base', 'md_nv_cache', 'l2p']:
                if after['ftl'][category]['read']['blocks'] != stable['ftl'][category]['read']['blocks']:
                    raise RuntimeError('FTL read counters have not become stable; evidence is inconclusive')
                if category in ['user', 'cmp', 'gc'] and after['ftl'][category]['write']['blocks'] != stable['ftl'][category]['write']['blocks']:
                    raise RuntimeError('USER/CMP/GC writes appeared after the probe; evidence is inconclusive')
            first_ftl = next(b for b in after['bdev']['bdevs'] if b['name'] == args.ftl_name)
            stable_ftl = next(b for b in stable['bdev']['bdevs'] if b['name'] == args.ftl_name)
            if first_ftl['bytes_written'] != stable_ftl['bytes_written']:
                raise RuntimeError('FTL foreground writes appeared after the probe; evidence is inconclusive')
            final_inventory = call(['bdev_get_bdevs'])
            save('bdevs.after.json', final_inventory)
            final_devices, _, _ = inventory_roles(final_inventory, args)
            for name in [args.ftl_name, args.tlc_name, args.qlc_name]:
                if devices[name].get('uuid') != final_devices[name].get('uuid'):
                    raise RuntimeError('Bdev identity changed while probing: %s' % name)
            report = summarize(before, after, fio, idle_before, idle_after,
                               ftl_name=args.ftl_name, tlc_name=args.tlc_name, qlc_name=args.qlc_name,
                               minimum_qlc_fraction=args.minimum_qlc_fraction, expected_io_bytes=args.probe_bytes)
            report['read_profile'] = read
            report['models'] = models
            save('verification.json', report)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            print('Evidence directory:', output)
            return 0 if report['passed'] else 2
        finally:
            if probe_launched:
                # A unique PID file and command-line tag limit cleanup to this
                # probe's timeout process group, never another fio session.
                cleanup = '''set -e
path=@@PID_FILE@@
if test -f "$path"; then
    read -r pid < "$path"
    if [[ $pid =~ ^[0-9]+$ ]] && test -r "/proc/$pid/cmdline" && grep -a -F -q -- @@TAG@@ "/proc/$pid/cmdline"; then
        kill -TERM -- "-$pid" 2>/dev/null || true
        sleep 1
        if test -r "/proc/$pid/cmdline" && grep -a -F -q -- @@TAG@@ "/proc/$pid/cmdline"; then kill -KILL -- "-$pid" 2>/dev/null || true; fi
    fi
    rm -f -- "$path"
fi
'''.replace('@@PID_FILE@@', shlex.quote(probe_pid_file)).replace('@@TAG@@', shlex.quote('ada-read-probe-' + probe_tag))
                try:
                    guest('bash -c ' + shlex.quote(cleanup), limit=10)
                except (RuntimeError, subprocess.SubprocessError):
                    print('Probe cleanup SSH unavailable; guest timeout still limits its own read process.', file=sys.stderr)
            if collector is not None:
                if collector.poll() is None:
                    collector.terminate()
                try:
                    collector.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    collector.kill()
                    collector.wait()
            if collector_file:
                collector_file.close()
            try:
                subprocess.run(['ssh', *ssh_opts, '-O', 'exit', args.ssh_target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            except subprocess.SubprocessError:
                pass


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print('Read path NOT verified:', error, file=sys.stderr)
        sys.exit(2)
