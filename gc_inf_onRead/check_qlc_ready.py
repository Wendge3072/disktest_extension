#!/usr/bin/env python
"""Guest-side validation of the host's complete-range QLC evidence (Python 2.7/3)."""
from __future__ import print_function
try:
    import configparser
except ImportError:
    import ConfigParser as configparser
import json
import os
import re
import sys


def validate(proof, profile, capacity):
    if proof.get('schema') != 2 or proof.get('passed') is not True or proof.get('complete') is not True:
        raise ValueError('A completed, passing full-range QLC verification is required')
    checks = proof.get('exact_checks')
    required = ('independently_audited_binary_pinned', 'complete_selected_range_read',
                'every_requested_byte_has_mapped_user_data', 'only_l2p_non_user_reads',
                'all_user_read_bytes_on_qlc', 'tlc_reads_exactly_equal_l2p_metadata',
                'conservative_evidence_valid_except_target_coverage')
    if not isinstance(checks, dict) or any(checks.get(key) is not True for key in required):
        raise ValueError('Missing strict QLC checks; random-sample evidence is insufficient')
    if proof.get('capacity_bytes') != capacity:
        raise ValueError('Verified FTL capacity differs from the guest device')
    global_job = dict(profile.items('global'))
    read = dict(global_job, **dict(profile.items('foreground_read')))
    write = dict(global_job, **dict(profile.items('foreground_write')))
    verified = proof['read_profile']
    if (read.get('filename') != verified['device'] or int(read['offset']) != verified['offset']
            or int(read['size']) != verified['size'] or read.get('rw') != 'randread'
            or read.get('bs') != '4k' or read.get('direct') != '1'):
        raise ValueError('Formal read device/range differs from the completely verified range')
    prefix, floor = proof.get('prefix_size_bytes'), proof.get('read_floor_bytes')
    if (isinstance(prefix, bool) or isinstance(floor, bool)
            or not isinstance(prefix, (int, type(2**64)))
            or not isinstance(floor, (int, type(2**64)))
            or not 0 < prefix < floor <= verified['offset']):
        raise ValueError('Missing/invalid protected read floor and prefix limits')
    if (write.get('filename') != verified['device'] or write.get('rw') != 'randwrite'
            or int(write['offset']) < 0 or int(write['size']) <= 0
            or int(write['offset']) + int(write['size']) > prefix):
        raise ValueError('Formal writes must stay inside the low-address prefix')
    metrics = proof.get('scan_metrics', {})
    size = verified['size']
    if any(metrics.get(key) != size for key in ('fio_read_bytes', 'ftl_logical_read_bytes',
                                                'user_physical_read_bytes', 'qlc_total_read_bytes')):
        raise ValueError('Complete QLC scan byte totals do not match the formal read range')
    identity = proof['bdev_identity']
    ftl_name = proof.get('ftl_name', 'FTL0')
    ftl_uuid = identity[ftl_name]['uuid']
    binary_hash = proof.get('binary_sha256')
    if not isinstance(ftl_uuid, str) and not isinstance(ftl_uuid, type(u'')):
        raise ValueError('Invalid verified FTL UUID')
    if not re.match(r'^[A-Za-z0-9-]+$', ftl_uuid):
        raise ValueError('Invalid verified FTL UUID')
    if not isinstance(binary_hash, (str, type(u''))) or not re.match(r'^[0-9a-f]{64}$', binary_hash):
        raise ValueError('Invalid audited binary SHA256')
    return ftl_uuid, binary_hash


def main():
    if len(sys.argv) != 5:
        raise ValueError('Usage: check_qlc_ready.py VERIFICATION_JSON PROFILE CAPACITY OUTPUT_TSV')
    proof_path, profile_path, capacity, output = sys.argv[1:]
    proof_path = os.path.abspath(proof_path)
    if any(c in proof_path for c in '\n\r\t'):
        raise ValueError('Invalid verification path')
    proof = json.load(open(proof_path))
    profile = configparser.ConfigParser()
    profile.read(profile_path)
    ftl_uuid, binary_hash = validate(proof, profile, int(capacity))
    with open(output, 'w') as handle:
        handle.write('qlc_verification_file\t%s\nqlc_verified_ftl_uuid\t%s\nqlc_verified_binary_sha256\t%s\n' %
                     (proof_path, ftl_uuid, binary_hash))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, IOError) as error:
        sys.exit('QLC preparation not verified: %s' % error)
