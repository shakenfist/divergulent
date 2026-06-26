import contextlib
import io
import json
import os
import shutil
import tempfile
from unittest import mock

import testtools

from divergulent import cli
from divergulent import debversion
from divergulent.inventory import InstalledPackage
from divergulent.sources.repology import StalenessResult, StalenessState


def _pkg(binary, source, source_version, arch='amd64'):
    return InstalledPackage(
        binary_name=binary,
        binary_version=debversion.parse('1-1'),
        source_name=source,
        source_version=debversion.parse(source_version),
        architecture=arch)


def _result(name, installed, newest, state):
    return StalenessResult(name, debversion.parse(installed), newest, state)


class FakeSource:
    def __init__(self, results_by_name):
        self.results_by_name = results_by_name
        self.calls = []

    def staleness(self, name, version):
        self.calls.append((name, version))
        return self.results_by_name[name]


class GatherTestCase(testtools.TestCase):

    def test_dedups_by_source_package(self):
        packages = [
            _pkg('libc6', 'glibc', '2.36-9'),
            _pkg('libc-bin', 'glibc', '2.36-9'),
            _pkg('bash', 'bash', '5.2-1'),
        ]
        source = FakeSource({
            'glibc': _result('glibc', '2.36-9', '2.37', StalenessState.BEHIND),
            'bash': _result('bash', '5.2-1', '5.2', StalenessState.CURRENT),
        })
        results = cli._gather_staleness(source, packages)
        self.assertEqual(2, len(results))
        self.assertEqual(['bash', 'glibc'], sorted(name for name, _ in source.calls))


class SelectTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.results = [
            _result('a', '1', '2', StalenessState.BEHIND),
            _result('b', '1', '1', StalenessState.CURRENT),
            _result('c', '1', None, StalenessState.UNKNOWN),
        ]

    def test_default_shows_only_behind(self):
        self.assertEqual(['a'], [r.source_package for r in cli._select(self.results, show_all=False)])

    def test_all_orders_behind_unknown_current(self):
        self.assertEqual(
            ['a', 'c', 'b'], [r.source_package for r in cli._select(self.results, show_all=True)])


class StalenessCommandTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        # Isolate the cache dir so a real stored bundle on the operator's machine
        # is never auto-discovered by cli.main (default args -> default_cache_dir).
        cache_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cache_dir, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {'DIVERGULENT_CACHE_DIR': cache_dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.packages = [_pkg('bash', 'bash', '5.2-1'), _pkg('libc6', 'glibc', '2.36-9')]
        self.source = FakeSource({
            'bash': _result('bash', '5.2-1', '5.3', StalenessState.BEHIND),
            'glibc': _result('glibc', '2.36-9', '2.36', StalenessState.CURRENT),
        })

    def _run(self, argv):
        out = io.StringIO()
        with mock.patch('divergulent.cli.inventory.list_installed', return_value=self.packages), \
                mock.patch('divergulent.cli._repology', return_value=self.source), \
                contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_json_default_only_behind(self):
        rc, output = self._run(['staleness', '--json'])
        self.assertEqual(0, rc)
        data = json.loads(output)
        self.assertEqual(1, len(data))
        self.assertEqual('bash', data[0]['source'])
        self.assertEqual('5.3', data[0]['newest'])
        self.assertEqual('behind', data[0]['state'])

    def test_all_table_includes_current(self):
        rc, output = self._run(['staleness', '--all'])
        self.assertEqual(0, rc)
        self.assertIn('bash', output)
        self.assertIn('glibc', output)
