"""Filesystem and simulated protocol checks: never access an SSD or real RPC."""
import configparser
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
NEW = ROOT / 'gc_inf_onRead'
BASH = shutil.which('bash') or '/bin/bash'


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='disktest-extension-')
        self.base = Path(self.tmp.name)
        self.device = self.base / 'fake-device'
        with self.device.open('wb') as stream:
            stream.truncate(128 * 1024 * 1024)
        self.env = dict(os.environ, DEVICE=str(self.device), DEVICE_BYTES=str(128 * 1024 * 1024),
                        ALLOW_REGULAR_FILE='1', READ_IOPS='100', WRITE_MIB='1', RUNTIME='4',
                        GC_START='1', GC_THRESHOLD='920', WRITE_OFFSET='32M', WRITE_SIZE='16M',
                        READ_OFFSET='0', READ_SIZE='16M', RESULTS_ROOT=str(self.base / 'guest'),
                        RUN_ID='test-run')
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.env['PATH'] = str(self.bin) + os.pathsep + self.env['PATH']
        self.env['MOCK_CALLS'] = str(self.base / 'calls.jsonl')
        # The production host/guest use GNU date. Provide its millisecond format
        # on macOS so sub-second failure tests do not inherit BSD date rounding.
        self.write_executable('date', '''#!/usr/bin/env python3
import subprocess,sys,time
if '+%s%3N' in sys.argv:
 print(int(time.time()*1000)); raise SystemExit(0)
raise SystemExit(subprocess.run(['/bin/date',*sys.argv[1:]]).returncode)
''')
        self.write_executable('mock-fio', '''#!/usr/bin/env python3
import json,os,re,sys,time
from pathlib import Path
if '--version' in sys.argv: print('fio-3.7'); raise SystemExit(0)
with open(os.environ['MOCK_CALLS'],'a') as f: f.write(json.dumps({'tool':'fio','args':sys.argv[1:],'epoch':time.time(),'pid':os.getpid()})+'\\n')
text=Path(sys.argv[1]).read_text()
runtime=int(re.search(r'^runtime=(\\d+)',text,re.M).group(1))
if os.environ.get('FAIL_FIO'): time.sleep(.3); raise SystemExit(7)
time.sleep(runtime+.1)
out=next(x.split('=',1)[1] for x in sys.argv if x.startswith('--output='))
Path(out).write_text(json.dumps({'fio version':'fio-3.7','jobs':[]}))
''')
        self.env['FIO_BIN'] = str(self.bin / 'mock-fio')

    def tearDown(self):
        self.tmp.cleanup()

    def write_executable(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def command(self, script, env=None, timeout=15, args=()):
        return subprocess.run([BASH, str(NEW / script), *args], env=env or self.env,
                              capture_output=True, text=True, timeout=timeout)

    def test_upstream_files_preserved_byte_for_byte(self):
        record = json.loads((ROOT / 'UPSTREAM.json').read_text())
        self.assertEqual(len(record['files']), 44)
        for item in record['files']:
            self.assertEqual(hashlib.sha256((ROOT / item['path']).read_bytes()).hexdigest(),
                             item['sha256'], item['path'])

    def test_generation_separates_read_write_and_epoch_logs(self):
        result = self.command('make_job.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(result.stdout)
        self.assertEqual(config['global']['group_reporting'], '0')
        self.assertEqual(config['global']['log_unix_epoch'], '1')
        self.assertEqual(config['global']['log_hist_coarseness'], '0')
        self.assertEqual(config['foreground_write']['rw'], 'randwrite')
        self.assertEqual(config['foreground_read']['rw'], 'randread')
        self.assertEqual(config['global']['numjobs'], '1')
        self.assertEqual(config.sections(), ['global', 'foreground_write', 'foreground_read'])
        self.assertNotIn('stonewall', result.stdout)
        self.assertNotIn('new_group', result.stdout)
        self.assertNotEqual(config['foreground_read']['write_hist_log'],
                            config['foreground_write']['write_hist_log'])
        self.assertFalse((self.base / 'calls.jsonl').exists())

    def test_invalid_ranges_and_write_budget_are_rejected_before_io(self):
        for change in [{'READ_OFFSET': '32M'}, {'READ_SIZE': '1G'}, {'WRITE_MIB': '5'},
                       {'READ_IOPS': '0'}, {'GC_START': '4'}]:
            with self.subTest(change=change):
                result = self.command('make_job.sh', dict(self.env, **change))
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('[foreground_read]', result.stdout)
        self.assertFalse((self.base / 'calls.jsonl').exists())

    def test_dry_run_never_launches_fio_and_refuses_overwrite(self):
        env = dict(self.env, DRY_RUN='1')
        result = self.command('start.sh', env)
        self.assertEqual(result.returncode, 0, result.stderr)
        run = self.base / 'guest' / 'test-run'
        self.assertTrue((run / 'profile.fio').exists())
        before = (run / 'request.tsv').read_bytes()
        self.assertFalse((run / 'started.tsv').exists())
        self.assertFalse((self.base / 'calls.jsonl').exists())
        self.assertNotEqual(self.command('start.sh', env).returncode, 0)
        self.assertEqual((run / 'request.tsv').read_bytes(), before)

    def test_guest_starts_independently_and_emits_only_started_marker(self):
        result = self.command('start.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r'^STARTED\ttest-run\t[0-9]+\n$')
        run = self.base / 'guest' / 'test-run'
        started = (run / 'started.tsv').read_text().strip().split('\t')
        self.assertEqual(started, result.stdout.strip().split('\t')[1:])
        finished = (run / 'finished.tsv').read_text().strip().split('\t')
        self.assertEqual(finished[:2], ['test-run', '0'])
        self.assertEqual((run / 'fio.version').read_text().strip(), 'fio-3.7')
        self.assertFalse((run / 'controller.ready').exists())
        self.assertFalse((run / 'controller.failed').exists())
        self.assertTrue((run / 'profile.fio').exists())
        self.assertTrue((run / 'request.tsv').exists())

    def test_positional_arguments_select_run_and_rates(self):
        result = self.command('start.sh', dict(self.env, DRY_RUN='1'),
                              args=('parameter-run', '321', '2'))
        self.assertEqual(result.returncode, 0, result.stderr)
        request = dict(line.split('\t', 1) for line in
                       (self.base / 'guest' / 'parameter-run' / 'request.tsv').read_text().splitlines())
        self.assertEqual(request['run_id'], 'parameter-run')
        self.assertEqual(request['read_iops'], '321')
        self.assertEqual(request['write_mib'], '2')
        self.assertFalse((self.base / 'calls.jsonl').exists())

    def test_write_range_must_stay_in_prepared_prefix(self):
        env = dict(self.env, DRY_RUN='1', DEVICE_BYTES=str(4 * 1024 ** 4),
                   WRITE_OFFSET='1690G', WRITE_SIZE='32G', READ_OFFSET='2048G', READ_SIZE='1024G')
        result = self.command('start.sh', env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.base / 'calls.jsonl').exists())

    def make_host_mocks(self):
        self.write_executable('ssh', '''#!/usr/bin/env python3
import json,os,sys,time
args=sys.argv[1:]
index=next(i for i,x in enumerate(args) if x=='root@127.0.0.1')
command=args[index+1]
with open(os.environ['MOCK_CALLS'],'a') as f: f.write(json.dumps({'tool':'ssh','command':command,'epoch':time.time()})+'\\n')
# Replace the mock process: inherited environment and signals remain available
# to the simulated remote Guest, as they do for an SSH session's command.
os.execvp('bash',['bash','-c',command])
''')
        self.write_executable('flock', '''#!/usr/bin/env python3
import fcntl,sys
try: fcntl.flock(int(sys.argv[-1]),fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError: raise SystemExit(1)
''')
        rpc = self.write_executable('mock-rpc', '''#!/usr/bin/env python3
import json,os,sys,time
threshold=int(sys.argv[-1])
with open(os.environ['MOCK_CALLS'],'a') as f: f.write(json.dumps({'tool':'rpc','threshold':threshold,'epoch':time.time()})+'\\n')
if str(threshold)==os.environ.get('FAIL_THRESHOLD'): raise SystemExit(23)
''')
        log = self.base / 'spdk.log'
        log.write_text('')
        return dict(self.env, GUEST_RESULTS_ROOT=str(self.base / 'guest'), GUEST_DIR=str(NEW),
                    SPDK_DIR=str(ROOT), SSH_PORT='32001', SSH_TARGET='root@127.0.0.1',
                    HOST_RESULTS_ROOT=str(self.base / 'host'), SPDK_LOG=str(log), RPC_BIN=str(rpc),
                    CONTROL_LOCK_DIR=str(self.base), BASELINE_SETTLE='0', TAIL_START_SETTLE='0',
                    POLL_INTERVAL='0.05')

    def run_simulated_host(self, extra=None):
        host_env = self.make_host_mocks()
        if extra:
            host_env.update(extra)
        host = subprocess.Popen([BASH, str(NEW / 'start_gc_host.sh'), host_env['RUN_ID'],
                                 host_env['READ_IOPS'], host_env['WRITE_MIB']], env=host_env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            hout, herr = host.communicate(timeout=15)
            calls = [json.loads(line) for line in (self.base / 'calls.jsonl').read_text().splitlines()]
            return host.returncode, calls, hout + herr
        finally:
            if host.poll() is None:
                os.killpg(host.pid, signal.SIGTERM)
                try: host.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(host.pid, signal.SIGKILL)
                    host.wait()

    def test_host_starts_guest_once_and_applies_0_positive_0_from_guest_marker(self):
        hrc, calls, error = self.run_simulated_host()
        self.assertEqual(hrc, 0, error)
        rpc = [c for c in calls if c['tool'] == 'rpc']
        self.assertEqual([c['threshold'] for c in rpc], [0, 920, 0])
        self.assertEqual(sum(c['tool'] == 'ssh' for c in calls), 1)
        started_ms = int((self.base / 'guest' / 'test-run' / 'started.tsv').read_text().split('\t')[1])
        self.assertLess(rpc[0]['epoch'], started_ms / 1000)
        self.assertGreaterEqual(rpc[1]['epoch'] - started_ms / 1000, 1)
        self.assertLess(rpc[1]['epoch'] - started_ms / 1000, 2)
        self.assertTrue((self.base / 'host' / 'test-run' / 'controller.complete').exists())
        finished = (self.base / 'guest' / 'test-run' / 'finished.tsv').read_text().strip().split('\t')
        self.assertEqual(finished[1], '0')
        self.assertFalse((self.base / 'guest' / 'test-run' / 'controller.ready').exists())
        self.assertFalse((self.base / 'guest' / 'test-run' / 'controller.failed').exists())

    def test_rpc_failure_restores_0(self):
        hrc, calls, error = self.run_simulated_host({'FAIL_THRESHOLD': '920'})
        self.assertNotEqual(hrc, 0, error)
        self.assertEqual([c['threshold'] for c in calls if c['tool'] == 'rpc'], [0, 920, 0])
        self.assertTrue((self.base / 'host' / 'test-run' / 'controller.aborted').exists())
        self.assertFalse((self.base / 'guest' / 'test-run' / 'controller.failed').exists())

    def test_fio_failure_prevents_gc_on(self):
        hrc, calls, error = self.run_simulated_host({'FAIL_FIO': '1'})
        self.assertNotEqual(hrc, 0, error)
        finished = (self.base / 'guest' / 'test-run' / 'finished.tsv').read_text().strip().split('\t')
        self.assertEqual(finished[1], '7')
        self.assertTrue(all(c['threshold'] == 0 for c in calls if c['tool'] == 'rpc'))
        self.assertTrue((self.base / 'host' / 'test-run' / 'controller.aborted').exists())


if __name__ == '__main__':
    unittest.main()
