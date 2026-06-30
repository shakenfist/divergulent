"""Tests for divergulent.classify.popcon -- the pinned popcon snapshot.

Offline. Parsing is checked against a by_inst fixture (header, data, the trailing
divider and Total row); the fetch path uses an injected ``download`` that writes
the fixture, so no network. The reach buckets that consume this snapshot are
tested separately in ``test_reach``.
"""
import os
import tempfile

import testtools

from divergulent.classify import popcon


# A miniature by_inst table: the comment header, a few packages spanning the
# scale, the trailing divider, and the Total summary row (whose inst is the
# population SUM and must be skipped so it does not become the anchor).
FIXTURE = """#Format
#
#<name> is the package name;
#<inst> is the number of people who installed this package;
#rank name                 inst  vote   old recent no-files (maintainer)
1     debconf            279046 263215  1421 14383    27 (Debconf Developers)
2     libc6              278978 142127  1476 13998    34 (GNU Libc Maintainers)
3     openssl            278070 200000  1000 10000    50 (Debian OpenSSL Team)
500   nginx               27975  10000  1000  2000    10 (Debian Nginx Team)
9000  rman                  137     50    40     5     2 (Not in sid)
------------------------------------------------------------------
9001 Total                  390445941 143785603 113347048 28899468 104413822 0
"""


class ParseTestCase(testtools.TestCase):

    def test_parse_skips_header_divider_and_total(self):
        rows = popcon.parse_by_inst(FIXTURE)
        names = [name for name, _, _ in rows]
        self.assertEqual(['debconf', 'libc6', 'openssl', 'nginx', 'rman'], names)
        self.assertNotIn('Total', names)

    def test_parse_reads_inst_and_vote(self):
        rows = dict((name, (inst, vote)) for name, inst, vote in popcon.parse_by_inst(FIXTURE))
        self.assertEqual((278070, 200000), rows['openssl'])
        self.assertEqual((137, 50), rows['rman'])

    def test_anchor_is_max_inst_not_the_total_row(self):
        rows = popcon.parse_by_inst(FIXTURE)
        self.assertEqual(279046, max(inst for _, inst, _ in rows))

    def test_garbage_lines_are_skipped(self):
        text = FIXTURE + 'oops not a row\n42 short cols\n'
        rows = popcon.parse_by_inst(text)
        self.assertEqual(5, len(rows))


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
        path, anchor = popcon.pull(
            corpus, snapshot_date='2026-06-28', download=self._fake_download)
        self.assertEqual(popcon.default_popcon_path(corpus), path)
        self.assertEqual(279046, anchor)
        conn = popcon.open_snapshot(path)
        self.addCleanup(conn.close)
        meta = popcon.snapshot_meta(conn)
        self.assertEqual('2026-06-28', meta['snapshot_date'])
        self.assertEqual('5', meta['row_count'])
        self.assertEqual('279046', meta['anchor_inst'])
        self.assertEqual(279046, popcon.anchor_inst(conn))

    def test_installs_by_binary_inst_and_vote(self):
        corpus = self._corpus()
        path, _ = popcon.pull(corpus, snapshot_date='2026-06-28', download=self._fake_download)
        conn = popcon.open_snapshot(path)
        self.addCleanup(conn.close)
        inst = popcon.installs_by_binary(conn)
        self.assertEqual(278070, inst['openssl'])
        vote = popcon.installs_by_binary(conn, column='vote')
        self.assertEqual(200000, vote['openssl'])

    def test_installs_by_binary_rejects_unknown_column(self):
        corpus = self._corpus()
        path, _ = popcon.pull(corpus, snapshot_date='2026-06-28', download=self._fake_download)
        conn = popcon.open_snapshot(path)
        self.addCleanup(conn.close)
        self.assertRaises(ValueError, popcon.installs_by_binary, conn, column='old')

    def test_pull_refuses_empty_download(self):
        corpus = self._corpus()

        def empty(url, dest_path):
            with open(dest_path, 'w', encoding='utf-8') as handle:
                handle.write('#Format\n#only comments\n')

        self.assertRaises(ValueError, popcon.pull,
                          corpus, snapshot_date='2026-06-28', download=empty)
        # And nothing was pinned.
        self.assertFalse(os.path.exists(popcon.default_popcon_path(corpus)))

    def test_pull_replaces_a_prior_snapshot(self):
        corpus = self._corpus()
        popcon.pull(corpus, snapshot_date='2026-06-01', download=self._fake_download)

        def smaller(url, dest_path):
            with open(dest_path, 'w', encoding='utf-8') as handle:
                handle.write(
                    '#rank name inst vote old recent no-files (m)\n'
                    '1 libc6 100 50 1 1 1 (x)\n')

        path, anchor = popcon.pull(corpus, snapshot_date='2026-06-28', download=smaller)
        self.assertEqual(100, anchor)
        conn = popcon.open_snapshot(path)
        self.addCleanup(conn.close)
        # The old rows are gone (wholly superseded, not merged).
        self.assertEqual({'libc6': 100}, popcon.installs_by_binary(conn))
        self.assertEqual('2026-06-28', popcon.snapshot_meta(conn)['snapshot_date'])
