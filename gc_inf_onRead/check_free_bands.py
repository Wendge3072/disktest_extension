#!/usr/bin/env python3
"""Gate a baseline on recent free-band observations and zero GC writes."""
import argparse,re,sys,time
from pathlib import Path

def check(path,ftl,minimum,quiet_samples=0,active_samples=0):
    if time.time()-path.stat().st_mtime>5:
        raise ValueError('SPDK telemetry is stale (>5 seconds)')
    with path.open('rb') as f:
        f.seek(max(0,path.stat().st_size-524288));text=f.read().decode(errors='replace')
    prefix=r'\[FTL\]\['+re.escape(ftl)+r'\].*'
    bands=re.findall(prefix+r'Poller Free Bands:\s*(\d+)',text)
    gc=re.findall(prefix+r'GC Writing:\s*([0-9.]+)',text)
    if not bands:raise ValueError('No free-band telemetry; enable the existing FTL state log')
    value=int(bands[-1])
    if value<minimum:raise ValueError('Only %d free bands; require %d'%(value,minimum))
    if quiet_samples and (len(gc)<quiet_samples or any(float(v)>0 for v in gc[-quiet_samples:])):
        raise ValueError('GC has not been quiet for %d logged samples'%quiet_samples)
    if active_samples and (len(gc)<active_samples or any(float(v)<=0 for v in gc[-active_samples:])):
        raise ValueError('GC is not actively moving data for %d logged samples'%active_samples)
    return value

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('log',type=Path);p.add_argument('ftl');p.add_argument('minimum',type=int)
    p.add_argument('--quiet-samples',type=int,default=0)
    p.add_argument('--active-samples',type=int,default=0)
    a=p.parse_args()
    try:print(check(a.log,a.ftl,a.minimum,a.quiet_samples,a.active_samples))
    except (ValueError,OSError) as e:sys.exit(str(e))
