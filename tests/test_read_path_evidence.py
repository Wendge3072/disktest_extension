"""Synthetic evidence cases; these never issue IO or RPCs."""
import copy
import importlib.util
import json
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / 'gc_inf_onRead' / 'read_path_evidence.py'
SPEC = importlib.util.spec_from_file_location('read_path_evidence', MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
summarize = MODULE.summarize
SIZE = 64 * 1024 * 1024
BLOCK = 4096


def snapshot(ticks):
    bdevs = []
    for name in ('FTL0', 'nvme0n1', 'nvme1n1'):
        device = {'name': name}
        device.update({key: 0 for key in MODULE.BDEV_COUNTERS})
        bdevs.append(device)
    ftl = {'name': 'FTL0'}
    for name in MODULE.FTL_GROUPS:
        ftl[name] = {}
        for direction in ('read', 'write'):
            errors = {'media': 0, 'other': 0}
            if direction == 'read':
                errors['crc'] = 0
            ftl[name][direction] = {'ios': 0, 'blocks': 0, 'errors': errors}
    return {'bdev': {'ticks': ticks, 'tick_rate': 1000000000, 'bdevs': bdevs}, 'ftl': ftl}


def bdev(s, name):
    return next(d for d in s['bdev']['bdevs'] if d['name'] == name)


def fixture():
    before = snapshot(2000000000)
    after = snapshot(3000000000)
    bdev(after, 'FTL0').update(bytes_read=SIZE, num_read_ops=SIZE // BLOCK)
    # Bottom-layer operation counts intentionally differ from the fio count.
    bdev(after, 'nvme1n1').update(bytes_read=SIZE, num_read_ops=71)
    after['ftl']['user']['read'].update(blocks=SIZE // BLOCK, ios=71)
    fio = {'jobs': [{'error': 0, 'read': {'io_bytes': SIZE}, 'write': {'io_bytes': 0}}]}
    return before, after, fio, snapshot(0), snapshot(1000000000)


class ReadPathEvidenceTests(unittest.TestCase):
    def evaluate(self, values):
        result = summarize(*values)
        json.dumps(result, allow_nan=False)
        return result

    def test_all_qlc_read_passes_and_preserves_inputs(self):
        values = fixture()
        original = copy.deepcopy(values)
        result = self.evaluate(values)
        self.assertTrue(result['passed'], result['reasons'])
        self.assertEqual(result['metrics']['sample_qlc_foreground_lower_bound_fraction'], 1)
        self.assertIsNone(result['metrics']['formal_run_qlc_fraction'])
        self.assertEqual(values, original)

    def test_unmapped_lbas_returning_zero_do_not_pass(self):
        values = fixture()
        bdev(values[1], 'nvme1n1')['bytes_read'] = SIZE // 2
        values[1]['ftl']['user']['read']['blocks'] = SIZE // BLOCK // 2
        result = self.evaluate(values)
        self.assertFalse(result['passed'])
        self.assertIn('user_read_coverage_below_minimum_possible_unmapped_lbas', result['reasons'])

    def test_tlc_reads_are_physical_but_not_qlc_evidence(self):
        values = fixture()
        bdev(values[1], 'nvme1n1').update(bytes_read=0, num_read_ops=0)
        bdev(values[1], 'nvme0n1').update(bytes_read=SIZE, num_read_ops=71)
        result = self.evaluate(values)
        self.assertFalse(result['passed'])
        self.assertEqual(result['metrics']['user_physical_to_logical_fraction'], 1)
        self.assertIn('sample_qlc_lower_bound_below_minimum', result['reasons'])

    def test_gc_mixed_with_read_cannot_pass_despite_qlc_coverage(self):
        values = fixture()
        values[1]['ftl']['gc']['read'].update(blocks=1024, ios=2)
        values[1]['ftl']['gc']['write'].update(blocks=1024, ios=2)
        bdev(values[1], 'nvme1n1')['bytes_read'] += 1024 * BLOCK
        result = self.evaluate(values)
        self.assertFalse(result['passed'])
        self.assertEqual(result['metrics']['sample_qlc_foreground_lower_bound_fraction'], 1)
        self.assertIn('probe_gc_activity_present', result['reasons'])

    def test_counter_reset_fails_instead_of_subtracting_negative_bytes(self):
        values = fixture()
        bdev(values[0], 'nvme0n1')['bytes_read'] = BLOCK
        result = self.evaluate(values)
        self.assertFalse(result['passed'])
        self.assertTrue(any('counter_reset_or_regression' in reason for reason in result['reasons']))

    def test_tlc_metadata_reads_are_conservatively_subtracted(self):
        values = fixture()
        extra = 512 * BLOCK
        values[1]['ftl']['md_nv_cache']['read'].update(blocks=512, ios=512)
        bdev(values[1], 'nvme0n1').update(bytes_read=extra, num_read_ops=512)
        result = self.evaluate(values)
        self.assertTrue(result['passed'], result['reasons'])
        self.assertEqual(result['metrics']['sample_qlc_foreground_lower_bound_bytes'], SIZE - extra)

    def test_qlc_metadata_does_not_inflate_foreground_fraction(self):
        values = fixture()
        extra = 512 * BLOCK
        values[1]['ftl']['md_base']['read'].update(blocks=512, ios=512)
        bdev(values[1], 'nvme1n1')['bytes_read'] += extra
        result = self.evaluate(values)
        self.assertTrue(result['passed'], result['reasons'])
        self.assertEqual(result['metrics']['sample_qlc_foreground_lower_bound_bytes'], SIZE)

    def test_uncategorized_read_and_excess_lower_bound_fail(self):
        values = fixture()
        bdev(values[1], 'nvme1n1')['bytes_read'] += BLOCK
        result = self.evaluate(values)
        self.assertFalse(result['passed'])
        self.assertIn('uncategorized_physical_read', result['reasons'])
        self.assertIn('qlc_foreground_lower_bound_exceeds_fio_bytes', result['reasons'])

    def test_excess_user_bytes_are_not_clamped(self):
        values = fixture()
        values[1]['ftl']['user']['read']['blocks'] += 1
        bdev(values[1], 'nvme1n1')['bytes_read'] += BLOCK
        result = self.evaluate(values)
        self.assertFalse(result['passed'])
        self.assertIn('user_physical_reads_exceed_logical_read', result['reasons'])
        self.assertGreater(result['metrics']['user_physical_to_logical_fraction'], 1)

    def test_idle_foreground_read_is_not_ignored(self):
        values = fixture()
        bdev(values[4], 'FTL0').update(bytes_read=BLOCK, num_read_ops=1)
        values[4]['ftl']['user']['read'].update(blocks=1, ios=1)
        result = self.evaluate(values)
        self.assertFalse(result['passed'])
        self.assertIn('idle_ftl_logical_read_present', result['reasons'])
        self.assertIn('idle_user_read_present', result['reasons'])

    def test_write_fio_error_and_partial_job_are_rejected(self):
        for key, value, reason in [('error', 1, 'fio_error_nonzero'),
                                   ('read', {'io_bytes': SIZE - BLOCK}, 'fio_sample_size_mismatch'),
                                   ('write', {'io_bytes': BLOCK}, 'fio_wrote_data')]:
            with self.subTest(key=key):
                values = fixture()
                values[2]['jobs'][0][key] = value
                result = self.evaluate(values)
                self.assertFalse(result['passed'])
                self.assertIn(reason, result['reasons'])

    def test_time_frequency_and_missing_units_fail(self):
        for mutation, expected in [
            (lambda v: v[1]['bdev'].update(tick_rate=2), 'tick_rate_inconsistent_across_snapshots'),
            (lambda v: v[1]['bdev'].update(ticks=v[0]['bdev']['ticks']), 'nonpositive_or_reversed_time'),
            (lambda v: v[2]['jobs'][0]['read'].pop('io_bytes'), 'invalid_counter_or_unit'),
        ]:
            with self.subTest(expected=expected):
                values = fixture()
                mutation(values)
                result = self.evaluate(values)
                self.assertFalse(result['passed'])
                self.assertTrue(any(expected in reason for reason in result['reasons']))

    def test_multiple_fio_jobs_fail(self):
        values = fixture()
        values[2]['jobs'].append(copy.deepcopy(values[2]['jobs'][0]))
        result = self.evaluate(values)
        self.assertFalse(result['passed'])
        self.assertIn('fio_must_have_exactly_one_job', result['reasons'])

    def test_idle_uncategorized_physical_reads_are_rejected(self):
        values = fixture()
        bdev(values[4], 'nvme0n1')['bytes_read'] = BLOCK
        result = self.evaluate(values)
        self.assertFalse(result['passed'])
        self.assertIn('idle_uncategorized_physical_read', result['reasons'])

    def test_invalid_block_size_still_returns_serializable_failure(self):
        result = summarize(*fixture(), block_size=float('nan'))
        self.assertFalse(result['passed'])
        json.dumps(result, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
