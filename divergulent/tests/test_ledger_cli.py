"""Tests for the step-3d supersession/redo operations and the ledger CLI.

All offline.  The supersession cases hand-build a small ledger with
``ledger.create_ledger`` + ``register_rules`` + ``append_decision`` /
``append_observation`` directly (no corpus, no rule engine), so the surgical-redo
semantics are exercised in isolation:

  * Supersession re-queues EXACTLY the affected fingerprints: a fingerprint
    decided only by the superseded rule loses its live decision and appears in
    ``queue``; a fingerprint decided by a different live rule stays settled and is
    NOT re-queued.  The re-queue is DERIVED by ``verdict.queue`` — nothing stores
    a queue.
  * The audit trail is intact: the superseded decision row still exists with its
    ``superseded_at`` set and its original content unchanged; ``retire_rule`` set
    the registry flag.
  * A higher-version re-registration takes precedence on recompute: after
    superseding rule X v1 and appending a new live decision for that fingerprint,
    ``current_verdict`` returns the new one.

The CLI is smoke-tested through ``main``: a ``build`` over a tiny synthetic
corpus, then ``report`` and ``supersede`` over the built ledger, asserting exit 0
and that the actions land in the ledger.
"""
import io
import os
import sqlite3
import tempfile
from contextlib import redirect_stdout

import testtools

from divergulent.classify import ledger as ledger_mod
from divergulent.classify import verdict as verdict_mod
from divergulent.classify.corpus import body_sha256
from divergulent.classify.fingerprint import fingerprint


WHEN = '2026-06-14T00:00:00Z'
LATER = '2026-06-15T00:00:00Z'
LATEST = '2026-06-16T00:00:00Z'


class SupersedeFixture:
    """Mixin: a fresh, registry-populated ledger and an append shortcut."""

    def _ledger(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, 'ledger.sqlite')
        conn = ledger_mod.create_ledger(path)
        self.addCleanup(conn.close)
        ledger_mod.register_rules(conn, ledger_mod.default_registry())
        return conn

    def _decide(self, conn, fingerprint, *, category='unknown', confidence='low',
                decided_by='substantive', rule_version=1, kind='heuristic',
                evidence=None, decided_at=WHEN):
        return ledger_mod.append_decision(
            conn, fingerprint=fingerprint, category=category, confidence=confidence,
            decided_by=decided_by, rule_version=rule_version, kind=kind,
            evidence=evidence, decided_at=decided_at)


class SupersedeRuleTestCase(SupersedeFixture, testtools.TestCase):

    def test_supersession_requeues_exactly_the_affected_fingerprints(self):
        conn = self._ledger()
        # fp-x is decided only by doc-only; fp-y by a different live rule (empty).
        self._decide(conn, 'fp-x', category='documentation', decided_by='doc-only')
        self._decide(conn, 'fp-y', category='packaging', confidence='high',
                     decided_by='empty')
        # Before: nothing queued (both settled, neither unknown).
        self.assertEqual([], verdict_mod.queue(conn))

        result = ledger_mod.supersede_rule(
            conn, rule_id='doc-only', version=1, superseded_at=LATER)
        self.assertEqual(1, result.decisions_superseded)
        self.assertEqual(0, result.observations_superseded)
        self.assertTrue(result.retired)

        # fp-x lost its only live decision -> no current verdict -> re-queued.
        self.assertNotIn('fp-x', verdict_mod.current_verdict(conn))
        # fp-y untouched: still settled, still has its live verdict.
        self.assertIn('fp-y', verdict_mod.current_verdict(conn))
        self.assertEqual('packaging', verdict_mod.current_verdict(conn)['fp-y'].category)
        # The queue is EXACTLY the affected fingerprint, derived not stored.
        self.assertEqual(['fp-x'], verdict_mod.queue(conn))

    def test_supersede_observations_via_supersede_rule(self):
        conn = self._ledger()
        # A fingerprint with a live decision (by empty) plus a dangerous-construct
        # observation from the scan rule.  Superseding the scan rule must mark the
        # observation superseded without touching the category decision.
        self._decide(conn, 'fp-flag', category='packaging', confidence='high',
                     decided_by='empty')
        ledger_mod.append_observation(
            conn, fingerprint='fp-flag', kind='dangerous-construct', detail='shell-out',
            evidence='system(x)', observed_by='dangerous-construct-scan',
            rule_version=1, observed_at=WHEN)

        result = ledger_mod.supersede_rule(
            conn, rule_id='dangerous-construct-scan', version=1, superseded_at=LATER)
        self.assertEqual(0, result.decisions_superseded)
        self.assertEqual(1, result.observations_superseded)
        # The category decision is untouched; only the observation was superseded.
        self.assertEqual('packaging', verdict_mod.current_verdict(conn)['fp-flag'].category)
        self.assertEqual([], ledger_mod.live_observations(conn))

    def test_audit_trail_intact_after_supersession(self):
        conn = self._ledger()
        decision_id = self._decide(
            conn, 'fp-x', category='documentation', confidence='medium',
            decided_by='doc-only', evidence='all touched files are docs')

        ledger_mod.supersede_rule(conn, rule_id='doc-only', version=1, superseded_at=LATER)

        # The superseded decision row STILL EXISTS, marked but unedited.
        rows = ledger_mod.decisions_for(conn, 'fp-x')
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(decision_id, row['id'])
        self.assertEqual(LATER, row['superseded_at'])
        # Original content unchanged.
        self.assertEqual('documentation', row['category'])
        self.assertEqual('medium', row['confidence'])
        self.assertEqual('doc-only', row['decided_by'])
        self.assertEqual('all touched files are docs', row['evidence'])
        self.assertEqual(WHEN, row['decided_at'])

        # retire_rule flipped the registry flag for exactly this rule version.
        retired = {(r['rule_id'], r['version']): r['retired']
                   for r in ledger_mod.registered_rules(conn)}
        self.assertEqual(1, retired[('doc-only', 1)])
        # A different rule's registry row is untouched.
        self.assertEqual(0, retired[('empty', 1)])

    def test_keep_flag_supersedes_without_retiring(self):
        conn = self._ledger()
        self._decide(conn, 'fp-x', category='documentation', decided_by='doc-only')
        result = ledger_mod.supersede_rule(
            conn, rule_id='doc-only', version=1, superseded_at=LATER, retire=False)
        self.assertFalse(result.retired)
        self.assertEqual(1, result.decisions_superseded)
        retired = {(r['rule_id'], r['version']): r['retired']
                   for r in ledger_mod.registered_rules(conn)}
        self.assertEqual(0, retired[('doc-only', 1)])

    def test_higher_version_reregistration_takes_precedence_on_recompute(self):
        conn = self._ledger()
        # X v1 decides fp-x.  Supersede it, then append a NEW live decision (v2)
        # for the same fingerprint.  The recompute must pick the v2 decision.
        self._decide(conn, 'fp-x', category='documentation', decided_by='doc-only',
                     rule_version=1, decided_at=WHEN)
        ledger_mod.supersede_rule(conn, rule_id='doc-only', version=1, superseded_at=LATER)
        self.assertNotIn('fp-x', verdict_mod.current_verdict(conn))

        # Re-register a higher version and append its decision.
        ledger_mod.register_rules(conn, [ledger_mod.RegisteredRule(
            rule_id='doc-only', version=2, kind='heuristic', purity='pure',
            description='doc-only v2', category_enum_version=ledger_mod.CATEGORY_ENUM_VERSION)])
        self._decide(conn, 'fp-x', category='packaging', confidence='high',
                     decided_by='doc-only', rule_version=2, decided_at=LATEST)

        winner = verdict_mod.current_verdict(conn)['fp-x']
        self.assertEqual(2, winner.rule_version)
        self.assertEqual('packaging', winner.category)
        # fp-x is settled again, so it is no longer queued.
        self.assertEqual([], verdict_mod.queue(conn))

    def test_retire_rule_is_idempotent(self):
        conn = self._ledger()
        ledger_mod.retire_rule(conn, 'doc-only', 1)
        ledger_mod.retire_rule(conn, 'doc-only', 1)
        retired = {(r['rule_id'], r['version']): r['retired']
                   for r in ledger_mod.registered_rules(conn)}
        self.assertEqual(1, retired[('doc-only', 1)])


# ---------------------------------------------------------------------------
# CLI smoke tests.
# ---------------------------------------------------------------------------

MODE_ONLY = (
    'Index: pkg/script.sh\n'
    '===================================================================\n'
    'old mode 100644\n'
    'new mode 100755\n'
)

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


def _build_synthetic_corpus(corpus_dir):
    """Lay down two bodies + a phase-1 fingerprint index for a known-answer run."""
    bodies = {'mode.patch': MODE_ONLY, 'doc.patch': DOC_ONLY}
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
    ]

    index_path = os.path.join(corpus_dir, 'fingerprints.sqlite')
    connection = sqlite3.connect(index_path)
    try:
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
    return index_path


class LedgerCliTestCase(testtools.TestCase):

    def _corpus(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path = _build_synthetic_corpus(tmp.name)
        return tmp.name, index_path

    def _run_main(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = ledger_mod.main(argv)
        return code, out.getvalue()

    def test_build_then_report_smoke(self):
        corpus_dir, _ = self._corpus()
        ledger_path = os.path.join(corpus_dir, 'ledger.sqlite')

        code, build_out = self._run_main(['build', corpus_dir])
        self.assertEqual(0, code)
        self.assertTrue(os.path.exists(ledger_path))
        self.assertIn('# Ledger report', build_out)
        self.assertIn('built ledger', build_out)

        # The built ledger reproduces the deterministic verdicts.
        conn = sqlite3.connect(ledger_path)
        self.addCleanup(conn.close)
        verdicts = verdict_mod.current_verdict(conn)
        self.assertEqual(2, len(verdicts))

        code, report_out = self._run_main(['report', ledger_path])
        self.assertEqual(0, code)
        self.assertIn('# Ledger report', report_out)
        self.assertIn('Fingerprints with a live verdict: 2', report_out)

    def _add_human_decision(self, ledger_path):
        """Append an irreplaceable human decision to simulate review work at risk."""
        conn = sqlite3.connect(ledger_path)
        try:
            ledger_mod.append_decision(
                conn, fingerprint='h' * 64, category='packaging', confidence='high',
                decided_by='human-review', rule_version=1, kind='human', verified=True,
                evidence=None, decided_at=WHEN)
        finally:
            conn.close()

    def _set_stdin(self, text, *, tty):
        import sys

        class _Stdin(io.StringIO):
            def isatty(self):
                return tty

        original = sys.stdin
        sys.stdin = _Stdin(text)
        self.addCleanup(setattr, sys, 'stdin', original)

    def test_rebuild_refused_without_force_noninteractively(self):
        corpus_dir, _ = self._corpus()
        ledger_path = os.path.join(corpus_dir, 'ledger.sqlite')
        self._run_main(['build', corpus_dir])
        self._add_human_decision(ledger_path)

        self._set_stdin('', tty=False)  # no TTY -> must refuse, not silently wipe
        code, _ = self._run_main(['build', corpus_dir])
        self.assertEqual(1, code)
        # The human decision survived: the ledger was NOT wiped.
        conn = sqlite3.connect(ledger_path)
        self.addCleanup(conn.close)
        self.assertEqual(
            1, conn.execute("SELECT COUNT(*) FROM decision WHERE kind='human'").fetchone()[0])

    def test_rebuild_with_force_overwrites(self):
        corpus_dir, _ = self._corpus()
        ledger_path = os.path.join(corpus_dir, 'ledger.sqlite')
        self._run_main(['build', corpus_dir])
        self._add_human_decision(ledger_path)

        code, _ = self._run_main(['build', corpus_dir, '--force'])
        self.assertEqual(0, code)
        # --force wiped and rebuilt: the human decision is gone.
        conn = sqlite3.connect(ledger_path)
        self.addCleanup(conn.close)
        self.assertEqual(
            0, conn.execute("SELECT COUNT(*) FROM decision WHERE kind='human'").fetchone()[0])

    def test_rebuild_confirmed_interactively_proceeds(self):
        corpus_dir, _ = self._corpus()
        ledger_path = os.path.join(corpus_dir, 'ledger.sqlite')
        self._run_main(['build', corpus_dir])
        self._add_human_decision(ledger_path)

        self._set_stdin('wipe\n', tty=True)  # operator confirms
        code, _ = self._run_main(['build', corpus_dir])
        self.assertEqual(0, code)
        conn = sqlite3.connect(ledger_path)
        self.addCleanup(conn.close)
        self.assertEqual(
            0, conn.execute("SELECT COUNT(*) FROM decision WHERE kind='human'").fetchone()[0])

    def test_record_applies_rules_nondestructively(self):
        corpus_dir, _ = self._corpus()
        ledger_path = os.path.join(corpus_dir, 'ledger.sqlite')
        self._run_main(['build', corpus_dir])
        self._add_human_decision(ledger_path)

        code, out = self._run_main(['record', ledger_path, corpus_dir])
        self.assertEqual(0, code)
        self.assertIn('# Ledger report', out)
        self.assertIn('recorded into ledger', out)

        conn = sqlite3.connect(ledger_path)
        self.addCleanup(conn.close)
        # The human decision survived (record never recreates the ledger)...
        self.assertEqual(
            1, conn.execute("SELECT COUNT(*) FROM decision WHERE kind='human'").fetchone()[0])
        # ...and the recorded category-enum version reflects the current rules.
        meta = dict(conn.execute('SELECT key, value FROM meta').fetchall())
        self.assertEqual(str(ledger_mod.CATEGORY_ENUM_VERSION), meta['category_enum_version'])

    def test_record_refuses_unbuilt_ledger(self):
        corpus_dir, _ = self._corpus()
        missing = os.path.join(corpus_dir, 'nope.sqlite')
        code, _ = self._run_main(['record', missing, corpus_dir])
        self.assertEqual(1, code)

    def test_supersede_through_main(self):
        corpus_dir, _ = self._corpus()
        ledger_path = os.path.join(corpus_dir, 'ledger.sqlite')
        self._run_main(['build', corpus_dir])

        code, out = self._run_main(['supersede', ledger_path, 'doc-only', '1'])
        self.assertEqual(0, code)
        self.assertIn('superseded rule doc-only v1', out)
        self.assertIn('decisions=1', out)
        self.assertIn('queue size', out)

        # The doc-only fingerprint is now re-queued (no live decision); the
        # registry row is retired.
        conn = sqlite3.connect(ledger_path)
        self.addCleanup(conn.close)
        self.assertEqual(1, len(verdict_mod.queue(conn)))
        retired = {(r['rule_id'], r['version']): r['retired']
                   for r in ledger_mod.registered_rules(conn)}
        self.assertEqual(1, retired[('doc-only', 1)])

    def test_supersede_keep_does_not_retire(self):
        corpus_dir, _ = self._corpus()
        ledger_path = os.path.join(corpus_dir, 'ledger.sqlite')
        self._run_main(['build', corpus_dir])

        code, _ = self._run_main(['supersede', ledger_path, 'doc-only', '1', '--keep'])
        self.assertEqual(0, code)
        conn = sqlite3.connect(ledger_path)
        self.addCleanup(conn.close)
        retired = {(r['rule_id'], r['version']): r['retired']
                   for r in ledger_mod.registered_rules(conn)}
        self.assertEqual(0, retired[('doc-only', 1)])
