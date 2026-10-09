"""Merge fio 3.7 coarse=0 completion histograms into window percentiles.

Layout/units checked against upstream fio-3.7 stat.h/stat.c/iolog.c:
1856 nanosecond bins, prefix timestamp_ms,direction,block_size. Counts in
the written log are interval deltas, not cumulative counters. Intervals
crossing either analysis boundary are excluded rather than fractionally
inventing IO counts. No third-party Python packages are required.
"""
import csv
import math

BIN_COUNT = 29 * 64


def bin_midpoint_ns(index):
    if not 0 <= index < BIN_COUNT:
        raise ValueError('Histogram index outside fio 3.7 layout')
    if index < 128:
        return index
    error_bits = (index >> 6) - 1
    return (1 << (error_bits + 6)) + (index % 64) * (1 << error_bits) + (1 << (error_bits - 1))


def percentiles(counts):
    if len(counts) != BIN_COUNT or any(c < 0 for c in counts):
        raise ValueError('Invalid fio 3.7 histogram')
    total = sum(counts)
    if not total:
        return {key: None for key in ['p50_ns', 'p99_ns', 'p999_ns']}
    targets = [('p50_ns', math.ceil(total * 0.50)),
               ('p99_ns', math.ceil(total * 0.99)),
               ('p999_ns', math.ceil(total * 0.999))]
    result, accumulated = {}, 0
    for index, count in enumerate(counts):
        accumulated += count
        for name, target in targets:
            if name not in result and accumulated >= target:
                result[name] = bin_midpoint_ns(index)
    return result


def merge_window(paths, lower_s, upper_s, started_ms, time_base='unix_ms'):
    counts = [0] * BIN_COUNT
    rows_used = 0
    coverage_ms = 0
    boundary_rows = 0
    for path in paths:
        previous = None
        with path.open() as stream:
            for number, row in enumerate(csv.reader(stream), 1):
                if not row:
                    continue
                if len(row) != BIN_COUNT + 3:
                    raise ValueError('%s:%s: expected 1856 bins with 3 prefix columns' % (path.name, number))
                values = [int(cell.strip()) for cell in row]
                stamp, direction, block_size = values[:3]
                if direction != 0:
                    continue
                if block_size <= 0 or any(c < 0 for c in values[3:]):
                    raise ValueError('%s:%s: invalid histogram count/block size' % (path.name, number))
                elapsed_ms = stamp - started_ms if time_base == 'unix_ms' else stamp
                if previous is not None and elapsed_ms <= previous:
                    raise ValueError('%s:%s: non-increasing histogram timestamps' % (path.name, number))
                # The first record has no observed interval start, so skip it.
                if previous is not None and previous >= lower_s * 1000 and elapsed_ms <= upper_s * 1000:
                    for index, value in enumerate(values[3:]):
                        counts[index] += value
                    rows_used += 1
                    coverage_ms += elapsed_ms - previous
                elif lower_s * 1000 < elapsed_ms <= upper_s * 1000:
                    boundary_rows += 1
                previous = elapsed_ms
    return {'status': 'computed_fio_3.7_ns' if sum(counts) else 'missing_no_complete_histogram_intervals',
            'files': [path.name for path in paths], 'samples': sum(counts),
            'intervals': rows_used, 'covered_ms': coverage_ms,
            'boundary_or_first_rows_excluded': boundary_rows,
            'method': 'sum_interval_bins_then_select_percentile_bucket_midpoint',
            **percentiles(counts)}
