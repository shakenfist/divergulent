import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
from unittest import mock

import testtools

from divergulent import bundle
from divergulent import cli
from divergulent import debversion
from divergulent.inventory import InstalledPackage
from divergulent.sources.bundle_backed import FallbackDivergence, FallbackStaleness
from divergulent.sources.debian_patches import DebianPatchesSource
from divergulent.sources.repology import RepologySource


# The fixture bundle is built at this instant; FRESH_NOW is within the staleness
# window and STALE_NOW is well past it, so freshness is deterministic regardless
# of the wall clock.
GENERATED_AT = '2026-06-18T00:00:00+00:00'
FRESH_NOW = datetime.datetime(2026, 6, 18, 12, 0, 0, tzinfo=datetime.timezone.utc)
STALE_NOW = datetime.datetime(2026, 7, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)


def _pkg(binary, binary_version, source, source_version, arch):
    return InstalledPackage(
        binary_name=binary,
        binary_version=debversion.parse(binary_version),
        source_name=source,
        source_version=debversion.parse(source_version),
        architecture=arch)


SAMPLE = [
    _pkg('libc6', '2.36-9', 'glibc', '2.36-9', 'amd64'),
    _pkg('bash', '5.2-1', 'bash', '5.2-1', 'amd64'),
]


def _make_bundle(release='trixie', schema=None, cache_schema=None, generated_at=GENERATED_AT):
    return bundle.Bundle(
        schema=bundle.SCHEMA_VERSION if schema is None else schema,
        cache_schema=bundle.CACHE_SCHEMA_VERSION if cache_schema is None else cache_schema,
        generated_at=generated_at,
        release=release,
        repology_repo='debian_unstable',
        built_on={'arch': 'amd64', 'release': release},
        staleness={'bash': '5.3', 'glibc': '2.36'},
        divergence={
            'bash': {'version': '5.2-1', 'format': '3.0 (quilt)', 'total': 2, 'state': 'patched'},
            'glibc': {'version': '2.36-9', 'format': '3.0 (quilt)', 'total': 0, 'state': 'clean'},
        })


def _write_bundle(testcase, **kwargs):
    fd, path = tempfile.mkstemp(suffix='.json.gz')
    os.close(fd)
    testcase.addCleanup(os.unlink, path)
    bundle.write(_make_bundle(**kwargs), path)
    return path


class RaisingHttp:
    '''Stands in for the live HTTP client; any use means the bundle was bypassed.'''

    def get_json(self, *a, **k):
        raise AssertionError('network must not be queried for a bundle-covered package')

    def get_text(self, *a, **k):
        raise AssertionError('network must not be queried for a bundle-covered package')


class UsableBundleTestCase(testtools.TestCase):

    def test_unset_path_is_none(self):
        self.assertIsNone(cli._usable_bundle(None))

    def test_missing_file_is_none(self):
        self.assertIsNone(cli._usable_bundle('/nonexistent/bundle.json.gz'))

    def test_recognised_and_release_matched_loads(self):
        path = _write_bundle(self)
        with mock.patch('divergulent.cli._detect_release', return_value='trixie'):
            loaded = cli._usable_bundle(path)
        self.assertIsNotNone(loaded)
        self.assertEqual('trixie', loaded.release)

    def test_unrecognised_schema_is_none(self):
        path = _write_bundle(self, schema=999)
        with mock.patch('divergulent.cli._detect_release', return_value='trixie'):
            self.assertIsNone(cli._usable_bundle(path))

    def test_wrong_release_is_none(self):
        path = _write_bundle(self, release='bookworm')
        with mock.patch('divergulent.cli._detect_release', return_value='trixie'):
            self.assertIsNone(cli._usable_bundle(path))


class ResolveSourcesTestCase(testtools.TestCase):

    def test_no_bundle_yields_live_sources(self):
        args = mock.Mock(bundle=None)
        staleness, divergence = cli._resolve_sources(args)
        self.assertIsInstance(staleness, RepologySource)
        self.assertIsInstance(divergence, DebianPatchesSource)

    def test_fresh_bundle_yields_fallback_sources(self):
        path = _write_bundle(self)
        args = mock.Mock(bundle=path)
        with mock.patch('divergulent.cli._detect_release', return_value='trixie'), \
                mock.patch('divergulent.cli._utc_now', return_value=FRESH_NOW):
            staleness, divergence = cli._resolve_sources(args)
        self.assertIsInstance(staleness, FallbackStaleness)
        self.assertIsInstance(divergence, FallbackDivergence)

    def test_aged_bundle_serves_divergence_but_staleness_goes_live(self):
        path = _write_bundle(self)
        args = mock.Mock(bundle=path)
        with mock.patch('divergulent.cli._detect_release', return_value='trixie'), \
                mock.patch('divergulent.cli._utc_now', return_value=STALE_NOW):
            staleness, divergence = cli._resolve_sources(args)
        # Divergence is immutable, so the bundle still serves it; staleness has
        # aged past the window, so it falls back to the live source.
        self.assertIsInstance(staleness, RepologySource)
        self.assertIsInstance(divergence, FallbackDivergence)

    def test_bad_bundle_falls_back_to_live(self):
        path = _write_bundle(self, release='bookworm')
        args = mock.Mock(bundle=path)
        with mock.patch('divergulent.cli._detect_release', return_value='trixie'):
            staleness, divergence = cli._resolve_sources(args)
        self.assertIsInstance(staleness, RepologySource)
        self.assertIsInstance(divergence, DebianPatchesSource)


class AutoDiscoveryTestCase(testtools.TestCase):
    '''Without --bundle, a stored bundle for the release is used automatically.'''

    def setUp(self):
        super().setUp()
        self.cache_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cache_dir, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {'DIVERGULENT_CACHE_DIR': self.cache_dir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _store_bundle(self, **kwargs):
        path = bundle.stored_path(self.cache_dir, kwargs.get('release', 'trixie'))
        bundle.write(_make_bundle(**kwargs), path)
        return path

    def test_stored_bundle_used_without_flag(self):
        self._store_bundle()
        args = mock.Mock(bundle=None)
        with mock.patch('divergulent.cli._detect_release', return_value='trixie'), \
                mock.patch('divergulent.cli._utc_now', return_value=FRESH_NOW):
            staleness, divergence = cli._resolve_sources(args)
        self.assertIsInstance(staleness, FallbackStaleness)
        self.assertIsInstance(divergence, FallbackDivergence)

    def test_absent_store_is_silent_live(self):
        args = mock.Mock(bundle=None)
        with mock.patch('divergulent.cli._detect_release', return_value='trixie'):
            staleness, divergence = cli._resolve_sources(args)
        self.assertIsInstance(staleness, RepologySource)
        self.assertIsInstance(divergence, DebianPatchesSource)


class BundleBackedScoreTestCase(testtools.TestCase):

    def _run(self, argv, release='trixie'):
        out = io.StringIO()
        with mock.patch('divergulent.cli.inventory.list_installed', return_value=list(SAMPLE)), \
                mock.patch('divergulent.cli._detect_release', return_value=release), \
                mock.patch('divergulent.cli._utc_now', return_value=FRESH_NOW), \
                mock.patch('divergulent.cli._http_client', return_value=RaisingHttp()):
            with contextlib.redirect_stdout(out):
                rc = cli.main(argv)
        return rc, out.getvalue()

    def test_score_resolves_from_bundle_without_network(self):
        path = _write_bundle(self)
        rc, output = self._run(['score', '--bundle', path, '--all', '--json', '--workers', '1'])
        self.assertEqual(0, rc)
        data = {d['source']: d for d in json.loads(output)}
        # bash: behind (5.2 < 5.3) and 2 carried patches -> score W_BEHIND + 2*W_PATCH = 4.
        self.assertEqual('behind', data['bash']['staleness'])
        self.assertEqual('5.3', data['bash']['newest'])
        self.assertEqual(2, data['bash']['total_patches'])
        self.assertEqual(4, data['bash']['score'])
        # glibc: current and clean -> score 0.
        self.assertEqual('current', data['glibc']['staleness'])
        self.assertEqual(0, data['glibc']['score'])

    def test_staleness_resolves_from_bundle_without_network(self):
        path = _write_bundle(self)
        rc, output = self._run(['staleness', '--bundle', path, '--all', '--json'])
        self.assertEqual(0, rc)
        data = {d['source']: d for d in json.loads(output)}
        self.assertEqual('behind', data['bash']['state'])
        self.assertEqual('current', data['glibc']['state'])

    def test_divergence_resolves_from_bundle_without_network(self):
        path = _write_bundle(self)
        rc, output = self._run(['divergence', '--bundle', path, '--all', '--json', '--workers', '1'])
        self.assertEqual(0, rc)
        data = {d['source']: d for d in json.loads(output)}
        self.assertEqual(2, data['bash']['total'])
        self.assertEqual('patched', data['bash']['state'])
        self.assertEqual('clean', data['glibc']['state'])
