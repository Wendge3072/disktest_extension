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
        self.end = 3 * LARGE_BLOCK + 3 * BLOCK
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
                        ALLOW_REGULAR_FILE='1', PREPARE_END_BYTES=str(self.end),
                        CACHE_GUARD_BYTES=str(LARGE_BLOCK), READ_OFFSET='0', READ_SIZE=str(LARGE_BLOCK), SKIP_RANDOM='1',
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

    def test_preview_only_writes_four_tib_and_preserves_three_original_random_jobs(self):
        result = self.command({'DEVICE_BYTES':str(REAL_CAPACITY),
                               'PREPARE_END_BYTES':str(4*1024**4),
                               'CACHE_GUARD_BYTES':str(1700*1024**3), 'READ_OFFSET':'1024G', 'READ_SIZE':'1024G', 'SKIP_RANDOM':'0'}, args=('--dry-run',))
        self.assertEqual(result.returncode,0,result.stderr)
        plan=self.plan()
        self.assertEqual(int(plan['sequential_main_bytes']),4*1024**4)
        self.assertEqual(int(plan['sequential_tail_bytes']),0)
        self.assert_single_pass(self.job_options('partial_sequential_main.fio'),0,4*1024**4,LARGE_BLOCK)
        self.assertFalse(list(self.run_dir().glob('full_*')))
        self.assertFalse(list(self.run_dir().glob('prefix_*')))
        self.assertFalse(self.calls())

    def test_success_runs_one_partial_pass_and_exact_tail(self):
        result=self.command();self.assertEqual(result.returncode,0,result.stderr)
        calls=self.calls()
        self.assertEqual([x['name'] for x in calls],['partial_sequential_main.fio','partial_sequential_tail.fio'])
        self.assert_single_pass(calls[0]['effective'][0],0,3*LARGE_BLOCK,LARGE_BLOCK)
        self.assert_single_pass(calls[1]['effective'][0],3*LARGE_BLOCK,3*BLOCK,BLOCK)
        done=json.loads((self.run_dir()/'completed.json').read_text())
        self.assertEqual(done['prepare_end_bytes'],self.end)
        self.assertIs(done['qlc_verified'],False)

    def test_original_random_jobs_precede_partial_pass_unchanged(self):
        result=self.command({'SKIP_RANDOM':'0'});self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual([x['name'] for x in self.calls()],['rnd.job','rnd2.job','rnd3.job','partial_sequential_main.fio','partial_sequential_tail.fio'])
        for name in ['rnd.job','rnd2.job','rnd3.job']:
            self.assertEqual((self.run_dir()/name).read_bytes(),(ROOT/'gc_inf_onBW'/name).read_bytes())

    def test_short_write_error_or_unexpected_read_prevents_completion(self):
        for key in ['PARTIAL_JOB','ERROR_JOB','READ_JOB','EXIT_FAILURE_JOB']:
            result=self.command({key:'partial_sequential_main.fio','PREPARE_RUN_ID':key.lower()})
            self.assertNotEqual(result.returncode,0,key)
            self.assertFalse((self.run_dir(key.lower())/'completed.json').exists())

    def test_invalid_geometry_is_rejected_before_io(self):
        for i,change in enumerate([{'PREPARE_END_BYTES':str(self.capacity)},
                                  {'CACHE_GUARD_BYTES':str(self.end)},
                                  {'PREPARE_END_BYTES':str(self.end+1)},
                                  {'CACHE_GUARD_BYTES':'1.7TB'}, {'SKIP_RANDOM':'2'}]):
            result=self.command(dict(change,PREPARE_RUN_ID='bad-'+str(i)))
            self.assertNotEqual(result.returncode,0)
        self.assertFalse(self.calls())

    def test_existing_result_is_not_overwritten(self):
        result=self.command();self.assertEqual(result.returncode,0,result.stderr)
        before=self.calls();self.assertNotEqual(self.command().returncode,0)
        self.assertEqual(self.calls(),before)

if __name__ == '__main__':
    unittest.main()
