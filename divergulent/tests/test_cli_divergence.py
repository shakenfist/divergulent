import contextlib
import io
import json
from unittest import mock

import testtools

from divergulent import cli
from divergulent import debversion
from divergulent.inventory import InstalledPackage
from divergulent.sources.debian_patches import DivergenceState, DivergenceSummary


def _pkg(binary, source, source_version, arch='amd64'):
    return InstalledPackage(
        binary_name=binary,
        binary_version=debversion.parse('1-1'),
        source_name=source,
        source_version=debversion.parse(source_version),
        architecture=arch)


def _sum(name, version, total, state=DivergenceState.PATCHED):
    return DivergenceSummary(name, version, '3.0 (quilt)', total, state)


class FakeSource:
    def __init__(self, by_name):
        self.by_name = by_name
        self.calls = []

    def summary(self, name, version):
        self.calls.append(name)
        return self.by_name[name]


class GatherTestCase(testtools.TestCase):

    def test_dedups_by_source(self):
        packages = [
            _pkg('libc6', 'glibc', '2.36-9'),
            _pkg('libc-bin', 'glibc', '2.36-9'),
            _pkg('bash', 'bash', '5.2-1'),
        ]
        source = FakeSource({
            'glibc': _sum('glibc', '2.36-9', 3),
            'bash': _sum('bash', '5.2-1', 0, DivergenceState.CLEAN),
        })
        results = cli._gather_divergence(source, packages)
        self.assertEqual(2, len(results))
        self.assertEqual(['bash', 'glibc'], sorted(source.calls))

    def test_limit_caps_sources(self):
        packages = [_pkg('a', 'a', '1-1'), _pkg('b', 'b', '1-1'), _pkg('c', 'c', '1-1')]
        source = FakeSource({n: _sum(n, '1-1', 0, DivergenceState.CLEAN) for n in 'abc'})
        cli._gather_divergence(source, packages, limit=2)
        self.assertEqual(2, len(source.calls))


class SelectTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.results = [
            _sum('few', '1', 2),
            _sum('many', '1', 5),
            _sum('clean', '1', 0, DivergenceState.CLEAN),
        ]

    def test_default_only_carrying_ranked(self):
        selected = cli._select_divergence(self.results, show_all=False)
        self.assertEqual(['many', 'few'], [r.source_package for r in selected])

    def test_all_includes_clean(self):
        selected = cli._select_divergence(self.results, show_all=True)
        self.assertIn('clean', [r.source_package for r in selected])


class DivergenceCommandTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.packages = [_pkg('bash', 'bash', '5.2-1'), _pkg('libc6', 'glibc', '2.36-9')]
        self.source = FakeSource({
            'bash': _sum('bash', '5.2-1', 2),
            'glibc': _sum('glibc', '2.36-9', 0, DivergenceState.CLEAN),
        })

    def _run(self, argv):
        out = io.StringIO()
        with mock.patch('divergulent.cli.inventory.list_installed', return_value=self.packages), \
                mock.patch('divergulent.cli.DebianPatchesSource', return_value=self.source), \
                contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_json_default_only_carrying(self):
        rc, output = self._run(['divergence', '--json'])
        self.assertEqual(0, rc)
        data = json.loads(output)
        self.assertEqual(1, len(data))
        self.assertEqual('bash', data[0]['source'])
        self.assertEqual(2, data[0]['total'])

    def test_all_table_includes_clean(self):
        rc, output = self._run(['divergence', '--all'])
        self.assertEqual(0, rc)
        self.assertIn('bash', output)
        self.assertIn('glibc', output)
