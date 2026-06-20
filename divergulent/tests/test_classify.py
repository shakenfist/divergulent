"""Tests for divergulent.classify.classify — the step-2d driver.

All tests are offline.  A small synthetic corpus + phase-1 fingerprint index is
laid down by hand (the test_measure approach), then classified end to end.  The
known patches exercise every category and the load-bearing logic:

  * a mode-only change      -> packaging (empty-after-normalisation)
  * a doc-only change       -> documentation
  * a substantive code edit -> unknown (the phase-4 residue)
  * a patch that CLAIMS "fix typo" (documentation) but ADDS ``system("...")`` to
    a ``.c`` file -> review_flag True, content unknown, a dangerous-construct
    flag, consistency ``content-substantive`` (content cannot confirm the claim).

Coverage asserts the classification-table rows, the review-flag logic, the
consistency values, the summary counts (including flag-by-detail), and that the
findings note is written with the headline counts.
"""
import json
import os
import sqlite3
import tempfile

import testtools

from divergulent.classify import classify
from divergulent.classify.corpus import body_sha256
from divergulent.classify.fingerprint import fingerprint


# --- mode-only: normalises to empty -> packaging --------------------------
# A quilt mode-only change carries no +/- content lines.
MODE_ONLY = (
    'Index: pkg/script.sh\n'
    '===================================================================\n'
    'old mode 100644\n'
    'new mode 100755\n'
)

# --- doc-only: a manpage edit -> documentation ----------------------------
# The DEP-3 description claims documentation too, so claim and content AGREE.
DOC_ONLY = (
    'Description: update the manpage wording\n'
    '--- a/doc/tool.1\n'
    '+++ b/doc/tool.1\n'
    '@@ -1,3 +1,3 @@\n'
    ' .TH TOOL 1\n'
    '-old description\n'
    '+new description\n'
    ' .SH NAME\n'
)

# --- substantive code edit -> unknown (residue) ---------------------------
SUBSTANTIVE = (
    '--- a/src/widget.c\n'
    '+++ b/src/widget.c\n'
    '@@ -10,3 +10,3 @@\n'
    ' int n = compute();\n'
    '-    return n;\n'
    '+    return n + adjust(n);\n'
    ' }\n'
)

# --- the threat-model case: claims "fix typo" but adds system() to a .c file
# The DEP-3 header claims documentation; the diff adds a shell-out in code.
TROJAN = (
    'Description: fix typo in comment\n'
    ' A harmless spelling correction.\n'
    '--- a/src/loader.c\n'
    '+++ b/src/loader.c\n'
    '@@ -5,2 +5,3 @@\n'
    ' void load(void) {\n'
    '+    system("/bin/sh /opt/setup.sh");\n'
    ' }\n'
)


def _build_synthetic_corpus(corpus_dir):
    """Lay down bodies + a phase-1 fingerprint index for a known-answer run.

    Provenance:
      pkg-a: MODE_ONLY (mode.patch)
      pkg-b: DOC_ONLY  (doc.patch)
      pkg-c: SUBSTANTIVE (code.patch)   } SUBSTANTIVE recurs across two
      pkg-d: SUBSTANTIVE (code.patch)   } packages -> n_occurrences=2, n_packages=2
      pkg-e: TROJAN (typo-fix.patch)    } the threat-model case
    """
    bodies = {
        'mode.patch': MODE_ONLY,
        'doc.patch': DOC_ONLY,
        'code.patch': SUBSTANTIVE,
        'typo-fix.patch': TROJAN,
    }
    for text in bodies.values():
        sha = body_sha256(text)
        directory = os.path.join(corpus_dir, 'bodies', sha[:2])
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, sha), 'w', encoding='utf-8') as handle:
            handle.write(text)

    patch_rows = [
        {'source_package': 'pkg-a', 'version': '1-1', 'patch_name': 'mode.patch',
         'raw_sha256': body_sha256(MODE_ONLY)},
        {'source_package': 'pkg-b', 'version': '1-1', 'patch_name': 'doc.patch',
         'raw_sha256': body_sha256(DOC_ONLY)},
        {'source_package': 'pkg-c', 'version': '1-1', 'patch_name': 'code.patch',
         'raw_sha256': body_sha256(SUBSTANTIVE)},
        {'source_package': 'pkg-d', 'version': '1-1', 'patch_name': 'code.patch',
         'raw_sha256': body_sha256(SUBSTANTIVE)},
        {'source_package': 'pkg-e', 'version': '1-1', 'patch_name': 'typo-fix.patch',
         'raw_sha256': body_sha256(TROJAN)},
    ]

    index_path = os.path.join(corpus_dir, 'fingerprints.sqlite')
    connection = sqlite3.connect(index_path)
    try:
        connection.execute(
            'CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        connection.executemany(
            'INSERT INTO meta (key, value) VALUES (?, ?)',
            [('normalisation_version', '1'), ('strip_path', 'True'),
             ('drop_context', 'False'), ('variant', 'strip_path,keep_context')])
        connection.execute(
            'CREATE TABLE patch ('
            'source_package TEXT NOT NULL, version TEXT NOT NULL, '
            'patch_name TEXT NOT NULL, raw_sha256 TEXT NOT NULL, '
            'normalisation_version INTEGER NOT NULL, fingerprint TEXT NOT NULL)')
        connection.executemany(
            'INSERT INTO patch (source_package, version, patch_name, raw_sha256, '
            'normalisation_version, fingerprint) VALUES (?, ?, ?, ?, ?, ?)',
            [(row['source_package'], row['version'], row['patch_name'], row['raw_sha256'],
              1, fingerprint(bodies[row['patch_name']])[1]) for row in patch_rows])
        connection.commit()
    finally:
        connection.close()

    # Manifests written too, so the corpus is self-consistent (not strictly
    # needed by classify, which reads only the index + bodies).
    with open(os.path.join(corpus_dir, 'patches.jsonl'), 'w', encoding='utf-8') as handle:
        for row in patch_rows:
            handle.write(json.dumps(row, sort_keys=True) + '\n')

    return index_path


def _fp(text):
    return fingerprint(text)[1]


class ClassifyIndexTestCase(testtools.TestCase):

    def _corpus(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path = _build_synthetic_corpus(tmp.name)
        return tmp.name, index_path

    def _by_fingerprint(self):
        corpus_dir, index_path = self._corpus()
        result = classify.classify_index(corpus_dir, index_path)
        return {c.fingerprint: c for c in result.classifications}, result

    def test_distinct_fingerprint_count_and_occurrences(self):
        by_fp, result = self._by_fingerprint()
        # MODE_ONLY, DOC_ONLY, SUBSTANTIVE, TROJAN = four distinct fingerprints.
        self.assertEqual(4, result.total_fingerprints)
        # Five provenance rows total (SUBSTANTIVE recurs across two packages).
        self.assertEqual(5, result.total_occurrences)

        substantive = by_fp[_fp(SUBSTANTIVE)]
        self.assertEqual(2, substantive.n_occurrences)
        self.assertEqual(2, substantive.n_packages)

    def test_mode_only_is_packaging(self):
        by_fp, _ = self._by_fingerprint()
        c = by_fp[_fp(MODE_ONLY)]
        self.assertEqual('packaging', c.content_category)
        self.assertEqual('high', c.confidence)
        self.assertFalse(c.review_flag)
        self.assertIn('empty', c.rule_ids)

    def test_doc_only_is_documentation(self):
        by_fp, _ = self._by_fingerprint()
        c = by_fp[_fp(DOC_ONLY)]
        self.assertEqual('documentation', c.content_category)
        self.assertEqual('high', c.confidence)
        self.assertFalse(c.review_flag)

    def test_substantive_is_unknown_residue(self):
        by_fp, _ = self._by_fingerprint()
        c = by_fp[_fp(SUBSTANTIVE)]
        self.assertEqual('unknown', c.content_category)
        self.assertEqual('low', c.confidence)
        self.assertEqual(0, c.flag_count)
        # No benign claim over it (claim is unknown), so no review flag.
        self.assertFalse(c.review_flag)
        self.assertEqual('claim-unknown', c.consistency)

    def test_trojan_is_review_flagged(self):
        by_fp, _ = self._by_fingerprint()
        c = by_fp[_fp(TROJAN)]
        # Content cannot confirm the claimed category.
        self.assertEqual('unknown', c.content_category)
        # The author claimed documentation ("fix typo").
        self.assertEqual('documentation', c.claim_category)
        # The dangerous construct fired AND the benign-claim-over-code rule.
        self.assertTrue(c.review_flag)
        self.assertGreaterEqual(c.flag_count, 1)
        self.assertIn('shell-out', c.flag_details)
        # Content is substantive -> consistency is content-substantive, not a
        # false 'agree' and not 'disagree' (content named no rival category).
        self.assertEqual('content-substantive', c.consistency)


class ConsistencyAndReviewLogicTestCase(testtools.TestCase):
    """Unit coverage of the pure consistency / review-flag helpers."""

    def test_consistency_claim_unknown(self):
        self.assertEqual('claim-unknown', classify._consistency('unknown', 'packaging'))
        # claim-unknown wins even when content is also unknown.
        self.assertEqual('claim-unknown', classify._consistency('unknown', 'unknown'))

    def test_consistency_content_substantive(self):
        self.assertEqual('content-substantive', classify._consistency('documentation', 'unknown'))

    def test_consistency_agree_and_disagree(self):
        self.assertEqual('agree', classify._consistency('packaging', 'packaging'))
        self.assertEqual('disagree', classify._consistency('documentation', 'packaging'))

    def test_review_flag_dangerous_construct_always_fires(self):
        # Even a non-benign claim flags when a dangerous construct is present.
        self.assertTrue(classify._review_flag('feature', 'unknown', True, True))

    def test_review_flag_benign_claim_over_code(self):
        # documentation claim + substantive content + touches code -> flag.
        self.assertTrue(classify._review_flag('documentation', 'unknown', True, False))
        self.assertTrue(classify._review_flag('packaging', 'unknown', True, False))

    def test_review_flag_benign_claim_no_code_does_not_fire(self):
        # Substantive non-code change (e.g. a big data file) must not cry wolf.
        self.assertFalse(classify._review_flag('documentation', 'unknown', False, False))

    def test_review_flag_agreeing_benign_does_not_fire(self):
        # Claim and content both packaging, no danger -> no flag.
        self.assertFalse(classify._review_flag('packaging', 'packaging', False, False))


class ClassificationTableTestCase(testtools.TestCase):

    def _written(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path = _build_synthetic_corpus(tmp.name)
        result = classify.classify_index(tmp.name, index_path)
        out_path = os.path.join(tmp.name, 'classification.sqlite')
        rows = classify.write_classification(result, out_path)
        return out_path, rows

    def test_table_has_one_row_per_fingerprint(self):
        out_path, rows = self._written()
        self.assertEqual(4, rows)
        connection = sqlite3.connect(out_path)
        self.addCleanup(connection.close)
        (count,) = connection.execute('SELECT COUNT(*) FROM classification').fetchone()
        self.assertEqual(4, count)

    def test_table_columns_carry_the_classification(self):
        out_path, _ = self._written()
        connection = sqlite3.connect(out_path)
        self.addCleanup(connection.close)

        row = connection.execute(
            'SELECT content_category, claim_category, consistency, review_flag, '
            'n_occurrences, n_packages, flag_count, flag_details '
            'FROM classification WHERE fingerprint = ?', (_fp(TROJAN),)).fetchone()
        (content_category, claim_category, consistency, review_flag,
         n_occurrences, n_packages, flag_count, flag_details) = row
        self.assertEqual('unknown', content_category)
        self.assertEqual('documentation', claim_category)
        self.assertEqual('content-substantive', consistency)
        self.assertEqual(1, review_flag)
        self.assertEqual(1, n_occurrences)
        self.assertEqual(1, n_packages)
        self.assertGreaterEqual(flag_count, 1)
        self.assertIn('shell-out', flag_details)

        # SUBSTANTIVE recurs across two packages.
        (occ, pkgs) = connection.execute(
            'SELECT n_occurrences, n_packages FROM classification WHERE fingerprint = ?',
            (_fp(SUBSTANTIVE),)).fetchone()
        self.assertEqual(2, occ)
        self.assertEqual(2, pkgs)

    def test_table_has_meta_and_indexes(self):
        out_path, _ = self._written()
        connection = sqlite3.connect(out_path)
        self.addCleanup(connection.close)
        meta = dict(connection.execute('SELECT key, value FROM meta'))
        self.assertIn('claim_rule_version', meta)
        self.assertIn('content_rule_version', meta)
        self.assertIn('rules_version', meta)
        names = {n for (n,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertIn('idx_classification_fingerprint', names)
        self.assertIn('idx_classification_content_category', names)

    def test_rerun_overwrites(self):
        out_path, _ = self._written()
        # Re-running with the same path must not duplicate rows.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path = _build_synthetic_corpus(tmp.name)
        result = classify.classify_index(tmp.name, index_path)
        classify.write_classification(result, out_path)
        classify.write_classification(result, out_path)
        connection = sqlite3.connect(out_path)
        self.addCleanup(connection.close)
        (count,) = connection.execute('SELECT COUNT(*) FROM classification').fetchone()
        self.assertEqual(4, count)


class SummaryTestCase(testtools.TestCase):

    def _summary(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path = _build_synthetic_corpus(tmp.name)
        result = classify.classify_index(tmp.name, index_path)
        return classify.summarise(result)

    def test_category_counts_by_fingerprint_and_occurrence(self):
        summary = self._summary()
        self.assertEqual(4, summary.total_fingerprints)
        self.assertEqual(5, summary.total_occurrences)
        # packaging (MODE_ONLY), documentation (DOC_ONLY), unknown (SUBSTANTIVE,
        # TROJAN) = one each except unknown which has two.
        self.assertEqual(1, summary.category_fingerprints['packaging'])
        self.assertEqual(1, summary.category_fingerprints['documentation'])
        self.assertEqual(2, summary.category_fingerprints['unknown'])
        # By occurrence, unknown carries SUBSTANTIVE (2 occ) + TROJAN (1) = 3.
        self.assertEqual(3, summary.category_occurrences['unknown'])

    def test_settled_fraction(self):
        summary = self._summary()
        # 2 of 4 distinct fingerprints settle deterministically.
        self.assertAlmostEqual(0.5, summary.settled_fraction())

    def test_review_flag_counts(self):
        summary = self._summary()
        # Only TROJAN is review-flagged.
        self.assertEqual(1, summary.review_flag_fingerprints)
        self.assertEqual(1, summary.review_flag_occurrences)

    def test_flag_detail_counts(self):
        summary = self._summary()
        # The shell-out pattern fired once (TROJAN); no backtick over-fire.
        self.assertEqual({'shell-out': 1}, summary.flag_detail_fingerprints)

    def test_consistency_distribution(self):
        summary = self._summary()
        dist = summary.consistency_fingerprints
        # MODE_ONLY: claim unknown -> claim-unknown. DOC_ONLY: claims doc,
        # content doc -> agree. SUBSTANTIVE: claim unknown -> claim-unknown.
        # TROJAN: claims doc, content unknown -> content-substantive.
        self.assertEqual(2, dist['claim-unknown'])
        self.assertEqual(1, dist['agree'])
        self.assertEqual(1, dist['content-substantive'])

    def test_samples_present_for_each_category(self):
        summary = self._summary()
        for category in ('packaging', 'documentation', 'unknown'):
            self.assertTrue(summary.samples_by_category.get(category))
        self.assertTrue(summary.review_samples)


class FindingsNoteTestCase(testtools.TestCase):

    def test_findings_note_written_with_headline_counts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path = _build_synthetic_corpus(tmp.name)
        result = classify.classify_index(tmp.name, index_path)
        summary = classify.summarise(result)
        findings_path = os.path.join(tmp.name, 'classification-findings.md')
        classify.write_findings(summary, findings_path)

        self.assertTrue(os.path.exists(findings_path))
        with open(findings_path, encoding='utf-8') as handle:
            text = handle.read()
        # The headline deterministic-settlement measurement.
        self.assertIn('4 distinct fingerprints', text)
        self.assertIn('50.0% settle deterministically', text)
        # The phase-4 residue label and the review-flag accounting.
        self.assertIn('unknown-substantive (phase-4 residue)', text)
        self.assertIn('Review-flagged', text)
        # The dangerous-construct detail breakdown names shell-out.
        self.assertIn('shell-out', text)
        # The threat-model sample surfaces with its evidence.
        self.assertIn('system(', text)


class CliTestCase(testtools.TestCase):

    def test_main_writes_table_and_findings(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _build_synthetic_corpus(tmp.name)
        out_path = os.path.join(tmp.name, 'cls.sqlite')
        findings_path = os.path.join(tmp.name, 'note.md')

        rc = classify.main([tmp.name, '--out', out_path, '--findings', findings_path, '--samples', '2'])
        self.assertEqual(0, rc)
        self.assertTrue(os.path.exists(out_path))
        self.assertTrue(os.path.exists(findings_path))

        connection = sqlite3.connect(out_path)
        self.addCleanup(connection.close)
        (count,) = connection.execute('SELECT COUNT(*) FROM classification').fetchone()
        self.assertEqual(4, count)

    def test_main_defaults_index_to_fingerprints_sqlite(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _build_synthetic_corpus(tmp.name)
        # No --index: must default to <corpus_dir>/fingerprints.sqlite.
        rc = classify.main([tmp.name])
        self.assertEqual(0, rc)
        self.assertTrue(os.path.exists(os.path.join(tmp.name, 'classification.sqlite')))
        self.assertTrue(os.path.exists(os.path.join(tmp.name, 'classification-findings.md')))
