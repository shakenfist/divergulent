"""Tests for divergulent.classify.export -- the ledger JSONL source of truth.

The round-trip is the trust anchor for the whole publish pipeline: what the
operator commits (JSONL) must reconstruct, row for row, the ledger CI builds the
signed bundle from. So these assert losslessness (ids/flags/evidence preserved),
determinism (two exports are byte-identical), and idempotent re-import -- offline,
against a seeded ledger.
"""
import io
import os
import sqlite3
import tempfile

import testtools

from divergulent.classify import export
from divergulent.classify import ledger as ledger_mod


_TABLES = ('meta', 'rule', 'decision', 'observation', 'review_queue', 'note')


class ExportFixture:

    def _tmp(self, name):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return os.path.join(tmp.name, name)

    def _seeded_ledger(self):
        """A ledger exercising every table, kind, verified flag and supersession."""
        path = self._tmp('ledger.sqlite')
        conn = ledger_mod.create_ledger(path)
        self.addCleanup(conn.close)
        ledger_mod.register_rules(conn, ledger_mod.default_registry())

        # A heuristic verdict, a verified LLM verdict, and a signed human verdict --
        # the three precedence kinds, plus one superseded decision.
        ledger_mod.append_decision(
            conn, fingerprint='fp-heur', category='test', confidence='high',
            decided_by='test-only', rule_version=1, kind='heuristic', evidence=None,
            decided_at='2026-06-26T00:00:00Z', commit=False)
        ledger_mod.append_decision(
            conn, fingerprint='fp-llm', category='security', confidence='medium',
            decided_by='llm-triage:claude', rule_version=3, kind='llm',
            evidence='{"raw_response": "..."}', decided_at='2026-06-26T00:01:00Z',
            verified=True, commit=False)
        ledger_mod.append_decision(
            conn, fingerprint='fp-human', category='feature', confidence='high',
            decided_by='human:mikal', rule_version=1, kind='human', evidence='{}',
            decided_at='2026-06-26T00:02:00Z', verified=True, signature='sig-bundle',
            signed_by='mikal@stillhq.com', commit=False)
        ledger_mod.append_decision(
            conn, fingerprint='fp-heur', category='unknown', confidence='low',
            decided_by='substantive', rule_version=1, kind='heuristic', evidence=None,
            decided_at='2026-06-25T00:00:00Z', commit=False)
        ledger_mod.supersede_decisions(
            conn, decided_by='substantive', rule_version=1, superseded_at='2026-06-26T00:00:00Z')

        # Observations across the axes, plus a review item and a note.
        ledger_mod.append_observation(
            conn, fingerprint='fp-llm', kind='security-risk', detail='high',
            evidence='{"raw_response": "..."}', observed_by='risk-gate', rule_version=2,
            observed_at='2026-06-26T00:03:00Z', commit=False)
        ledger_mod.append_observation(
            conn, fingerprint='fp-llm', kind='reach', detail='XL',
            evidence='{"fraction": 0.9}', observed_by='popcon-rule', rule_version=1,
            observed_at='2026-06-26T00:04:00Z', commit=False)
        ledger_mod.append_review_item(
            conn, fingerprint='fp-llm', reason='high risk, wide reach',
            draft_category='security', draft_confidence='medium',
            enqueued_at='2026-06-26T00:05:00Z')
        ledger_mod.append_note(
            conn, fingerprint='fp-llm', body='looked closely; genuine CVE fix',
            signed_by='mikal@stillhq.com', signature='note-sig',
            created_at='2026-06-26T00:06:00Z')
        conn.commit()
        return conn, path

    def _dump(self, conn):
        """Every row of every table, as comparable tuples keyed by table."""
        conn.row_factory = None
        out = {}
        for table in _TABLES:
            columns = [row[1] for row in conn.execute('PRAGMA table_info(%s)' % table)]
            rows = conn.execute(
                'SELECT * FROM %s ORDER BY %s'
                % (table, 'key' if table == 'meta' else 'id'
                   if table != 'rule' else 'rule_id, version')).fetchall()
            out[table] = (tuple(columns), [tuple(r) for r in rows])
        return out


class RoundTripTestCase(ExportFixture, testtools.TestCase):

    def test_import_of_export_reproduces_the_ledger(self):
        source, _src_path = self._seeded_ledger()
        lines = list(export.export_ledger(source))

        dest_path = self._tmp('rebuilt.sqlite')
        rebuilt = export.import_ledger(lines, dest_path)
        self.addCleanup(rebuilt.close)

        self.assertEqual(self._dump(source), self._dump(rebuilt))

    def test_decision_ids_are_preserved(self):
        # Verdict precedence tie-breaks on decision.id, so a renumbered import would
        # silently change verdicts. Ids must survive the round-trip exactly.
        source, _ = self._seeded_ledger()
        source_ids = [r[0] for r in source.execute('SELECT id FROM decision ORDER BY id')]

        dest_path = self._tmp('rebuilt.sqlite')
        rebuilt = export.import_ledger(list(export.export_ledger(source)), dest_path)
        self.addCleanup(rebuilt.close)
        rebuilt_ids = [r[0] for r in rebuilt.execute('SELECT id FROM decision ORDER BY id')]
        self.assertEqual(source_ids, rebuilt_ids)

    def test_derived_verdict_survives_round_trip(self):
        from divergulent.classify import verdict as verdict_mod
        source, _ = self._seeded_ledger()
        before = verdict_mod.current_verdict(source)

        dest_path = self._tmp('rebuilt.sqlite')
        rebuilt = export.import_ledger(list(export.export_ledger(source)), dest_path)
        self.addCleanup(rebuilt.close)
        after = verdict_mod.current_verdict(rebuilt)

        self.assertEqual({fp: v.category for fp, v in before.items()},
                         {fp: v.category for fp, v in after.items()})


class DeterminismTestCase(ExportFixture, testtools.TestCase):

    def test_two_exports_are_byte_identical(self):
        source, _ = self._seeded_ledger()
        first = '\n'.join(export.export_ledger(source))
        second = '\n'.join(export.export_ledger(source))
        self.assertEqual(first, second)

    def test_reimport_then_reexport_is_stable(self):
        source, _ = self._seeded_ledger()
        first = list(export.export_ledger(source))

        dest_path = self._tmp('rebuilt.sqlite')
        rebuilt = export.import_ledger(first, dest_path)
        self.addCleanup(rebuilt.close)
        again = list(export.export_ledger(rebuilt))
        self.assertEqual(first, again)

    def test_lines_are_key_sorted_compact_json(self):
        source, _ = self._seeded_ledger()
        header = next(iter(export.export_ledger(source)))
        self.assertEqual('{"export_schema":%d}' % export.EXPORT_SCHEMA_VERSION, header)


class EmptyLedgerTestCase(ExportFixture, testtools.TestCase):

    def test_freshly_created_ledger_round_trips(self):
        path = self._tmp('empty.sqlite')
        source = ledger_mod.create_ledger(path)
        self.addCleanup(source.close)

        dest_path = self._tmp('rebuilt.sqlite')
        rebuilt = export.import_ledger(list(export.export_ledger(source)), dest_path)
        self.addCleanup(rebuilt.close)
        # Only the two seeded meta rows; every other table empty but present.
        self.assertEqual(self._dump(source), self._dump(rebuilt))


class MalformedInputTestCase(ExportFixture, testtools.TestCase):

    def test_missing_header_is_rejected(self):
        dest_path = self._tmp('rebuilt.sqlite')
        self.assertRaises(
            ValueError, export.import_ledger,
            ['{"table":"meta","row":{"key":"schema_version","value":"2"}}'], dest_path)

    def test_wrong_schema_version_is_rejected(self):
        dest_path = self._tmp('rebuilt.sqlite')
        self.assertRaises(
            ValueError, export.import_ledger, ['{"export_schema":999}'], dest_path)


class MainTestCase(ExportFixture, testtools.TestCase):

    def _run(self, argv):
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            rc = export.main(argv)
        return rc, out.getvalue()

    def test_export_then_import_via_main(self):
        _source, src_path = self._seeded_ledger()
        out_jsonl = self._tmp('ledger.jsonl')
        rc, text = self._run(['export', src_path, '--output', out_jsonl])
        self.assertEqual(0, rc)
        self.assertIn('exported', text)
        self.assertTrue(os.path.isfile(out_jsonl))

        rebuilt_path = self._tmp('rebuilt.sqlite')
        rc, text = self._run(['import', out_jsonl, '--ledger', rebuilt_path])
        self.assertEqual(0, rc)
        self.assertIn('imported', text)

        rebuilt = sqlite3.connect(rebuilt_path)
        self.addCleanup(rebuilt.close)
        self.assertEqual(
            4, rebuilt.execute('SELECT COUNT(*) FROM decision').fetchone()[0])

    def test_export_default_output_path(self):
        _source, src_path = self._seeded_ledger()
        rc, _ = self._run(['export', src_path])
        self.assertEqual(0, rc)
        self.assertTrue(os.path.isfile(export._default_export_path(src_path)))

    def test_export_of_missing_ledger_is_a_clean_error(self):
        rc, text = self._run(['export', self._tmp('nope.sqlite')])
        self.assertEqual(2, rc)
        self.assertIn('does not exist', text)
