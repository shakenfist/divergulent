"""Tests for divergulent.classify.triage_record -- the step-4c LLM recorder.

All OFFLINE: the ledger is built directly with ``create_ledger`` /
``append_decision``, and the :class:`triage.TriageResult` is constructed by hand
from an :class:`triage.LlmVerdict` and a :class:`triage.Verification` -- no LLM
``call``, no corpus, no network.

Coverage:

  * a ``verified`` result appends a VERIFIED llm decision and NO review item;
  * a ``needs_human`` result appends an UNVERIFIED llm decision AND a pending
    review item;
  * a second run is idempotent (nothing appended, no duplicate rows);
  * the model is encoded into the rule identity (``decided_by``) and the prompt
    version into the integer ``rule_version``, and the rule is registered;
  * the evidence records both the draft and the verification.
"""
import json
import os
import tempfile

import testtools

from divergulent.classify import ledger as ledger_mod
from divergulent.classify import triage_record
from divergulent.classify.triage import LlmVerdict, TriageResult, Verification


WHEN = '2026-06-14T00:00:00Z'
LATER = '2026-06-15T00:00:00Z'

MODEL = 'claude-sonnet-4-6'
DECIDED_BY = 'llm-triage:claude-sonnet-4-6'


def _verified_result(category='bugfix', confidence='high', model=MODEL):
    """A TriageResult that routes to 'verified'."""
    draft = LlmVerdict(
        category=category, confidence=confidence, reasoning='enlarges a buffer',
        model=model, prompt_version=1, raw_response='{"category": "bugfix"}')
    verification = Verification(
        agrees=True, confidence='high', reasoning='supports the category',
        model=model, prompt_version=1, raw_response='{"agrees": true}')
    return TriageResult(
        draft=draft, verification=verification, routing='verified',
        reason='verifier confirmed the drafted category at sufficient confidence')


def _needs_human_result(category='security', confidence='medium', model=MODEL):
    """A TriageResult that routes to 'needs_human'."""
    draft = LlmVerdict(
        category=category, confidence=confidence, reasoning='looks security-relevant',
        model=model, prompt_version=1, raw_response='{"category": "security"}')
    verification = Verification(
        agrees=False, confidence='low', reasoning='cannot confirm from the diff',
        model=model, prompt_version=1, raw_response='{"agrees": false}')
    return TriageResult(
        draft=draft, verification=verification, routing='needs_human',
        reason='verifier refuted the drafted category')


class TriageRecordFixture:
    """Mixin: a fresh, registry-populated ledger."""

    def _ledger(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, 'ledger.sqlite')
        conn = ledger_mod.create_ledger(path)
        self.addCleanup(conn.close)
        ledger_mod.register_rules(conn, ledger_mod.default_registry())
        return conn


class VerifiedResultTestCase(TriageRecordFixture, testtools.TestCase):

    def test_appends_verified_decision_and_no_review_item(self):
        conn = self._ledger()
        stats = triage_record.record_triage_result(
            conn, 'fp1', _verified_result(), now=WHEN)
        self.assertTrue(stats.decision_appended)
        self.assertTrue(stats.verified)
        self.assertFalse(stats.review_appended)

        rows = ledger_mod.live_decisions(conn)
        self.assertEqual(1, len(rows))
        decision = rows[0]
        self.assertEqual('fp1', decision['fingerprint'])
        self.assertEqual('bugfix', decision['category'])
        self.assertEqual('high', decision['confidence'])
        self.assertEqual('llm', decision['kind'])
        self.assertEqual(1, decision['verified'])
        self.assertEqual(DECIDED_BY, decision['decided_by'])
        self.assertEqual(1, decision['rule_version'])
        self.assertEqual(WHEN, decision['decided_at'])
        # No review item for a verified result.
        self.assertEqual([], ledger_mod.pending_review_items(conn))

    def test_model_is_the_rule_identity_prompt_is_the_version(self):
        conn = self._ledger()
        triage_record.record_triage_result(conn, 'fp1', _verified_result(), now=WHEN)
        # The model lives in decided_by; the prompt version is the integer
        # rule_version.  The rule is registered as kind='llm'.
        registry = {r['rule_id']: r for r in ledger_mod.registered_rules(conn)}
        self.assertIn(DECIDED_BY, registry)
        rule = registry[DECIDED_BY]
        self.assertEqual('llm', rule['kind'])
        self.assertEqual('pure', rule['purity'])
        self.assertEqual(1, rule['version'])
        self.assertIn(MODEL, rule['description'])

    def test_changing_the_model_is_a_new_rule_identity(self):
        conn = self._ledger()
        triage_record.record_triage_result(conn, 'fp1', _verified_result(), now=WHEN)
        triage_record.record_triage_result(
            conn, 'fp1', _verified_result(model='claude-opus-4-6'), now=LATER)
        decided_by = {r['decided_by'] for r in ledger_mod.live_decisions(conn)}
        self.assertEqual(
            {'llm-triage:claude-sonnet-4-6', 'llm-triage:claude-opus-4-6'}, decided_by)

    def test_evidence_records_both_draft_and_verification(self):
        conn = self._ledger()
        triage_record.record_triage_result(conn, 'fp1', _verified_result(), now=WHEN)
        evidence = json.loads(ledger_mod.live_decisions(conn)[0]['evidence'])
        self.assertEqual('verified', evidence['routing'])
        self.assertEqual('{"category": "bugfix"}', evidence['draft']['raw_response'])
        self.assertTrue(evidence['verification']['agrees'])
        self.assertEqual('{"agrees": true}', evidence['verification']['raw_response'])

    def test_no_register_skips_rule_row(self):
        conn = self._ledger()
        triage_record.record_triage_result(
            conn, 'fp1', _verified_result(), now=WHEN, register=False)
        registry = {r['rule_id'] for r in ledger_mod.registered_rules(conn)}
        self.assertNotIn(DECIDED_BY, registry)
        # But the decision was still appended.
        self.assertEqual(1, len(ledger_mod.live_decisions(conn)))


class NeedsHumanResultTestCase(TriageRecordFixture, testtools.TestCase):

    def test_appends_unverified_decision_and_pending_review_item(self):
        conn = self._ledger()
        stats = triage_record.record_triage_result(
            conn, 'fp1', _needs_human_result(), now=WHEN)
        self.assertTrue(stats.decision_appended)
        self.assertFalse(stats.verified)
        self.assertTrue(stats.review_appended)

        decision = ledger_mod.live_decisions(conn)[0]
        self.assertEqual('security', decision['category'])
        self.assertEqual('llm', decision['kind'])
        self.assertEqual(0, decision['verified'])

        pending = ledger_mod.pending_review_items(conn)
        self.assertEqual(1, len(pending))
        self.assertEqual('fp1', pending[0]['fingerprint'])
        self.assertEqual('verifier refuted the drafted category', pending[0]['reason'])
        self.assertEqual('security', pending[0]['draft_category'])
        self.assertEqual('medium', pending[0]['draft_confidence'])


class IdempotencyTestCase(TriageRecordFixture, testtools.TestCase):

    def test_second_run_appends_nothing(self):
        conn = self._ledger()
        first = triage_record.record_triage_result(
            conn, 'fp1', _needs_human_result(), now=WHEN)
        self.assertTrue(first.decision_appended)
        self.assertTrue(first.review_appended)

        # A second run -- even at a later timestamp -- appends nothing: the
        # decision exists live and a pending review item already awaits.
        second = triage_record.record_triage_result(
            conn, 'fp1', _needs_human_result(), now=LATER)
        self.assertFalse(second.decision_appended)
        self.assertTrue(second.decision_skipped)
        self.assertFalse(second.review_appended)
        self.assertTrue(second.review_skipped)

        (decisions,) = conn.execute('SELECT COUNT(*) FROM decision').fetchone()
        (items,) = conn.execute('SELECT COUNT(*) FROM review_queue').fetchone()
        self.assertEqual(1, decisions)
        self.assertEqual(1, items)
        # The original timestamp survives (nothing re-stamped).
        self.assertEqual(WHEN, ledger_mod.live_decisions(conn)[0]['decided_at'])
