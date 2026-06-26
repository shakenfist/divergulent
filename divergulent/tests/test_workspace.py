"""Tests for divergulent.classify.workspace -- data-root discovery + layout."""
import os
import tempfile
from pathlib import Path

import testtools

from divergulent.classify import workspace


class WorkspaceFixture:

    def _root(self, *, marker=True, ledger=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        (root / 'corpus').mkdir()
        if marker:
            (root / workspace.MARKER).write_text('# root\n')
        if ledger:
            (root / 'corpus' / 'ledger.sqlite').write_text('')
        return root


class FindTestCase(WorkspaceFixture, testtools.TestCase):

    def test_explicit_data_flag_wins(self):
        root = self._root()
        ws = workspace.find(str(root), environ={'DIVERGULENT_DATA': '/somewhere/else'})
        self.assertEqual(root, ws.root)

    def test_env_var_used_when_no_flag(self):
        root = self._root()
        ws = workspace.find(None, environ={'DIVERGULENT_DATA': str(root)}, start='/tmp')
        self.assertEqual(root, ws.root)

    def test_walks_up_to_the_marker(self):
        root = self._root()
        deep = root / 'corpus' / 'bodies' / 'ab'
        deep.mkdir(parents=True)
        ws = workspace.find(None, start=deep, environ={})
        self.assertEqual(root, ws.root)

    def test_lenient_fallback_on_corpus_ledger(self):
        # No marker, but the cwd directly holds corpus/ledger.sqlite -> it's a root.
        root = self._root(marker=False, ledger=True)
        ws = workspace.find(None, start=root, environ={})
        self.assertEqual(root, ws.root)

    def test_raises_with_actionable_message_when_none(self):
        empty = self._root(marker=False, ledger=False)
        # an empty subdir with no marker/ledger anywhere up the (tmp) tree
        sub = empty / 'corpus'
        exc = self.assertRaises(
            workspace.WorkspaceNotFound, workspace.find, None, start=sub, environ={})
        self.assertIn('divergulent-classify init', str(exc))

    def test_resolved_paths_follow_the_layout(self):
        root = self._root()
        ws = workspace.find(str(root))
        self.assertEqual(root / 'corpus', ws.corpus_dir)
        self.assertEqual(root / 'corpus' / 'ledger.sqlite', ws.ledger)
        self.assertEqual(root / 'corpus' / 'fingerprints.sqlite', ws.index)
        self.assertEqual(root / 'cache', ws.cache_dir)


class InitTestCase(testtools.TestCase):

    def test_init_creates_marker_and_dirs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = os.path.join(tmp.name, 'data')
        ws = workspace.init(target)
        self.assertTrue(ws.marker.is_file())
        self.assertTrue(ws.corpus_dir.is_dir())
        self.assertTrue(ws.cache_dir.is_dir())
        # Idempotent: re-init leaves it intact.
        ws.marker.write_text('# customised\n')
        workspace.init(target)
        self.assertEqual('# customised\n', ws.marker.read_text())
