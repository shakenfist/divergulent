"""Tests for divergulent.classify.reach -- the deterministic install-base axis.

Offline. ``bucket_for`` is a pure function of an install count and the snapshot
anchor; the reader tests lay down ``reach`` observations in a temp ledger and
assert the level/rank maps, including that ``unknown`` is never rankable. The
full record integration (popcon snapshot + ``package.binaries`` -> observation)
lives with R4 in ``test_record``.
"""
import json
import os
import tempfile

import testtools

from divergulent.classify import ledger as ledger_mod
from divergulent.classify import reach


WHEN = '2026-06-28T00:00:00Z'

# A representative snapshot anchor (the near-universal base package ~= the whole
# reporting population), so fractions read as the live by_inst snapshot does.
ANCHOR = 278978


class BucketTestCase(testtools.TestCase):

    def test_bucket_boundaries_are_inclusive_lower_bounds(self):
        # Exactly on a threshold takes the higher bucket; just under drops.
        self.assertEqual('XL', reach.bucket_for(int(ANCHOR * 0.5), ANCHOR))
        self.assertEqual('L', reach.bucket_for(int(ANCHOR * 0.5) - 1, ANCHOR))
        self.assertEqual('L', reach.bucket_for(int(ANCHOR * 0.1) + 1, ANCHOR))
        self.assertEqual('M', reach.bucket_for(int(ANCHOR * 0.1) - 1, ANCHOR))
        self.assertEqual('M', reach.bucket_for(int(ANCHOR * 0.01) + 1, ANCHOR))
        self.assertEqual('S', reach.bucket_for(int(ANCHOR * 0.01) - 1, ANCHOR))
        self.assertEqual('S', reach.bucket_for(int(ANCHOR * 0.001) + 1, ANCHOR))
        self.assertEqual('XS', reach.bucket_for(int(ANCHOR * 0.001) - 1, ANCHOR))

    def test_reference_packages_bucket_as_calibrated(self):
        # The packages used to calibrate the cuts (live snapshot, 2026-06-28).
        cases = {
            'libc6': (278978, 'XL'),
            'openssl': (278070, 'XL'),
            'git': (172355, 'XL'),       # 62% -- still XL, the fat top plateau
            'rsync': (151334, 'XL'),     # 54%
            'libgnutls30': (133984, 'L'),
            'nginx': (27975, 'L'),       # exactly at the 0.1 boundary
            'apache2': (60928, 'L'),
            'postgresql': (15452, 'M'),
            'docker.io': (14103, 'M'),
            'rman': (137, 'XS'),         # the motivating ancient/unloved package
        }
        for name, (inst, expected) in cases.items():
            self.assertEqual(expected, reach.bucket_for(inst, ANCHOR), name)

    def test_absent_from_snapshot_is_xs_not_unknown(self):
        # A source whose binaries do not appear in the snapshot reads as inst 0.
        self.assertEqual('XS', reach.bucket_for(0, ANCHOR))

    def test_degenerate_anchor_does_not_divide_by_zero(self):
        self.assertEqual(0.0, reach.fraction(5, 0))
        self.assertEqual('XS', reach.bucket_for(5, 0))

    def test_levels_and_ranks_are_ordered(self):
        self.assertEqual(('XS', 'S', 'M', 'L', 'XL'), reach.REACH_LEVELS)
        self.assertEqual(0, reach.REACH_RANK['XS'])
        self.assertEqual(4, reach.REACH_RANK['XL'])
        self.assertNotIn(reach.REACH_UNKNOWN, reach.REACH_RANK)


class EvidenceTestCase(testtools.TestCase):

    def test_evidence_is_canonical_explainable_json(self):
        data = json.loads(reach.evidence_for(
            binary='openssl', inst=278070, anchor=ANCHOR, snapshot_date='2026-06-28'))
        self.assertEqual('openssl', data['binary'])
        self.assertEqual(278070, data['inst'])
        self.assertEqual(ANCHOR, data['anchor_inst'])
        self.assertEqual('XL', data['bucket'])
        self.assertEqual(0.9967, data['fraction'])
        self.assertEqual('2026-06-28', data['snapshot_date'])

    def test_evidence_is_stable_for_identical_inputs(self):
        # Byte-identical -> the recorder can skip an unchanged re-record.
        a = reach.evidence_for(binary='rman', inst=137, anchor=ANCHOR, snapshot_date='2026-06-28')
        b = reach.evidence_for(binary='rman', inst=137, anchor=ANCHOR, snapshot_date='2026-06-28')
        self.assertEqual(a, b)


class ReaderTestCase(testtools.TestCase):

    def _ledger(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(tmpdir, ignore_errors=True))
        conn = ledger_mod.create_ledger(os.path.join(tmpdir, 'ledger.sqlite'))
        self.addCleanup(conn.close)
        return conn

    def _observe(self, conn, fingerprint, detail):
        ledger_mod.append_observation(
            conn, fingerprint=fingerprint, kind=reach.REACH_KIND, detail=detail,
            evidence=None, observed_by=reach.REACH_OBSERVED_BY,
            rule_version=reach.REACH_VERSION, observed_at=WHEN)

    def test_reach_by_fingerprint_reads_live_levels(self):
        conn = self._ledger()
        self._observe(conn, 'a' * 64, 'XL')
        self._observe(conn, 'b' * 64, 'M')
        self.assertEqual({'a' * 64: 'XL', 'b' * 64: 'M'}, reach.reach_by_fingerprint(conn))

    def test_reach_rank_by_fingerprint_maps_to_ordinals(self):
        conn = self._ledger()
        self._observe(conn, 'a' * 64, 'XL')
        self._observe(conn, 'b' * 64, 'XS')
        self.assertEqual({'a' * 64: 4, 'b' * 64: 0}, reach.reach_rank_by_fingerprint(conn))

    def test_unknown_is_not_rankable(self):
        conn = self._ledger()
        self._observe(conn, 'a' * 64, reach.REACH_UNKNOWN)
        self.assertEqual({}, reach.reach_by_fingerprint(conn))
        self.assertEqual({}, reach.reach_rank_by_fingerprint(conn))

    def test_other_observation_kinds_are_ignored(self):
        conn = self._ledger()
        ledger_mod.append_observation(
            conn, fingerprint='c' * 64, kind='reviewability', detail='large',
            evidence=None, observed_by='size-rule', rule_version=1, observed_at=WHEN)
        self.assertEqual({}, reach.reach_by_fingerprint(conn))
