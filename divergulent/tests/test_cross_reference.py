"""Tests for divergulent.classify.cross_reference -- the phase-6 CVE rule.

Offline and pure: a small snapshot is built with ``security_tracker.write_snapshot``
and ``verify_cve`` is checked at every boundary -- confirmed (for this source),
contradicted (invented id vs right-id/wrong-source), unknown (no reference), and
the confidence/freshness the recorder later stores.
"""
import os
import tempfile

import testtools

from divergulent.classify import cross_reference
from divergulent.classify import security_tracker


class VerifyCveTestCase(testtools.TestCase):

    def _snapshot(self, rows):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(tmpdir, ignore_errors=True))
        path = os.path.join(tmpdir, 'security_tracker.sqlite')
        security_tracker.write_snapshot(path, rows, snapshot_date='2026-07-10',
                                        source_url='x', release='trixie')
        conn = security_tracker.open_snapshot(path)
        self.addCleanup(conn.close)
        return conn

    def test_confirmed_for_this_source_resolved_is_high_confidence(self):
        conn = self._snapshot([('openssl', 'CVE-2021-3999', 'resolved', '3.0.0-1')])
        v = cross_reference.verify_cve(['CVE-2021-3999'], 'openssl', conn, snapshot_date='2026-07-10')
        self.assertEqual(cross_reference.CONFIRMED, v.outcome)
        self.assertEqual('CVE-2021-3999', v.cve)
        self.assertEqual('high', v.confidence)
        self.assertEqual('3.0.0-1', v.fixed_version)
        self.assertEqual('openssl', v.input_snapshot['source'])
        self.assertEqual('2026-07-10', v.input_snapshot['snapshot_date'])
        self.assertEqual('2026-08-09', v.fresh_until)  # +30 days

    def test_confirmed_open_without_fix_is_medium_confidence(self):
        conn = self._snapshot([('glibc', 'CVE-2022-1234', 'open', None)])
        v = cross_reference.verify_cve(['cve-2022-1234'], 'glibc', conn, snapshot_date='2026-07-10')
        self.assertEqual(cross_reference.CONFIRMED, v.outcome)
        self.assertEqual('medium', v.confidence)

    def test_case_insensitive_claim(self):
        conn = self._snapshot([('openssl', 'CVE-2021-3999', 'resolved', '3.0.0-1')])
        v = cross_reference.verify_cve(['cve-2021-3999'], 'openssl', conn, snapshot_date='2026-07-10')
        self.assertEqual(cross_reference.CONFIRMED, v.outcome)

    def test_first_confirmation_wins(self):
        conn = self._snapshot([('openssl', 'CVE-2021-3999', 'resolved', '3.0.0-1')])
        v = cross_reference.verify_cve(
            ['CVE-2000-0000', 'CVE-2021-3999'], 'openssl', conn, snapshot_date='2026-07-10')
        self.assertEqual('CVE-2021-3999', v.cve)

    def test_invented_cve_is_contradicted_not_found(self):
        conn = self._snapshot([('openssl', 'CVE-2021-3999', 'resolved', '3.0.0-1')])
        v = cross_reference.verify_cve(['CVE-2099-0000'], 'openssl', conn, snapshot_date='2026-07-10')
        self.assertEqual(cross_reference.CONTRADICTED, v.outcome)
        self.assertEqual(cross_reference.RESULT_NOT_FOUND, v.result)

    def test_right_cve_wrong_source_is_contradicted_wrong_source(self):
        conn = self._snapshot([('openssl', 'CVE-2021-3999', 'resolved', '3.0.0-1')])
        v = cross_reference.verify_cve(['CVE-2021-3999'], 'nginx', conn, snapshot_date='2026-07-10')
        self.assertEqual(cross_reference.CONTRADICTED, v.outcome)
        self.assertEqual(cross_reference.RESULT_WRONG_SOURCE, v.result)
        self.assertEqual(['CVE-2021-3999'], v.input_snapshot['cves'])

    def test_no_reference_is_unknown_with_nothing_to_record(self):
        conn = self._snapshot([('openssl', 'CVE-2021-3999', 'resolved', '3.0.0-1')])
        v = cross_reference.verify_cve([], 'openssl', conn, snapshot_date='2026-07-10')
        self.assertEqual(cross_reference.UNKNOWN, v.outcome)
        self.assertEqual({}, v.input_snapshot)
        self.assertIsNone(v.fresh_until)

    def test_fresh_until_honours_ttl(self):
        self.assertEqual('2026-07-17', cross_reference.fresh_until('2026-07-10', ttl_days=7))
