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
