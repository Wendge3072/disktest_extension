"""Paired latency regressions and a two-terminal simulation; no real disk/RPC."""
import csv,datetime as dt,json,subprocess,sys,tempfile,time,unittest
from pathlib import Path
import test_extension as support
NEW=support.NEW;BASH=support.BASH
sys.path.insert(0,str(NEW))
from analyze_sweep import evaluate_point
from check_free_bands import check

class SweepTests(unittest.TestCase):
    def setUp(self):
        support.ExtensionTests.setUp(self)
        self.env.update(PREPARE_END_BYTES=str(32*1024**2),CACHE_GUARD_BYTES=str(16*1024**2))
    tearDown=support.ExtensionTests.tearDown
    write_executable=support.ExtensionTests.write_executable
    command=support.ExtensionTests.command
    make_host_mocks=support.ExtensionTests.make_host_mocks

    def test_plan_generates_all_rates_repeats_and_no_histogram_logs(self):
        env=dict(self.env,DRY_RUN='1',DEVICE_BYTES=str(256*1024**2),READ_IOPS_LIST='100,200',REPEATS='2',MEASURE_SECONDS='2',RAMP_SECONDS='1',RECOVER_SECONDS='1',BASELINE_SETTLE='1',GC_SETTLE_SECONDS='1',TIMING_MARGIN='1')
        result=self.command('start_sweep.sh',env,args=('sweep',))
        self.assertEqual(result.returncode,0,result.stderr)
        root=self.base/'guest/sweep'
        with (root/'plan.tsv').open() as f:plan=list(csv.DictReader(f,delimiter='\t'))
        self.assertEqual([p['read_iops'] for p in plan],['100','200','200','100'])
        self.assertEqual(len(list(root.glob('*/profile.fio'))),8)
        for p in root.glob('*/profile.fio'):
            s=p.read_text();self.assertIn('ramp_time=1',s);self.assertNotIn('write_hist_log',s)
        self.assertFalse((self.base/'calls.jsonl').exists())

    def test_read_cache_guard_and_total_sweep_capacity_fail_before_io(self):
        for change in [dict(READ_OFFSET='16M'),dict(REPEATS='20')]:
            result=self.command('start_sweep.sh',dict(self.env,DRY_RUN='1',**change),args=('bad',))
            self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.base/'calls.jsonl').exists())

    def test_default_layout_reserves_whole_matrix_and_guards_full_read_range(self):
        env=dict(self.env,DRY_RUN='1',DEVICE_BYTES='14541900267520',REPEATS='3',WRITE_SIZE='128G')
        for k in ['PREPARE_END_BYTES','CACHE_GUARD_BYTES','READ_OFFSET','READ_SIZE','WRITE_OFFSET']:env.pop(k,None)
        result=self.command('start_sweep.sh',env,args=('auto',));self.assertEqual(result.returncode,0,result.stderr)
        root=self.base/'guest/auto';settings=dict(x.split('\t',1) for x in (root/'settings.tsv').read_text().splitlines())
        end=int(settings['PREPARE_END_BYTES']);self.assertLess(end,int(settings['DEVICE_BYTES'])*.8)
        self.assertLessEqual(int(settings['READ_SIZE']),end-int(settings['CACHE_GUARD_BYTES']))
        with (root/'plan.tsv').open() as f:plan=list(csv.DictReader(f,delimiter='\t'))
        offsets=[int(p[k]) for p in plan for k in ['baseline_write_offset','gc_write_offset']]
        self.assertEqual(len(offsets),36);self.assertEqual(offsets,list(range(end,end+36*128*1024**3,128*1024**3)))
        self.assertLessEqual(offsets[-1]+128*1024**3,int(settings['DEVICE_BYTES']))

    def test_verification_preview_uses_same_geometry_without_rpc(self):
        env=dict(self.env,DRY_RUN='1',READ_IOPS_LIST='100',MEASURE_SECONDS='2',RAMP_SECONDS='1',VERIFY_RESULTS_ROOT=str(self.base/'verify'),SPDK_DIR=str(support.ROOT))
        result=self.command('verify_prepared.sh',env,args=('verify',));self.assertEqual(result.returncode,0,result.stderr)
        text=(self.base/'verify/verify/profile.fio').read_text()
        self.assertIn('offset=0',text);self.assertIn('rw=write',text)
        self.assertFalse((self.base/'calls.jsonl').exists())

    def test_ramp_write_budget_is_counted(self):
        result=self.command('make_job.sh',dict(self.env,RAMP_TIME='20'))
        self.assertNotEqual(result.returncode,0)
        self.assertIn('including ramp',result.stderr)

    def test_two_terminals_switch_only_between_complete_phase_runs(self):
        host_env=self.make_host_mocks()
        p=self.bin/'mock-fio';s=p.read_text().replace('time.sleep(runtime+.1)','time.sleep(runtime+int(re.search(r"^ramp_time=(\\d+)",text,re.M).group(1))+.1)');p.write_text(s)
        settings=dict(READ_IOPS_LIST='100 200',REPEATS='1',MEASURE_SECONDS='1',RAMP_SECONDS='1',RECOVER_SECONDS='4',BASELINE_SETTLE='3',GC_SETTLE_SECONDS='2',TIMING_MARGIN='2',MIN_FREE_BANDS='256',START_AT=str(int(time.time())+8),TELEMETRY_WAIT_SECONDS='0.1')
        host_env.update(settings);guest_env=dict(self.env,**settings)
        host=subprocess.Popen([BASH,str(NEW/'start_gc_sweep.sh'),'sweep'],env=host_env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        writer=None
        try:
            log=Path(host_env['SPDK_LOG'])
            writer=subprocess.Popen([sys.executable,'-c',"import sys,time,json,pathlib\nf=open(sys.argv[1],'a',buffering=1)\nwhile True:\n p=pathlib.Path(sys.argv[2]);rows=[json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []\n rpc=[x for x in rows if x['tool']=='rpc'];gc=10 if rpc and rpc[-1]['threshold']>0 else 0\n f.write('[FTL][FTL0] Poller Free Bands: 1000\\n[FTL][FTL0] GC Writing: %s\\n'%gc);time.sleep(.05)",str(log),str(self.base/'calls.jsonl')])
            guest=self.command('start_sweep.sh',guest_env,args=('sweep',),timeout=40)
            hout,herr=host.communicate(timeout=5)
            self.assertEqual(guest.returncode,0,guest.stderr)
            self.assertEqual(host.returncode,0,hout+herr)
            self.assertEqual((self.base/'guest/sweep/plan.tsv').read_bytes(),(self.base/'host/sweep/plan.tsv').read_bytes())
            self.assertEqual((self.base/'guest/sweep/settings.tsv').read_bytes(),(self.base/'host/sweep/settings.tsv').read_bytes())
            calls=[json.loads(x) for x in (self.base/'calls.jsonl').read_text().splitlines()]
            self.assertEqual(sum(c['tool']=='fio' for c in calls),4)
            self.assertFalse(any(c['tool']=='ssh' for c in calls))
            for point in ['r01-iops100','r01-iops200']:
                root=self.base/'guest/sweep';hroot=self.base/'host/sweep'/point
                bs=int((root/(point+'-baseline')/'started.tsv').read_text().split()[1])
                gf=int((root/(point+'-gc')/'finished.tsv').read_text().split()[2])
                with (hroot/'control.csv').open() as f:events=list(csv.DictReader(f))
                on=next(e for e in events if e['detail']=='gc_on');off=next(e for e in events if e['detail']=='gc_off')
                self.assertGreater(int(on['epoch_ms']),bs+2000)
                self.assertGreater(int(off['epoch_ms']),gf)
        finally:
            if writer:writer.terminate();writer.wait()
            if host.poll() is None:host.terminate();host.communicate(timeout=5)

class AnalysisTests(unittest.TestCase):
    def fixture(self,base,contaminated=False,saturated=False):
        guest=base/'guest';host=base/'host';guest.mkdir();host.mkdir();point='r01-iops100';h=host/point;h.mkdir()
        settings={'RAMP_SECONDS':'1','MEASURE_SECONDS':'10','WRITE_MIB':'1','GC_THRESHOLD':'920'}
        p={'point_id':point,'repeat':'1','read_iops':'100','baseline_epoch':'1791504000','gc_epoch':'1791504020'}
        for phase,stamp,latency in [('baseline',1791504000,100000),('gc',1791504020,150000)]:
            d=guest/(point+'-'+phase);d.mkdir();rid=d.name
            req={'run_id':rid,'phase':phase,'read_iops':'100','write_mib':'1','ramp_time':'1','runtime':'10'}
            (d/'request.tsv').write_text(''.join(k+'\t'+v+'\n' for k,v in req.items()))
            (d/'started.tsv').write_text(rid+'\t'+str(stamp*1000)+'\n');(d/'finished.tsv').write_text(rid+'\t0\t'+str((stamp+11)*1000)+'\n')
            read={'total_ios':1000,'runtime':10000,'iops':80 if phase=='gc' and saturated else 100,'clat_ns':{'mean':latency*.5,'percentile':{str(k):latency for k in [50,95,99,99.9,99.99]}}}
            (d/'result.json').write_text(json.dumps({'fio version':'fio-3.7','jobs':[{'jobname':'foreground_read','error':0,'read':read},{'jobname':'foreground_write','error':0,'write':{'total_ios':160,'runtime':10000,'bw':1024}}]}))
        (h/'controller.complete').touch()
        (h/'control.csv').write_text('epoch_ms,event,threshold,detail\n1791503999000,rpc_success,0,baseline\n1791504012000,rpc_success,920,gc_on\n1791504032000,rpc_success,0,gc_off\n')
        with (h/'spdk.log').open('w') as f:
            for stamp in list(range(1791504001,1791504011))+list(range(1791504021,1791504031)):
                baseline=stamp<1791504020;stamp_text=dt.datetime.fromtimestamp(stamp,dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
                for label,val in [('Poller Free Bands',1000),('GC Writing',10 if not baseline or contaminated else 0),('Compaction Writing',1),('User writing BandWidth',1)]:f.write('[%s] f.c:1:f: [FTL][FTL0] %s: %s\n'%(stamp_text,label,val))
        return guest,host,p,settings

    def test_exact_json_phase_p99_and_increase_with_fio37(self):
        with tempfile.TemporaryDirectory() as tmp:
            g,h,p,s=self.fixture(Path(tmp));r=evaluate_point(g,h,p,s,dt.timezone.utc,.05)
            self.assertTrue(r['valid'],r['reasons']);self.assertEqual(r['baseline']['p99_us'],100);self.assertEqual(r['gc']['p99_us'],150);self.assertAlmostEqual(r['p99_increase_percent'],50)

    def test_later_write_slots_are_checked_against_the_shared_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            g,h,p,s=self.fixture(Path(tmp));s.update(PREPARE_END_BYTES='33554432',CACHE_GUARD_BYTES='16777216')
            p.update(baseline_write_offset='33554432',gc_write_offset='50331648')
            for phase,key in [('baseline','baseline_write_offset'),('gc','gc_write_offset')]:
                path=g/(p['point_id']+'-'+phase)/'request.tsv'
                with path.open('a') as f:f.write('write_offset_bytes\t'+p[key]+'\nread_offset_bytes\t0\nread_size_bytes\t16777216\nwrite_size_bytes\t16777216\n')
            r=evaluate_point(g,h,p,s,dt.timezone.utc,.05);self.assertTrue(r['valid'],r['reasons'])
            p['gc_write_offset']='67108864';r=evaluate_point(g,h,p,s,dt.timezone.utc,.05)
            self.assertFalse(r['valid']);self.assertTrue(any('offset' in x for x in r['reasons']))

    def test_contaminated_baseline_keeps_raw_latency_but_excludes_ratio(self):
        with tempfile.TemporaryDirectory() as tmp:
            g,h,p,s=self.fixture(Path(tmp),contaminated=True);r=evaluate_point(g,h,p,s,dt.timezone.utc,.05)
            self.assertFalse(r['valid']);self.assertIsNone(r['p99_increase_percent']);self.assertEqual(r['gc']['p99_us'],150)

    def test_saturation_is_not_silently_reported_as_matched_iops(self):
        with tempfile.TemporaryDirectory() as tmp:
            g,h,p,s=self.fixture(Path(tmp),saturated=True);r=evaluate_point(g,h,p,s,dt.timezone.utc,.05)
            self.assertFalse(r['valid']);self.assertTrue(any('achieved Read IOPS' in x for x in r['reasons']))

class StateGateTests(unittest.TestCase):
    def test_space_quiet_active_and_stale_log_gates(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'state.log'
            p.write_text('[FTL][FTL0] Poller Free Bands: 2048\n'+('[FTL][FTL0] GC Writing: 0\n'*10))
            self.assertEqual(check(p,'FTL0',2048,quiet_samples=10),2048)
            with self.assertRaises(ValueError):check(p,'FTL0',2049)
            with self.assertRaises(ValueError):check(p,'FTL0',2048,active_samples=10)
            p.write_text('[FTL][FTL0] Poller Free Bands: 2048\n'+('[FTL][FTL0] GC Writing: 2000\n'*10))
            self.assertEqual(check(p,'FTL0',2048,active_samples=10),2048)
            with self.assertRaises(ValueError):check(p,'FTL0',2048,quiet_samples=10)
            os.utime(p,(time.time()-10,time.time()-10))
            with self.assertRaises(ValueError):check(p,'FTL0',2048)

if __name__=='__main__':unittest.main()
