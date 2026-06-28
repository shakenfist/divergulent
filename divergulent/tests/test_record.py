"""Tests for divergulent.classify.record — the step-3b decision recorder.

All offline.  A small synthetic corpus + phase-1 fingerprint index is laid down
by hand (the test_classify / test_measure approach), a fresh ledger is created,
and ``record_to_ledger`` is run over it.  The known patches exercise the
mapping the recorder must get right:

  * a mode-only change      -> ONE 'packaging' decision, decided_by 'empty'
  * a doc-only change       -> ONE 'documentation' decision, decided_by 'doc-only'
  * a substantive code edit -> ONE 'unknown' decision, decided_by 'substantive'
  * a patch adding system("...") to a .c file -> an 'unknown'/substantive
    decision PLUS a dangerous-construct observation (the flag is never a
    category).

Coverage asserts: one live decision per fingerprint with the right
category/decided_by/rule_version/evidence; an observation per dangerous-construct
flag; and that a SECOND run appends nothing and duplicates no rows.
"""
import json
import os
import sqlite3
import tempfile

import testtools

from divergulent.classify import ledger as ledger_mod
from divergulent.classify import popcon as popcon_mod
from divergulent.classify import reach as reach_mod
from divergulent.classify import record
from divergulent.classify.corpus import body_sha256
from divergulent.classify.fingerprint import fingerprint
from divergulent.classify.rules import RULES_VERSION, _CATEGORY_RULES


WHEN = '2026-06-14T00:00:00Z'
LATER = '2026-06-15T00:00:00Z'


# Reuse the four known-answer bodies from the classify tests.
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

SUBSTANTIVE = (
    '--- a/src/widget.c\n'
    '+++ b/src/widget.c\n'
    '@@ -10,3 +10,3 @@\n'
    ' int n = compute();\n'
    '-    return n;\n'
    '+    return n + adjust(n);\n'
    ' }\n'
)

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
    """Lay down bodies + a phase-1 fingerprint index for a known-answer run."""
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


def _fp(text):
    return fingerprint(text)[1]


def _category_rule_version(rule_id):
    """The registered version of one content-category rule (from rules.py)."""
    for rid, version, _fn in _CATEGORY_RULES:
        if rid == rule_id:
            return version
    raise KeyError(rule_id)


class RecordToLedgerTestCase(testtools.TestCase):

    def _run(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path = _build_synthetic_corpus(tmp.name)
        path = os.path.join(tmp.name, 'ledger.sqlite')
        conn = ledger_mod.create_ledger(path)
        self.addCleanup(conn.close)
        stats = record.record_to_ledger(conn, tmp.name, index_path, now=WHEN)
        return conn, stats

    def _live_by_fingerprint(self, conn):
        return {row['fingerprint']: row for row in ledger_mod.live_decisions(conn)}

    def test_batched_decisions_persist_across_connections(self):
        # The recorder appends with commit=False and commits once at the end; a
        # SECOND connection must see the rows, proving the batch was committed.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path = _build_synthetic_corpus(tmp.name)
        path = os.path.join(tmp.name, 'ledger.sqlite')
        conn = ledger_mod.create_ledger(path)
        stats = record.record_to_ledger(conn, tmp.name, index_path, now=WHEN)
        conn.close()

        other = sqlite3.connect(path)
        self.addCleanup(other.close)
        (decisions,) = other.execute('SELECT COUNT(*) FROM decision').fetchone()
        (observations,) = other.execute('SELECT COUNT(*) FROM observation').fetchone()
        self.assertEqual(stats.decisions_appended, decisions)
        # The observation table now holds both the dangerous-construct flags and
        # one reviewability (size) observation per fingerprint.
        self.assertEqual(stats.observations_appended + stats.reviewability_appended, observations)
        self.assertGreater(decisions, 0)

    def test_one_live_decision_per_fingerprint(self):
        conn, stats = self._run()
        # Four distinct fingerprints -> four decisions.
        self.assertEqual(4, stats.fingerprints)
        self.assertEqual(4, stats.decisions_appended)
        self.assertEqual(0, stats.decisions_skipped)
        live = ledger_mod.live_decisions(conn)
        self.assertEqual(4, len(live))
        # Exactly one live decision per fingerprint.
        fps = [row['fingerprint'] for row in live]
        self.assertEqual(len(fps), len(set(fps)))

    def test_mode_only_decision_is_packaging_by_empty(self):
        conn, _ = self._run()
        row = self._live_by_fingerprint(conn)[_fp(MODE_ONLY)]
        self.assertEqual('packaging', row['category'])
        self.assertEqual('empty', row['decided_by'])
        self.assertEqual('high', row['confidence'])
        self.assertEqual('heuristic', row['kind'])
        self.assertEqual(_category_rule_version('empty'), row['rule_version'])
        self.assertEqual(WHEN, row['decided_at'])
        # Evidence carries the verdict signal.
        self.assertIn('normalises to empty', row['evidence'])

    def test_doc_only_decision_is_documentation(self):
        conn, _ = self._run()
        row = self._live_by_fingerprint(conn)[_fp(DOC_ONLY)]
        self.assertEqual('documentation', row['category'])
        self.assertEqual('doc-only', row['decided_by'])
        self.assertEqual(_category_rule_version('doc-only'), row['rule_version'])

    def test_substantive_decision_is_unknown_by_substantive(self):
        conn, _ = self._run()
        row = self._live_by_fingerprint(conn)[_fp(SUBSTANTIVE)]
        self.assertEqual('unknown', row['category'])
        self.assertEqual('substantive', row['decided_by'])
        self.assertEqual('low', row['confidence'])
        self.assertEqual(_category_rule_version('substantive'), row['rule_version'])

    def test_winning_rule_version_recorded_not_module_version(self):
        # The decision records the WINNING rule's own registered version (looked
        # up from the registry), never an unrelated module-level constant.
        conn, _ = self._run()
        by_fp = self._live_by_fingerprint(conn)
        for text, rule_id in (
                (MODE_ONLY, 'empty'), (DOC_ONLY, 'doc-only'),
                (SUBSTANTIVE, 'substantive'), (TROJAN, 'substantive')):
            row = by_fp[_fp(text)]
            self.assertEqual(rule_id, row['decided_by'])
            self.assertEqual(_category_rule_version(rule_id), row['rule_version'])

    def test_trojan_decision_is_substantive_with_observation(self):
        # The flag is an OBSERVATION, never a category: the decision is still
        # 'unknown'/substantive, and a dangerous-construct observation rides
        # alongside it.
        conn, stats = self._run()
        decision = self._live_by_fingerprint(conn)[_fp(TROJAN)]
        self.assertEqual('unknown', decision['category'])
        self.assertEqual('substantive', decision['decided_by'])

        self.assertEqual(1, stats.observations_appended)
        self.assertEqual(0, stats.observations_skipped)
        # The dangerous-construct flag (a reviewability observation also rides
        # alongside; select the flag specifically).
        flags = [o for o in ledger_mod.observations_for(conn, _fp(TROJAN))
                 if o['kind'] == 'dangerous-construct']
        self.assertEqual(1, len(flags))
        self.assertEqual('shell-out', flags[0]['detail'])
        self.assertEqual('dangerous-construct-scan', flags[0]['observed_by'])
        self.assertEqual(RULES_VERSION, flags[0]['rule_version'])
        self.assertEqual(WHEN, flags[0]['observed_at'])
        self.assertIn('system(', flags[0]['evidence'])

    def test_only_trojan_produces_a_dangerous_construct_observation(self):
        conn, _ = self._run()
        # Exactly one dangerous-construct flag, on the trojan. (Reviewability
        # observations ride alongside every fingerprint and are excluded here.)
        dangerous = [o for o in ledger_mod.live_observations(conn)
                     if o['kind'] == 'dangerous-construct']
        self.assertEqual(1, len(dangerous))
        self.assertEqual(_fp(TROJAN), dangerous[0]['fingerprint'])
        for text in (MODE_ONLY, DOC_ONLY, SUBSTANTIVE):
            flags = [o for o in ledger_mod.observations_for(conn, _fp(text))
                     if o['kind'] == 'dangerous-construct']
            self.assertEqual([], flags)

    def test_registry_is_populated(self):
        conn, _ = self._run()
        rows = {r['rule_id'] for r in ledger_mod.registered_rules(conn)}
        self.assertEqual({r.rule_id for r in ledger_mod.default_registry()}, rows)


class IdempotencyTestCase(testtools.TestCase):

    def _corpus_and_ledger(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path = _build_synthetic_corpus(tmp.name)
        path = os.path.join(tmp.name, 'ledger.sqlite')
        conn = ledger_mod.create_ledger(path)
        self.addCleanup(conn.close)
        return conn, tmp.name, index_path

    def test_second_run_appends_nothing(self):
        conn, corpus_dir, index_path = self._corpus_and_ledger()
        first = record.record_to_ledger(conn, corpus_dir, index_path, now=WHEN)
        self.assertEqual(4, first.decisions_appended)
        self.assertEqual(1, first.observations_appended)
        self.assertEqual(4, first.reviewability_appended)  # one per fingerprint

        # A second run -- even at a later timestamp -- must append nothing: every
        # decision/observation already exists live.
        second = record.record_to_ledger(conn, corpus_dir, index_path, now=LATER)
        self.assertEqual(0, second.decisions_appended)
        self.assertEqual(4, second.decisions_skipped)
        self.assertEqual(0, second.observations_appended)
        self.assertEqual(1, second.observations_skipped)
        self.assertEqual(0, second.reviewability_appended)
        self.assertEqual(4, second.reviewability_skipped)

    def test_second_run_does_not_duplicate_rows(self):
        conn, corpus_dir, index_path = self._corpus_and_ledger()
        record.record_to_ledger(conn, corpus_dir, index_path, now=WHEN)
        record.record_to_ledger(conn, corpus_dir, index_path, now=LATER)

        (decisions,) = conn.execute('SELECT COUNT(*) FROM decision').fetchone()
        (observations,) = conn.execute('SELECT COUNT(*) FROM observation').fetchone()
        self.assertEqual(4, decisions)
        # 1 dangerous-construct + 4 reviewability (one per fingerprint); the
        # second run duplicated none of them.
        self.assertEqual(5, observations)
        # And the surviving timestamps are the originals (nothing re-stamped).
        live = {row['fingerprint']: row for row in ledger_mod.live_decisions(conn)}
        self.assertEqual(WHEN, live[_fp(MODE_ONLY)]['decided_at'])


class ReconcileTestCase(testtools.TestCase):
    """``reconcile=True`` retires a fingerprint's stale heuristic decision when the
    winning rule changed -- keeping exactly one live deterministic decision, while
    llm/human verdicts (a different tier) are left untouched."""

    def _corpus_ledger(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path = _build_synthetic_corpus(tmp.name)
        conn = ledger_mod.create_ledger(os.path.join(tmp.name, 'ledger.sqlite'))
        self.addCleanup(conn.close)
        ledger_mod.register_rules(conn, ledger_mod.default_registry())
        record.record_to_ledger(conn, tmp.name, index_path, now=WHEN)
        return conn, tmp.name, index_path

    def _make_stale(self, conn, fp):
        # Simulate a pre-rule-change state: the real winner ('substantive') is
        # superseded and a DIFFERENT heuristic rule's decision is live instead.
        ledger_mod.supersede_decisions_for_fingerprint(
            conn, fingerprint=fp, kind='heuristic', superseded_at=WHEN)
        ledger_mod.append_decision(
            conn, fingerprint=fp, category='documentation', confidence='high',
            decided_by='doc-only', rule_version=1, kind='heuristic',
            evidence='stale', decided_at=WHEN)

    def _live_heuristic(self, conn, fp):
        return [r for r in ledger_mod.decisions_for(conn, fp)
                if r['kind'] == 'heuristic' and r['superseded_at'] is None]

    def test_reconcile_supersedes_stale_and_restores_winner(self):
        conn, corpus_dir, index_path = self._corpus_ledger()
        fp = _fp(SUBSTANTIVE)
        self._make_stale(conn, fp)
        # A verified llm verdict on the same fingerprint must survive reconcile.
        ledger_mod.append_decision(
            conn, fingerprint=fp, category='bugfix', confidence='high',
            decided_by='llm-triage:m', rule_version=1, kind='llm', verified=True,
            evidence=None, decided_at=WHEN)

        stats = record.record_to_ledger(
            conn, corpus_dir, index_path, now=LATER, reconcile=True)

        live = self._live_heuristic(conn, fp)
        self.assertEqual(1, len(live))                          # exactly one live heuristic
        self.assertEqual('substantive', live[0]['decided_by'])  # the real winner restored
        self.assertEqual('unknown', live[0]['category'])
        self.assertEqual(1, stats.decisions_superseded)
        # The llm decision is untouched.
        llm = [r for r in ledger_mod.decisions_for(conn, fp)
               if r['kind'] == 'llm' and r['superseded_at'] is None]
        self.assertEqual(1, len(llm))
        self.assertEqual('bugfix', llm[0]['category'])

    def test_default_does_not_supersede(self):
        # Without reconcile, the stale decision is left live -> two live heuristic
        # decisions (the messy state reconcile exists to fix).
        conn, corpus_dir, index_path = self._corpus_ledger()
        fp = _fp(SUBSTANTIVE)
        self._make_stale(conn, fp)

        stats = record.record_to_ledger(conn, corpus_dir, index_path, now=LATER)

        self.assertEqual(0, stats.decisions_superseded)
        live = self._live_heuristic(conn, fp)
        self.assertEqual(2, len(live))
        self.assertEqual({'doc-only', 'substantive'}, {r['decided_by'] for r in live})


def _add_package_and_popcon(corpus_dir, index_path):
    """Extend the synthetic corpus with a package.binaries table + popcon snapshot.

    Maps each source to a single binary with a chosen install count so the four
    distinct fingerprints land in known reach buckets; pkg-c and pkg-d share the
    SUBSTANTIVE fingerprint, so its reach must be the MAX over both their binaries.
    Returns the popcon snapshot path.
    """
    conn = sqlite3.connect(index_path)
    try:
        conn.execute('CREATE TABLE package (source_package TEXT, version TEXT, '
                     'changelog_date TEXT, binaries TEXT)')
        conn.executemany(
            'INSERT INTO package (source_package, binaries) VALUES (?, ?)',
            [('pkg-a', json.dumps(['aaa'])),
             ('pkg-b', json.dumps(['bbb'])),
             ('pkg-c', json.dumps(['ccc'])),
             ('pkg-d', json.dumps(['ddd'])),
             ('pkg-e', json.dumps(['eee']))])
        conn.commit()
    finally:
        conn.close()
    popcon_path = os.path.join(corpus_dir, 'popcon.sqlite')
    popcon_mod.write_snapshot(
        popcon_path,
        [('aaa', 278978, 142127),   # anchor -> XL
         ('bbb', 137, 50),          # XS
         ('ccc', 5000, 2000),       # the lower of the shared fp's two binaries
         ('ddd', 50000, 20000),     # the higher -> shared fp buckets L
         ('eee', 137, 50)],         # XS
        snapshot_date='2026-06-28', source_url='test')
    return popcon_path


class ReachRecordingTestCase(testtools.TestCase):

    def _corpus(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path = _build_synthetic_corpus(tmp.name)
        popcon_path = _add_package_and_popcon(tmp.name, index_path)
        path = os.path.join(tmp.name, 'ledger.sqlite')
        conn = ledger_mod.create_ledger(path)
        self.addCleanup(conn.close)
        return conn, tmp.name, index_path, popcon_path

    def test_reach_observations_recorded_with_expected_levels(self):
        conn, corpus_dir, index_path, popcon_path = self._corpus()
        stats = record.record_to_ledger(
            conn, corpus_dir, index_path, now=WHEN, popcon_path=popcon_path)

        levels = reach_mod.reach_by_fingerprint(conn)
        self.assertEqual('XL', levels[_fp(MODE_ONLY)])
        self.assertEqual('XS', levels[_fp(DOC_ONLY)])
        self.assertEqual('L', levels[_fp(SUBSTANTIVE)])   # MAX over pkg-c/pkg-d binaries
        self.assertEqual('XS', levels[_fp(TROJAN)])
        self.assertEqual(4, stats.reach_appended)         # one per distinct fingerprint
        self.assertEqual(0, stats.reach_unknown)

    def test_no_snapshot_records_no_reach(self):
        conn, corpus_dir, index_path, _popcon = self._corpus()
        stats = record.record_to_ledger(conn, corpus_dir, index_path, now=WHEN)
        self.assertEqual(0, stats.reach_appended)
        self.assertEqual(0, stats.reach_unknown)
        self.assertEqual({}, reach_mod.reach_by_fingerprint(conn))

    def test_second_run_is_idempotent(self):
        conn, corpus_dir, index_path, popcon_path = self._corpus()
        record.record_to_ledger(conn, corpus_dir, index_path, now=WHEN, popcon_path=popcon_path)
        stats = record.record_to_ledger(
            conn, corpus_dir, index_path, now=LATER, popcon_path=popcon_path)
        self.assertEqual(0, stats.reach_appended)
        self.assertEqual(4, stats.reach_skipped)

    def test_changed_snapshot_supersedes_prior_level(self):
        conn, corpus_dir, index_path, popcon_path = self._corpus()
        record.record_to_ledger(conn, corpus_dir, index_path, now=WHEN, popcon_path=popcon_path)

        # Re-pin a snapshot where pkg-b's binary is now near-universal: its
        # fingerprint's reach must move XS -> XL, superseding the prior row.
        popcon_mod.write_snapshot(
            popcon_path,
            [('aaa', 278978, 142127), ('bbb', 270000, 200000),
             ('ccc', 5000, 2000), ('ddd', 50000, 20000), ('eee', 137, 50)],
            snapshot_date='2026-07-01', source_url='test')
        stats = record.record_to_ledger(
            conn, corpus_dir, index_path, now=LATER, popcon_path=popcon_path)

        self.assertEqual('XL', reach_mod.reach_by_fingerprint(conn)[_fp(DOC_ONLY)])
        self.assertEqual(1, stats.reach_appended)   # only the changed fingerprint
        self.assertEqual(3, stats.reach_skipped)

    def test_count_drift_without_bucket_change_does_not_churn(self):
        # The key no-churn property: a fresh snapshot whose install counts drift
        # but whose buckets are unchanged must re-record nothing (the skip key is
        # the bucket, not the raw counts/date) -- so a daily popcon refresh does
        # not rewrite ~60k rows.
        conn, corpus_dir, index_path, popcon_path = self._corpus()
        record.record_to_ledger(conn, corpus_dir, index_path, now=WHEN, popcon_path=popcon_path)

        popcon_mod.write_snapshot(
            popcon_path,
            [('aaa', 278000, 142127), ('bbb', 140, 50),     # drifted, same buckets
             ('ccc', 5100, 2000), ('ddd', 49000, 20000), ('eee', 150, 50)],
            snapshot_date='2026-07-01', source_url='test')
        stats = record.record_to_ledger(
            conn, corpus_dir, index_path, now=LATER, popcon_path=popcon_path)

        self.assertEqual(0, stats.reach_appended)
        self.assertEqual(4, stats.reach_skipped)
