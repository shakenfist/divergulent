import contextlib
import io
import json
from unittest import mock

import testtools

from divergulent import cli
from divergulent import debversion
from divergulent.dep3 import PatchClass
from divergulent.inventory import InstalledPackage
from divergulent.sources.debian_patches import DivergenceState, DivergenceSummary, PackagePatches, PatchDetail
from divergulent.sources.repology import StalenessResult, StalenessState


def _pkg(binary, source, source_version, arch='amd64'):
    return InstalledPackage(
        binary_name=binary,
        binary_version=debversion.parse('1-1'),
        source_name=source,
        source_version=debversion.parse(source_version),
        architecture=arch)


def _stale(name, state):
    newest = '2.0' if state == StalenessState.BEHIND else None
    return StalenessResult(name, debversion.parse('1.0-1'), newest, state)


def _sum(name, total, state=DivergenceState.PATCHED):
    return DivergenceSummary(name, '1.0-1', '3.0 (quilt)', total, state)


class FakeRepology:
    def __init__(self, by_name):
        self.by_name = by_name
        self.calls = []

    def staleness(self, name, version):
        self.calls.append(name)
        return self.by_name[name]


class FakePatches:
    def __init__(self, by_name):
        self.by_name = by_name
        self.calls = []

    def summary(self, name, version):
        self.calls.append(name)
        return self.by_name[name]


class GatherTestCase(testtools.TestCase):

    def test_dedups_and_queries_both_axes_once_per_source(self):
        packages = [
            _pkg('libc6', 'glibc', '2.36-9'),
            _pkg('libc-bin', 'glibc', '2.36-9'),
            _pkg('bash', 'bash', '5.2-1'),
        ]
        repology = FakeRepology({
            'glibc': _stale('glibc', StalenessState.BEHIND),
            'bash': _stale('bash', StalenessState.CURRENT),
        })
        patches = FakePatches({
            'glibc': _sum('glibc', 1),
            'bash': _sum('bash', 0, DivergenceState.CLEAN),
        })
        drifts = cli._gather_score(repology, patches, packages)
        self.assertEqual(2, len(drifts))
        self.assertEqual(['bash', 'glibc'], sorted(repology.calls))
        self.assertEqual(['bash', 'glibc'], sorted(patches.calls))

    def test_limit(self):
        packages = [_pkg('a', 'a', '1-1'), _pkg('b', 'b', '1-1'), _pkg('c', 'c', '1-1')]
        repology = FakeRepology({n: _stale(n, StalenessState.CURRENT) for n in 'abc'})
        patches = FakePatches({n: _sum(n, 0, DivergenceState.CLEAN) for n in 'abc'})
        cli._gather_score(repology, patches, packages, limit=2)
        self.assertEqual(2, len(repology.calls))


class SelectTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.drifts = [
            cli.score.combine(_stale('low', StalenessState.BEHIND), _sum('low', 0, DivergenceState.CLEAN)),
            cli.score.combine(_stale('high', StalenessState.BEHIND), _sum('high', 5)),
            cli.score.combine(_stale('clean', StalenessState.CURRENT), _sum('clean', 0, DivergenceState.CLEAN)),
        ]

    def test_default_hides_zero_and_ranks_by_score(self):
        selected = cli._select_score(self.drifts, show_all=False)
        self.assertEqual(['high', 'low'], [d.source_package for d in selected])

    def test_all_includes_zero(self):
        selected = cli._select_score(self.drifts, show_all=True)
        self.assertIn('clean', [d.source_package for d in selected])


class ScoreCommandTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.packages = [_pkg('bash', 'bash', '5.2-1'), _pkg('libc6', 'glibc', '2.36-9')]
        self.repology = FakeRepology({
            'bash': _stale('bash', StalenessState.BEHIND),
            'glibc': _stale('glibc', StalenessState.CURRENT),
        })
        self.patches = FakePatches({
            'bash': _sum('bash', 2),
            'glibc': _sum('glibc', 0, DivergenceState.CLEAN),
        })

    def _run(self, argv):
        out = io.StringIO()
        with mock.patch('divergulent.cli.inventory.list_installed', return_value=self.packages), \
                mock.patch('divergulent.cli.RepologySource', return_value=self.repology), \
                mock.patch('divergulent.cli.DebianPatchesSource', return_value=self.patches), \
                contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_json_default_hides_clean(self):
        rc, output = self._run(['score', '--json'])
        self.assertEqual(0, rc)
        data = json.loads(output)
        self.assertEqual(1, len(data))
        self.assertEqual('bash', data[0]['source'])
        # behind (W_BEHIND) + 2 carried patches (2 * W_PATCH)
        self.assertEqual(cli.score.W_BEHIND + 2 * cli.score.W_PATCH, data[0]['score'])
        self.assertEqual(2, data[0]['total_patches'])

    def test_all_table_includes_clean(self):
        rc, output = self._run(['score', '--all'])
        self.assertEqual(0, rc)
        self.assertIn('bash', output)
        self.assertIn('glibc', output)


def _pp(name, classes, state=DivergenceState.PATCHED):
    patches = [PatchDetail('p%d.patch' % i, c, None, None, []) for i, c in enumerate(classes)]
    return PackagePatches(name, '1.0-1', '3.0 (quilt)', state, patches)


class FakeApt:
    def __init__(self, by_name, available=True):
        self.by_name = by_name
        self._available = available

    def available(self):
        return self._available

    def details(self, name, version):
        return self.by_name[name]


class ScoreClassifyTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.packages = [_pkg('bash', 'bash', '5.2-1')]
        self.repology = FakeRepology({'bash': _stale('bash', StalenessState.BEHIND)})
        self.apt = FakeApt({
            'bash': _pp('bash', [PatchClass.DEBIAN_ONLY, PatchClass.DEBIAN_ONLY, PatchClass.UNKNOWN]),
        })

    def test_classify_weights_debian_only(self):
        out = io.StringIO()
        with mock.patch('divergulent.cli.inventory.list_installed', return_value=self.packages), \
                mock.patch('divergulent.cli.RepologySource', return_value=self.repology), \
                mock.patch('divergulent.cli.AptSourcePatches', return_value=self.apt), \
                contextlib.redirect_stdout(out):
            rc = cli.main(['score', '--classify', '--json'])
        self.assertEqual(0, rc)
        data = json.loads(out.getvalue())
        self.assertEqual(1, len(data))
        expected = cli.score.W_BEHIND + 2 * cli.score.W_DEBIAN_ONLY + 1 * cli.score.W_UNKNOWN_PATCH
        self.assertEqual(expected, data[0]['score'])
        self.assertEqual(2, data[0]['debian_only'])
