"""Full-device sampler simulations: no SSH, SPDK RPC, or fio IO is issued."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_read_path_evidence import snapshot, bdev, BLOCK, SIZE
from test_verify_read_path import inventory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gc_inf_onRead'))
SPEC = importlib.util.spec_from_file_location('sample_qlc_full_device', ROOT / 'gc_inf_onRead/sample_qlc_full_device.py')
SAMPLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SAMPLE)
REAL_RUN = subprocess.run


class FullDeviceSamplerTests(unittest.TestCase):
    def test_partition_complete_aligned_and_balanced(self):
        for count in (1, 3, 32, 127):
            ranges = SAMPLE.partition_ranges(SAMPLE.EXPECTED_CAPACITY, count)
            self.assertEqual(len(ranges), count)
            self.assertEqual(sum(row['size_bytes'] for row in ranges), SAMPLE.EXPECTED_CAPACITY)
            self.assertEqual(ranges[0]['offset_bytes'], 0)
            self.assertEqual(ranges[-1]['offset_bytes'] + ranges[-1]['size_bytes'], SAMPLE.EXPECTED_CAPACITY)
            self.assertLessEqual(max(row['size_bytes'] for row in ranges) - min(row['size_bytes'] for row in ranges), BLOCK)
            self.assertEqual(len({row['seed'] for row in ranges}), count)
            self.assertTrue(all(row['offset_bytes'] % BLOCK == row['size_bytes'] % BLOCK == 0 for row in ranges))
        broken = copy.deepcopy(ranges)
        broken[1]['offset_bytes'] += BLOCK
        with self.assertRaisesRegex(ValueError, 'gap, overlap'):
            SAMPLE.validate_partition(broken, SAMPLE.EXPECTED_CAPACITY)

    def simulate(self, temp, routes, segments=3, contaminate=False, ssh_error=False):
        root = Path(temp)
        spdk = root / 'spdk'
        (spdk / 'scripts').mkdir(parents=True)
        (spdk / 'scripts/rpc.py').touch()
        socket = root / 'existing-master'
        socket.touch()
        output = root / 'results'
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
                    self.fail('Mutating/unexpected RPC: %r' % command)
                text = '/spdk/python/spdk/rpc/__init__.py\n' + json.dumps(value)
            elif command[0] == 'ssh':
                if kwargs.get('input') is not None:
                    self.assertIn('--readonly', command[-1])
                    script = shlex.split(command[-1])[-1]
                    syntax = REAL_RUN(['bash', '-n'], input=script, capture_output=True, text=True)
                    self.assertEqual(syntax.returncode, 0, syntax.stderr)
                    job = kwargs['input']
                    self.assertIn('rw=randread', job)
                    self.assertIn('direct=1', job)
                    self.assertIn('iodepth=32', job)
                    self.assertNotIn('randwrite', job)
                    route = routes[len(state['jobs'])]
                    state['jobs'].append(job)
                    if ssh_error:
                        return subprocess.CompletedProcess(command, 255, '', 'connection lost')
                    s = state['counters']
                    bdev(s, 'FTL0')['bytes_read'] += SIZE
                    bdev(s, 'FTL0')['num_read_ops'] += SIZE // BLOCK
                    if isinstance(route, dict):
                        mapped_blocks = route['mapped_blocks']
                        qlc_blocks = route.get('qlc_blocks', mapped_blocks)
                        metadata_blocks = route.get('metadata_blocks', 0)
                    else:
                        mapped_blocks = 0 if route == 'zero' else SIZE // BLOCK
                        qlc_blocks = 0 if route == 'tlc' else mapped_blocks
                        metadata_blocks = SIZE // BLOCK // 8 if route == 'cold' else 0
                    s['ftl']['user']['read']['blocks'] += mapped_blocks
                    s['ftl']['user']['read']['ios'] += mapped_blocks
                    for device, blocks in [('nvme1n1', qlc_blocks), ('nvme0n1', mapped_blocks - qlc_blocks)]:
                        bdev(s, device)['bytes_read'] += blocks * BLOCK
                        bdev(s, device)['num_read_ops'] += blocks
                    if metadata_blocks:
                        extra = metadata_blocks * BLOCK
                        s['ftl']['md_nv_cache']['read']['blocks'] += extra // BLOCK
                        s['ftl']['md_nv_cache']['read']['ios'] += extra // BLOCK
                        bdev(s, 'nvme0n1')['bytes_read'] += extra
                        bdev(s, 'nvme0n1')['num_read_ops'] += extra // BLOCK
                    if route == 'metadata_write':
                        extra = 128 * 1024
                        s['ftl']['l2p']['write']['blocks'] += extra // BLOCK
                        s['ftl']['l2p']['write']['ios'] += 1
                        bdev(s, 'nvme0n1')['bytes_written'] += extra
                        bdev(s, 'nvme0n1')['num_write_ops'] += 1
                    if contaminate is True:
                        s['ftl']['gc']['read']['blocks'] += 1
                        s['ftl']['gc']['read']['ios'] += 1
                        bdev(s, 'nvme1n1')['bytes_read'] += BLOCK
                        bdev(s, 'nvme1n1')['num_read_ops'] += 1
                    if contaminate in ('unclassified_write', 'user_write', 'cmp_write', 'gc_write'):
                        bdev(s, 'nvme1n1')['bytes_written'] += BLOCK
                        bdev(s, 'nvme1n1')['num_write_ops'] += 1
                        group = str(contaminate).replace('_write', '')
                        if group != 'unclassified':
                            s['ftl'][group]['write']['blocks'] += 1
                            s['ftl'][group]['write']['ios'] += 1
                        if group == 'user':
                            bdev(s, 'FTL0')['bytes_written'] += BLOCK
                            bdev(s, 'FTL0')['num_write_ops'] += 1
                    text = json.dumps({'jobs': [{'error': 0, 'read': {'io_bytes': SIZE}, 'write': {'io_bytes': 0}}]})
                elif command[-1].startswith('blockdev --getsize64'):
                    text = str(SAMPLE.EXPECTED_CAPACITY) + '\n'
                elif command[-1] == 'fio --version':
                    text = 'fio-3.7\n'
                elif command[-1].startswith('bash -c '):
                    script = shlex.split(command[-1])[-1]
                    syntax = REAL_RUN(['bash', '-n'], input=script, capture_output=True, text=True)
                    self.assertEqual(syntax.returncode, 0, syntax.stderr)
                    self.assertNotIn('pkill', script)
                    self.assertIn('/proc/$pid/cmdline', script)
                # Other commands only check the master or guard the guest.
            else:
                self.fail('Unexpected command: %r' % command)
            return subprocess.CompletedProcess(command, 0, text, '')

        args = ['--spdk-dir', str(spdk), '--ssh-control-path', str(socket),
                '--output-dir', str(output), '--segments', str(segments)]
        with patch.object(SAMPLE, 'check_controllers'), \
             patch.object(SAMPLE.subprocess, 'run', side_effect=fake_run), \
             patch.object(SAMPLE.time, 'sleep'), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = SAMPLE.main(args)
        report = json.loads((output / 'summary.json').read_text())
        self.assertFalse(any('-O' in command and 'exit' in command for command in state['commands']),
                         'The externally supplied SSH master must be preserved')
        syntax = REAL_RUN(['bash', '-n', str(output / 'commands.sh')], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        return status, report, state, output

    def test_zero_tlc_continue_and_cold_metadata_only_retries_once(self):
        with tempfile.TemporaryDirectory() as temp:
            status, report, state, output = self.simulate(temp, ['zero', 'tlc', 'cold', 'qlc', 'qlc'])
            self.assertEqual(status, 2)
            self.assertTrue(report['complete'])
            self.assertFalse(report['passed'])
            self.assertEqual(report['full_device_samples']['completed_samples'], 3)
            self.assertEqual(len(state['jobs']), 5)
            self.assertEqual(report['records'][0]['attempts'][0]['evidence']['classification'], 'unmapped_or_partly_unmapped')
            self.assertEqual(report['records'][1]['attempts'][0]['evidence']['classification'], 'tlc_or_mixed_residency')
            third = report['records'][2]
            self.assertEqual(third['selected_attempt'], 1)
            self.assertEqual(len(third['attempts']), 2)
            seed = lambda job: next(line for line in job.splitlines() if line.startswith('randseed='))
            self.assertEqual(seed(state['jobs'][2]), seed(state['jobs'][3]))
            self.assertEqual(seed(state['jobs'][-1]), 'randseed=2252')
            self.assertIn('offset=2199023255552', state['jobs'][-1])
            self.assertIn('size=1099511627776', state['jobs'][-1])
            self.assertAlmostEqual(report['full_device_samples']['qlc_foreground_lower_bound_fraction'], 1 / 3)
            self.assertEqual(report['classified_fio_read_bytes_all_attempts'], 5 * SIZE)
            self.assertTrue((output / 'segment-003/first_cold_read/evidence.json').exists())
            self.assertTrue((output / 'segment-003/warm_retry/evidence.json').exists())
            self.assertTrue((output / 'summary.csv').exists())

    def test_all_qlc_passes_without_extra_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            status, report, state, _ = self.simulate(temp, ['qlc', 'qlc'], segments=1)
            self.assertEqual(status, 0)
            self.assertTrue(report['complete'])
            self.assertTrue(report['passed'])
            self.assertEqual(len(state['jobs']), 2)
            self.assertEqual(report['all_selected_samples']['qlc_foreground_lower_bound_fraction'], 1)

    def test_partial_holes_retry_resolves_remaining_reads_as_qlc(self):
        mapped = SIZE // BLOCK * 4 // 5
        cold = {'mapped_blocks': mapped, 'metadata_blocks': SIZE // BLOCK // 2}
        warm = {'mapped_blocks': mapped}
        with tempfile.TemporaryDirectory() as temp:
            status, report, state, _ = self.simulate(temp, [cold, warm, 'qlc'], segments=1)
            self.assertEqual(status, 2)  # Original >=99% mapped threshold remains.
            self.assertTrue(report['complete'])
            self.assertEqual(len(state['jobs']), 3)
            selected = report['records'][0]['attempts'][1]['evidence']
            self.assertEqual(selected['mapped_residency'], 'qlc_dominant_mapped_data')
            self.assertEqual(selected['classification'], 'unmapped_or_partly_unmapped')
            self.assertTrue(selected['checks']['warm_user_read_bytes_matches_first'])
            self.assertEqual(selected['metrics']['qlc_mapped_data_lower_bound_fraction'], 1)
            full = report['full_device_samples']
            self.assertEqual(full['qlc_mapped_data_lower_bound_fraction'], 1)
            self.assertEqual(full['qlc_mapped_data_upper_bound_fraction'], 1)
            self.assertAlmostEqual(full['qlc_foreground_lower_bound_fraction'], .8, places=3)
            self.assertEqual(full['mapped_data_read_bytes'], mapped * BLOCK)

    def test_partial_mixed_tlc_qlc_residency_is_not_hidden_by_holes(self):
        mapped = SIZE // BLOCK * 4 // 5 - 1
        cold = {'mapped_blocks': mapped, 'qlc_blocks': mapped // 2,
                'metadata_blocks': SIZE // BLOCK // 2}
        warm = dict(cold, metadata_blocks=0)
        with tempfile.TemporaryDirectory() as temp:
            status, report, _, _ = self.simulate(temp, [cold, warm, 'qlc'], segments=1)
            self.assertEqual(status, 2)
            self.assertTrue(report['complete'])
            selected = report['records'][0]['attempts'][1]['evidence']
            self.assertEqual(selected['mapped_residency'], 'mixed_mapped_data')
            self.assertEqual(selected['metrics']['qlc_mapped_data_lower_bound_fraction'], .5)
            self.assertEqual(selected['metrics']['tlc_mapped_data_lower_bound_fraction'], .5)

    def test_entirely_unmapped_sample_does_not_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            status, report, state, _ = self.simulate(temp, ['zero', 'qlc'], segments=1)
            self.assertEqual(status, 2)
            self.assertTrue(report['complete'])
            self.assertEqual(len(state['jobs']), 2)
            selected = report['records'][0]['attempts'][0]['evidence']
            self.assertEqual(selected['mapped_residency'], 'no_mapped_data')
            self.assertIsNone(selected['metrics']['qlc_mapped_data_lower_bound_fraction'])
            self.assertIsNone(report['full_device_samples']['qlc_mapped_data_lower_bound_fraction'])

    def test_same_seed_warm_user_total_must_equal_first(self):
        mapped = SIZE // BLOCK * 4 // 5
        cold = {'mapped_blocks': mapped, 'metadata_blocks': SIZE // BLOCK // 2}
        warm = {'mapped_blocks': mapped + 1}
        with tempfile.TemporaryDirectory() as temp:
            status, report, state, output = self.simulate(temp, [cold, warm], segments=1)
            self.assertEqual(status, 2)
            self.assertFalse(report['complete'])
            self.assertEqual(len(state['jobs']), 2)
            selected = report['records'][0]['attempts'][1]['evidence']
            self.assertFalse(selected['evidence_valid'])
            self.assertFalse(selected['checks']['warm_user_read_bytes_matches_first'])
            self.assertIn('warm_user_read_bytes_changed', report['error'])
            saved = json.loads((output / 'segment-001/warm_retry/evidence.json').read_text())
            self.assertFalse(saved['warm_comparison']['equal'])

    def test_polluted_gc_sample_aborts_instead_of_continuing(self):
        with tempfile.TemporaryDirectory() as temp:
            status, report, state, output = self.simulate(temp, ['qlc'], segments=1, contaminate=True)
            self.assertEqual(status, 2)
            self.assertFalse(report['complete'])
            self.assertEqual(len(state['jobs']), 1)
            self.assertIn('contaminated or invalid', report['error'])
            self.assertFalse(report['records'][0]['attempts'][0]['evidence']['evidence_valid'])
            self.assertTrue((output / 'segment-001/first_cold_read/probe.after.json').exists())

    def test_closed_metadata_persistence_is_recorded_and_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            status, report, _, _ = self.simulate(temp, ['metadata_write', 'qlc'], segments=1)
            self.assertEqual(status, 0)
            self.assertTrue(report['passed'])
            metrics = report['records'][0]['attempts'][0]['evidence']['metrics']
            self.assertEqual(metrics['metadata_written_bytes'], 128 * 1024)
            self.assertEqual(metrics['idle_metadata_written_bytes'], 0)

    def test_user_cmp_gc_and_unclassified_writes_abort(self):
        for kind in ('unclassified_write', 'user_write', 'cmp_write', 'gc_write'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                status, report, state, _ = self.simulate(temp, ['qlc'], segments=1, contaminate=kind)
                self.assertEqual(status, 2)
                self.assertFalse(report['complete'])
                self.assertEqual(len(state['jobs']), 1)
                self.assertFalse(report['records'][0]['attempts'][0]['evidence']['evidence_valid'])

    def test_connection_error_aborts_and_cleanup_targets_only_own_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            status, report, state, _ = self.simulate(temp, ['qlc'], segments=1, ssh_error=True)
            self.assertEqual(status, 2)
            self.assertFalse(report['complete'])
            cleanup = [command[-1] for command in state['commands']
                       if command[0] == 'ssh' and command[-1].startswith('bash -c ') and 'read -r pid' in command[-1]]
            self.assertEqual(len(cleanup), 1)
            self.assertIn('/proc/$pid/cmdline', cleanup[0])
            self.assertNotIn('pkill', cleanup[0])

    def test_existing_controller_rejected_before_output_or_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'must-not-exist'
            with patch.object(SAMPLE, 'check_controllers', side_effect=RuntimeError('Existing GC controller')), \
                 patch.object(SAMPLE.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'Existing GC controller'):
                    SAMPLE.main(['--output-dir', str(output)])
                run.assert_not_called()
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
