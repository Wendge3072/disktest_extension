#!/usr/bin/env python3
"""Analyze fixed, aligned motivation windows without inventing phase p99."""
import argparse
import csv
import datetime as dt
import json
import math
import re
import statistics
from pathlib import Path
from histogram import merge_window


def read_manifest(path):
    result = {}
    for line in path.read_text().splitlines():
        fields = line.split('\t')
        if len(fields) != 2 or fields[0] in result:
            raise ValueError('Malformed/duplicate manifest field')
        result[fields[0]] = fields[1]
    return result


def mean(values):
    return statistics.mean(values) if values else None


def parse_spdk(path, start_ms, timezone):
    pattern = re.compile(r'^\[([^]]+)\].*\[FTL\]\[([^]]+)\]\s+(.*)$')
    fields = {
        'User writing BandWidth': 'write_mib_s',
        'Compaction Writing': 'compaction_mib_s',
        'GC Writing': 'gc_mib_s',
        'Poller Free Bands': 'free_bands',
        'Invalid Block Ratio': 'invalid_percent',
        'Max Invalidity': 'max_invalid_percent',
    }
    rows = []
    with path.open(errors='replace') as stream:
        for line in stream:
            match = pattern.match(line)
            if not match:
                continue
            stamp, ftl, message = match.groups()
            try:
                instant = dt.datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S.%f').replace(tzinfo=timezone)
            except ValueError:
                continue
            elapsed = instant.timestamp() - start_ms / 1000.0
            for label, key in fields.items():
                value = re.search(re.escape(label) + r':\s*([0-9]+(?:\.[0-9]+)?)', message)
                if value:
                    rows.append({'t': elapsed, 'ftl': ftl, 'key': key, 'value': float(value[1])})
                    break
    return rows


def read_fio_series(guest, kind, start_ms, time_base, job='read'):
    # Generated profile sets log_unix_epoch=1. Relative logs require an explicit CLI choice.
    files = sorted(p for p in guest.glob('*_%s.*.log' % kind) if 'hist' not in p.name and job in p.name.lower())
    rows = []
    for path in files:
        for line in path.open(errors='replace'):
            cells = [c.strip() for c in line.split(',')]
            try:
                stamp, value, direction = float(cells[0]), float(cells[1]), int(cells[2])
            except (ValueError, IndexError):
                continue
            if direction == 0 and math.isfinite(value):
                elapsed = (stamp - start_ms) / 1000.0 if time_base == 'unix_ms' else stamp / 1000.0
                rows.append((elapsed, value))
    return files, rows


def full_run_json(guest):
    names = ['fio.json', 'profile.json', 'fio-result.json', 'result.json']
    candidates = [guest / n for n in names if (guest / n).exists()]
    if not candidates:
        candidates = [p for p in guest.glob('*.json') if p.name != 'summary.json']
    for path in candidates:
        try:
            value = json.loads(path.read_text())
            if 'jobs' in value:
                return path.name, value
        except (ValueError, OSError):
            pass
    return None, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('guest_run_dir', type=Path)
    parser.add_argument('host_run_dir', type=Path)
    parser.add_argument('--output-dir', type=Path, help='Defaults to guest run dir / analysis')
    parser.add_argument('--spdk-utc-offset', required=True, help='Host log offset at run time, e.g. -04:00 (use --spdk-utc-offset=-04:00)')
    parser.add_argument('--ftl-name', default='FTL0')
    parser.add_argument('--minimum-free-bands', type=int, default=5)
    parser.add_argument('--fio-time-base', choices=['unix_ms', 'elapsed_ms'], default='unix_ms', help='Generated profile uses unix_ms; select elapsed_ms only for explicitly relative fio logs')
    parser.add_argument('--hist-layout', choices=['auto', 'fio-3.7'], default='auto', help='auto requires fio.version or JSON confirming fio-3.7; explicit override requires independent layout verification')
    parser.add_argument('--histogram-copy-audited', action='store_true', help='Only for a source/binary audited fix of the fio-3.7 half-copy bug; format/version alone is insufficient')
    args = parser.parse_args()
    offset = re.fullmatch(r'([+-])(\d{2}):(\d{2})', args.spdk_utc_offset)
    if not offset or int(offset[2]) > 23 or int(offset[3]) > 59:
        parser.error('Invalid SPDK UTC offset')
    delta = dt.timedelta(hours=int(offset[2]), minutes=int(offset[3]))
    timezone = dt.timezone(delta if offset[1] == '+' else -delta)
    guest, host = args.guest_run_dir, args.host_run_dir
    req = read_manifest(guest / 'request.tsv')
    if req.get('schema') != '1':
        raise ValueError('Unsupported request schema')
    run_id = req['run_id']
    runtime, gc_start, threshold = (float(req[k]) for k in ['runtime', 'gc_start', 'gc_threshold'])
    if runtime < gc_start + 240 or gc_start < 240:
        raise ValueError('Fixed 180 s windows require gc_start >= 240 and runtime >= gc_start + 240')
    marker = (guest / 'started.tsv').read_text().strip().split('\t')
    if len(marker) != 2 or marker[0] != run_id:
        raise ValueError('started.tsv run ID mismatch')
    start_ms = int(marker[1])
    if (host / 'scheduled.tsv').exists():
        scheduled = (host / 'scheduled.tsv').read_text().strip().split('\t')
        if len(scheduled) != 2 or scheduled[0] != run_id or abs(int(scheduled[1]) - start_ms) >= 1000:
            raise ValueError('Guest start differs from the scheduled host start by at least one second')
    if (host / 'request.tsv').exists() and read_manifest(host / 'request.tsv') != req:
        raise ValueError('Guest and host requests differ')
    if (host / 'started.tsv').exists() and (host / 'started.tsv').read_text().strip() != '\t'.join(marker):
        raise ValueError('Guest and host start markers differ')
    events = []
    if (host / 'control.csv').exists():
        with (host / 'control.csv').open() as f:
            for row in csv.DictReader(f):
                row['relative_seconds'] = (int(row['epoch_ms']) - start_ms) / 1000.0
                events.append(row)
    warnings = []
    if not (host / 'controller.complete').exists():
        warnings.append('Host controller completion marker missing; run may be incomplete/aborted.')
    if (host / 'controller.aborted').exists():
        warnings.append('Host controller aborted.')
    finished = guest / 'finished.tsv'
    if not finished.exists():
        warnings.append('Guest finished marker missing.')
    else:
        fields = finished.read_text().strip().split('\t')
        if len(fields) != 3 or fields[0] != run_id or fields[1] != '0':
            warnings.append('Guest completion marker invalid or fio failed.')
    if not any(e['event'] == 'rpc_success' and e.get('detail') == 'gc_on' for e in events):
        warnings.append('No successful GC-on event recorded.')
    spdk = parse_spdk(host / 'spdk.log', start_ms, timezone)
    spdk = [row for row in spdk if row['ftl'] == args.ftl_name]
    bw_files, bw_rows = read_fio_series(guest, 'bw', start_ms, args.fio_time_base)
    iops_files, iops_rows = read_fio_series(guest, 'iops', start_ms, args.fio_time_base)
    hist_files = sorted(guest.glob('*hist*.log'))
    source_json, fio_json = full_run_json(guest)
    version_path = guest / 'fio.version'
    fio_version = version_path.read_text().strip() if version_path.exists() else (fio_json or {}).get('fio version')
    supported_hist = (args.hist_layout == 'fio-3.7' or fio_version == 'fio-3.7') and args.histogram_copy_audited
    if fio_version == 'fio-3.7' and not args.histogram_copy_audited:
        warnings.append('fio-3.7 histogram upper half is not trustworthy without an audited copy fix. Use separate phase JSONs via start_sweep.sh/analyze_sweep.py.')
    read_hist_files = [p for p in hist_files if 'foreground_read' in p.name]
    windows = [('baseline', 60.0, 240.0), ('gc', gc_start + 60.0, gc_start + 240.0)]
    summaries = {}
    for name, lower, upper in windows:
        rows = [r for r in spdk if lower <= r['t'] < upper]
        stats = {}
        for key in ['write_mib_s', 'compaction_mib_s', 'gc_mib_s', 'free_bands', 'invalid_percent', 'max_invalid_percent']:
            values = [r['value'] for r in rows if r['key'] == key]
            stats[key] = {'n': len(values), 'mean': mean(values), 'min': min(values) if values else None, 'max': max(values) if values else None}
        gc_values = [r['value'] for r in rows if r['key'] == 'gc_mib_s']
        free = [r['value'] for r in rows if r['key'] == 'free_bands']
        write = [r['value'] for r in rows if r['key'] == 'write_mib_s']
        comp = [r['value'] for r in rows if r['key'] == 'compaction_mib_s']
        bw = [v for t, v in bw_rows if lower <= t < upper]
        iops = [v for t, v in iops_rows if lower <= t < upper]
        checks = {
            'observed_gc_zero' if name == 'baseline' else 'observed_gc_nonzero': (all(v == 0 for v in gc_values) if name == 'baseline' else any(v > 0 for v in gc_values)) if gc_values else None,
            'free_bands_above_necessary_gc_limit': all(v > args.minimum_free_bands for v in free) if free else None,
            'printed_write_below_positive_threshold': all(v < threshold for v in write) if write else None,
            'printed_compaction_below_positive_threshold': all(v < threshold for v in comp) if comp else None,
        }
        for label, value in checks.items():
            if value is not True:
                warnings.append('%s: %s is %s.' % (name, label, value))
        for key in ['gc_mib_s', 'free_bands', 'write_mib_s', 'compaction_mib_s']:
            if stats[key]['n'] < 150:
                warnings.append('%s: sparse SPDK samples for %s (%d); inspect log coverage.' % (name, key, stats[key]['n']))
        summaries[name] = {
            'window_seconds': [lower, upper], 'spdk': stats, 'checks': checks,
            'read_bw_log': {'files': [p.name for p in bw_files], 'n': len(bw), 'mean_KiB_s': mean(bw), 'mean_MiB_s': mean(bw) / 1024 if bw else None},
            'read_iops_log': {'files': [p.name for p in iops_files], 'n': len(iops), 'mean_iops': mean(iops)},
            'read_p99_ns': None, 'read_p999_ns': None,
            'latency_status': 'missing: histogram bin units/version are not inferred; full-run JSON cannot supply phase p99',
            'qlc_foreground_read_fraction': None, 'qlc_route_status': 'unknown: no dedicated foreground TLC/QLC route counters supplied',
        }
        if supported_hist and read_hist_files:
            try:
                latency = merge_window(read_hist_files, lower, upper, start_ms, args.fio_time_base)
                summaries[name]['read_histogram'] = latency
                summaries[name]['read_p99_ns'] = latency['p99_ns']
                summaries[name]['read_p999_ns'] = latency['p999_ns']
                summaries[name]['latency_status'] = latency['status']
            except (ValueError, OSError) as error:
                summaries[name]['latency_status'] = 'missing_invalid_histogram: ' + str(error)
        if summaries[name]['read_p99_ns'] is None:
            warnings.append('%s: phase latency missing; inspect histogram layout/version and coverage.' % name)
    if not bw_rows:
        warnings.append('No readable read bandwidth logs found.')
    if not iops_rows:
        warnings.append('No readable read IOPS logs found.')
    warnings.append('Printed per-second F/C checks are proxies; exact GC decisions use smoothed avg_bw/user_avg_bw, absent from these observations.')
    summary = {
        'schema': 1, 'run_id': run_id, 'request': req, 'started_epoch_ms': start_ms,
        'spdk_utc_offset': args.spdk_utc_offset, 'fio_time_base': args.fio_time_base, 'ftl_name': args.ftl_name, 'windows': summaries,
        'control_events': events, 'histogram_files': [p.name for p in hist_files],
        'fio_version': fio_version, 'histogram_layout': args.hist_layout,
        'full_run_fio_json': {'source': source_json, 'data': fio_json},
        'warnings': warnings,
        'assessment': 'requires_qlc_route_and_gc_read_validation',
        'notes': ['No experiment success or latency improvement is assumed.', 'IOPS/BW sample means require uniform fio log_avg_msec; intervals crossing a window edge need separate treatment.', 'All read series use fio direction 0 and file names containing read; channels/jobs are not silently merged into latency percentiles.'],
    }
    output = args.output_dir or guest / 'analysis'
    output.mkdir(parents=True, exist_ok=True)
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    with (output / 'summary.tsv').open('w', newline='') as f:
        writer = csv.writer(f, delimiter='\t')
        writer.writerow(['run_id', 'phase', 'start_s', 'end_s', 'read_iops', 'read_MiB_s', 'read_p99_ns', 'read_p999_ns', 'gc_MiB_s', 'write_MiB_s', 'compaction_MiB_s', 'free_bands_min', 'qlc_route'])
        for name, phase in summaries.items():
            s = phase['spdk']
            writer.writerow([run_id, name, *phase['window_seconds'], phase['read_iops_log']['mean_iops'], phase['read_bw_log']['mean_MiB_s'], phase['read_p99_ns'], phase['read_p999_ns'], s['gc_mib_s']['mean'], s['write_mib_s']['mean'], s['compaction_mib_s']['mean'], s['free_bands']['min'], 'unknown'])
    print(output / 'summary.json')


if __name__ == '__main__':
    main()
