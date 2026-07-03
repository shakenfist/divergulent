"""Tests for divergulent.classify.export -- the sharded ledger source of truth.

The round-trip is the trust anchor for the whole publish pipeline: what the
operator commits (the ledger/ export dir) must reconstruct, row for row, the
ledger CI builds the signed bundle from. So these assert losslessness (ids/flags/
evidence preserved), determinism (two exports are byte-identical), idempotent
re-import, month-sharding of the big tables, and null-column compaction -- offline,
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
        """A ledger exercising every table, kind, verified flag, supersession, and
        rows spanning two months (so the sharding is exercised)."""
        path = self._tmp('ledger.sqlite')
        conn = ledger_mod.create_ledger(path)
        self.addCleanup(conn.close)
        ledger_mod.register_rules(conn, ledger_mod.default_registry())

        # Decisions in June and July -> two decision shards.
        ledger_mod.append_decision(
            conn, fingerprint='fp-heur', category='test', confidence='high',
            decided_by='test-only', rule_version=1, kind='heuristic', evidence=None,
            decided_at='2026-06-26T00:00:00Z', commit=False)
        ledger_mod.append_decision(
            conn, fingerprint='fp-llm', category='security', confidence='medium',
            decided_by='llm-triage:claude', rule_version=3, kind='llm',
            evidence='{"raw_response": "..."}', decided_at='2026-07-01T00:01:00Z',
            verified=True, commit=False)
        ledger_mod.append_decision(
            conn, fingerprint='fp-human', category='feature', confidence='high',
            decided_by='human:mikal', rule_version=1, kind='human', evidence='{}',
            decided_at='2026-07-02T00:02:00Z', verified=True, signature='sig-bundle',
            signed_by='mikal@stillhq.com', commit=False)
        ledger_mod.append_decision(
            conn, fingerprint='fp-heur', category='unknown', confidence='low',
            decided_by='substantive', rule_version=1, kind='heuristic', evidence=None,
            decided_at='2026-06-25T00:00:00Z', commit=False)
        ledger_mod.supersede_decisions(
            conn, decided_by='substantive', rule_version=1, superseded_at='2026-06-26T00:00:00Z')

        # Observations across the axes and two months, plus a review item and a note.
        ledger_mod.append_observation(
            conn, fingerprint='fp-llm', kind='security-risk', detail='high',
            evidence='{"raw_response": "..."}', observed_by='risk-gate', rule_version=2,
            observed_at='2026-06-26T00:03:00Z', commit=False)
        ledger_mod.append_observation(
            conn, fingerprint='fp-llm', kind='reach', detail='XL',
            evidence='{"fraction": 0.9}', observed_by='popcon-rule', rule_version=1,
            observed_at='2026-07-04T00:04:00Z', commit=False)
        ledger_mod.append_review_item(
            conn, fingerprint='fp-llm', reason='high risk, wide reach',
            draft_category='security', draft_confidence='medium',
            enqueued_at='2026-07-01T00:05:00Z')
        ledger_mod.append_note(
            conn, fingerprint='fp-llm', body='looked closely; genuine CVE fix',
            signed_by='mikal@stillhq.com', signature='note-sig',
            created_at='2026-07-01T00:06:00Z')
        conn.commit()
        return conn, path

    def _dump(self, conn):
        """Every row of every table, as comparable tuples keyed by table."""
        conn.row_factory = None
        out = {}
        for table in _TABLES:
            columns = [row[1] for row in conn.execute('PRAGMA table_info(%s)' % table)]
            order = 'key' if table == 'meta' else 'rule_id, version' if table == 'rule' else 'id'
            rows = conn.execute('SELECT * FROM %s ORDER BY %s' % (table, order)).fetchall()
            out[table] = (tuple(columns), [tuple(r) for r in rows])
        return out

    def _dir_bytes(self, export_dir):
        """The whole export dir as {filename: bytes} -- for byte-determinism."""
        return {name: open(os.path.join(export_dir, name), 'rb').read()
                for name in sorted(os.listdir(export_dir))}


class RoundTripTestCase(ExportFixture, testtools.TestCase):

    def test_import_of_export_reproduces_the_ledger(self):
        source, _src_path = self._seeded_ledger()
        export_dir = self._tmp('ledger')
        export.write_export(source, export_dir)

        dest_path = self._tmp('rebuilt.sqlite')
        rebuilt = export.load_export(export_dir, dest_path)
        self.addCleanup(rebuilt.close)
        self.assertEqual(self._dump(source), self._dump(rebuilt))

    def test_decision_ids_are_preserved(self):
        # Verdict precedence tie-breaks on decision.id, so a renumbered import would
        # silently change verdicts. Ids must survive the round-trip exactly.
        source, _ = self._seeded_ledger()
        source_ids = [r[0] for r in source.execute('SELECT id FROM decision ORDER BY id')]
        export_dir = self._tmp('ledger')
        export.write_export(source, export_dir)
        rebuilt = export.load_export(export_dir, self._tmp('rebuilt.sqlite'))
        self.addCleanup(rebuilt.close)
        rebuilt_ids = [r[0] for r in rebuilt.execute('SELECT id FROM decision ORDER BY id')]
        self.assertEqual(source_ids, rebuilt_ids)

    def test_derived_verdict_survives_round_trip(self):
        from divergulent.classify import verdict as verdict_mod
        source, _ = self._seeded_ledger()
        before = verdict_mod.current_verdict(source)
        export_dir = self._tmp('ledger')
        export.write_export(source, export_dir)
        rebuilt = export.load_export(export_dir, self._tmp('rebuilt.sqlite'))
        self.addCleanup(rebuilt.close)
        after = verdict_mod.current_verdict(rebuilt)
        self.assertEqual({fp: v.category for fp, v in before.items()},
                         {fp: v.category for fp, v in after.items()})


class ShardingTestCase(ExportFixture, testtools.TestCase):

    def test_big_tables_are_sharded_by_month(self):
        source, _ = self._seeded_ledger()
        export_dir = self._tmp('ledger')
        manifest = export.write_export(source, export_dir)
        shards = set(manifest['shards'])
        # Decisions span June and July; observations span June and July.
        self.assertIn('decision-2026-06.jsonl', shards)
        self.assertIn('decision-2026-07.jsonl', shards)
        self.assertIn('observation-2026-06.jsonl', shards)
        self.assertIn('observation-2026-07.jsonl', shards)
        # Small tables stay whole (no month suffix).
        self.assertIn('review_queue.jsonl', shards)
        self.assertIn('rule.jsonl', shards)
        self.assertIn('meta.jsonl', shards)

    def test_manifest_records_schema_and_row_count(self):
        source, _ = self._seeded_ledger()
        manifest = export.write_export(source, self._tmp('ledger'))
        self.assertEqual(export.EXPORT_SCHEMA_VERSION, manifest['export_schema'])
        total = sum(source.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]
                    for t in _TABLES)
        self.assertEqual(total, manifest['rows'])

    def test_null_columns_are_omitted_from_rows(self):
        # A heuristic decision has null evidence/superseded_at/signature/... which
        # must NOT appear in its compact line (import restores them as NULL).
        source, _ = self._seeded_ledger()
        export_dir = self._tmp('ledger')
        export.write_export(source, export_dir)
        line = open(os.path.join(export_dir, 'decision-2026-07.jsonl')).readline()
        self.assertNotIn('"evidence":null', line)
        self.assertNotIn('"input_snapshot"', line)
        self.assertNotIn('"signature":null', line)


class DeterminismTestCase(ExportFixture, testtools.TestCase):

    def test_two_exports_are_byte_identical(self):
        source, _ = self._seeded_ledger()
        first, second = self._tmp('a'), self._tmp('b')
        export.write_export(source, first)
        export.write_export(source, second)
        self.assertEqual(self._dir_bytes(first), self._dir_bytes(second))

    def test_reimport_then_reexport_is_stable(self):
        source, _ = self._seeded_ledger()
        first = self._tmp('first')
        export.write_export(source, first)
        rebuilt = export.load_export(first, self._tmp('rebuilt.sqlite'))
        self.addCleanup(rebuilt.close)
        again = self._tmp('again')
        export.write_export(rebuilt, again)
        self.assertEqual(self._dir_bytes(first), self._dir_bytes(again))

    def test_reexport_removes_a_stale_shard(self):
        # Re-exporting into a dir that holds an orphan shard must clear it, so the
        # committed export never carries a file the ledger no longer backs.
        source, _ = self._seeded_ledger()
        export_dir = self._tmp('ledger')
        export.write_export(source, export_dir)
        orphan = os.path.join(export_dir, 'decision-1999-01.jsonl')
        open(orphan, 'w').write('{"stale": true}\n')
        export.write_export(source, export_dir)
        self.assertFalse(os.path.exists(orphan))


class EmptyLedgerTestCase(ExportFixture, testtools.TestCase):

    def test_freshly_created_ledger_round_trips(self):
        path = self._tmp('empty.sqlite')
        source = ledger_mod.create_ledger(path)
        self.addCleanup(source.close)
        export_dir = self._tmp('ledger')
        export.write_export(source, export_dir)
        rebuilt = export.load_export(export_dir, self._tmp('rebuilt.sqlite'))
        self.addCleanup(rebuilt.close)
        self.assertEqual(self._dump(source), self._dump(rebuilt))


class MalformedInputTestCase(ExportFixture, testtools.TestCase):

    def test_missing_manifest_is_rejected(self):
        empty = self._tmp('empty-dir')
        os.makedirs(empty)
        self.assertRaises(ValueError, export.load_export, empty, self._tmp('x.sqlite'))

    def test_wrong_schema_version_is_rejected(self):
        bad = self._tmp('bad')
        os.makedirs(bad)
        open(os.path.join(bad, export.MANIFEST_NAME), 'w').write('{"export_schema": 999, "shards": []}')
        self.assertRaises(ValueError, export.load_export, bad, self._tmp('x.sqlite'))


class TableOfTestCase(testtools.TestCase):

    def test_table_derivation_from_shard_name(self):
        self.assertEqual('decision', export._table_of('decision-2026-06.jsonl'))
        self.assertEqual('observation', export._table_of('observation-undated.jsonl'))
        self.assertEqual('review_queue', export._table_of('review_queue.jsonl'))
        self.assertEqual('meta', export._table_of('meta.jsonl'))
        self.assertRaises(ValueError, export._table_of, 'mystery.jsonl')


class MainTestCase(ExportFixture, testtools.TestCase):

    def _run(self, argv):
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            rc = export.main(argv)
        return rc, out.getvalue()

    def test_export_then_import_via_main(self):
        _source, src_path = self._seeded_ledger()
        out_dir = self._tmp('ledger')
        rc, text = self._run(['export', src_path, '--output', out_dir])
        self.assertEqual(0, rc)
        self.assertIn('exported', text)
        self.assertTrue(os.path.isfile(os.path.join(out_dir, export.MANIFEST_NAME)))

        rebuilt_path = self._tmp('rebuilt.sqlite')
        rc, text = self._run(['import', out_dir, '--ledger', rebuilt_path])
        self.assertEqual(0, rc)
        self.assertIn('imported', text)
        rebuilt = sqlite3.connect(rebuilt_path)
        self.addCleanup(rebuilt.close)
        self.assertEqual(4, rebuilt.execute('SELECT COUNT(*) FROM decision').fetchone()[0])

    def test_export_default_output_dir(self):
        _source, src_path = self._seeded_ledger()
        rc, _ = self._run(['export', src_path])
        self.assertEqual(0, rc)
        self.assertTrue(os.path.isdir(export._default_export_dir(src_path)))

    def test_export_of_missing_ledger_is_a_clean_error(self):
        rc, text = self._run(['export', self._tmp('nope.sqlite')])
        self.assertEqual(2, rc)
        self.assertIn('does not exist', text)
