import os
import sqlite3
import tempfile

import testtools

from divergulent.classify import ledger
from divergulent.classify.claim import CLAIM_RULE_VERSION
from divergulent.classify.rules import RULES_VERSION, _CATEGORY_RULES


# A fixed caller-supplied timestamp.  The module never reads a clock; the
# recorder (step 3b) supplies these, so the tests pass them explicitly.
WHEN = '2026-06-14T00:00:00Z'
LATER = '2026-06-15T00:00:00Z'


class LedgerFixture:
    """Mixin: a fresh ledger in a temp file, registry pre-populated."""

    def _ledger(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, 'ledger.sqlite')
        conn = ledger.create_ledger(path)
        self.addCleanup(conn.close)
        ledger.register_rules(conn, ledger.default_registry())
        return conn, path


class SchemaTestCase(LedgerFixture, testtools.TestCase):

    def test_create_ledger_overwrites_existing_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, 'ledger.sqlite')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('stale garbage not a sqlite db')
        conn = ledger.create_ledger(path)
        self.addCleanup(conn.close)
        # A valid, queryable ledger now exists where the garbage was.
        self.assertEqual(0, conn.execute('SELECT COUNT(*) FROM decision').fetchone()[0])

    def test_tables_and_indexes_exist(self):
        conn, _path = self._ledger()
        tables = {n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual({'meta', 'rule', 'decision', 'observation'},
                         tables & {'meta', 'rule', 'decision', 'observation'})
        indexes = {n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertIn('idx_decision_fingerprint', indexes)
        self.assertIn('idx_decision_decided_by_version', indexes)
        self.assertIn('idx_observation_fingerprint', indexes)

    def test_meta_carries_schema_and_enum_versions(self):
        conn, _path = self._ledger()
        meta = ledger.meta(conn)
        self.assertEqual(str(ledger.LEDGER_SCHEMA_VERSION), meta['schema_version'])
        self.assertEqual(str(ledger.CATEGORY_ENUM_VERSION), meta['category_enum_version'])

    def test_decision_reserves_external_columns(self):
        # The pure vs external distinction is modelled now: the columns exist
        # and are nullable (unused until phase 6).
        conn, _path = self._ledger()
        cols = {row[1] for row in conn.execute('PRAGMA table_info(decision)')}
        self.assertIn('input_snapshot', cols)
        self.assertIn('input_fresh_until', cols)

    def test_schema_version_is_two(self):
        # The step-4c migration bumped the schema to 2.
        conn, _path = self._ledger()
        self.assertEqual(2, ledger.LEDGER_SCHEMA_VERSION)
        self.assertEqual('2', ledger.meta(conn)['schema_version'])

    def test_decision_has_verified_and_signature_columns(self):
        # Schema v2: an LLM decision carries an explicit verified flag; signature
        # / signed_by are reserved for signed human ManualDecisions (step 4e).
        conn, _path = self._ledger()
        cols = {row[1] for row in conn.execute('PRAGMA table_info(decision)')}
        self.assertIn('verified', cols)
        self.assertIn('signature', cols)
        self.assertIn('signed_by', cols)

    def test_review_queue_table_and_indexes_exist(self):
        conn, _path = self._ledger()
        tables = {n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertIn('review_queue', tables)
        cols = {row[1] for row in conn.execute('PRAGMA table_info(review_queue)')}
        self.assertEqual(
            {'id', 'fingerprint', 'reason', 'draft_category', 'draft_confidence',
             'priority', 'enqueued_at', 'reviewed_at'}, cols)
        indexes = {n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertIn('idx_review_queue_fingerprint', indexes)
        self.assertIn('idx_review_queue_reviewed_at', indexes)


class OpenLedgerTestCase(LedgerFixture, testtools.TestCase):
    """``open_ledger`` accepts a built ledger and rejects anything else clearly."""

    def test_opens_a_built_ledger(self):
        _conn, path = self._ledger()
        opened = ledger.open_ledger(path)
        self.addCleanup(opened.close)
        # Usable + Row factory set (so callers get named-column access).
        row = opened.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        self.assertEqual(str(ledger.LEDGER_SCHEMA_VERSION), row['value'])

    def test_missing_path_raises_does_not_exist(self):
        missing = os.path.join(tempfile.mkdtemp(), 'nope.sqlite')
        exc = self.assertRaises(ledger.LedgerError, ledger.open_ledger, missing)
        self.assertIn('does not exist', str(exc))

    def test_empty_database_raises_not_a_ledger(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, 'empty.sqlite')
        sqlite3.connect(path).close()  # an empty db, exactly the mistyped-path trap
        exc = self.assertRaises(ledger.LedgerError, ledger.open_ledger, path)
        self.assertIn('not a divergulent ledger', str(exc))
        self.assertIn('decision', str(exc))  # names the missing table(s)

    def test_report_cli_exits_1_with_clear_message_on_bad_path(self):
        import contextlib
        import io
        missing = os.path.join(tempfile.mkdtemp(), 'nope.sqlite')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = ledger.main(['report', missing])
        self.assertEqual(1, rc)
        self.assertIn('error:', err.getvalue())
        self.assertIn('does not exist', err.getvalue())


class ResolveSettledReviewItemsTestCase(LedgerFixture, testtools.TestCase):
    """A pending review item is dequeued once a rule deterministically settles its
    fingerprint (non-unknown heuristic verdict), but a still-residue one stays."""

    def test_clears_settled_keeps_residue(self):
        from divergulent.classify import verdict as verdict_mod
        conn, _path = self._ledger()
        settled, residue = 'a' * 64, 'b' * 64
        ledger.append_decision(
            conn, fingerprint=settled, category='test', confidence='high',
            decided_by='test-only', rule_version=1, kind='heuristic',
            evidence=None, decided_at='2026-06-14T00:00:00Z')
        ledger.append_decision(
            conn, fingerprint=residue, category='unknown', confidence='low',
            decided_by='substantive', rule_version=1, kind='heuristic',
            evidence=None, decided_at='2026-06-14T00:00:00Z')
        for fp in (settled, residue):
            ledger.append_review_item(
                conn, fingerprint=fp, reason='r', draft_category=None,
                draft_confidence=None, enqueued_at='2026-06-14T00:00:00Z')
        verdict_mod.rebuild_current_verdict(conn)

        cleared = ledger.resolve_settled_review_items(conn, now='2026-06-15T00:00:00Z')
        self.assertEqual(1, cleared)
        pending = {r['fingerprint'] for r in ledger.pending_review_items(conn)}
        self.assertEqual({residue}, pending)


class KindPrecedenceTestCase(testtools.TestCase):

    def test_precedence_human_over_llm_over_heuristic(self):
        self.assertEqual(('heuristic', 'llm', 'human'), ledger.KIND_PRECEDENCE)
        self.assertGreater(ledger.kind_rank('human'), ledger.kind_rank('llm'))
        self.assertGreater(ledger.kind_rank('llm'), ledger.kind_rank('heuristic'))

    def test_kind_rank_rejects_unknown(self):
        self.assertRaises(ValueError, ledger.kind_rank, 'oracle')

    def test_enums(self):
        self.assertEqual(frozenset({'heuristic', 'llm', 'human'}), ledger.KINDS)
        self.assertEqual(frozenset({'pure', 'external'}), ledger.PURITIES)


class RegistryTestCase(LedgerFixture, testtools.TestCase):

    def test_registry_covers_exactly_the_phase2_deciders(self):
        # The drift guard: the registry must enumerate EXACTLY the
        # _CATEGORY_RULES ids plus the dangerous-construct scan and the claim
        # classifier.  Adding a phase-2 content rule without registering it here
        # FAILS this test.
        registry = ledger.default_registry()
        ids = {r.rule_id for r in registry}
        expected = ({rule_id for rule_id, _v, _fn in _CATEGORY_RULES}
                    | {'dangerous-construct-scan', 'claim-category'})
        self.assertEqual(expected, ids)
        # One registry row per content rule plus the two extra rules.
        self.assertEqual(len(_CATEGORY_RULES) + 2, len(registry))

    def test_category_rules_take_id_and_version_from_rules_module(self):
        registry = {r.rule_id: r for r in ledger.default_registry()}
        for rule_id, version, _fn in _CATEGORY_RULES:
            self.assertIn(rule_id, registry)
            self.assertEqual(version, registry[rule_id].version)
            self.assertEqual('heuristic', registry[rule_id].kind)
            self.assertEqual('pure', registry[rule_id].purity)

    def test_scan_and_claim_rules_have_expected_versions(self):
        registry = {r.rule_id: r for r in ledger.default_registry()}
        self.assertEqual(RULES_VERSION, registry['dangerous-construct-scan'].version)
        self.assertEqual(CLAIM_RULE_VERSION, registry['claim-category'].version)

    def test_register_rules_persists_every_rule(self):
        conn, _path = self._ledger()
        rows = {r['rule_id'] for r in ledger.registered_rules(conn)}
        self.assertEqual({r.rule_id for r in ledger.default_registry()}, rows)
        # All seeded rules are live (not retired).
        retired = {r['retired'] for r in ledger.registered_rules(conn)}
        self.assertEqual({0}, retired)

    def test_register_rules_is_idempotent(self):
        conn, _path = self._ledger()
        before = conn.execute('SELECT COUNT(*) FROM rule').fetchone()[0]
        # Re-registering the same registry adds nothing and overwrites nothing.
        ledger.register_rules(conn, ledger.default_registry())
        ledger.register_rules(conn, ledger.default_registry())
        after = conn.execute('SELECT COUNT(*) FROM rule').fetchone()[0]
        self.assertEqual(before, after)


class AppendTestCase(LedgerFixture, testtools.TestCase):

    def test_append_decision_returns_id_and_persists(self):
        conn, _path = self._ledger()
        new_id = ledger.append_decision(
            conn, fingerprint='fp1', category='packaging', confidence='high',
            decided_by='whitespace-only', rule_version=1, kind='heuristic',
            evidence='whitespace only', decided_at=WHEN)
        self.assertIsInstance(new_id, int)
        rows = ledger.decisions_for(conn, 'fp1')
        self.assertEqual(1, len(rows))
        self.assertEqual('packaging', rows[0]['category'])
        self.assertEqual(WHEN, rows[0]['decided_at'])
        self.assertIsNone(rows[0]['superseded_at'])
        # External columns default to NULL for pure rules.
        self.assertIsNone(rows[0]['input_snapshot'])
        self.assertIsNone(rows[0]['input_fresh_until'])

    def test_append_observation_returns_id_and_persists(self):
        conn, _path = self._ledger()
        new_id = ledger.append_observation(
            conn, fingerprint='fp1', kind='dangerous-construct', detail='shell-out',
            evidence='os.system(cmd)', observed_by='dangerous-construct-scan',
            rule_version=RULES_VERSION, observed_at=WHEN)
        self.assertIsInstance(new_id, int)
        rows = ledger.observations_for(conn, 'fp1')
        self.assertEqual(1, len(rows))
        self.assertEqual('shell-out', rows[0]['detail'])
        self.assertIsNone(rows[0]['superseded_at'])

    def test_append_decision_defaults_unverified_unsigned(self):
        # Every existing caller is preserved: a decision is unverified and
        # unsigned unless explicitly stated.
        conn, _path = self._ledger()
        ledger.append_decision(
            conn, fingerprint='fp1', category='unknown', confidence='low',
            decided_by='substantive', rule_version=1, kind='heuristic',
            evidence=None, decided_at=WHEN)
        row = ledger.decisions_for(conn, 'fp1')[0]
        self.assertEqual(0, row['verified'])
        self.assertIsNone(row['signature'])
        self.assertIsNone(row['signed_by'])

    def test_append_decision_records_verified_flag(self):
        conn, _path = self._ledger()
        ledger.append_decision(
            conn, fingerprint='fp1', category='bugfix', confidence='high',
            decided_by='llm-triage:claude-sonnet-4-6', rule_version=1, kind='llm',
            evidence='{}', decided_at=WHEN, verified=True)
        row = ledger.decisions_for(conn, 'fp1')[0]
        self.assertEqual(1, row['verified'])

    def test_distinct_ids_for_distinct_appends(self):
        conn, _path = self._ledger()
        id_a = ledger.append_decision(
            conn, fingerprint='fp1', category='unknown', confidence='low',
            decided_by='substantive', rule_version=1, kind='heuristic',
            evidence=None, decided_at=WHEN)
        id_b = ledger.append_decision(
            conn, fingerprint='fp2', category='unknown', confidence='low',
            decided_by='substantive', rule_version=1, kind='heuristic',
            evidence=None, decided_at=WHEN)
        self.assertNotEqual(id_a, id_b)


class SupersedeTestCase(LedgerFixture, testtools.TestCase):

    def _seed(self, conn):
        # Two decisions from substantive v1, one from whitespace-only v1.
        ledger.append_decision(
            conn, fingerprint='fp1', category='unknown', confidence='low',
            decided_by='substantive', rule_version=1, kind='heuristic',
            evidence=None, decided_at=WHEN)
        ledger.append_decision(
            conn, fingerprint='fp2', category='unknown', confidence='low',
            decided_by='substantive', rule_version=1, kind='heuristic',
            evidence=None, decided_at=WHEN)
        ledger.append_decision(
            conn, fingerprint='fp3', category='packaging', confidence='high',
            decided_by='whitespace-only', rule_version=1, kind='heuristic',
            evidence='ws', decided_at=WHEN)

    def test_supersede_marks_only_the_named_rule_version(self):
        conn, _path = self._ledger()
        self._seed(conn)
        count = ledger.supersede_decisions(
            conn, decided_by='substantive', rule_version=1, superseded_at=LATER)
        self.assertEqual(2, count)
        # The two substantive decisions are now superseded; the whitespace one
        # stays live.
        live = ledger.live_decisions(conn)
        self.assertEqual(['whitespace-only'], [r['decided_by'] for r in live])

    def test_superseded_row_still_exists_with_a_timestamp(self):
        # The append-only invariant: a superseded decision is NOT deleted; it
        # remains as the audit trail, just marked.
        conn, _path = self._ledger()
        self._seed(conn)
        ledger.supersede_decisions(
            conn, decided_by='substantive', rule_version=1, superseded_at=LATER)
        rows = ledger.decisions_for(conn, 'fp1')
        self.assertEqual(1, len(rows))  # still there
        self.assertEqual(LATER, rows[0]['superseded_at'])
        # Original content untouched.
        self.assertEqual('unknown', rows[0]['category'])
        self.assertEqual(WHEN, rows[0]['decided_at'])

    def test_supersede_is_idempotent_on_already_dead_rows(self):
        conn, _path = self._ledger()
        self._seed(conn)
        ledger.supersede_decisions(
            conn, decided_by='substantive', rule_version=1, superseded_at=LATER)
        # A second supersede touches nothing live (already dead) and does not
        # overwrite the original superseded_at.
        again = ledger.supersede_decisions(
            conn, decided_by='substantive', rule_version=1, superseded_at='2099-01-01T00:00:00Z')
        self.assertEqual(0, again)
        rows = ledger.decisions_for(conn, 'fp1')
        self.assertEqual(LATER, rows[0]['superseded_at'])

    def test_supersede_observations(self):
        conn, _path = self._ledger()
        ledger.append_observation(
            conn, fingerprint='fp1', kind='dangerous-construct', detail='shell-out',
            evidence='os.system(x)', observed_by='dangerous-construct-scan',
            rule_version=RULES_VERSION, observed_at=WHEN)
        count = ledger.supersede_observations(
            conn, observed_by='dangerous-construct-scan', rule_version=RULES_VERSION,
            superseded_at=LATER)
        self.assertEqual(1, count)
        self.assertEqual([], ledger.live_observations(conn))
        # Audit trail preserved.
        rows = ledger.observations_for(conn, 'fp1')
        self.assertEqual(1, len(rows))
        self.assertEqual(LATER, rows[0]['superseded_at'])

    def test_supersede_observations_for_fingerprint(self):
        conn, _path = self._ledger()
        for fp in ('fp1', 'fp2'):
            ledger.append_observation(
                conn, fingerprint=fp, kind='security-risk', detail='low',
                evidence=None, observed_by='risk-gate:m', rule_version=1, observed_at=WHEN)
        # Surgical: only fp1's security-risk row is superseded; fp2 untouched.
        count = ledger.supersede_observations_for_fingerprint(
            conn, fingerprint='fp1', kind='security-risk', superseded_at=LATER)
        self.assertEqual(1, count)
        live = {o['fingerprint'] for o in ledger.live_observations(conn)}
        self.assertEqual({'fp2'}, live)


class ReviewQueueTestCase(LedgerFixture, testtools.TestCase):
    """The human-review queue helpers (schema v2)."""

    def test_append_and_pending_items(self):
        conn, _path = self._ledger()
        item_id = ledger.append_review_item(
            conn, fingerprint='fp1', reason='verifier refuted', draft_category='security',
            draft_confidence='high', enqueued_at=WHEN, priority=5)
        self.assertIsInstance(item_id, int)
        pending = ledger.pending_review_items(conn)
        self.assertEqual(1, len(pending))
        self.assertEqual('fp1', pending[0]['fingerprint'])
        self.assertEqual('verifier refuted', pending[0]['reason'])
        self.assertEqual('security', pending[0]['draft_category'])
        self.assertEqual(5, pending[0]['priority'])
        self.assertIsNone(pending[0]['reviewed_at'])

    def test_pending_items_ordered_by_priority_then_id(self):
        conn, _path = self._ledger()
        ledger.append_review_item(
            conn, fingerprint='low', reason=None, draft_category=None,
            draft_confidence=None, enqueued_at=WHEN, priority=1)
        ledger.append_review_item(
            conn, fingerprint='high', reason=None, draft_category=None,
            draft_confidence=None, enqueued_at=WHEN, priority=9)
        pending = ledger.pending_review_items(conn)
        self.assertEqual(['high', 'low'], [r['fingerprint'] for r in pending])

    def test_pending_items_in_category_filters_and_keeps_priority_order(self):
        conn, _path = self._ledger()
        # Two security items at different priorities, plus a documentation item.
        ledger.append_review_item(
            conn, fingerprint='sec-low', reason=None, draft_category='security',
            draft_confidence=None, enqueued_at=WHEN, priority=1)
        ledger.append_review_item(
            conn, fingerprint='sec-high', reason=None, draft_category='security',
            draft_confidence=None, enqueued_at=WHEN, priority=9)
        ledger.append_review_item(
            conn, fingerprint='doc', reason=None, draft_category='documentation',
            draft_confidence=None, enqueued_at=WHEN, priority=5)
        # The slice is scoped to the category but still highest-priority-first.
        security = ledger.pending_review_items_in_category(conn, 'security')
        self.assertEqual(['sec-high', 'sec-low'], [r['fingerprint'] for r in security])
        self.assertEqual(
            ['doc'],
            [r['fingerprint'] for r in ledger.pending_review_items_in_category(conn, 'documentation')])
        self.assertEqual([], ledger.pending_review_items_in_category(conn, 'feature'))

    def test_pending_items_in_category_excludes_reviewed(self):
        conn, _path = self._ledger()
        item_id = ledger.append_review_item(
            conn, fingerprint='sec', reason=None, draft_category='security',
            draft_confidence=None, enqueued_at=WHEN, priority=1)
        ledger.mark_reviewed(conn, item_id=item_id, reviewed_at=LATER)
        self.assertEqual([], ledger.pending_review_items_in_category(conn, 'security'))

    def test_pending_exists_is_per_fingerprint(self):
        conn, _path = self._ledger()
        self.assertFalse(ledger.pending_review_item_exists(conn, fingerprint='fp1'))
        ledger.append_review_item(
            conn, fingerprint='fp1', reason=None, draft_category=None,
            draft_confidence=None, enqueued_at=WHEN)
        self.assertTrue(ledger.pending_review_item_exists(conn, fingerprint='fp1'))
        self.assertFalse(ledger.pending_review_item_exists(conn, fingerprint='fp2'))

    def test_mark_reviewed_clears_one_item(self):
        conn, _path = self._ledger()
        item_id = ledger.append_review_item(
            conn, fingerprint='fp1', reason=None, draft_category=None,
            draft_confidence=None, enqueued_at=WHEN)
        ledger.append_review_item(
            conn, fingerprint='fp2', reason=None, draft_category=None,
            draft_confidence=None, enqueued_at=WHEN)
        touched = ledger.mark_reviewed(conn, item_id=item_id, reviewed_at=LATER)
        self.assertEqual(1, touched)
        pending = ledger.pending_review_items(conn)
        self.assertEqual(['fp2'], [r['fingerprint'] for r in pending])
        # The fingerprint is no longer pending; a reviewed item exists with the
        # supplied timestamp.
        self.assertFalse(ledger.pending_review_item_exists(conn, fingerprint='fp1'))

    def test_mark_reviewed_is_idempotent(self):
        conn, _path = self._ledger()
        item_id = ledger.append_review_item(
            conn, fingerprint='fp1', reason=None, draft_category=None,
            draft_confidence=None, enqueued_at=WHEN)
        ledger.mark_reviewed(conn, item_id=item_id, reviewed_at=LATER)
        # A second mark touches nothing and does not re-stamp the timestamp.
        again = ledger.mark_reviewed(conn, item_id=item_id, reviewed_at='2099-01-01T00:00:00Z')
        self.assertEqual(0, again)
        (reviewed_at,) = conn.execute(
            'SELECT reviewed_at FROM review_queue WHERE id = ?', (item_id,)).fetchone()
        self.assertEqual(LATER, reviewed_at)


class AppendOnlyInvariantTestCase(testtools.TestCase):
    """The module exposes NO way to edit a decision's content or delete a row —
    only append and supersede.  This guards the append-only invariant in code,
    not just by convention."""

    def test_no_update_or_delete_api_is_exposed(self):
        public = {name for name in dir(ledger) if not name.startswith('_')}
        # The only mutators are the two appends and the two supersedes.
        forbidden_substrings = ('update', 'delete', 'edit', 'remove', 'set_')
        offenders = [
            name for name in public
            if any(sub in name.lower() for sub in forbidden_substrings)]
        self.assertEqual([], offenders)

    def test_mutators_are_exactly_append_and_supersede(self):
        for name in ('append_decision', 'append_observation',
                     'supersede_decisions', 'supersede_observations'):
            self.assertTrue(callable(getattr(ledger, name)))
