"""Tests for divergulent.classify.bts -- the pinned BTS bug-index snapshot.

Offline. Parsing is checked against a tolerant TSV fixture (comments, blanks,
malformed rows, whitespace-separated); the fetch path uses an injected
``download`` that writes the fixture, so no network.
"""
import os
import tempfile

import testtools

from divergulent.classify import bts


FIXTURE = """# bug\tsource\tstatus
123456\topenssl\tdone
234567\tcoreutils\tpending
345678\tnginx\tforwarded

# a comment line and a blank line above are skipped
notanumber\tfoo\tpending
99\tshort
"""


class ParseTestCase(testtools.TestCase):

    def test_parses_valid_rows(self):
        rows = {bug: (source, status) for bug, source, status in bts.parse_bug_index(FIXTURE)}
        self.assertEqual(('openssl', 'done'), rows[123456])
        self.assertEqual(('coreutils', 'pending'), rows[234567])
        self.assertEqual(('nginx', 'forwarded'), rows[345678])

    def test_skips_comments_blanks_and_malformed(self):
        rows = bts.parse_bug_index(FIXTURE)
        self.assertEqual(3, len(rows))
        bugs = {bug for bug, _, _ in rows}
        self.assertNotIn(99, bugs)  # too few columns

    def test_status_is_lower_cased(self):
        rows = {bug: status for bug, _, status in bts.parse_bug_index('42 curl DONE\n')}
        self.assertEqual('done', rows[42])


class SnapshotTestCase(testtools.TestCase):

    def _corpus(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(tmpdir, ignore_errors=True))
        return tmpdir

    def _fake_download(self, url, dest_path):
        with open(dest_path, 'w', encoding='utf-8') as handle:
            handle.write(FIXTURE)

    def test_pull_pins_snapshot_with_meta(self):
        corpus = self._corpus()
        path, count = bts.pull(corpus, snapshot_date='2026-07-10', download=self._fake_download)
        self.assertEqual(bts.default_bts_path(corpus), path)
        self.assertEqual(3, count)
        conn = bts.open_snapshot(path)
        self.addCleanup(conn.close)
        self.assertEqual('2026-07-10', bts.snapshot_meta(conn)['snapshot_date'])

    def test_bug_row_lookup(self):
        corpus = self._corpus()
        path, _ = bts.pull(corpus, snapshot_date='2026-07-10', download=self._fake_download)
        conn = bts.open_snapshot(path)
        self.addCleanup(conn.close)
        row = bts.bug_row(conn, 123456)
        self.assertEqual('openssl', row['source'])
        self.assertEqual('done', row['status'])
        self.assertIsNone(bts.bug_row(conn, 999999))

    def test_pull_refuses_empty_download(self):
        corpus = self._corpus()

        def empty(url, dest_path):
            with open(dest_path, 'w', encoding='utf-8') as handle:
                handle.write('# only a comment\n')

        self.assertRaises(ValueError, bts.pull,
                          corpus, snapshot_date='2026-07-10', download=empty)
        self.assertFalse(os.path.exists(bts.default_bts_path(corpus)))

    def test_pull_transparently_decompresses_gzip(self):
        # The hosted artifact is bts-index.tsv.gz; pull must gunzip it and produce
        # the same snapshot as the plain-TSV path.
        corpus = self._corpus()

        def gzipped(url, dest_path):
            import gzip
            with open(dest_path, 'wb') as handle:
                handle.write(gzip.compress(FIXTURE.encode('utf-8')))

        path, count = bts.pull(corpus, snapshot_date='2026-07-10', download=gzipped)
        self.assertEqual(3, count)
        conn = bts.open_snapshot(path)
        self.addCleanup(conn.close)
        self.assertEqual('openssl', bts.bug_row(conn, 123456)['source'])

    def test_default_url_is_the_hosted_rolling_asset(self):
        self.assertEqual(
            'https://github.com/shakenfist/divergulent/releases/download/bts/bts-index.tsv.gz',
            bts.BTS_URL)
