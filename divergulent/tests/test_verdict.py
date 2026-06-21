"""Tests for divergulent.classify.verdict — the step-3c derived view + queue.

All offline.  Each test hand-builds a small ledger with ``ledger.create_ledger``
and ``append_decision`` / ``append_observation`` directly — no corpus, no rule
engine — so the precedence, queue, materialiser, and report logic is exercised
in isolation from how decisions actually get recorded.

The cases that matter:

  * Precedence: human beats llm beats heuristic for one fingerprint; a
    superseded higher-kind decision is ignored (falls back to the live one).
  * Tie-breaks: most-recent ``decided_at`` wins within a kind; same timestamp
    falls through to confidence.
  * Queue: a live ``unknown`` verdict is queued, a settled ``packaging`` is not,
    and a fingerprint with every decision superseded (no live verdict) is queued.
  * Materialiser: ``rebuild_current_verdict`` writes one row per fingerprint
    matching the query, and a second rebuild is stable.
  * Report: category counts and queue size match the ledger.
"""
import os
import tempfile

import testtools

from divergulent.classify import ledger as ledger_mod
from divergulent.classify import verdict as verdict_mod


WHEN = '2026-06-14T00:00:00Z'
LATER = '2026-06-15T00:00:00Z'
LATEST = '2026-06-16T00:00:00Z'


class VerdictFixture:
    """Mixin: a fresh, registry-populated ledger and append shortcuts."""

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


class PrecedenceTestCase(VerdictFixture, testtools.TestCase):

    def test_human_beats_llm_beats_heuristic(self):
        conn = self._ledger()
        # All three kinds live for one fingerprint; human must win.
        self._decide(conn, 'fp1', category='unknown', kind='heuristic', decided_at=WHEN)
        self._decide(conn, 'fp1', category='documentation', confidence='medium',
                     decided_by='llm-triage', kind='llm', decided_at=LATER)
        self._decide(conn, 'fp1', category='packaging', confidence='high',
                     decided_by='human-review', kind='human', decided_at=LATEST)
        winner = verdict_mod.current_verdict(conn)['fp1']
        self.assertEqual('human', winner.kind)
        self.assertEqual('packaging', winner.category)
        self.assertEqual('human-review', winner.decided_by)

    def test_llm_beats_heuristic_when_no_human(self):
        conn = self._ledger()
        self._decide(conn, 'fp1', category='unknown', kind='heuristic', decided_at=LATEST)
        self._decide(conn, 'fp1', category='documentation', confidence='medium',
                     decided_by='llm-triage', kind='llm', decided_at=WHEN)
        winner = verdict_mod.current_verdict(conn)['fp1']
        # kind outranks recency: the older llm decision still beats the newer
        # heuristic one.
        self.assertEqual('llm', winner.kind)
        self.assertEqual('documentation', winner.category)

    def test_superseded_higher_kind_is_ignored(self):
        conn = self._ledger()
        heuristic_id = self._decide(conn, 'fp1', category='unknown', kind='heuristic')
        human_id = self._decide(conn, 'fp1', category='packaging', confidence='high',
                                decided_by='human-review', kind='human', decided_at=LATER)
        # The human decision is superseded -> it must drop out of the view, which
        # falls back to the live heuristic one.
        conn.execute('UPDATE decision SET superseded_at = ? WHERE id = ?',
                     (LATEST, human_id))
        conn.commit()
        winner = verdict_mod.current_verdict(conn)['fp1']
        self.assertEqual('heuristic', winner.kind)
        self.assertEqual('unknown', winner.category)
        self.assertEqual(heuristic_id, winner.decision_id)


class TieBreakTestCase(VerdictFixture, testtools.TestCase):

    def test_most_recent_decided_at_wins_within_kind(self):
        conn = self._ledger()
        self._decide(conn, 'fp1', category='packaging', decided_by='whitespace-only',
                     decided_at=WHEN)
        self._decide(conn, 'fp1', category='documentation', decided_by='doc-only',
                     decided_at=LATER)
        winner = verdict_mod.current_verdict(conn)['fp1']
        self.assertEqual('documentation', winner.category)
        self.assertEqual('doc-only', winner.decided_by)

    def test_same_timestamp_falls_through_to_confidence(self):
        conn = self._ledger()
        self._decide(conn, 'fp1', category='unknown', confidence='low',
                     decided_by='substantive', decided_at=WHEN)
        self._decide(conn, 'fp1', category='packaging', confidence='high',
                     decided_by='empty', decided_at=WHEN)
        winner = verdict_mod.current_verdict(conn)['fp1']
        # Same kind, same timestamp -> higher confidence wins.
        self.assertEqual('high', winner.confidence)
        self.assertEqual('packaging', winner.category)

    def test_full_tie_breaks_on_decision_id(self):
        conn = self._ledger()
        first = self._decide(conn, 'fp1', category='packaging', confidence='high',
                             decided_by='empty', decided_at=WHEN)
        second = self._decide(conn, 'fp1', category='packaging', confidence='high',
                              decided_by='whitespace-only', decided_at=WHEN)
        self.assertGreater(second, first)
        # Same kind / timestamp / confidence -> most recent insert (id) wins.
        winner = verdict_mod.current_verdict(conn)['fp1']
        self.assertEqual(second, winner.decision_id)


class QueueTestCase(VerdictFixture, testtools.TestCase):

    def test_unknown_is_queued_settled_is_not(self):
        conn = self._ledger()
        self._decide(conn, 'unknown-fp', category='unknown', decided_by='substantive')
        self._decide(conn, 'settled-fp', category='packaging', confidence='high',
                     decided_by='empty')
        self.assertEqual(['unknown-fp'], verdict_mod.queue(conn))

    def test_all_superseded_fingerprint_is_queued(self):
        conn = self._ledger()
        self._decide(conn, 'settled-fp', category='packaging', confidence='high',
                     decided_by='empty')
        dead_id = self._decide(conn, 'dead-fp', category='documentation',
                               decided_by='doc-only')
        # Every decision for dead-fp superseded -> no live verdict -> re-queued.
        conn.execute('UPDATE decision SET superseded_at = ? WHERE id = ?',
                     (LATER, dead_id))
        conn.commit()
        self.assertNotIn('dead-fp', verdict_mod.current_verdict(conn))
        self.assertEqual(['dead-fp'], verdict_mod.queue(conn))

    def test_queue_is_sorted_and_deduplicated(self):
        conn = self._ledger()
        # Two unknowns out of insertion order, and a settled one.
        self._decide(conn, 'fp-z', category='unknown', decided_by='substantive')
        self._decide(conn, 'fp-a', category='unknown', decided_by='substantive')
        self._decide(conn, 'fp-m', category='packaging', confidence='high',
                     decided_by='empty')
        self.assertEqual(['fp-a', 'fp-z'], verdict_mod.queue(conn))

    def test_unknown_and_all_superseded_unioned(self):
        conn = self._ledger()
        self._decide(conn, 'live-unknown', category='unknown', decided_by='substantive')
        dead_id = self._decide(conn, 'dead-fp', category='packaging',
                               confidence='high', decided_by='empty')
        conn.execute('UPDATE decision SET superseded_at = ? WHERE id = ?',
                     (LATER, dead_id))
        conn.commit()
        self.assertEqual(['dead-fp', 'live-unknown'], verdict_mod.queue(conn))


class MaterialiserTestCase(VerdictFixture, testtools.TestCase):

    def _seed(self, conn):
        self._decide(conn, 'fp-pack', category='packaging', confidence='high',
                     decided_by='empty')
        self._decide(conn, 'fp-doc', category='documentation', decided_by='doc-only')
        self._decide(conn, 'fp-unknown', category='unknown', decided_by='substantive')

    def test_one_row_per_fingerprint_matching_the_query(self):
        conn = self._ledger()
        self._seed(conn)
        count = verdict_mod.rebuild_current_verdict(conn)
        self.assertEqual(3, count)

        rows = conn.execute(
            'SELECT fingerprint, category, decided_by, rule_version, kind, '
            'confidence, evidence, decided_at, decision_id FROM current_verdict').fetchall()
        materialised = {row[0]: row for row in rows}
        self.assertEqual(3, len(materialised))

        derived = verdict_mod.current_verdict(conn)
        self.assertEqual(set(derived), set(materialised))
        for fp, v in derived.items():
            row = materialised[fp]
            self.assertEqual(
                (v.fingerprint, v.category, v.decided_by, v.rule_version, v.kind,
                 v.confidence, v.evidence, v.decided_at, v.decision_id),
                tuple(row))

    def test_second_rebuild_is_stable(self):
        conn = self._ledger()
        self._seed(conn)
        verdict_mod.rebuild_current_verdict(conn)
        first = conn.execute(
            'SELECT * FROM current_verdict ORDER BY fingerprint').fetchall()
        # Rebuilding over an unchanged ledger reproduces the same rows.
        verdict_mod.rebuild_current_verdict(conn)
        second = conn.execute(
            'SELECT * FROM current_verdict ORDER BY fingerprint').fetchall()
        self.assertEqual([tuple(r) for r in first], [tuple(r) for r in second])

    def test_materialiser_reflects_supersession_on_rebuild(self):
        conn = self._ledger()
        live_id = self._decide(conn, 'fp1', category='unknown', decided_by='substantive')
        human_id = self._decide(conn, 'fp1', category='packaging', confidence='high',
                                decided_by='human-review', kind='human', decided_at=LATER)
        verdict_mod.rebuild_current_verdict(conn)
        (cat,) = conn.execute(
            'SELECT category FROM current_verdict WHERE fingerprint = ?', ('fp1',)).fetchone()
        self.assertEqual('packaging', cat)

        # Supersede the human decision and rebuild: the cache must rebuild from
        # the query, falling back to the live heuristic.
        conn.execute('UPDATE decision SET superseded_at = ? WHERE id = ?',
                     (LATEST, human_id))
        conn.commit()
        verdict_mod.rebuild_current_verdict(conn)
        cat, decision_id = conn.execute(
            'SELECT category, decision_id FROM current_verdict '
            'WHERE fingerprint = ?', ('fp1',)).fetchone()
        self.assertEqual('unknown', cat)
        self.assertEqual(live_id, decision_id)


class ReportTestCase(VerdictFixture, testtools.TestCase):

    def _seed(self, conn):
        # Three packaging, two documentation, two unknown, plus a dead one.
        for fp in ('p1', 'p2', 'p3'):
            self._decide(conn, fp, category='packaging', confidence='high', decided_by='empty')
        for fp in ('d1', 'd2'):
            self._decide(conn, fp, category='documentation', decided_by='doc-only')
        for fp in ('u1', 'u2'):
            self._decide(conn, fp, category='unknown', decided_by='substantive')
        dead_id = self._decide(conn, 'dead', category='packaging', confidence='high',
                               decided_by='empty')
        conn.execute('UPDATE decision SET superseded_at = ? WHERE id = ?', (LATER, dead_id))
        conn.commit()
        ledger_mod.append_observation(
            conn, fingerprint='u1', kind='dangerous-construct', detail='shell-out',
            evidence='system(x)', observed_by='dangerous-construct-scan',
            rule_version=1, observed_at=WHEN)

    def test_category_counts_and_queue_size_match(self):
        conn = self._ledger()
        self._seed(conn)
        summary = verdict_mod.summarise_ledger(conn)
        # 3 packaging + 2 documentation + 2 unknown live verdicts (dead excluded).
        self.assertEqual(7, summary.fingerprints_with_verdict)
        self.assertEqual(
            {'packaging': 3, 'documentation': 2, 'unknown': 2},
            summary.verdicts_by_category)
        # Queue = 2 live unknowns + 1 all-superseded fingerprint.
        self.assertEqual(3, summary.queue_size)
        self.assertEqual(len(verdict_mod.queue(conn)), summary.queue_size)

    def test_decisions_by_rule_and_observations(self):
        conn = self._ledger()
        self._seed(conn)
        summary = verdict_mod.summarise_ledger(conn)
        self.assertEqual(
            {'empty': 3, 'doc-only': 2, 'substantive': 2},
            summary.decisions_by_rule)
        self.assertEqual({'shell-out': 1}, summary.observations_by_detail)
        self.assertEqual(1, summary.superseded_decisions)

    def test_render_report_contains_headline_and_counts(self):
        conn = self._ledger()
        self._seed(conn)
        report = verdict_mod.render_report(verdict_mod.summarise_ledger(conn))
        self.assertIn('# Ledger report', report)
        self.assertIn('Fingerprints with a live verdict: 7', report)
        self.assertIn('Queue size (phase-4 residue): 3', report)
        self.assertIn('Superseded decisions (audit trail): 1', report)
        self.assertIn('- packaging: 3', report)
        self.assertIn('- shell-out: 1', report)
