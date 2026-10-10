"""Host preflight protocol simulations; no SSH, RPC, fio or SSD access."""
from contextlib import redirect_stdout
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
from unittest.mock import patch, MagicMock

from test_read_path_evidence import snapshot, bdev, SIZE, BLOCK

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gc_inf_onRead'))
SPEC = importlib.util.spec_from_file_location('verify_read_path', ROOT / 'gc_inf_onRead/verify_read_path.py')
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)
REAL_RUN = subprocess.run
REAL_POPEN = subprocess.Popen


def inventory():
    result = []
    for name in ['FTL0', 'nvme0n1', 'nvme1n1']:
        entry = {'name': name, 'uuid': name + '-uuid', 'block_size': 4096, 'num_blocks': 3550268620}
        if name == 'FTL0':
            entry['driver_specific'] = {'ftl': {'cache': 'nvme0n1', 'base_bdev': 'nvme1n1'}}
        else:
            model = 'INTEL SSDPE2KX020T8' if name == 'nvme0n1' else 'INTEL SSDPF2NV153TZ'
            entry['driver_specific'] = {'nvme': [{'ctrlr_data': {'model_number': model}}]}
        result.append(entry)
    return result


class VerifyReadPathTests(unittest.TestCase):
    def test_host_preflight_records_evidence_without_write_rpc(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'result'
            argv = ['verify_read_path.py', '--profile', str(ROOT / 'gc_inf_onRead/examples/w512-r25000.job'),
                    '--output-dir', str(output)]
            state = {'sample': -1, 'probe': False, 'calls': []}
            def fake_run(cmd, **kwargs):
                state['calls'].append((cmd, kwargs.get('input')))
                text = ''
                if 'scripts/rpc.py' in ' '.join(cmd):
                    if 'bdev_get_bdevs' in cmd:
                        value = inventory()
                    elif 'bdev_get_iostat' in cmd:
                        state['sample'] += 1
                        value = current_snapshot()['bdev']
                    elif 'bdev_ftl_get_stats' in cmd:
                        value = current_snapshot()['ftl']
                    else:
                        self.fail('Unexpected or mutating RPC: ' + repr(cmd))
                    text = '/private/spdk/python/spdk/rpc/__init__.py\n' + json.dumps(value)
                elif cmd[0] == 'ssh':
                    if cmd[-1].startswith('bash -c '):
                        script = shlex.split(cmd[-1])[-1]
                        self.assertNotIn('\x00', script)
                        with patch.object(subprocess, 'Popen', REAL_POPEN):
                            syntax = REAL_RUN(['bash', '-n'], input=script, capture_output=True, text=True)
                        self.assertEqual(syntax.returncode, 0, syntax.stderr)
                    if kwargs.get('input'):
                        self.assertFalse(state['probe'])
                        state['probe'] = True
                        job = kwargs['input']
                        self.assertIn('rw=randread', job)
                        self.assertNotIn('randwrite', job)
                        self.assertNotIn('foreground_write', job)
                        self.assertIn('offset=2199023255552', job)
                        self.assertIn('io_size=67108864', job)
                        self.assertIn('--readonly', cmd[-1])
                        self.assertIn('timeout --signal=TERM', cmd[-1])
                        self.assertNotIn('\x00', cmd[-1])
                        text = json.dumps({'jobs': [{'error': 0, 'read': {'io_bytes': SIZE}, 'write': {'io_bytes': 0}}]})
                    elif cmd[-1].startswith('blockdev --getsize64'):
                        text = '14541900267520\n'
                    elif cmd[-1] == 'fio --version':
                        text = 'fio-3.7\n'
                else:
                    self.fail('Unexpected command: ' + repr(cmd))
                return subprocess.CompletedProcess(cmd, 0, text, '')
            def current_snapshot():
                sample = snapshot((state['sample'] + 1) * 1000000000)
                if state['probe']:
                    bdev(sample, 'FTL0').update(bytes_read=SIZE, num_read_ops=SIZE // BLOCK)
                    bdev(sample, 'nvme1n1').update(bytes_read=SIZE, num_read_ops=SIZE // BLOCK)
                    sample['ftl']['user']['read'].update(ios=SIZE // BLOCK, blocks=SIZE // BLOCK)
                return sample
            collector = MagicMock()
            collector.poll.return_value = None
            with patch.object(sys, 'argv', argv), patch.object(VERIFY, 'check_controllers'), \
                 patch.object(VERIFY.subprocess, 'run', side_effect=fake_run), \
                 patch.object(VERIFY.subprocess, 'Popen', return_value=collector), \
                 patch.object(VERIFY.time, 'sleep'), redirect_stdout(io.StringIO()):
                self.assertEqual(VERIFY.main(), 0)
            report = json.loads((output / 'verification.json').read_text())
            self.assertTrue(report['passed'], report['reasons'])
            self.assertEqual(report['metrics']['sample_qlc_foreground_lower_bound_fraction'], 1)
            self.assertTrue((output / 'probe.after.stable.json').exists())
            self.assertTrue(state['probe'])
            collector.terminate.assert_called_once()
            self.assertTrue(any('-O' in cmd for cmd, _ in state['calls']))

    def test_existing_controller_rejects_before_ssh_rpc_or_output(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'must-not-exist'
            argv = ['verify_read_path.py', '--profile', str(ROOT / 'gc_inf_onRead/examples/w512-r25000.job'),
                    '--output-dir', str(output)]
            with patch.object(sys, 'argv', argv), \
                 patch.object(VERIFY.subprocess, 'check_output', return_value='11165 bash start_gc.sh\n'), \
                 patch.object(VERIFY.subprocess, 'run') as run, patch.object(VERIFY.subprocess, 'Popen') as popen:
                with self.assertRaisesRegex(RuntimeError, 'finish the old experiment'):
                    VERIFY.main()
                run.assert_not_called()
                popen.assert_not_called()
                self.assertFalse(output.exists())

    def test_manual_controller_also_prevents_read_probes(self):
        with patch.object(VERIFY.subprocess, 'check_output',
                          return_value='123 bash /tmp/start_gc_manual.sh read-gc-001 25000 512\n'):
            with self.assertRaisesRegex(RuntimeError, 'Existing GC controller PID 123'):
                VERIFY.check_controllers()


if __name__ == '__main__':
    unittest.main()
