"""Preparation protocol checks using a temporary file and a non-IO fio mock."""
import configparser
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / 'gc_inf_onRead' / 'prepare.sh'
BASH = shutil.which('bash') or '/bin/bash'
BLOCK = 4096
LARGE_BLOCK = 65536
REAL_CAPACITY = 14541900267520


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='disktest-prepare-')
        self.base = Path(self.tmp.name)
        self.device = self.base / 'fake-device'
        self.capacity = 4 * LARGE_BLOCK + 12 * BLOCK
        self.prefix = LARGE_BLOCK + 3 * BLOCK
        with self.device.open('wb') as stream:
            stream.truncate(self.capacity)
        self.calls_path = self.base / 'calls.jsonl'
        self.mock_fio = self.base / 'mock-fio'
        self.mock_fio.write_text('''#!/usr/bin/env python3
import configparser,json,os,sys
from pathlib import Path
if '--version' in sys.argv:
 print('fio-3.7'); raise SystemExit(0)
job_path=Path(sys.argv[1])
config=configparser.ConfigParser(interpolation=None)
config.read(job_path)
global_options=dict(config['global']) if 'global' in config else {}
jobs=[]
effective=[]
for name in config.sections():
 if name=='global': continue
 options=dict(global_options,**dict(config[name]))
 for argument in sys.argv[2:]:
  if argument.startswith('--filename='): options['filename']=argument.split('=',1)[1]
 effective.append(options)
 expected=int(options.get('io_size',options.get('size','0'))) if options.get('rw')=='write' else 4096
 written=expected
 if os.environ.get('PARTIAL_JOB')==job_path.name: written-=4096
 read=4096 if os.environ.get('READ_JOB')==job_path.name else 0
 error=5 if os.environ.get('ERROR_JOB')==job_path.name else 0
 jobs.append({'jobname':name,'error':error,'write':{'io_bytes':written},'read':{'io_bytes':read}})
with open(os.environ['MOCK_CALLS'],'a') as stream:
 stream.write(json.dumps({'name':job_path.name,'args':sys.argv[1:],'effective':effective})+'\\n')
if os.environ.get('EXIT_FAILURE_JOB')==job_path.name: raise SystemExit(9)
output=next(value.split('=',1)[1] for value in sys.argv[1:] if value.startswith('--output='))
Path(output).write_text(json.dumps({'fio version':'fio-3.7','jobs':jobs}))
''')
        self.mock_fio.chmod(0o755)
        self.env = dict(os.environ, DEVICE=str(self.device), DEVICE_BYTES=str(self.capacity),
                        ALLOW_REGULAR_FILE='1', PREFIX_SIZE_BYTES=str(self.prefix),
                        READ_FLOOR_BYTES=str(2 * LARGE_BLOCK), SKIP_RANDOM='1',
                        PREPARE_SETTLE_SECONDS='0', PREPARE_RUN_ID='test-preparation',
                        RESULTS_ROOT=str(self.base / 'results'), FIO_BIN=str(self.mock_fio),
                        MOCK_CALLS=str(self.calls_path))
        # Prevent developer-shell overrides from changing the protocol under test.
        for name in ('DRY_RUN', 'PARTIAL_JOB', 'READ_JOB', 'ERROR_JOB', 'EXIT_FAILURE_JOB'):
            self.env.pop(name, None)

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, changes=None, args=()):
        return subprocess.run([BASH, str(PREPARE), *args],
                              env=dict(self.env, **(changes or {})),
                              capture_output=True, text=True, timeout=15)

    def calls(self):
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()]

    def run_dir(self, run_id='test-preparation'):
        return self.base / 'results' / run_id

    def plan(self, run_id='test-preparation'):
        return dict(line.split('\t', 1) for line in
                    (self.run_dir(run_id) / 'plan.tsv').read_text().splitlines())

    def job_options(self, name, run_id='test-preparation'):
        config = configparser.ConfigParser(interpolation=None)
        config.read(self.run_dir(run_id) / name)
        sections = [name for name in config.sections() if name != 'global']
        self.assertEqual(len(sections), 1)
        options = dict(config['global']) if 'global' in config else {}
        options.update(config[sections[0]])
        return options

    def assert_single_pass(self, options, offset, size, block_size):
        self.assertEqual(options['rw'], 'write')
        self.assertEqual(options['filename'], str(self.device))
        self.assertEqual(int(options['offset']), offset)
        self.assertEqual(int(options['size']), size)
        self.assertEqual(int(options['io_size']), size)
        self.assertEqual(options['time_based'], '0')
        self.assertEqual(options['loops'], '1')
        self.assertEqual(options['numjobs'], '1')
        self.assertEqual(options['direct'], '1')
        actual_bs = options['bs'].lower()
        self.assertIn(actual_bs, {str(block_size), str(block_size // 1024) + 'k'})

    def test_dry_run_covers_real_device_tail_without_launching_fio(self):
        result = self.command({'DEVICE_BYTES': str(REAL_CAPACITY),
                               'PREFIX_SIZE_BYTES': str(1700 * 1024 ** 3),
                               'READ_FLOOR_BYTES': str(2048 * 1024 ** 3)},
                              args=('--dry-run',))
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = self.plan()
        self.assertEqual(int(plan['device_bytes']), REAL_CAPACITY)
        self.assertEqual(int(plan['full_main_bytes']), REAL_CAPACITY - 49152)
        self.assertEqual(int(plan['full_tail_bytes']), 49152)
        self.assertEqual(int(plan['prefix_main_bytes']) + int(plan['prefix_tail_bytes']),
                         1700 * 1024 ** 3)
        self.assert_single_pass(self.job_options('full_sequential_main.fio'), 0,
                               REAL_CAPACITY - 49152, LARGE_BLOCK)
        self.assert_single_pass(self.job_options('full_sequential_tail.fio'),
                               REAL_CAPACITY - 49152, 49152, BLOCK)
        self.assertFalse(self.calls())
        self.assertFalse((self.run_dir() / 'completed.json').exists())

    def test_success_runs_exact_full_and_prefix_passes_in_order(self):
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = [('full_sequential_main.fio', 0, 4 * LARGE_BLOCK, LARGE_BLOCK),
                    ('full_sequential_tail.fio', 4 * LARGE_BLOCK, 12 * BLOCK, BLOCK),
                    ('prefix_sequential_main.fio', 0, LARGE_BLOCK, LARGE_BLOCK),
                    ('prefix_sequential_tail.fio', LARGE_BLOCK, 3 * BLOCK, BLOCK)]
        calls = self.calls()
        self.assertEqual([call['name'] for call in calls], [item[0] for item in expected])
        for call, (name, offset, size, block_size) in zip(calls, expected):
            with self.subTest(stage=name):
                self.assert_single_pass(call['effective'][0], offset, size, block_size)
                self.assertIn('--output-format=json', call['args'])
        completed = json.loads((self.run_dir() / 'completed.json').read_text())
        self.assertIs(completed['qlc_verified'], False)
        commands = (self.run_dir() / 'commands.sh').read_text()
        self.assertEqual(sorted(commands.index(name) for name, *_ in expected),
                         [commands.index(name) for name, *_ in expected])
        self.assertIn(str(self.mock_fio), commands)

    def test_exactly_aligned_ranges_do_not_repeat_or_generate_tail_jobs(self):
        capacity = 8 * LARGE_BLOCK
        with self.device.open('r+b') as stream:
            stream.truncate(capacity)
        result = self.command({'DEVICE_BYTES': str(capacity),
                               'PREFIX_SIZE_BYTES': str(2 * LARGE_BLOCK),
                               'READ_FLOOR_BYTES': str(4 * LARGE_BLOCK)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call['name'] for call in self.calls()],
                         ['full_sequential_main.fio', 'prefix_sequential_main.fio'])
        self.assertEqual(int(self.plan()['full_tail_bytes']), 0)
        self.assertEqual(int(self.plan()['prefix_tail_bytes']), 0)
        self.assertFalse((self.run_dir() / 'full_sequential_tail.fio').exists())
        self.assertFalse((self.run_dir() / 'prefix_sequential_tail.fio').exists())

    def test_original_random_jobs_precede_the_sequential_passes(self):
        result = self.command({'SKIP_RANDOM': '0'})
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([call['name'] for call in calls],
                         ['rnd.job', 'rnd2.job', 'rnd3.job', 'rnd64.job',
                          'full_sequential_main.fio', 'full_sequential_tail.fio',
                          'prefix_sequential_main.fio', 'prefix_sequential_tail.fio'])
        for call in calls:
            for options in call['effective']:
                self.assertEqual(options['filename'], str(self.device))
        self.assertEqual(self.plan()['skip_random'], '0')

    def test_fio_error_short_write_and_process_failure_stop_before_prefix(self):
        for variable in ('ERROR_JOB', 'PARTIAL_JOB', 'EXIT_FAILURE_JOB'):
            with self.subTest(failure=variable):
                run_id = variable.lower()
                before = len(self.calls())
                result = self.command({variable: 'full_sequential_tail.fio',
                                       'PREPARE_RUN_ID': run_id})
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual([call['name'] for call in self.calls()[before:]],
                                 ['full_sequential_main.fio', 'full_sequential_tail.fio'])
                self.assertFalse((self.run_dir(run_id) / 'completed.json').exists())

    def test_unexpected_read_bytes_in_write_stage_prevent_completion(self):
        result = self.command({'READ_JOB': 'full_sequential_main.fio'})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call['name'] for call in self.calls()], ['full_sequential_main.fio'])
        self.assertFalse((self.run_dir() / 'completed.json').exists())

    def test_invalid_geometry_and_units_are_rejected_before_io(self):
        changes = [
            {'PREFIX_SIZE_BYTES': str(2 * LARGE_BLOCK)},
            {'READ_FLOOR_BYTES': str(self.capacity)},
            {'DEVICE_BYTES': str(self.capacity + 1)},
            {'PREFIX_SIZE_BYTES': str(self.prefix + 1)},
            {'READ_FLOOR_BYTES': str(2 * LARGE_BLOCK + 1)},
            {'PREFIX_SIZE_BYTES': '1.7TB'},
            {'PREFIX_SIZE_BYTES': '-4096'},
            {'SKIP_RANDOM': '2'},
        ]
        for index, change in enumerate(changes):
            with self.subTest(change=change):
                result = self.command(dict(change, DRY_RUN='1',
                                           PREPARE_RUN_ID='invalid-' + str(index)))
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse((self.run_dir('invalid-' + str(index)) / 'completed.json').exists())
        self.assertFalse(self.calls())

    def test_regular_file_needs_explicit_test_mode_and_actual_capacity(self):
        env = self.env.copy()
        env.pop('ALLOW_REGULAR_FILE', None)
        result = subprocess.run([BASH, str(PREPARE)], env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.command({'DEVICE_BYTES': str(self.capacity + BLOCK),
                               'PREPARE_RUN_ID': 'wrong-capacity'})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.calls())

    def test_existing_preparation_is_never_overwritten_or_repeated(self):
        first = self.command()
        self.assertEqual(first.returncode, 0, first.stderr)
        plan_before = (self.run_dir() / 'plan.tsv').read_bytes()
        completed_before = (self.run_dir() / 'completed.json').read_bytes()
        calls_before = self.calls()
        second = self.command()
        self.assertNotEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual((self.run_dir() / 'plan.tsv').read_bytes(), plan_before)
        self.assertEqual((self.run_dir() / 'completed.json').read_bytes(), completed_before)
        self.assertEqual(self.calls(), calls_before)


if __name__ == '__main__':
    unittest.main()
