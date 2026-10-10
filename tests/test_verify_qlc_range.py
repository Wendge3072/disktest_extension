"""Complete-range verifier simulations; no SSH, RPC, or device IO is issued."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_read_path_evidence import snapshot, bdev, SIZE, BLOCK
from test_verify_read_path import inventory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gc_inf_onRead'))
SPEC = importlib.util.spec_from_file_location('verify_qlc_range', ROOT / 'gc_inf_onRead/verify_qlc_range.py')
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)
REAL_RUN = subprocess.run


class CompleteRangeTests(unittest.TestCase):
    def simulate(self, temp, route='qlc', pin_error=False, changed_pin=False, short_read=False):
        root = Path(temp)
        spdk = root / 'spdk'
        (spdk / 'scripts').mkdir(parents=True)
        (spdk / 'scripts/rpc.py').touch()
        socket = root / 'ssh-master'
        socket.touch()
        output = root / 'output'
        profile = root / 'formal.fio'
        profile.write_text('[global]\ndirect=1\nfilename=/dev/vda\n'
                           '[foreground_read]\nrw=randread\nbs=4k\niodepth=16\n'
                           'offset=2147483648\nsize=%s\nrandseed=2252\n' % SIZE)
        state = {'counters': snapshot(0), 'tick': 0, 'jobs': [], 'commands': []}

        def fake_run(command, **kwargs):
            state['commands'].append(command)
            text = ''
            if command[0] == sys.executable:
                self.assertTrue(command[1].endswith('/scripts/rpc.py'))
                if 'bdev_get_bdevs' in command:
                    value = inventory()
                elif 'bdev_get_iostat' in command:
                    state['tick'] += 1000000000
                    state['counters']['bdev']['ticks'] = state['tick']
                    value = state['counters']['bdev']
                elif 'bdev_ftl_get_stats' in command:
                    value = state['counters']['ftl']
                else:
                    self.fail('Unexpected/mutating RPC: %r' % command)
                text = '/spdk/python/spdk/rpc/__init__.py\n' + json.dumps(value)
            elif command[0] == 'ssh':
                if kwargs.get('input') is not None:
                    job = kwargs['input']
                    self.assertIn('--readonly', command[-1])
                    syntax = REAL_RUN(['bash', '-n'], input=shlex.split(command[-1])[-1],
                                      capture_output=True, text=True)
                    self.assertEqual(syntax.returncode, 0, syntax.stderr)
                    self.assertIn('rw=read\n', job)
                    self.assertNotIn('randread', job)
                    self.assertNotIn('rate=', job)
                    self.assertNotIn('rate_iops', job)
                    for setting in ('bs=1M', 'direct=1', 'time_based=0', 'loops=1',
                                    'numjobs=1', 'iodepth=16', 'io_size=%s' % SIZE):
                        self.assertIn(setting, job)
                    state['jobs'].append(job)
                    s = state['counters']
                    total = SIZE - VERIFY.MIB if short_read else SIZE
                    logical = total - BLOCK if route == 'logical_mismatch' else total
                    user = total - BLOCK if route == 'one_hole' else total
                    qlc = user - BLOCK if route == 'one_tlc' else user
                    metadata = total if route in ('l2p_metadata', 'other_metadata') else 0
                    bdev(s, 'FTL0')['bytes_read'] += logical
                    bdev(s, 'FTL0')['num_read_ops'] += logical // BLOCK
                    bdev(s, 'nvme1n1')['bytes_read'] += qlc
                    bdev(s, 'nvme1n1')['num_read_ops'] += qlc // BLOCK
                    bdev(s, 'nvme0n1')['bytes_read'] += user - qlc + metadata
                    bdev(s, 'nvme0n1')['num_read_ops'] += (user - qlc + metadata) // BLOCK
                    s['ftl']['user']['read']['blocks'] += user // BLOCK
                    s['ftl']['user']['read']['ios'] += user // BLOCK
                    if metadata:
                        group = 'l2p' if route == 'l2p_metadata' else 'md_nv_cache'
                        s['ftl'][group]['read']['blocks'] += metadata // BLOCK
                        s['ftl'][group]['read']['ios'] += metadata // BLOCK
                        s['ftl']['l2p']['write']['blocks'] += BLOCK // BLOCK
                        s['ftl']['l2p']['write']['ios'] += 1
                        bdev(s, 'nvme0n1')['bytes_written'] += BLOCK
                        bdev(s, 'nvme0n1')['num_write_ops'] += 1
                    if route == 'gc':
                        s['ftl']['gc']['read']['blocks'] += 1
                        s['ftl']['gc']['read']['ios'] += 1
                        bdev(s, 'nvme1n1')['bytes_read'] += BLOCK
                        bdev(s, 'nvme1n1')['num_read_ops'] += 1
                    if route == 'user_write':
                        s['ftl']['user']['write']['blocks'] += 1
                        s['ftl']['user']['write']['ios'] += 1
                        bdev(s, 'FTL0')['bytes_written'] += BLOCK
                        bdev(s, 'FTL0')['num_write_ops'] += 1
                        bdev(s, 'nvme0n1')['bytes_written'] += BLOCK
                        bdev(s, 'nvme0n1')['num_write_ops'] += 1
                    text = json.dumps({'jobs': [{'error': 0, 'read': {'io_bytes': total},
                                                  'write': {'io_bytes': 0}}]})
                elif command[-1].startswith('blockdev --getsize64'):
                    text = str(VERIFY.sampler.EXPECTED_CAPACITY) + '\n'
                elif command[-1] == 'fio --version':
                    text = 'fio-3.7\n'
            else:
                self.fail('Unexpected command: %r' % command)
            return subprocess.CompletedProcess(command, 0, text, '')

        pin = {'saved_sha256': VERIFY.AUDITED_VHOST_SHA256, 'pid': 123}
        if pin_error:
            pin_effect = RuntimeError('binary differs; re-audit required')
        else:
            pin_effect = [pin, dict(pin, pid=124) if changed_pin else pin]
        argv = ['--profile', str(profile), '--spdk-dir', str(spdk),
                '--read-floor-bytes', str(2 * VERIFY.sampler.GIB),
                '--prefix-size-bytes', str(VERIFY.sampler.GIB),
                '--ssh-control-path', str(socket), '--output-dir', str(output)]
        with patch.object(VERIFY, 'live_vhost_identity', side_effect=pin_effect), \
             patch.object(VERIFY.sampler, 'check_controllers'), \
             patch.object(VERIFY.sampler.subprocess, 'run', side_effect=fake_run), \
             patch.object(VERIFY.time, 'sleep'), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = VERIFY.main(argv)
        result = json.loads((output / 'verification.json').read_text())
        syntax = REAL_RUN(['bash', '-n', str(output / 'commands.sh')], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertIn('verify_qlc_range.py', (output / 'commands.sh').read_text())
        self.assertFalse(any('-O' in command and 'exit' in command for command in state['commands']))
        self.assertEqual(json.loads((output / 'request.json').read_text())['argv'], argv)
        return status, result, state

    def test_complete_qlc_scan_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            status, result, state = self.simulate(temp)
            self.assertEqual(status, 0)
            self.assertTrue(result['passed'])
            self.assertTrue(result['complete'])
            self.assertEqual(result['scope'], VERIFY.SCOPE)
            self.assertEqual(result['schema'], 2)
            self.assertEqual(result['read_floor_bytes'], 2 * VERIFY.sampler.GIB)
            self.assertEqual(result['prefix_size_bytes'], VERIFY.sampler.GIB)
            self.assertEqual(result['read_profile']['size'], SIZE)
            self.assertEqual(result['read_profile']['device'], '/dev/vda')
            self.assertEqual(len(state['jobs']), 1)
            self.assertTrue(all(result['exact_checks'].values()))

    def test_tlc_l2p_reads_can_pass_exact_proof_when_conservative_bound_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            status, result, _ = self.simulate(temp, route='l2p_metadata')
            self.assertEqual(status, 0)
            self.assertTrue(result['passed'])
            self.assertFalse(result['conservative_evidence']['passed'])
            self.assertTrue(result['conservative_evidence']['evidence_valid'])
            self.assertEqual(result['scan_metrics']['l2p_read_bytes'], SIZE)
            self.assertEqual(result['scan_metrics']['metadata_written_bytes'], BLOCK)

    def test_one_hole_one_tlc_or_other_metadata_cannot_pass(self):
        for route, check in [('one_hole', 'every_requested_byte_has_mapped_user_data'),
                             ('one_tlc', 'all_user_read_bytes_on_qlc'),
                             ('other_metadata', 'only_l2p_non_user_reads'),
                             ('logical_mismatch', 'complete_selected_range_read')]:
            with self.subTest(route=route), tempfile.TemporaryDirectory() as temp:
                status, result, _ = self.simulate(temp, route=route)
                self.assertEqual(status, 2)
                self.assertFalse(result['passed'])
                self.assertFalse(result['exact_checks'][check])

    def test_short_scan_gc_or_user_writes_fail(self):
        for options in [{'short_read': True}, {'route': 'gc'}, {'route': 'user_write'}]:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temp:
                status, result, _ = self.simulate(temp, **options)
                self.assertEqual(status, 2)
                self.assertFalse(result['passed'])

    def test_binary_mismatch_prevents_io_and_changed_live_process_rejects_scan(self):
        for option in ('pin_error', 'changed_pin'):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as temp:
                status, result, state = self.simulate(temp, **{option: True})
                self.assertEqual(status, 2)
                self.assertFalse(result['passed'])
                self.assertFalse(result['complete'])
                self.assertIn('binary' if option == 'pin_error' else 'changed', result['error'])
                self.assertEqual(len(state['jobs']), 0 if option == 'pin_error' else 1)

    def test_profile_alignment_rejected_before_io(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / 'read.fio'
            profile.write_text('[global]\ndirect=1\nfilename=/dev/vda\n'
                               '[foreground_read]\nrw=randread\nbs=4k\niodepth=1\n'
                               'offset=4096\nsize=1048576\nrandseed=1\n')
            with patch.object(VERIFY.sampler.subprocess, 'run') as run, \
                 redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                VERIFY.main(['--profile', str(profile)])
            run.assert_not_called()


class PinAndIdleTests(unittest.TestCase):
    def test_disk_and_live_hash_must_match_audited_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / 'build/bin/vhost'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'audited-vhost')
            binary = binary.resolve()
            expected = hashlib.sha256(binary.read_bytes()).hexdigest()
            for disk_hash, live_hash, target, succeeds in [
                    (expected, expected, str(binary), True),
                    ('0' * 64, expected, str(binary), False),
                    (expected, '0' * 64, str(binary), False),
                    (expected, expected, str(binary) + ' (deleted)', False)]:
                with self.subTest(disk_hash=disk_hash, live_hash=live_hash, target=target), \
                     patch.object(Path, 'resolve', return_value=binary), \
                     patch.object(Path, 'iterdir', return_value=[Path('/proc/123')]), \
                     patch.object(VERIFY.os, 'readlink', return_value=target), \
                     patch.object(VERIFY, 'sha256_file', side_effect=[disk_hash, live_hash]):
                    if succeeds:
                        value = VERIFY.live_vhost_identity(temp, expected)
                        self.assertFalse(value['uses_default_audited_build'])
                        self.assertEqual(value['running_vhost']['pid'], 123)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 're-audit'):
                            VERIFY.live_vhost_identity(temp, expected)

    def idle_runner(self, temp, states, timeout=600):
        runner = VERIFY.RangeVerifier.__new__(VERIFY.RangeVerifier)
        runner.output = Path(temp)
        runner.args = SimpleNamespace(wait_timeout=timeout, ftl_name='FTL0',
                                      tlc_name='nvme0n1', qlc_name='nvme1n1')
        events = iter(states)
        runner.snapshot = lambda directory, label: next(events)
        runner.guard = lambda: None
        runner.verify_inventory = lambda directory, label: None
        return runner

    def test_busy_gc_waits_and_then_idle_succeeds(self):
        states = [snapshot(0), snapshot(1000000000), snapshot(2000000000)]
        for state in states[1:]:
            state['ftl']['gc']['read']['blocks'] = 1
            state['ftl']['gc']['read']['ios'] = 1
            bdev(state, 'nvme1n1')['bytes_read'] = BLOCK
            bdev(state, 'nvme1n1')['num_read_ops'] = 1
        with tempfile.TemporaryDirectory() as temp, patch.object(VERIFY.time, 'sleep') as sleep:
            self.idle_runner(temp, states).wait_until_idle()
            self.assertEqual(sleep.call_count, 2)

    def test_counter_regression_error_or_identity_change_is_not_retried(self):
        for mode in ('regression', 'errors', 'identity'):
            states = [snapshot(1000000000), snapshot(2000000000)]
            if mode == 'regression':
                bdev(states[0], 'nvme0n1')['bytes_read'] = BLOCK
            elif mode == 'errors':
                states[1]['ftl']['gc']['read']['errors']['media'] = 1
                states[1]['ftl']['gc']['read']['blocks'] = 1
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp, \
                 patch.object(VERIFY.time, 'sleep') as sleep:
                runner = self.idle_runner(temp, states)
                if mode == 'identity':
                    runner.verify_inventory = lambda *a: (_ for _ in ()).throw(RuntimeError('Bdev identity changed'))
                with self.assertRaises((ValueError, RuntimeError)):
                    runner.wait_until_idle()
                self.assertLessEqual(sleep.call_count, 1)

    def test_busy_timeout_never_launches_scan(self):
        states = [snapshot(0), snapshot(1000000000)]
        states[1]['ftl']['gc']['read']['blocks'] = 1
        with tempfile.TemporaryDirectory() as temp, patch.object(VERIFY.time, 'sleep'), \
             patch.object(VERIFY.time, 'monotonic', side_effect=[0, 3]):
            with self.assertRaisesRegex(RuntimeError, 'did not drain'):
                self.idle_runner(temp, states, timeout=2).wait_until_idle()


if __name__ == '__main__':
    unittest.main()
