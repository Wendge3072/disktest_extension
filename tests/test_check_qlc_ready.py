"""Binding of complete QLC range evidence to a formal read/write profile."""
import configparser
import copy
import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / 'gc_inf_onRead' / 'check_qlc_ready.py'
SPEC = importlib.util.spec_from_file_location('check_qlc_ready', MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

GIB = 1024 ** 3
CAPACITY = 14541900267520
READ_OFFSET = 2048 * GIB
READ_SIZE = 1024 * GIB
PREFIX_SIZE = 1700 * GIB
UUID = 'b2868f47-529b-47bb-91e9-697b03614975'
BINARY_HASH = 'b3' * 32


def proof_fixture():
    return {
        'schema': 2, 'passed': True, 'complete': True,
        'capacity_bytes': CAPACITY,
        'prefix_size_bytes': PREFIX_SIZE,
        'read_floor_bytes': READ_OFFSET,
        'read_profile': {'device': '/dev/vda', 'offset': READ_OFFSET, 'size': READ_SIZE},
        'exact_checks': {
            'independently_audited_binary_pinned': True,
            'complete_selected_range_read': True,
            'every_requested_byte_has_mapped_user_data': True,
            'only_l2p_non_user_reads': True,
            'all_user_read_bytes_on_qlc': True,
            'tlc_reads_exactly_equal_l2p_metadata': True,
            'conservative_evidence_valid_except_target_coverage': True,
        },
        'scan_metrics': {
            'fio_read_bytes': READ_SIZE,
            'ftl_logical_read_bytes': READ_SIZE,
            'user_physical_read_bytes': READ_SIZE,
            'qlc_total_read_bytes': READ_SIZE,
        },
        'ftl_name': 'FTL0', 'bdev_identity': {'FTL0': {'uuid': UUID}},
        'binary_sha256': BINARY_HASH,
    }


def profile_fixture():
    profile = configparser.ConfigParser(interpolation=None)
    profile.read_dict({
        'global': {'filename': '/dev/vda', 'direct': '1', 'numjobs': '1',
                   'ioengine': 'libaio', 'time_based': '1', 'runtime': '600'},
        'foreground_read': {'rw': 'randread', 'bs': '4k', 'offset': str(READ_OFFSET),
                            'size': str(READ_SIZE), 'rate_iops': '25000',
                            'randseed': '2252', 'iodepth': '32'},
        'foreground_write': {'rw': 'randwrite', 'bs': '64k', 'offset': '0',
                             'size': str(320 * GIB), 'io_size': str(320 * GIB),
                             'rate': ',512m', 'iodepth': '128'},
    })
    return profile


class CheckQlcReadyTests(unittest.TestCase):
    def assert_rejected(self, proof, profile=None, capacity=CAPACITY):
        with self.assertRaises((ValueError, KeyError, TypeError)):
            MODULE.validate(proof, profile or profile_fixture(), capacity)

    def test_complete_scan_can_be_reused_for_different_read_rates_and_seeds(self):
        proof = proof_fixture()
        original = copy.deepcopy(proof)
        profile = profile_fixture()
        self.assertEqual(MODULE.validate(proof, profile, CAPACITY), (UUID, BINARY_HASH))
        profile['foreground_read']['rate_iops'] = '50000'
        profile['foreground_read']['randseed'] = '9991'
        self.assertEqual(MODULE.validate(proof, profile, CAPACITY), (UUID, BINARY_HASH))
        self.assertEqual(proof, original)

    def test_incomplete_or_failed_scan_cannot_authorize_formal_io(self):
        for key in ('passed', 'complete'):
            with self.subTest(state=key):
                proof = proof_fixture()
                proof[key] = False
                self.assert_rejected(proof)

    def test_partial_reads_or_sampling_checks_do_not_prove_the_whole_range(self):
        for mutation in ('missing_complete_check', 'partial_fio', 'unmapped_user_data',
                         'some_user_data_on_tlc'):
            with self.subTest(failure=mutation):
                proof = proof_fixture()
                if mutation == 'missing_complete_check':
                    del proof['exact_checks']['complete_selected_range_read']
                else:
                    metric = {'partial_fio': 'fio_read_bytes',
                              'unmapped_user_data': 'user_physical_read_bytes',
                              'some_user_data_on_tlc': 'qlc_total_read_bytes'}[mutation]
                    proof['scan_metrics'][metric] -= 4096
                self.assert_rejected(proof)

    def test_evidence_is_bound_to_the_capacity_device_and_exact_read_range(self):
        proof = proof_fixture()
        self.assert_rejected(proof, capacity=CAPACITY - 4096)
        for key, value in (('offset', str(READ_OFFSET + 4096)),
                           ('size', str(READ_SIZE - 4096)), ('filename', '/dev/vdb')):
            with self.subTest(changed=key):
                profile = profile_fixture()
                profile['foreground_read'][key] = value
                self.assert_rejected(proof, profile)

    def test_formal_writes_cannot_leave_the_prepared_low_address_prefix(self):
        for offset in (PREFIX_SIZE - 320 * GIB + 4096, CAPACITY - 320 * GIB):
            with self.subTest(write_offset=offset):
                profile = profile_fixture()
                profile['foreground_write']['offset'] = str(offset)
                self.assert_rejected(proof_fixture(), profile)

    def test_invalid_prefix_and_protected_read_floor_cannot_authorize_io(self):
        for change in ({'prefix_size_bytes': READ_OFFSET},
                       {'read_floor_bytes': READ_OFFSET + 4096},
                       {'prefix_size_bytes': True}):
            with self.subTest(change=change):
                proof = proof_fixture()
                proof.update(change)
                self.assert_rejected(proof)

    def test_missing_or_invalid_device_identity_and_binary_pin_are_rejected(self):
        for mutation in ('missing_identity', 'missing_uuid', 'missing_binary_hash',
                         'invalid_binary_hash'):
            with self.subTest(failure=mutation):
                proof = proof_fixture()
                if mutation == 'missing_identity':
                    del proof['bdev_identity']
                elif mutation == 'missing_uuid':
                    del proof['bdev_identity']['FTL0']['uuid']
                elif mutation == 'missing_binary_hash':
                    del proof['binary_sha256']
                else:
                    proof['binary_sha256'] = 'unknown'
                self.assert_rejected(proof)


if __name__ == '__main__':
    unittest.main()
