"""Tests for divergulent.classify.reviewability -- the deterministic size axis.

Offline. The classifier is a pure function of the content profile's changed-line
count; the integration test lays down a tiny synthetic corpus spanning the three
tiers and runs ``record_to_ledger``, asserting one ``reviewability`` observation
per fingerprint with the right level and provenance.
"""
import json
import os
import sqlite3
import tempfile

import testtools

from divergulent.classify import content as content_mod
from divergulent.classify import ledger as ledger_mod
from divergulent.classify import record
from divergulent.classify import reviewability
from divergulent.classify.corpus import body_sha256
from divergulent.classify.fingerprint import fingerprint


WHEN = '2026-06-27T00:00:00Z'


def _diff_adding(n_lines, path='src/big.c'):
    """A unified diff that adds ``n_lines`` lines (so changed_lines == n_lines)."""
    header = '--- a/%s\n+++ b/%s\n@@ -1 +1,%d @@\n int x;\n' % (path, path, n_lines + 1)
    return header + ''.join('+line %d\n' % i for i in range(n_lines))


class ClassifierTestCase(testtools.TestCase):

    def test_level_for_boundaries(self):
        # normal up to and including LARGE; large above it up to OVERSIZED;
        # oversized strictly above OVERSIZED.
        self.assertEqual('normal', reviewability.level_for(0))
        self.assertEqual('normal', reviewability.level_for(reviewability.REVIEWABILITY_LARGE_LINES))
        self.assertEqual('large', reviewability.level_for(reviewability.REVIEWABILITY_LARGE_LINES + 1))
        self.assertEqual('large', reviewability.level_for(reviewability.REVIEWABILITY_OVERSIZED_LINES))
        self.assertEqual(
            'oversized', reviewability.level_for(reviewability.REVIEWABILITY_OVERSIZED_LINES + 1))

    def test_classify_via_profile(self):
        self.assertEqual('normal', reviewability.classify(content_mod.profile(_diff_adding(10))))
        self.assertEqual('large', reviewability.classify(content_mod.profile(_diff_adding(600))))
        self.assertEqual('oversized', reviewability.classify(content_mod.profile(_diff_adding(6000))))

    def test_changed_lines_counts_added_and_removed(self):
        text = (
            '--- a/src/w.c\n+++ b/src/w.c\n@@ -1,2 +1,2 @@\n'
            '-old one\n-old two\n+new one\n+new two\n')
        prof = content_mod.profile(text)
        self.assertEqual(4, reviewability.changed_lines(prof))

    def test_evidence_is_canonical_json(self):
        prof = content_mod.profile(_diff_adding(600))
        data = json.loads(reviewability.evidence_for(prof))
        self.assertEqual(600, data['changed_lines'])
        self.assertEqual(600, data['added_lines'])
        self.assertEqual(0, data['removed_lines'])


def _build_corpus(corpus_dir, bodies_by_name):
    """Lay down bodies + a phase-1 index for the named patches; one package each."""
    for text in bodies_by_name.values():
        sha = body_sha256(text)
        directory = os.path.join(corpus_dir, 'bodies', sha[:2])
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, sha), 'w', encoding='utf-8') as handle:
            handle.write(text)
    index_path = os.path.join(corpus_dir, 'fingerprints.sqlite')
    conn = sqlite3.connect(index_path)
    try:
        conn.execute(
            'CREATE TABLE patch (source_package TEXT NOT NULL, version TEXT NOT NULL, '
            'patch_name TEXT NOT NULL, raw_sha256 TEXT NOT NULL, '
            'normalisation_version INTEGER NOT NULL, fingerprint TEXT NOT NULL)')
        conn.executemany(
            'INSERT INTO patch VALUES (?, ?, ?, ?, ?, ?)',
            [('pkg-%s' % name, '1-1', name, body_sha256(text), 1, fingerprint(text)[1])
             for name, text in bodies_by_name.items()])
        conn.commit()
    finally:
        conn.close()
    return index_path


class RecordIntegrationTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.bodies = {
            'small.patch': _diff_adding(10),     # normal
            'big.patch': _diff_adding(600),      # large
            'huge.patch': _diff_adding(6000),    # oversized
        }
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path = _build_corpus(tmp.name, self.bodies)
        path = os.path.join(tmp.name, 'ledger.sqlite')
        self.conn = ledger_mod.create_ledger(path)
        self.addCleanup(self.conn.close)
        self.stats = record.record_to_ledger(self.conn, tmp.name, index_path, now=WHEN)

    def _fp(self, name):
        return fingerprint(self.bodies[name])[1]

    def test_one_reviewability_observation_per_fingerprint(self):
        self.assertEqual(3, self.stats.reviewability_appended)
        levels = reviewability.reviewability_by_fingerprint(self.conn)
        self.assertEqual('normal', levels[self._fp('small.patch')])
        self.assertEqual('large', levels[self._fp('big.patch')])
        self.assertEqual('oversized', levels[self._fp('huge.patch')])

    def test_observation_provenance(self):
        obs = [o for o in ledger_mod.live_observations(self.conn)
               if o['kind'] == reviewability.REVIEWABILITY_KIND
               and o['fingerprint'] == self._fp('huge.patch')]
        self.assertEqual(1, len(obs))
        self.assertEqual(reviewability.REVIEWABILITY_OBSERVED_BY, obs[0]['observed_by'])
        self.assertEqual(reviewability.REVIEWABILITY_VERSION, obs[0]['rule_version'])
        self.assertEqual('oversized', obs[0]['detail'])

    def test_oversized_fingerprints(self):
        self.assertEqual({self._fp('huge.patch')}, reviewability.oversized_fingerprints(self.conn))
