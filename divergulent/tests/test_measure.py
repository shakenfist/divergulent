import json
import os
import sqlite3
import tempfile

import testtools

from divergulent.classify import measure
from divergulent.classify.corpus import body_sha256
from divergulent.classify.fingerprint import fingerprint


# A patch that recurs IDENTICALLY across three packages -- the recurring
# boilerplate; its canonical fingerprint should top the recurrence list.
RECUR = '--- a/recur.c\n+++ b/recur.c\n@@ -1,3 +1,3 @@\n before\n-old\n+new\n after\n'

# Two patches that differ ONLY by the target path: under strip_path they share a
# fingerprint; under keep_path they are distinct.
PATH_X = '--- a/dir/one.c\n+++ b/dir/one.c\n@@ -1 +1 @@\n-foo\n+bar\n'
PATH_Y = '--- a/other/two.c\n+++ b/other/two.c\n@@ -1 +1 @@\n-foo\n+bar\n'

# A genuinely unique patch.
UNIQUE = '--- a/uniq.c\n+++ b/uniq.c\n@@ -10 +10 @@\n-alpha\n+omega\n'


def _build_synthetic_corpus(corpus_dir):
    """Lay down bodies + manifests by hand for a known-answer measurement.

    Provenance (one patch row each):
      pkg-a: RECUR
      pkg-b: RECUR
      pkg-c: RECUR, UNIQUE              (UNIQUE makes pkg-c carry two patches)
      pkg-d: PATH_X, PATH_Y            (intra-package dedup under strip_path)
    Accounting-only packages: clean, native, fetch-failure, non-quilt.
    """
    bodies = [RECUR, PATH_X, PATH_Y, UNIQUE]
    for text in bodies:
        sha = body_sha256(text)
        directory = os.path.join(corpus_dir, 'bodies', sha[:2])
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, sha), 'w', encoding='utf-8') as handle:
            handle.write(text)

    patch_rows = [
        {'source_package': 'pkg-a', 'version': '1-1', 'patch_name': 'r.patch',
         'raw_sha256': body_sha256(RECUR)},
        {'source_package': 'pkg-b', 'version': '1-1', 'patch_name': 'r.patch',
         'raw_sha256': body_sha256(RECUR)},
        {'source_package': 'pkg-c', 'version': '1-1', 'patch_name': 'r.patch',
         'raw_sha256': body_sha256(RECUR)},
        {'source_package': 'pkg-c', 'version': '1-1', 'patch_name': 'u.patch',
         'raw_sha256': body_sha256(UNIQUE)},
        {'source_package': 'pkg-d', 'version': '2-1', 'patch_name': 'x.patch',
         'raw_sha256': body_sha256(PATH_X)},
        {'source_package': 'pkg-d', 'version': '2-1', 'patch_name': 'y.patch',
         'raw_sha256': body_sha256(PATH_Y)},
    ]
    with open(os.path.join(corpus_dir, 'patches.jsonl'), 'w', encoding='utf-8') as handle:
        for row in patch_rows:
            handle.write(json.dumps(row, sort_keys=True) + '\n')

    package_rows = [
        {'source_package': 'pkg-a', 'version': '1-1', 'state': 'patched',
         'source_format': '3.0 (quilt)', 'n_patches': 1, 'changelog_date': '2020-05-20',
         'error': None},
        {'source_package': 'pkg-b', 'version': '1-1', 'state': 'patched',
         'source_format': '3.0 (quilt)', 'n_patches': 1, 'error': None},
        {'source_package': 'pkg-c', 'version': '1-1', 'state': 'patched',
         'source_format': '3.0 (quilt)', 'n_patches': 2, 'error': None},
        {'source_package': 'pkg-d', 'version': '2-1', 'state': 'patched',
         'source_format': '3.0 (quilt)', 'n_patches': 2, 'error': None},
        {'source_package': 'clean-pkg', 'version': '1-1', 'state': 'clean',
         'source_format': '3.0 (quilt)', 'n_patches': 0, 'error': None},
        {'source_package': 'native-pkg', 'version': '1', 'state': 'native',
         'source_format': '3.0 (native)', 'n_patches': 0, 'error': None},
        {'source_package': 'broken-pkg', 'version': '1-1', 'state': 'unknown',
         'source_format': None, 'n_patches': 0, 'error': 'fetch-failed'},
        {'source_package': 'nonquilt-pkg', 'version': '1-1', 'state': 'unknown',
         'source_format': '1.0', 'n_patches': 0, 'error': 'non-quilt-format'},
    ]
    with open(os.path.join(corpus_dir, 'packages.jsonl'), 'w', encoding='utf-8') as handle:
        for row in package_rows:
            handle.write(json.dumps(row, sort_keys=True) + '\n')


class MeasureCorpusTestCase(testtools.TestCase):

    def _corpus(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _build_synthetic_corpus(tmp.name)
        return tmp.name

    def test_sensitivity_matrix_matches_hand_computed_counts(self):
        measurement = measure.measure_corpus(self._corpus())

        self.assertEqual(6, measurement.patch_rows)
        # Distinct raw bodies: RECUR, PATH_X, PATH_Y, UNIQUE.
        self.assertEqual(4, measurement.distinct_bodies)

        by_label = {v.label: v for v in measurement.matrix}
        # All four variants present.
        self.assertEqual(
            {'strip_path,keep_context', 'strip_path,drop_context',
             'keep_path,keep_context', 'keep_path,drop_context'},
            set(by_label))

        # strip_path,keep_context (canonical): RECUR, merged(PATH_X==PATH_Y), UNIQUE = 3.
        self.assertEqual(3, by_label['strip_path,keep_context'].distinct_fingerprints)
        # keep_path,keep_context: paths distinguish PATH_X from PATH_Y = 4.
        self.assertEqual(4, by_label['keep_path,keep_context'].distinct_fingerprints)
        # Dropping context does not change distinctness for these bodies.
        self.assertEqual(3, by_label['strip_path,drop_context'].distinct_fingerprints)
        self.assertEqual(4, by_label['keep_path,drop_context'].distinct_fingerprints)

        # Dedup ratio for the canonical variant: 6 rows / 3 distinct.
        self.assertAlmostEqual(2.0, by_label['strip_path,keep_context'].dedup_ratio)

    def test_canonical_variant_is_strip_path_keep_context(self):
        measurement = measure.measure_corpus(self._corpus())
        self.assertEqual('strip_path,keep_context', measurement.canonical_label)
        self.assertEqual(3, measurement.canonical.distinct_fingerprints)

    def test_multiplicity_histogram_is_correct(self):
        measurement = measure.measure_corpus(self._corpus())
        histogram = dict(measurement.multiplicity_histogram)
        # RECUR is in 3 distinct packages -> bucket '3-5'.
        self.assertEqual(1, histogram['3-5'])
        # The merged PATH fingerprint is in 1 package (pkg-d); UNIQUE in 1
        # (pkg-c) -> two fingerprints in bucket '1'.
        self.assertEqual(2, histogram['1'])
        self.assertEqual(0, histogram['2'])
        self.assertEqual(0, histogram['6-10'])
        self.assertEqual(0, histogram['51+'])

    def test_top_recurring_entry_is_the_three_package_patch(self):
        measurement = measure.measure_corpus(self._corpus())
        self.assertTrue(measurement.top_recurring)
        top = measurement.top_recurring[0]
        _version, recur_fp = fingerprint(RECUR)
        self.assertEqual(recur_fp, top.fingerprint)
        self.assertEqual(3, top.package_count)
        self.assertEqual(3, top.row_count)
        # The sample is the canonical-normalised body (paths stripped, @@ collapsed).
        self.assertIn('+new', top.sample)
        self.assertIn('@@', top.sample)

    def test_top_recurring_respects_top_n(self):
        measurement = measure.measure_corpus(self._corpus(), top_n=1)
        self.assertEqual(1, len(measurement.top_recurring))

    def test_intra_package_dedup_reports_pkg_d(self):
        measurement = measure.measure_corpus(self._corpus())
        # pkg-d carries two patches (PATH_X, PATH_Y) that merge to one canonical
        # fingerprint; pkg-c carries two genuinely distinct patches.
        by_pkg = {entry.source_package: entry for entry in measurement.intra_package_dedup}
        self.assertIn('pkg-d', by_pkg)
        self.assertEqual(2, by_pkg['pkg-d'].patches)
        self.assertEqual(1, by_pkg['pkg-d'].distinct_fingerprints)
        self.assertNotIn('pkg-c', by_pkg)
        self.assertNotIn('pkg-a', by_pkg)

    def test_accounting_matches_packages_jsonl(self):
        measurement = measure.measure_corpus(self._corpus())
        accounting = measurement.accounting
        self.assertEqual(8, accounting.packages_total)
        self.assertEqual(4, accounting.by_state['patched'])
        self.assertEqual(1, accounting.by_state['clean'])
        self.assertEqual(1, accounting.by_state['native'])
        self.assertEqual(2, accounting.by_state['unknown'])
        self.assertEqual(1, accounting.fetch_failures)
        self.assertEqual(1, accounting.non_quilt_skipped)


class WriteIndexTestCase(testtools.TestCase):

    def _corpus(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _build_synthetic_corpus(tmp.name)
        return tmp.name

    def test_index_has_expected_rows_and_meta(self):
        corpus_dir = self._corpus()
        index_path = os.path.join(corpus_dir, 'fingerprints.sqlite')
        rows_written = measure.write_index(corpus_dir, index_path)
        self.assertEqual(6, rows_written)

        connection = sqlite3.connect(index_path)
        self.addCleanup(connection.close)

        # One patch row per provenance row.
        (count,) = connection.execute('SELECT COUNT(*) FROM patch').fetchone()
        self.assertEqual(6, count)

        # All rows carry normalisation version 1.
        versions = {v for (v,) in connection.execute(
            'SELECT DISTINCT normalisation_version FROM patch')}
        self.assertEqual({1}, versions)

        # The RECUR fingerprint joins across exactly three packages.
        _version, recur_fp = fingerprint(RECUR)
        packages = {p for (p,) in connection.execute(
            'SELECT source_package FROM patch WHERE fingerprint = ?', (recur_fp,))}
        self.assertEqual({'pkg-a', 'pkg-b', 'pkg-c'}, packages)

        # PATH_X and PATH_Y merge to one canonical fingerprint, both in pkg-d.
        _v, path_fp = fingerprint(PATH_X)
        path_rows = connection.execute(
            'SELECT patch_name FROM patch WHERE fingerprint = ?', (path_fp,)).fetchall()
        self.assertEqual({'x.patch', 'y.patch'}, {name for (name,) in path_rows})

        # The meta table makes the index self-describing.
        meta = dict(connection.execute('SELECT key, value FROM meta'))
        self.assertEqual('1', meta['normalisation_version'])
        self.assertEqual('True', meta['strip_path'])
        self.assertEqual('False', meta['drop_context'])
        self.assertEqual('strip_path,keep_context', meta['variant'])

    def test_index_has_useful_indexes(self):
        corpus_dir = self._corpus()
        index_path = os.path.join(corpus_dir, 'fingerprints.sqlite')
        measure.write_index(corpus_dir, index_path)
        connection = sqlite3.connect(index_path)
        self.addCleanup(connection.close)
        names = {n for (n,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertIn('idx_patch_fingerprint', names)
        self.assertIn('idx_patch_source_package', names)

    def test_index_has_a_package_table_with_changelog_dates(self):
        corpus_dir = self._corpus()
        index_path = os.path.join(corpus_dir, 'fingerprints.sqlite')
        measure.write_index(corpus_dir, index_path)
        connection = sqlite3.connect(index_path)
        self.addCleanup(connection.close)
        dates = dict(connection.execute('SELECT source_package, changelog_date FROM package'))
        self.assertEqual('2020-05-20', dates['pkg-a'])   # captured changelog date
        self.assertIsNone(dates['pkg-b'])                # no date in its row -> NULL


class FindingsNoteTestCase(testtools.TestCase):

    def _corpus(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _build_synthetic_corpus(tmp.name)
        return tmp.name

    def test_findings_note_written_with_headline(self):
        corpus_dir = self._corpus()
        measurement = measure.measure_corpus(corpus_dir)
        findings_path = os.path.join(corpus_dir, 'findings.md')
        measure.write_findings(measurement, findings_path)

        self.assertTrue(os.path.exists(findings_path))
        with open(findings_path, encoding='utf-8') as handle:
            text = handle.read()
        # The master plan's success-criterion phrasing, with the real numbers.
        self.assertIn('≈6 carried patches → 3 distinct', text)
        # The sensitivity table lists all four variants.
        self.assertIn('strip_path,keep_context', text)
        self.assertIn('keep_path,drop_context', text)
        # The honest accounting is present.
        self.assertIn('Fetch failures: 1', text)
        self.assertIn('Non-quilt skipped: 1', text)


class CliTestCase(testtools.TestCase):

    def test_main_writes_index_and_findings(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _build_synthetic_corpus(tmp.name)
        index_path = os.path.join(tmp.name, 'fp.sqlite')
        findings_path = os.path.join(tmp.name, 'note.md')

        rc = measure.main([tmp.name, '--index', index_path, '--findings', findings_path, '--top', '2'])
        self.assertEqual(0, rc)
        self.assertTrue(os.path.exists(index_path))
        self.assertTrue(os.path.exists(findings_path))

        connection = sqlite3.connect(index_path)
        self.addCleanup(connection.close)
        (count,) = connection.execute('SELECT COUNT(*) FROM patch').fetchone()
        self.assertEqual(6, count)


class AppendOnlyDuplicateTestCase(testtools.TestCase):
    """A crash mid-write or a retried package can leave duplicate manifest rows;
    the measurement must dedup by the natural key so counts stay exact."""

    def _corpus_with_duplicates(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _build_synthetic_corpus(tmp.name)
        # Re-append an existing patch row (pkg-a's r.patch) and a superseding
        # package row for the broken package (a later success after a retry).
        with open(os.path.join(tmp.name, 'patches.jsonl'), 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(
                {'source_package': 'pkg-a', 'version': '1-1', 'patch_name': 'r.patch',
                 'raw_sha256': body_sha256(RECUR)}, sort_keys=True) + '\n')
        with open(os.path.join(tmp.name, 'packages.jsonl'), 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(
                {'source_package': 'broken-pkg', 'version': '1-1', 'state': 'clean',
                 'source_format': '3.0 (quilt)', 'n_patches': 0, 'error': None}, sort_keys=True) + '\n')
        return tmp.name

    def test_duplicate_patch_row_does_not_inflate_counts(self):
        measurement = measure.measure_corpus(self._corpus_with_duplicates())
        # Still six distinct provenance rows despite the duplicate append.
        self.assertEqual(6, measurement.patch_rows)
        self.assertEqual(3, measurement.canonical.distinct_fingerprints)

    def test_latest_package_row_wins_in_accounting(self):
        measurement = measure.measure_corpus(self._corpus_with_duplicates())
        # The retried broken-pkg is now clean, not a fetch failure.
        self.assertEqual(0, measurement.accounting.fetch_failures)
        self.assertEqual(8, measurement.accounting.packages_total)

    def test_index_dedups_patch_rows(self):
        corpus_dir = self._corpus_with_duplicates()
        index_path = os.path.join(corpus_dir, 'fp.sqlite')
        measure.write_index(corpus_dir, index_path)
        connection = sqlite3.connect(index_path)
        self.addCleanup(connection.close)
        (count,) = connection.execute('SELECT COUNT(*) FROM patch').fetchone()
        self.assertEqual(6, count)
