#!/usr/bin/env python3
"""Compare separate, ramp-excluded fio JSONs; never use phase histogram logs."""
import argparse,csv,datetime as dt,json,math,statistics
from pathlib import Path
from analyze import parse_spdk,read_manifest

PERCENTILES={'p50':50.,'p95':95.,'p99':99.,'p999':99.9,'p9999':99.99}

def timezone(value):
    import re
    m=re.fullmatch(r'([+-])(\d{2}):(\d{2})',value)
    if not m or int(m[2])>23 or int(m[3])>59:raise ValueError('Invalid SPDK UTC offset')
    delta=dt.timedelta(hours=int(m[2]),minutes=int(m[3]))
    return dt.timezone(delta if m[1]=='+' else -delta)

def phase_data(directory,phase,rate,scheduled,settings):
    req=read_manifest(directory/'request.tsv')
    if req.get('phase')!=phase or int(req['read_iops'])!=rate:raise ValueError('Phase/rate metadata mismatch')
    if int(req['ramp_time'])!=int(settings['RAMP_SECONDS']) or int(req['runtime'])!=int(settings['MEASURE_SECONDS']):raise ValueError('Phase duration metadata mismatch')
    if int(req['write_mib'])!=int(settings['WRITE_MIB']):raise ValueError('Write rate mismatch')
    run_id,start=(directory/'started.tsv').read_text().strip().split('\t');start=int(start)
    if run_id!=req['run_id'] or abs(start-scheduled*1000)>=1000:raise ValueError('Guest/host start mismatch (>=1 second)')
    finished=(directory/'finished.tsv').read_text().strip().split('\t')
    if len(finished)!=3 or finished[:2]!=[run_id,'0']:raise ValueError('Guest fio failed or did not finish')
    j=json.loads((directory/'result.json').read_text())
    jobs=j.get('jobs',[])
    if len(jobs)!=2 or any(x.get('error',0)!=0 for x in jobs):raise ValueError('Expected two successful fio jobs')
    reads=[x for x in jobs if x.get('jobname')=='foreground_read'];writes=[x for x in jobs if x.get('jobname')=='foreground_write']
    if len(reads)!=1 or len(writes)!=1:raise ValueError('Read/write jobs not independently reported')
    r=reads[0]['read'];w=writes[0]['write'];clat=r.get('clat_ns',{})
    if r.get('total_ios',0)<=0 or w.get('total_ios',0)<=0:raise ValueError('Missing completed IOs')
    duration=int(settings['MEASURE_SECONDS'])*1000
    if any(abs(x.get('runtime',0)-duration)>max(1500,duration*.01) for x in [r,w]):raise ValueError('fio measured duration differs from requested duration')
    percentiles={float(k):v for k,v in clat.get('percentile',{}).items()}
    result={'started_epoch_ms':start,'measurement_start_epoch_ms':start+int(req['ramp_time'])*1000,'measurement_end_epoch_ms':start+(int(req['ramp_time'])+int(req['runtime']))*1000,'actual_read_iops':float(r['iops']),'actual_write_mib_s':float(w['bw'])/1024,'read_ios':r['total_ios'],'read_clat_mean_us':float(clat['mean'])/1000,'request':req,'fio_version':j.get('fio version')}
    for name,p in PERCENTILES.items():
        value=percentiles.get(p)
        if value is None or not math.isfinite(value) or value<=0:raise ValueError('Missing/invalid JSON clat percentile '+name)
        result[name+'_us']=value/1000
    if not math.isfinite(result['actual_read_iops']) or result['actual_read_iops']<=0:raise ValueError('Invalid achieved IOPS')
    return result

def spdk_window(rows,phase):
    a=phase['measurement_start_epoch_ms']/1000;b=phase['measurement_end_epoch_ms']/1000
    values={k:[r['value'] for r in rows if r['key']==k and a<=r['t']<b] for k in ['gc_mib_s','free_bands','compaction_mib_s','write_mib_s','invalid_percent','max_invalid_percent']}
    result={k:{'n':len(v),'mean':statistics.mean(v) if v else None,'min':min(v) if v else None,'max':max(v) if v else None} for k,v in values.items()}
    result['gc_positive_samples']=sum(v>0 for v in values['gc_mib_s'])
    return result

def evaluate_point(guest,host,p,settings,tz,tolerance):
    rate=int(p['read_iops']);point=p['point_id'];out={'point_id':point,'repeat':int(p['repeat']),'read_iops_target':rate,'valid':False,'reasons':[]}
    reasons=out['reasons']
    try:
        b=phase_data(guest/(point+'-baseline'),'baseline',rate,int(p['baseline_epoch']),settings)
        g=phase_data(guest/(point+'-gc'),'gc',rate,int(p['gc_epoch']),settings)
        matchkeys=['device','write_mib','read_iops','runtime','ramp_time','read_offset_bytes','read_size_bytes','write_size_bytes','write_rw','read_qd','write_qd','read_seed','write_seed']
        if any(b['request'].get(k)!=g['request'].get(k) for k in matchkeys):raise ValueError('Paired workload settings differ')
        # Each phase gets its own later write slot. Different offsets are
        # intentional; both must match the common host/guest plan exactly.
        if 'baseline_write_offset' in p:
            for data,key in [(b,'baseline_write_offset'),(g,'gc_write_offset')]:
                req=data['request']
                if int(req['write_offset_bytes'])!=int(p[key]):raise ValueError('Phase write offset differs from sweep plan')
                if int(req['write_offset_bytes'])<int(settings['PREPARE_END_BYTES']):raise ValueError('Writes overlap the preparation region')
                if int(req['read_offset_bytes'])+int(req['read_size_bytes'])>int(settings['PREPARE_END_BYTES'])-int(settings['CACHE_GUARD_BYTES']):raise ValueError('Read region violates TLC guard')
        out.update(baseline=b,gc=g)
        hp=host/point
        if not (hp/'controller.complete').exists():reasons.append('Host point controller did not complete')
        rows=parse_spdk(hp/'spdk.log',0,tz)
        rows=[r for r in rows if r['ftl']==settings.get('FTL_NAME','FTL0')]
        bs=spdk_window(rows,b);gs=spdk_window(rows,g);out['baseline_spdk']=bs;out['gc_spdk']=gs
        with (hp/'control.csv').open() as f:events=list(csv.DictReader(f))
        successful=[e for e in events if e['event']=='rpc_success']
        for phase,name,threshold in [(b,'baseline','0'),(g,'gc_on',settings['GC_THRESHOLD'])]:
            candidates=[e for e in successful if e['detail']==name and e['threshold']==threshold]
            if len(candidates)!=1 or int(candidates[0]['epoch_ms'])>phase['started_epoch_ms']:reasons.append('Missing/late '+name+' RPC')
        switches=[e for e in successful if e['detail']=='gc_on']
        if switches and int(switches[0]['epoch_ms'])<b['measurement_end_epoch_ms']:reasons.append('GC switched on before baseline measurement ended')
        switches=[e for e in successful if e['detail']=='gc_off']
        if len(switches)!=1 or int(switches[0]['epoch_ms'])<g['measurement_end_epoch_ms']:reasons.append('GC stopped before GC measurement ended')
        needed=max(1,int(int(settings['MEASURE_SECONDS'])*.9))
        for label,stats in [('baseline',bs),('gc',gs)]:
            if any(stats[k]['n']<needed for k in ['gc_mib_s','free_bands','compaction_mib_s','write_mib_s']):reasons.append(label+': insufficient 1-second SPDK telemetry')
            if stats['free_bands']['min'] is None or stats['free_bands']['min']<=5:reasons.append(label+': necessary-GC boundary reached')
        if bs['gc_positive_samples']>0:reasons.append('Baseline includes GC writes')
        if gs['gc_positive_samples']<needed:reasons.append('GC not sustained during measurement')
        for label,data in [('baseline',b),('gc',g)]:
            if abs(data['actual_read_iops']/rate-1)>tolerance:reasons.append(label+': achieved Read IOPS differs from target')
            if abs(data['actual_write_mib_s']/int(settings['WRITE_MIB'])-1)>tolerance:reasons.append(label+': achieved write rate differs from target')
        # Report steady-state balance separately; read-latency validity is not a claim of write/compaction balance.
        out['write_compaction_balanced']=all(s['compaction_mib_s']['mean'] is not None and s['write_mib_s']['mean'] is not None and s['write_mib_s']['mean']>0 and abs(s['compaction_mib_s']['mean']/s['write_mib_s']['mean']-1)<=tolerance for s in [bs,gs])
        for k in PERCENTILES:out[k+'_observed_increase_percent']=(g[k+'_us']/b[k+'_us']-1)*100
        out['valid']=not reasons
        for k in PERCENTILES:out[k+'_increase_percent']=out[k+'_observed_increase_percent'] if out['valid'] else None
    except (ValueError,OSError,KeyError,TypeError,IndexError,ZeroDivisionError) as error:reasons.append(str(error))
    return out

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('guest_sweep_dir',type=Path);parser.add_argument('host_sweep_dir',type=Path)
    parser.add_argument('--spdk-utc-offset',required=True);parser.add_argument('--rate-tolerance',type=float,default=.05)
    parser.add_argument('--output-dir',type=Path)
    args=parser.parse_args()
    if not 0<args.rate_tolerance<=.2:parser.error('Rate tolerance must be >0 and <=0.2')
    guest,host=args.guest_sweep_dir,args.host_sweep_dir
    settings=read_manifest(guest/'settings.tsv')
    if settings!=read_manifest(host/'settings.tsv') or (guest/'plan.tsv').read_bytes()!=(host/'plan.tsv').read_bytes():parser.error('Host/guest sweep plans or settings differ')
    sweep_errors=[]
    for label,directory in [('guest',guest),('host',host)]:
        try:
            marker=(directory/'sweep.finished.tsv').read_text().strip().split('\t')
            if marker!=[settings['SWEEP_ID'],'0']:sweep_errors.append(label+' sweep incomplete/failed')
        except OSError:sweep_errors.append(label+' sweep completion missing')
    with (guest/'plan.tsv').open() as f:plan=list(csv.DictReader(f,delimiter='\t'))
    points=[evaluate_point(guest,host,p,settings,timezone(args.spdk_utc_offset),args.rate_tolerance) for p in plan]
    for p in points:
        if sweep_errors:
            p['reasons']+=sweep_errors;p['valid']=False
            for k in PERCENTILES:p[k+'_increase_percent']=None
    rates=[]
    for rate in sorted({int(p['read_iops']) for p in plan}):
        valid=[p for p in points if p['read_iops_target']==rate and p['valid']]
        r={'read_iops_target':rate,'valid_pairs':len(valid),'total_pairs':sum(p['read_iops_target']==rate for p in points)}
        for k in PERCENTILES:
            v=[p[k+'_increase_percent'] for p in valid]
            r[k+'_increase_percent_mean']=statistics.mean(v) if v else None
            r[k+'_increase_percent_stdev']=statistics.stdev(v) if len(v)>1 else None
        rates.append(r)
    output=args.output_dir or guest/'analysis';output.mkdir(parents=True,exist_ok=True)
    (output/'sweep-summary.json').write_text(json.dumps({'schema':1,'settings':settings,'points':points,'rates':rates,'warnings':sweep_errors+['Continuous sweeps change invalid-data/free-band state; temporal repeats are not independent reset replicas.','JSON percentiles describe completion latency after ramp; no faulty fio-3.7 histogram logs are used.','QLC residency must be verified before the sweep; physical GC writes are a proxy for GC activity, not dedicated GC-read counters.']},ensure_ascii=False,indent=2)+'\n')
    columns=['point_id','repeat','read_iops_target','valid','reasons','write_compaction_balanced']
    columns += [phase+'_'+k for phase in ['baseline','gc'] for k in ['actual_read_iops','actual_write_mib_s','read_clat_mean_us',*[x+'_us' for x in PERCENTILES]]]
    columns += [phase+'_spdk_'+k for phase in ['baseline','gc'] for k in ['gc_mib_s','compaction_mib_s','free_bands','invalid_percent','max_invalid_percent']]
    columns += [k+suffix for k in PERCENTILES for suffix in ['_observed_increase_percent','_increase_percent']]
    with (output/'pairs.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=columns);w.writeheader()
        for p in points:
            row={k:p.get(k) for k in columns};row['reasons']='; '.join(p['reasons'])
            for phase in ['baseline','gc']:
                for k,v in p.get(phase,{}).items():
                    if phase+'_'+k in columns:row[phase+'_'+k]=v
            for phase in ['baseline','gc']:
                for k,stats in p.get(phase+'_spdk',{}).items():
                    key=phase+'_spdk_'+k
                    if key in columns:row[key]=stats.get('mean')
            w.writerow(row)
    with (output/'by-iops.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rates[0]));w.writeheader();w.writerows(rates)
    print(output/'sweep-summary.json')
    print('%d/%d valid pairs'%(sum(p['valid'] for p in points),len(points)))
    if not any(p['valid'] for p in points):raise SystemExit(1)

if __name__=='__main__':main()
