import contextlib
import io
import json
from unittest import mock

import testtools

from divergulent import cli
from divergulent import debversion
from divergulent.dep3 import BugRef, PatchClass
from divergulent.inventory import InstalledPackage
from divergulent.sources.debian_patches import DivergenceResult, DivergenceState, PackagePatches, PatchDetail
from divergulent.sources.repology import StalenessResult, StalenessState


def _pkg(binary, source, source_version, arch='amd64'):
    return InstalledPackage(
        binary_name=binary,
        binary_version=debversion.parse('1-1'),
        source_name=source,
        source_version=debversion.parse(source_version),
        architecture=arch)


PACKAGES = [_pkg('bash', 'bash', '5.2-1'), _pkg('libfoo1', 'foo', '3.0-2')]


class FakeRepology:
    def __init__(self, result):
        self.result = result

    def staleness(self, name, version):
        return self.result


class FakePatches:
    def __init__(self, package, divergence):
        self.package = package
        self.divergence_result = divergence

    def details(self, name, version):
        return self.package

    def divergence(self, name, version):
        return self.divergence_result


class ResolveTestCase(testtools.TestCase):

    def test_resolves_binary_name(self):
        self.assertEqual('bash', cli._resolve_package('bash', PACKAGES)[0])

    def test_resolves_source_name(self):
        # 'foo' is the source of binary 'libfoo1'.
        self.assertEqual('foo', cli._resolve_package('foo', PACKAGES)[0])

    def test_unknown_returns_none(self):
        self.assertIsNone(cli._resolve_package('nope', PACKAGES))


class BugLinkTestCase(testtools.TestCase):

    def test_debian_number_linkified(self):
        self.assertEqual('https://bugs.debian.org/123', cli._bug_link(BugRef('debian', '123')))

    def test_debian_hash_number_linkified(self):
        self.assertEqual('https://bugs.debian.org/123', cli._bug_link(BugRef('debian', '#123')))

    def test_url_passes_through(self):
        self.assertEqual('https://up/9', cli._bug_link(BugRef('upstream', 'https://up/9')))


class ShowCommandTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.staleness = StalenessResult('bash', debversion.parse('5.2-1'), '5.3', StalenessState.BEHIND)
        self.package = PackagePatches(
            'bash', '5.2-1', '3.0 (quilt)', DivergenceState.PATCHED,
            [
                PatchDetail('deb-config.diff', PatchClass.DEBIAN_ONLY, 'configure for Debian', None,
                            [BugRef('debian', '123')]),
                PatchDetail('misc.diff', PatchClass.UNKNOWN, None, None, []),
            ])
        self.divergence = DivergenceResult('bash', '5.2-1', '3.0 (quilt)', 2, 1, 0, 1, DivergenceState.PATCHED)

    def _run(self, argv):
        out = io.StringIO()
        with mock.patch('divergulent.cli.inventory.list_installed', return_value=PACKAGES), \
                mock.patch('divergulent.cli.RepologySource', return_value=FakeRepology(self.staleness)), \
                mock.patch('divergulent.cli.DebianPatchesSource',
                           return_value=FakePatches(self.package, self.divergence)), \
                contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_not_installed_errors(self):
        err = io.StringIO()
        with mock.patch('divergulent.cli.inventory.list_installed', return_value=PACKAGES), \
                contextlib.redirect_stderr(err):
            rc = cli.main(['show', 'nope'])
        self.assertEqual(1, rc)
        self.assertIn('not an installed package', err.getvalue())

    def test_text_output_has_bug_link_and_none_declared(self):
        rc, output = self._run(['show', 'bash'])
        self.assertEqual(0, rc)
        self.assertIn('https://bugs.debian.org/123', output)
        self.assertIn('none declared', output)
        self.assertIn('deb-config.diff', output)

    def test_json_output(self):
        rc, output = self._run(['show', 'bash', '--json'])
        self.assertEqual(0, rc)
        data = json.loads(output)
        self.assertEqual('bash', data['source'])
        self.assertEqual(2, len(data['patches']))
        self.assertEqual('https://bugs.debian.org/123', data['patches'][0]['bugs'][0]['url'])
