"""Tests for divergulent.classify.cli -- the curation dispatcher.

OFFLINE: the underlying module mains are monkeypatched to recorders, so no real
triage / LLM / web server runs. The dispatcher's job is to resolve the data root
and forward the right argv; that is what these assert.
"""
import io
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import testtools

from divergulent.classify import cli, workspace


class DispatcherFixture:

    def _root(self, *, with_ledger=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = workspace.init(tmp.name)
        if with_ledger:
            ws.ledger.write_text('')  # a stand-in ledger file
        return ws

    def _run(self, argv):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            rc = cli.main(argv)
        return rc, out.getvalue()


class ForwardingTestCase(DispatcherFixture, testtools.TestCase):

    def test_triage_forwards_ledger_corpus_and_rest(self):
        ws = self._root()
        with mock.patch('divergulent.classify.triage.main', return_value=0) as m:
            rc, _ = self._run(['--data', str(ws.root), 'triage', '--limit', '5'])
        self.assertEqual(0, rc)
        m.assert_called_once_with([str(ws.ledger), str(ws.corpus_dir), '--limit', '5'])

    def test_risk_forwards(self):
        ws = self._root()
        with mock.patch('divergulent.classify.risk.main', return_value=0) as m:
            self._run(['--data', str(ws.root), 'risk', '--model', 'claude-sonnet-4-6'])
        m.assert_called_once_with(
            [str(ws.ledger), str(ws.corpus_dir), '--model', 'claude-sonnet-4-6'])

    def test_web_forwards_as_flags(self):
        ws = self._root()
        with mock.patch('divergulent.classify.review_web.main', return_value=0) as m:
            self._run(['--data', str(ws.root), 'web'])
        m.assert_called_once_with(['--ledger', str(ws.ledger), '--corpus', str(ws.corpus_dir)])

    def test_report_forwards_ledger_only(self):
        ws = self._root()
        with mock.patch('divergulent.classify.ledger.main', return_value=0) as m:
            self._run(['--data', str(ws.root), 'report'])
        m.assert_called_once_with(['report', str(ws.ledger)])

    def test_record_forwards_subcommand_ledger_and_corpus(self):
        ws = self._root()
        with mock.patch('divergulent.classify.ledger.main', return_value=0) as m:
            self._run(['--data', str(ws.root), 'record'])
        m.assert_called_once_with(['record', str(ws.ledger), str(ws.corpus_dir)])

    def test_requeue_forwards_subcommand_and_fingerprint(self):
        ws = self._root()
        with mock.patch('divergulent.classify.review.main', return_value=0) as m:
            self._run(['--data', str(ws.root), 'requeue', 'deadbeef'])
        m.assert_called_once_with(['requeue', str(ws.ledger), 'deadbeef'])

    def test_review_forwards_subcommand(self):
        ws = self._root()
        with mock.patch('divergulent.classify.review.main', return_value=0) as m:
            self._run(['--data', str(ws.root), 'review', '--limit', '3'])
        m.assert_called_once_with(['review', str(ws.ledger), str(ws.corpus_dir), '--limit', '3'])

    def test_popcon_forwards_corpus_and_rest(self):
        ws = self._root()
        with mock.patch('divergulent.classify.popcon.main', return_value=0) as m:
            rc, _ = self._run(['--data', str(ws.root), 'popcon', '--date', '2026-06-28'])
        self.assertEqual(0, rc)
        m.assert_called_once_with([str(ws.corpus_dir), '--date', '2026-06-28'])

    def test_bundle_forwards_ledger_and_rest(self):
        ws = self._root()
        with mock.patch('divergulent.classify.classification_bundle.main', return_value=0) as m:
            rc, _ = self._run(['--data', str(ws.root), 'bundle', '--release', 'trixie'])
        self.assertEqual(0, rc)
        m.assert_called_once_with([str(ws.ledger), '--release', 'trixie'])

    def test_export_forwards_ledger_and_default_output_dir(self):
        # The dispatcher injects a default --output of <root>/ledger (the tracked
        # export dir); an operator --output in rest would override it (last-wins).
        ws = self._root()
        default_out = os.path.join(str(ws.root), 'ledger')
        with mock.patch('divergulent.classify.export.main', return_value=0) as m:
            rc, _ = self._run(['--data', str(ws.root), 'export'])
        self.assertEqual(0, rc)
        m.assert_called_once_with(['export', str(ws.ledger), '--output', default_out])

    def test_import_forwards_input_and_dest_ledger(self):
        # import REBUILDS the ledger, so it must not require one to pre-exist and it
        # forwards the dest ledger as --ledger with the input JSONL as the positional.
        ws = self._root(with_ledger=False)
        with mock.patch('divergulent.classify.export.main', return_value=0) as m:
            rc, out = self._run(['--data', str(ws.root), 'import', 'in.jsonl'])
        self.assertEqual(0, rc)
        self.assertNotIn('no ledger', out)
        m.assert_called_once_with(['import', 'in.jsonl', '--ledger', str(ws.ledger)])

    def test_popcon_does_not_require_a_ledger(self):
        # popcon writes corpus/popcon.sqlite; it is a corpus-only verb, so a missing
        # ledger must NOT block it (unlike the ledger-consuming verbs).
        ws = self._root(with_ledger=False)
        with mock.patch('divergulent.classify.popcon.main', return_value=0) as m:
            rc, out = self._run(['--data', str(ws.root), 'popcon'])
        self.assertEqual(0, rc)
        self.assertNotIn('no ledger', out)
        m.assert_called_once_with([str(ws.corpus_dir)])


class GuardrailTestCase(DispatcherFixture, testtools.TestCase):

    def test_missing_ledger_is_a_clear_error_not_a_crash(self):
        ws = self._root(with_ledger=False)
        with mock.patch('divergulent.classify.triage.main') as m:
            rc, out = self._run(['--data', str(ws.root), 'triage'])
        self.assertEqual(2, rc)
        self.assertIn('no ledger at', out)
        m.assert_not_called()  # never reached the underlying command

    def test_not_in_a_root_is_a_clear_error(self):
        with mock.patch(
                'divergulent.classify.cli.workspace.find',
                side_effect=workspace.WorkspaceNotFound(
                    'not inside a divergulent data root.\n  run "divergulent-classify init"')):
            rc, out = self._run(['triage'])
        self.assertEqual(2, rc)
        self.assertIn('divergulent-classify init', out)

    def test_init_creates_a_root(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = os.path.join(tmp.name, 'newdata')
        rc, out = self._run(['init', target])
        self.assertEqual(0, rc)
        self.assertTrue(os.path.isfile(os.path.join(target, workspace.MARKER)))
        self.assertIn('initialised', out)


class StatusTestCase(DispatcherFixture, testtools.TestCase):

    def _seeded_root(self):
        from divergulent.classify import ledger as ledger_mod
        from divergulent.classify import risk
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = workspace.init(tmp.name)
        conn = ledger_mod.create_ledger(str(ws.ledger))
        self.addCleanup(conn.close)
        # fp1 settled as bugfix; fp2 left as residue and scored HIGH risk.
        ledger_mod.append_decision(
            conn, fingerprint='fp1', category='bugfix', confidence='high',
            decided_by='llm-triage:m', rule_version=1, kind='llm', verified=True,
            evidence='', decided_at='2026-06-26T00:00:00Z', commit=False)
        ledger_mod.append_decision(
            conn, fingerprint='fp2', category='unknown', confidence='low',
            decided_by='substantive', rule_version=1, kind='heuristic', evidence=None,
            decided_at='2026-06-26T00:00:00Z', commit=False)
        risk.record_risk_observation(
            conn, 'fp2', risk.RiskScore(level='high', rank=risk.RISK_RANK['high'], reason='r',
                                        model='m', prompt_version=1, raw_response='{}'),
            now='2026-06-26T00:00:00Z', commit=False)
        ledger_mod.append_review_item(
            conn, fingerprint='fp2', reason='r', draft_category='unknown',
            draft_confidence='low', enqueued_at='2026-06-26T00:00:00Z')
        conn.commit()
        return ws

    def test_status_orients_the_operator(self):
        ws = self._seeded_root()
        rc, out = self._run(['--data', str(ws.root), 'status'])
        self.assertEqual(0, rc)
        self.assertIn('residue (un-settled fingerprints): 1', out)   # fp2
        self.assertIn('bugfix', out)                                  # fp1's verdict
        self.assertIn('high', out)                                    # the risk level
        self.assertIn('elevated+ still in the residue', out)
        self.assertIn('pending human review: 1', out)
        self.assertIn('cache:', out)                                  # best-effort cache line

    def test_status_nudges_when_a_record_run_is_due(self):
        # _seeded_root never recorded reviewability (nor registered the rules), so
        # status must flag that a `record` run is due, and say how to run it.
        ws = self._seeded_root()
        _rc, out = self._run(['--data', str(ws.root), 'status'])
        self.assertIn('a `record` run is due', out)
        self.assertIn('no size tier', out)                 # the coverage-gap reason
        self.assertIn('divergulent-classify record', out)  # the actionable fix

    def test_status_quiet_when_up_to_date(self):
        from divergulent.classify import ledger as ledger_mod
        from divergulent.classify import reviewability
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = workspace.init(tmp.name)
        conn = ledger_mod.create_ledger(str(ws.ledger))
        self.addCleanup(conn.close)
        # Rules registered AND every verdict has a reviewability tier -> no drift,
        # no coverage gap, so status stays silent about `record`.
        ledger_mod.register_rules(conn, ledger_mod.default_registry())
        ledger_mod.append_decision(
            conn, fingerprint='fp1', category='bugfix', confidence='high',
            decided_by='substantive', rule_version=1, kind='heuristic', evidence=None,
            decided_at='2026-06-26T00:00:00Z', commit=False)
        ledger_mod.append_observation(
            conn, fingerprint='fp1', kind=reviewability.REVIEWABILITY_KIND, detail='normal',
            evidence='{}', observed_by=reviewability.REVIEWABILITY_OBSERVED_BY,
            rule_version=reviewability.REVIEWABILITY_VERSION, observed_at='2026-06-26T00:00:00Z')
        conn.commit()
        _rc, out = self._run(['--data', str(ws.root), 'status'])
        self.assertNotIn('a `record` run is due', out)


class CacheAgeTestCase(testtools.TestCase):

    def test_age_and_staleness(self):
        import datetime
        now = datetime.datetime(2026, 6, 26, tzinfo=datetime.timezone.utc)
        self.assertEqual(0, cli._age_days('2026-06-26T00:00:00Z', now=now))
        self.assertEqual(30, cli._age_days('2026-05-27T00:00:00Z', now=now))
        self.assertIsNone(cli._age_days('not a timestamp', now=now))
        self.assertGreater(cli._age_days('2026-05-01T00:00:00Z', now=now), cli.CACHE_STALE_DAYS)
