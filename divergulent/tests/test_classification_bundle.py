"""Tests for divergulent.classify.classification_bundle -- the published bundle.

The bundle is the shareable half of the classification: fingerprint→verdict plus
the review axes, keyed by content hash, lean (no raw evidence), schema-versioned.
These assert it carries the derived verdict + axes, that it NEVER leaks the bulky
LLM evidence, and that it round-trips and is deterministic -- offline, against a
seeded ledger.
"""
import os
import tempfile

import testtools

from divergulent.classify import classification_bundle as cb
from divergulent.classify import ledger as ledger_mod


class BundleFixture:

    def _tmp(self, name):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return os.path.join(tmp.name, name)

    def _seeded_ledger(self, *, return_path=False):
        path = self._tmp('ledger.sqlite')
        conn = ledger_mod.create_ledger(path)
        self.addCleanup(conn.close)
        ledger_mod.register_rules(conn, ledger_mod.default_registry())

        # A deterministic verdict, a verified-LLM verdict with bulky evidence, and
        # an un-reviewed unknown residue -- all three must appear in the bundle.
        ledger_mod.append_decision(
            conn, fingerprint='fp-test', category='test', confidence='high',
            decided_by='test-only', rule_version=1, kind='heuristic', evidence=None,
            decided_at='2026-06-26T00:00:00Z', commit=False)
        ledger_mod.append_decision(
            conn, fingerprint='fp-sec', category='security', confidence='medium',
            decided_by='llm-triage:claude', rule_version=3, kind='llm',
            evidence='{"raw_response": "SECRET-MODEL-TEXT-should-not-ship"}',
            decided_at='2026-06-26T00:01:00Z', verified=True, commit=False)
        ledger_mod.append_decision(
            conn, fingerprint='fp-unk', category='unknown', confidence='low',
            decided_by='substantive', rule_version=1, kind='heuristic', evidence=None,
            decided_at='2026-06-26T00:02:00Z', commit=False)

        # Axes on the security fingerprint only.
        ledger_mod.append_observation(
            conn, fingerprint='fp-sec', kind='security-risk', detail='high',
            evidence='{"raw_response": "MORE-SECRET-TEXT"}', observed_by='risk-gate',
            rule_version=2, observed_at='2026-06-26T00:03:00Z', commit=False)
        ledger_mod.append_observation(
            conn, fingerprint='fp-sec', kind='reach', detail='XL',
            evidence='{"fraction": 0.9}', observed_by='popcon-rule', rule_version=1,
            observed_at='2026-06-26T00:04:00Z', commit=False)
        ledger_mod.append_observation(
            conn, fingerprint='fp-sec', kind='reviewability', detail='large',
            evidence='{}', observed_by='size-rule', rule_version=1,
            observed_at='2026-06-26T00:05:00Z', commit=False)
        conn.commit()
        if return_path:
            return conn, path
        return conn

    def _bundle(self, conn):
        return cb.build_classification_bundle(
            conn, generated_at='2026-06-26T12:00:00Z', source_release='trixie')


class BuildTestCase(BundleFixture, testtools.TestCase):

    def test_every_current_verdict_is_included(self):
        bundle = self._bundle(self._seeded_ledger())
        self.assertEqual({'fp-test', 'fp-sec', 'fp-unk'}, set(bundle.verdicts))

    def test_category_and_provenance_reason_present(self):
        bundle = self._bundle(self._seeded_ledger())
        sec = bundle.verdicts['fp-sec']
        self.assertEqual('security', sec['category'])
        self.assertEqual('llm-triage:claude', sec['decided_by'])
        self.assertEqual(3, sec['rule_version'])
        self.assertIn('verified LLM triage', sec['reason'])

    def test_external_cve_reason_surfaces_the_confirmed_phrase(self):
        from divergulent.classify import cross_reference as xref_mod
        conn = self._seeded_ledger()
        # An external CVE decision wins fp-ext; its reason should be the compact
        # confirmed-CVE evidence phrase (id + snapshot date), not "deterministic rule".
        ledger_mod.append_decision(
            conn, fingerprint='fp-ext', category='security', confidence='high',
            decided_by=xref_mod.EXTERNAL_CVE_RULE_ID, rule_version=xref_mod.EXTERNAL_CVE_VERSION,
            kind='heuristic', evidence='confirmed CVE-2021-3999 (security-tracker 2026-07-10)',
            decided_at='2026-06-26T00:06:00Z',
            input_snapshot='{"cve": "CVE-2021-3999"}', input_fresh_until='2026-08-09')
        conn.commit()
        reason = self._bundle(conn).verdicts['fp-ext']['reason']
        self.assertEqual('confirmed CVE-2021-3999 (security-tracker 2026-07-10)', reason)

    def test_axes_attached_when_scored_and_absent_otherwise(self):
        bundle = self._bundle(self._seeded_ledger())
        sec = bundle.verdicts['fp-sec']
        self.assertEqual('high', sec['risk'])
        self.assertEqual('XL', sec['reach'])
        self.assertEqual('large', sec['reviewability'])
        # fp-test was never scored on any axis -> those keys are omitted.
        test = bundle.verdicts['fp-test']
        self.assertNotIn('risk', test)
        self.assertNotIn('reach', test)
        self.assertNotIn('reviewability', test)

    def test_schema_and_enum_versions_recorded(self):
        bundle = self._bundle(self._seeded_ledger())
        self.assertEqual(cb.CLASSIFICATION_SCHEMA_VERSION, bundle.schema)
        self.assertEqual(cb.ENTRY_SCHEMA_VERSION, bundle.entry_schema)
        self.assertEqual(ledger_mod.CATEGORY_ENUM_VERSION, bundle.category_enum_version)
        self.assertEqual('trixie', bundle.source_release)


class LeanTestCase(BundleFixture, testtools.TestCase):

    def test_raw_evidence_never_ships(self):
        # The whole point of the lean bundle: it carries provenance, not the bulky
        # raw model responses. Assert against the serialised bytes, not just the
        # dataclass, so nothing leaks through the wire form.
        bundle = self._bundle(self._seeded_ledger())
        path = self._tmp('classification-trixie.json.gz')
        cb.write(bundle, path)
        import gzip
        payload = gzip.open(path, 'rb').read().decode('utf-8')
        self.assertNotIn('raw_response', payload)
        self.assertNotIn('SECRET-MODEL-TEXT', payload)
        self.assertNotIn('MORE-SECRET-TEXT', payload)


class RoundTripTestCase(BundleFixture, testtools.TestCase):

    def test_write_then_load(self):
        bundle = self._bundle(self._seeded_ledger())
        path = self._tmp('classification-trixie.json.gz')
        cb.write(bundle, path)
        loaded = cb.load(path)
        self.assertEqual(bundle.to_dict(), loaded.to_dict())

    def test_write_is_deterministic(self):
        bundle = self._bundle(self._seeded_ledger())
        first, second = self._tmp('a.json.gz'), self._tmp('b.json.gz')
        cb.write(bundle, first)
        cb.write(bundle, second)
        import gzip
        self.assertEqual(gzip.open(first, 'rb').read(), gzip.open(second, 'rb').read())


class MainTestCase(BundleFixture, testtools.TestCase):

    def test_main_builds_a_bundle_file(self):
        from contextlib import redirect_stdout
        import io
        _conn, ledger_path = self._seeded_ledger(return_path=True)

        out = self._tmp('classification-trixie.json.gz')
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cb.main([ledger_path, '--release', 'trixie',
                          '--generated-at', '2026-06-26T12:00:00Z', '--output', out])
        self.assertEqual(0, rc)
        self.assertIn('built classification bundle', buf.getvalue())
        self.assertEqual(3, len(cb.load(out).verdicts))
