#!/usr/bin/env python3
"""Conservative read-path evidence from four already-collected SPDK snapshots.

No IO is performed here. Blocks use the caller's independently verified block
size. FTL/bdev snapshots are not atomic: unexplained discrepancies fail closed.
"""
import math


FTL_GROUPS = ('user', 'cmp', 'gc', 'md_base', 'md_nv_cache', 'l2p')
BDEV_COUNTERS = ('bytes_read', 'num_read_ops', 'bytes_written', 'num_write_ops',
                 'bytes_unmapped', 'num_unmap_ops', 'read_latency_ticks',
                 'write_latency_ticks', 'unmap_latency_ticks')
REQUIRED_BDEV_COUNTERS = BDEV_COUNTERS[:4]
ERROR_KINDS = ('media', 'crc', 'other')
BYTE_TOLERANCE = 0


class EvidenceError(ValueError):
    pass


def _integer(value, label, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise EvidenceError('invalid_counter_or_unit: ' + label)
    return value


def _snapshot(value, names, ftl_name, label):
    if not isinstance(value, dict) or not isinstance(value.get('bdev'), dict) or not isinstance(value.get('ftl'), dict):
        raise EvidenceError('invalid_snapshot: ' + label)
    bdev, ftl = value['bdev'], value['ftl']
    frequency = _integer(bdev.get('tick_rate'), label + '.tick_rate', positive=True)
    ticks = _integer(bdev.get('ticks'), label + '.ticks')
    devices = bdev.get('bdevs')
    if not isinstance(devices, list):
        raise EvidenceError('missing_bdev_inventory: ' + label)
    selected = {}
    for device in devices:
        if not isinstance(device, dict):
            raise EvidenceError('invalid_bdev_entry: ' + label)
        name = device.get('name')
        if name not in names:
            continue
        if name in selected:
            raise EvidenceError('duplicate_bdev_name: ' + label + '.' + name)
        for key in REQUIRED_BDEV_COUNTERS:
            if key not in device:
                raise EvidenceError('missing_counter: ' + label + '.' + name + '.' + key)
        selected[name] = {key: _integer(device[key], label + '.' + name + '.' + key)
                          for key in BDEV_COUNTERS if key in device}
    if set(selected) != set(names):
        raise EvidenceError('missing_bdev: ' + label)
    if ftl.get('name') != ftl_name:
        raise EvidenceError('ftl_name_mismatch: ' + label)
    groups = {}
    # Every standard class is required. Also account for future classes instead
    # of silently discarding their reads from the conservative subtraction.
    extra = [key for key, item in ftl.items()
             if key not in FTL_GROUPS and isinstance(item, dict)
             and ('read' in item or 'write' in item)]
    for group in FTL_GROUPS + tuple(extra):
        if not isinstance(ftl.get(group), dict):
            raise EvidenceError('missing_ftl_class: ' + label + '.' + group)
        groups[group] = {}
        for direction in ('read', 'write'):
            item = ftl[group].get(direction)
            if not isinstance(item, dict) or not isinstance(item.get('errors'), dict):
                raise EvidenceError('missing_ftl_direction_or_errors: ' + label + '.' + group + '.' + direction)
            values = {key: _integer(item.get(key), label + '.' + group + '.' + direction + '.' + key)
                      for key in ('ios', 'blocks')}
            for key in ERROR_KINDS:
                # This RPC serializes read CRC errors, but write normally only
                # has media/other. A missing write.crc means the valid schema's
                # unexposed field, not a guessed missing read/error counter.
                value = item['errors'].get(key, 0) if direction == 'write' and key == 'crc' else item['errors'].get(key)
                values['errors.' + key] = _integer(value, label + '.' + group + '.' + direction + '.errors.' + key)
            groups[group][direction] = values
    return {'tick_rate': frequency, 'ticks': ticks, 'bdevs': selected, 'groups': groups}


def _subtract(before, after, label):
    if before['tick_rate'] != after['tick_rate']:
        raise EvidenceError('tick_rate_changed: ' + label)
    if after['ticks'] <= before['ticks']:
        raise EvidenceError('nonpositive_or_reversed_time: ' + label)
    if set(before['groups']) != set(after['groups']):
        raise EvidenceError('ftl_classes_changed: ' + label)
    result = {'duration_seconds': (after['ticks'] - before['ticks']) / before['tick_rate'],
              'tick_rate': before['tick_rate'], 'bdevs': {}, 'groups': {}}
    for name in before['bdevs']:
        if set(before['bdevs'][name]) != set(after['bdevs'][name]):
            raise EvidenceError('bdev_counter_schema_changed: ' + label + '.' + name)
        result['bdevs'][name] = {}
        for key, old in before['bdevs'][name].items():
            new = after['bdevs'][name][key]
            if new < old:
                raise EvidenceError('counter_reset_or_regression: ' + label + '.' + name + '.' + key)
            result['bdevs'][name][key] = new - old
    for group in before['groups']:
        result['groups'][group] = {}
        for direction in ('read', 'write'):
            result['groups'][group][direction] = {}
            for key, old in before['groups'][group][direction].items():
                new = after['groups'][group][direction][key]
                if new < old:
                    raise EvidenceError('counter_reset_or_regression: ' + label + '.' + group + '.' + direction + '.' + key)
                result['groups'][group][direction][key] = new - old
    return result


def summarize(before, after, fio_json, idle_before, idle_after, block_size=4096,
              ftl_name='FTL0', tlc_name='nvme0n1', qlc_name='nvme1n1',
              minimum_qlc_fraction=.95, expected_io_bytes=64 * 1024 * 1024):
    """Return JSON-serializable evidence with a ``passed`` boolean.

    idle_before/idle_after bracket an independent no-fio idle observation;
    before/after bracket the read-only probe. No automatic unit inference,
    counter reset compensation, or operation-count equivalence is applied.
    """
    result = {
        'schema': 1, 'passed': False, 'reasons': [], 'checks': {}, 'metrics': {},
        'scope': 'Only the sampled read probe; not the whole address range or a later formal experiment.',
        'units': {'bdev_bytes': 'bytes', 'ftl_blocks_bytes': None,
                  'latency_counters': 'ticks', 'sampling_tolerance_bytes': BYTE_TOLERANCE},
        'limitations': [
            'FTL and bdev snapshots are non-atomic; unaccounted discrepancies fail closed.',
            'All non-USER reads, including TLC metadata reads, are subtracted from QLC total reads; this is a conservative lower bound.',
            'The caller must independently verify namespace identities, block sizes, and absence of other foreground readers.',
            'Read operations may split or merge; byte totals, not a one-to-one operation count, establish coverage.',
        ],
    }

    def check(name, condition, reason):
        result['checks'][name] = bool(condition)
        if not condition and reason not in result['reasons']:
            result['reasons'].append(reason)

    try:
        block_size = _integer(block_size, 'block_size', positive=True)
        result['units']['ftl_blocks_bytes'] = block_size
        expected_io_bytes = _integer(expected_io_bytes, 'expected_io_bytes', positive=True)
        if expected_io_bytes % block_size:
            raise EvidenceError('expected_bytes_not_block_aligned')
        if (isinstance(minimum_qlc_fraction, bool) or not isinstance(minimum_qlc_fraction, (int, float))
                or not math.isfinite(minimum_qlc_fraction) or not 0 <= minimum_qlc_fraction <= 1):
            raise EvidenceError('invalid_minimum_qlc_fraction')
        names = (ftl_name, tlc_name, qlc_name)
        if any(not isinstance(n, str) or not n for n in names) or len(set(names)) != 3:
            raise EvidenceError('invalid_or_duplicate_device_names')
        raw = [(before, 'before'), (after, 'after'), (idle_before, 'idle_before'), (idle_after, 'idle_after')]
        values = [_snapshot(item, names, ftl_name, label) for item, label in raw]
        if len(set(v['tick_rate'] for v in values)) != 1:
            raise EvidenceError('tick_rate_inconsistent_across_snapshots')
        probe = _subtract(values[0], values[1], 'probe')
        idle = _subtract(values[2], values[3], 'idle')
        result['metrics']['probe_delta'] = probe
        result['metrics']['idle_delta'] = idle
        if not isinstance(fio_json, dict) or not isinstance(fio_json.get('jobs'), list) or len(fio_json['jobs']) != 1:
            raise EvidenceError('fio_must_have_exactly_one_job')
        job = fio_json['jobs'][0]
        if not isinstance(job, dict) or not isinstance(job.get('read'), dict) or not isinstance(job.get('write'), dict):
            raise EvidenceError('invalid_fio_job')
        error = _integer(job.get('error'), 'fio.error')
        read_bytes = _integer(job['read'].get('io_bytes'), 'fio.read.io_bytes')
        write_bytes = _integer(job['write'].get('io_bytes'), 'fio.write.io_bytes')
        check('fio_success', error == 0, 'fio_error_nonzero')
        check('fio_read_only', write_bytes == 0, 'fio_wrote_data')
        check('fio_exact_sample_size', read_bytes == expected_io_bytes, 'fio_sample_size_mismatch')
        logical = probe['bdevs'][ftl_name]['bytes_read']
        user_physical = probe['groups']['user']['read']['blocks'] * block_size
        non_user = sum(group['read']['blocks'] * block_size
                       for name, group in probe['groups'].items() if name != 'user')
        qlc = probe['bdevs'][qlc_name]['bytes_read']
        tlc = probe['bdevs'][tlc_name]['bytes_read']
        physical = qlc + tlc
        lower_bound = max(0, qlc - non_user)
        coverage = user_physical / logical if logical else None
        qlc_fraction = lower_bound / read_bytes if read_bytes else None
        result['metrics'].update({
            'expected_io_bytes': expected_io_bytes, 'expected_sample_blocks': expected_io_bytes // block_size,
            'fio_read_bytes': read_bytes, 'fio_write_bytes': write_bytes,
            'ftl_logical_read_bytes': logical, 'user_physical_read_bytes': user_physical,
            'tlc_total_read_bytes': tlc, 'qlc_total_read_bytes': qlc,
            'non_user_read_bytes_all_devices': non_user, 'physical_total_read_bytes': physical,
            'classified_physical_read_bytes': user_physical + non_user,
            'physical_minus_classified_read_bytes': physical - user_physical - non_user,
            'user_physical_to_logical_fraction': coverage,
            'sample_qlc_foreground_lower_bound_bytes': lower_bound,
            'sample_qlc_foreground_lower_bound_fraction': qlc_fraction,
            'minimum_qlc_fraction': minimum_qlc_fraction,
            'formal_run_qlc_fraction': None,
        })
        check('logical_read_matches_fio', abs(logical - read_bytes) <= BYTE_TOLERANCE, 'ftl_logical_read_fio_mismatch')
        check('physical_user_read_coverage', coverage is not None and coverage >= .99, 'user_read_coverage_below_minimum_possible_unmapped_lbas')
        check('physical_user_read_not_excessive', user_physical <= logical + BYTE_TOLERANCE, 'user_physical_reads_exceed_logical_read')
        check('physical_read_classification_complete', abs(physical - user_physical - non_user) <= BYTE_TOLERANCE, 'uncategorized_physical_read')
        check('qlc_lower_bound_not_excessive', lower_bound <= read_bytes + BYTE_TOLERANCE, 'qlc_foreground_lower_bound_exceeds_fio_bytes')
        check('sample_qlc_lower_bound_sufficient', qlc_fraction is not None and qlc_fraction >= minimum_qlc_fraction, 'sample_qlc_lower_bound_below_minimum')
        for label, delta in [('probe', probe), ('idle', idle)]:
            logical_io = delta['bdevs'][ftl_name]
            user_io = delta['groups']['user']
            check(label + '_no_logical_write', logical_io['bytes_written'] == 0 and logical_io['num_write_ops'] == 0,
                  label + '_ftl_logical_write_present')
            check(label + '_no_user_write', user_io['write']['blocks'] == 0 and user_io['write']['ios'] == 0,
                  label + '_user_write_present')
            for kind in ('gc', 'cmp'):
                quiet = all(delta['groups'][kind][direction][counter] == 0
                            for direction in ('read', 'write') for counter in ('ios', 'blocks'))
                check(label + '_no_' + kind, quiet, label + '_' + kind + '_activity_present')
            errors = sum(counters[key] for group in delta['groups'].values()
                         for counters in group.values() for key in counters if key.startswith('errors.'))
            check(label + '_no_ftl_errors', errors == 0, label + '_ftl_error_counter_increased')
        check('idle_no_logical_read', idle['bdevs'][ftl_name]['bytes_read'] == 0 and idle['bdevs'][ftl_name]['num_read_ops'] == 0,
              'idle_ftl_logical_read_present')
        check('idle_no_user_read', idle['groups']['user']['read']['blocks'] == 0 and idle['groups']['user']['read']['ios'] == 0,
              'idle_user_read_present')
        idle_physical = idle['bdevs'][qlc_name]['bytes_read'] + idle['bdevs'][tlc_name]['bytes_read']
        idle_classified = sum(group['read']['blocks'] * block_size for group in idle['groups'].values())
        result['metrics']['idle_physical_total_read_bytes'] = idle_physical
        result['metrics']['idle_classified_read_bytes'] = idle_classified
        check('idle_physical_read_classification_complete', abs(idle_physical - idle_classified) <= BYTE_TOLERANCE,
              'idle_uncategorized_physical_read')
        result['passed'] = not result['reasons'] and all(result['checks'].values())
    except (EvidenceError, KeyError, TypeError, ValueError, OverflowError) as exc:
        result['reasons'].append(str(exc))
        result['passed'] = False
    return result
