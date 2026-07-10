"""Tests for divergulent.classify.security_tracker -- the pinned CVE snapshot.

Offline. Parsing is checked against a trimmed tracker-JSON fixture (target-release
collapse, cross-release aggregation, malformed islands); the fetch path uses an
injected ``download`` that writes the fixture, so no network. The CVE rule that
consumes this snapshot is tested separately in ``test_cross_reference``.
"""
import json
import os
import tempfile

import testtools

from divergulent.classify import security_tracker


# A miniature tracker document: one source with a CVE resolved in the target
# release, one CVE only open in an older release (cross-release aggregation), a
# malformed CVE island, and a non-CVE key that must be ignored.
FIXTURE = {
    'openssl': {
        'CVE-2021-3999': {
            'description': 'off-by-one',
            'releases': {
                'trixie': {'status': 'resolved', 'fixed_version': '3.0.0-1', 'urgency': 'high'},
                'bookworm': {'status': 'open'},
            },
        },
        'CVE-2020-0001': {
            'description': 'only tracked in an older release',
            'releases': {
                'bullseye': {'status': 'resolved', 'fixed_version': '1.1.1-2'},
            },
        },
        'not-a-cve': {'releases': {}},
        'CVE-2019-9999': 'malformed-not-a-dict',
    },
    'glibc': {
        'cve-2022-1234': {  # lower-case in the source; must be upper-cased
            'releases': {'trixie': {'status': 'open'}},
        },
    },
    'weird-source': 'not-a-dict',
}


class ParseTestCase(testtools.TestCase):

    def _rows(self, release='trixie'):
        return {(source, cve): (status, fixed)
                for source, cve, status, fixed in security_tracker.parse_tracker_json(FIXTURE, release=release)}

    def test_target_release_status_and_fixed_version(self):
        rows = self._rows()
        self.assertEqual(('resolved', '3.0.0-1'), rows[('openssl', 'CVE-2021-3999')])

    def test_cross_release_aggregation_when_target_absent(self):
        # No trixie entry -> aggregate: the bullseye resolved+fixed wins.
        rows = self._rows()
        self.assertEqual(('resolved', '1.1.1-2'), rows[('openssl', 'CVE-2020-0001')])

    def test_cve_ids_are_upper_cased(self):
        rows = self._rows()
        self.assertIn(('glibc', 'CVE-2022-1234'), rows)

    def test_malformed_and_non_cve_entries_skipped(self):
        rows = self._rows()
        self.assertNotIn(('openssl', 'CVE-2019-9999'), rows)
        keys = {cve for (_, cve) in rows}
        self.assertNotIn('not-a-cve', keys)
        self.assertNotIn('NOT-A-CVE', keys)

    def test_non_dict_document_yields_no_rows(self):
        self.assertEqual([], security_tracker.parse_tracker_json(['nope']))


class SnapshotTestCase(testtools.TestCase):

    def _corpus(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(tmpdir, ignore_errors=True))
        return tmpdir

    def _fake_download(self, url, dest_path):
        with open(dest_path, 'w', encoding='utf-8') as handle:
            json.dump(FIXTURE, handle)

    def test_pull_pins_snapshot_with_meta(self):
        corpus = self._corpus()
        path, count = security_tracker.pull(
            corpus, snapshot_date='2026-07-10', download=self._fake_download)
        self.assertEqual(security_tracker.default_security_tracker_path(corpus), path)
        self.assertEqual(3, count)
        conn = security_tracker.open_snapshot(path)
        self.addCleanup(conn.close)
        meta = security_tracker.snapshot_meta(conn)
        self.assertEqual('2026-07-10', meta['snapshot_date'])
        self.assertEqual('trixie', meta['release'])
        self.assertEqual('3', meta['row_count'])

    def test_cve_row_is_the_corroboration_lookup(self):
        corpus = self._corpus()
        path, _ = security_tracker.pull(corpus, snapshot_date='2026-07-10', download=self._fake_download)
        conn = security_tracker.open_snapshot(path)
        self.addCleanup(conn.close)
        row = security_tracker.cve_row(conn, 'openssl', 'cve-2021-3999')  # case-insensitive
        self.assertIsNotNone(row)
        self.assertEqual('resolved', row['status'])
        self.assertEqual('3.0.0-1', row['fixed_version'])
        # Right CVE, wrong source -> no corroboration.
        self.assertIsNone(security_tracker.cve_row(conn, 'nginx', 'CVE-2021-3999'))

    def test_cve_exists_spans_all_sources(self):
        corpus = self._corpus()
        path, _ = security_tracker.pull(corpus, snapshot_date='2026-07-10', download=self._fake_download)
        conn = security_tracker.open_snapshot(path)
        self.addCleanup(conn.close)
        self.assertTrue(security_tracker.cve_exists(conn, 'CVE-2021-3999'))
        self.assertFalse(security_tracker.cve_exists(conn, 'CVE-2099-0000'))

    def test_pull_refuses_empty_document(self):
        corpus = self._corpus()

        def empty(url, dest_path):
            with open(dest_path, 'w', encoding='utf-8') as handle:
                json.dump({}, handle)

        self.assertRaises(ValueError, security_tracker.pull,
                          corpus, snapshot_date='2026-07-10', download=empty)
        self.assertFalse(os.path.exists(security_tracker.default_security_tracker_path(corpus)))

    def test_pull_replaces_a_prior_snapshot(self):
        corpus = self._corpus()
        security_tracker.pull(corpus, snapshot_date='2026-06-01', download=self._fake_download)

        def smaller(url, dest_path):
            with open(dest_path, 'w', encoding='utf-8') as handle:
                json.dump({'curl': {'CVE-2024-0001': {'releases': {'trixie': {'status': 'open'}}}}}, handle)

        path, count = security_tracker.pull(corpus, snapshot_date='2026-07-10', download=smaller)
        self.assertEqual(1, count)
        conn = security_tracker.open_snapshot(path)
        self.addCleanup(conn.close)
        self.assertIsNone(security_tracker.cve_row(conn, 'openssl', 'CVE-2021-3999'))
        self.assertIsNotNone(security_tracker.cve_row(conn, 'curl', 'CVE-2024-0001'))


class MainTestCase(testtools.TestCase):

    def test_main_pulls_via_default_url(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(tmpdir, ignore_errors=True))
        fixture_path = os.path.join(tmpdir, 'data.json')
        with open(fixture_path, 'w', encoding='utf-8') as handle:
            json.dump(FIXTURE, handle)
        rc = security_tracker.main([tmpdir, '--url', 'file://' + fixture_path, '--date', '2026-07-10'])
        self.assertEqual(0, rc)
        self.assertTrue(os.path.exists(security_tracker.default_security_tracker_path(tmpdir)))
