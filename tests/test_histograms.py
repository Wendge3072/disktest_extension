import datetime as dt
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('histogram', ROOT / 'gc_inf_onRead/histogram.py')
histogram = importlib.util.module_from_spec(spec)
spec.loader.exec_module(histogram)


class HistogramTests(unittest.TestCase):
    def write_hist(self, path, started, samples):
        with path.open('w') as stream:
            for seconds, counts in samples:
                bins = [0] * histogram.BIN_COUNT
                for index, count in counts.items(): bins[index] = count
                stream.write(','.join(map(str, [started + seconds * 1000, 0, 4096, *bins])) + '\n')

    def test_merges_io_counts_and_excludes_boundary_intervals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'profile_foreground_read_clat_hist.2.log'
            started = 1791504000000
            # The interval ending at60 crosses the analysis start; its huge slow
            # population must not leak into [60,240]. The two complete intervals
            # have unequal populations, so averaging row percentiles is wrong.
            self.write_hist(path, started, [(59,{127:1}), (60,{1000:1000000}),
                                            (61,{127:10000,128:1}), (62,{127:1,128:999})])
            result = histogram.merge_window([path], 60, 240, started)
            self.assertEqual(result['samples'], 11001)
            self.assertEqual(result['p99_ns'], 129)
            self.assertEqual(result['p999_ns'], 129)
            self.assertEqual(result['covered_ms'], 2000)

    def test_unknown_histogram_shape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'read_hist.log'
            path.write_text('1000,0,4096,1,2,3\n')
            with self.assertRaises(ValueError):
                histogram.merge_window([path], 60, 240, 0)

    def test_legacy_analyzer_rejects_unaudited_fio37_histograms_and_whole_run_p99(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);guest=base/'guest';host=base/'host';guest.mkdir();host.mkdir()
            started=1791504000000
            request='schema\t1\nrun_id\ttest\nruntime\t600\ngc_start\t240\ngc_threshold\t920\n'
            for directory in [guest,host]:
                (directory/'request.tsv').write_text(request)
                (directory/'started.tsv').write_text('test\t%s\n'%started)
            (guest/'finished.tsv').write_text('test\t0\t%s\n'%(started+600000))
            (guest/'fio.version').write_text('fio-3.7\n')
            (guest/'result.json').write_text(json.dumps({'fio version':'fio-3.7','jobs':[{'read':{'clat_ns':{'percentile':{'99.000000':999999999}}}}]}))
            (host/'controller.complete').write_text('test\n')
            (host/'control.csv').write_text('epoch_ms,event,threshold,detail\n%s,rpc_success,920,gc_on\n'%(started+240000))
            with (host/'spdk.log').open('w') as log, (guest/'profile_foreground_read_iops.2.log').open('w') as iops, (guest/'profile_foreground_read_bw.2.log').open('w') as bw:
                for seconds in range(600):
                    stamp=dt.datetime.fromtimestamp(started/1000+seconds,dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
                    for label,value in [('User writing BandWidth',512),('Compaction Writing',512),('GC Writing',0 if seconds<240 else 1000),('Poller Free Bands',1000)]:
                        log.write('[%s] file.c:1:f: *NOTICE*: [FTL][FTL0] %s: %s\n'%(stamp,label,value))
                    iops.write('%s,%s,0,4096\n'%(started+seconds*1000,100 if seconds<240 else 80))
                    bw.write('%s,%s,0,4096\n'%(started+seconds*1000,400 if seconds<240 else 320))
            self.write_hist(guest/'profile_foreground_read_clat_hist.2.log',started,
                            [(s,{127 if s<240 else 128:100}) for s in range(1,601)])
            result=subprocess.run([sys.executable,str(ROOT/'gc_inf_onRead/analyze.py'),str(guest),str(host),'--spdk-utc-offset=+00:00'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            summary=json.loads((guest/'analysis/summary.json').read_text())
            self.assertEqual(summary['windows']['baseline']['read_iops_log']['mean_iops'],100)
            self.assertEqual(summary['windows']['gc']['read_iops_log']['mean_iops'],80)
            self.assertIsNone(summary['windows']['baseline']['read_p99_ns'])
            self.assertIsNone(summary['windows']['gc']['read_p99_ns'])
            self.assertTrue(any('upper half' in msg for msg in summary['warnings']))
            self.assertIsNone(summary['windows']['gc']['qlc_foreground_read_fraction'])
            self.assertFalse(any('completion marker invalid' in msg for msg in summary['warnings']))


if __name__=='__main__':unittest.main()
