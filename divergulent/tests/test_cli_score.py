import contextlib
import io
import json
from unittest import mock

import testtools

from divergulent import cli
from divergulent import debversion
from divergulent.inventory import InstalledPackage
from divergulent.sources.debian_patches import DivergenceResult, DivergenceState
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


def _div(name, debian_only=0, forwarded=0, unknown=0, state=DivergenceState.PATCHED):
    total = debian_only + forwarded + unknown
    return DivergenceResult(name, '1.0-1', '3.0 (quilt)', total, debian_only, forwarded, unknown, state)


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

    def divergence(self, name, version):
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
            'glibc': _div('glibc', debian_only=1),
            'bash': _div('bash', state=DivergenceState.CLEAN),
        })
        drifts = cli._gather_score(repology, patches, packages)
        self.assertEqual(2, len(drifts))
        self.assertEqual(['bash', 'glibc'], sorted(repology.calls))
        self.assertEqual(['bash', 'glibc'], sorted(patches.calls))

    def test_limit(self):
        packages = [_pkg('a', 'a', '1-1'), _pkg('b', 'b', '1-1'), _pkg('c', 'c', '1-1')]
        repology = FakeRepology({n: _stale(n, StalenessState.CURRENT) for n in 'abc'})
        patches = FakePatches({n: _div(n, state=DivergenceState.CLEAN) for n in 'abc'})
        cli._gather_score(repology, patches, packages, limit=2)
        self.assertEqual(2, len(repology.calls))


class SelectTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        from divergulent import score
        self.drifts = [
            score.combine(_stale('low', StalenessState.BEHIND), _div('low', state=DivergenceState.CLEAN)),
            score.combine(_stale('high', StalenessState.BEHIND), _div('high', debian_only=5)),
            score.combine(_stale('clean', StalenessState.CURRENT), _div('clean', state=DivergenceState.CLEAN)),
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
            'bash': _div('bash', debian_only=2),
            'glibc': _div('glibc', state=DivergenceState.CLEAN),
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
        # behind (2) + 2 debian-only patches (2*3) = 8
        self.assertEqual(8, data[0]['score'])

    def test_all_table_includes_clean(self):
        rc, output = self._run(['score', '--all'])
        self.assertEqual(0, rc)
        self.assertIn('bash', output)
        self.assertIn('glibc', output)
