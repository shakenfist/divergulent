import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import testtools

from divergulent import bundle
from divergulent import cli
from divergulent.cache import Cache
from divergulent.http import HttpClient


FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'sources-index-sample.txt')


def _entry(repo, version, status, srcname):
    return {'repo': repo, 'version': version, 'status': status, 'srcname': srcname, 'visiblename': srcname}


# One Repology projects page covering the fixture archive's sources.
STALENESS_PAGE = {
    'bash': [_entry('debian_unstable', '5.3', 'newest', 'bash')],
    'hello': [_entry('debian_unstable', '2.12', 'newest', 'hello')],
    'zlib': [_entry('debian_unstable', '1.3', 'newest', 'zlib')],
}

# Patches-API responses keyed by source package name.
PATCHES = {
    'bash': {'format': '3.0 (quilt)', 'patches': ['a.patch', 'b.patch']},
    'hello': {'format': '3.0 (native)', 'patches': []},
    'zlib': {'format': '3.0 (quilt)', 'patches': []},
}


class FakeBuildHttp:
    '''Dispatches Repology projects pages and patches-API lookups by URL.'''

    def __init__(self):
        self.projects_calls = 0

    def get_json(self, url, *, cache_namespace, cache_key, ttl_seconds):
        if '/api/v1/projects/' in url:
            self.projects_calls += 1
            return STALENESS_PAGE if self.projects_calls == 1 else {}
        if '/patches/api/' in url:
            pkg = url.split('/patches/api/', 1)[1].split('/', 1)[0]
            return PATCHES.get(pkg)
        raise AssertionError('unexpected url %s' % url)

    def get_text(self, url, *, cache_namespace, cache_key, ttl_seconds):  # pragma: no cover - summary only
        raise AssertionError('summary() must not fetch patch bodies')


def _build(http, workers=1):
    return cli.build_bundle(
        http, [FIXTURE], release='trixie', repology_repo='debian_unstable', arch='amd64',
        generated_at='2026-06-17T00:00:00+00:00', workers=workers)


class BuildBundleTestCase(testtools.TestCase):

    def test_assembles_staleness_and_divergence(self):
        result = _build(FakeBuildHttp())

        self.assertEqual('trixie', result.release)
        self.assertEqual('debian_unstable', result.repology_repo)
        self.assertEqual({'arch': 'amd64', 'release': 'trixie'}, result.built_on)
        self.assertEqual('2026-06-17T00:00:00+00:00', result.generated_at)

        self.assertEqual({'bash': '5.3', 'hello': '2.12', 'zlib': '1.3'}, result.staleness)

        # Divergence keys on the newest version per source from the fixture.
        self.assertEqual(
            {'version': '5.2.15-3', 'format': '3.0 (quilt)', 'total': 2, 'state': 'patched'},
            result.divergence['bash'])
        self.assertEqual(
            {'version': '2.10-3', 'format': '3.0 (native)', 'total': 0, 'state': 'native'},
            result.divergence['hello'])
        # A quilt source with an empty series is CLEAN, not UNKNOWN.
        self.assertEqual('clean', result.divergence['zlib']['state'])
        self.assertEqual('1:1.2.13.dfsg-1', result.divergence['zlib']['version'])

    def test_concurrent_and_serial_agree(self):
        self.assertEqual(_build(FakeBuildHttp(), workers=1), _build(FakeBuildHttp(), workers=4))


class FakeBytesUrlopen:
    '''A urllib-style urlopen returning recorded bytes by URL, counting calls.'''

    def __init__(self):
        self.calls = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.calls.append(url)
        if '/api/v1/projects/' in url:
            page = STALENESS_PAGE if url.endswith('projects/?inrepo=debian_unstable') else {}
            return _Resp(json.dumps(page).encode('utf-8'))
        if '/patches/api/' in url:
            pkg = url.split('/patches/api/', 1)[1].split('/', 1)[0]
            return _Resp(json.dumps(PATCHES.get(pkg)).encode('utf-8'))
        raise AssertionError('unexpected url %s' % url)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self, amt=None):
        return self._payload if amt is None else self._payload[:amt]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class BuildBundleRefreshTestCase(testtools.TestCase):
    '''A refresh-mode build re-hits the origins even with a warm cache.'''

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Cache(Path(tmp.name), clock=lambda: 0.0)

    def _client(self, urlopen, refresh):
        return HttpClient(
            self.cache, urlopen=urlopen, sleep=lambda s: None,
            host_intervals={'sources.debian.org': 0.0}, min_interval=0.0, refresh=refresh)

    def test_refresh_refetches_after_warm_build(self):
        # First build warms the cache.
        warm = FakeBytesUrlopen()
        _build(self._client(warm, refresh=False))
        self.assertNotEqual([], warm.calls)

        # A non-refresh rebuild reuses the cache and touches no network.
        reused = FakeBytesUrlopen()
        _build(self._client(reused, refresh=False))
        self.assertEqual([], reused.calls)

        # A refresh rebuild re-hits the origins despite the warm cache.
        refreshed = FakeBytesUrlopen()
        _build(self._client(refreshed, refresh=True))
        self.assertNotEqual([], refreshed.calls)


def _bundle_bytes(testcase, release='trixie'):
    obj = bundle.Bundle(
        schema=bundle.SCHEMA_VERSION,
        cache_schema=bundle.CACHE_SCHEMA_VERSION,
        generated_at='2026-06-18T00:00:00+00:00',
        release=release,
        repology_repo='debian_unstable',
        built_on={'arch': 'amd64', 'release': release},
        staleness={'bash': '5.3'},
        divergence={'bash': {'version': '5.2-1', 'format': '3.0 (quilt)', 'total': 2, 'state': 'patched'}})
    fd, path = tempfile.mkstemp(suffix='.json.gz')
    os.close(fd)
    testcase.addCleanup(os.unlink, path)
    bundle.write(obj, path)
    with open(path, 'rb') as handle:
        return handle.read()


class FakeDownloadHttp:
    '''Stands in for the HTTP client: get_bytes returns canned bytes (or None).'''

    def __init__(self, data):
        self.data = data
        self.urls = []

    def get_bytes(self, url):
        self.urls.append(url)
        return self.data


class CachePullTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.cache_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cache_dir, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {'DIVERGULENT_CACHE_DIR': self.cache_dir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, argv, http):
        with mock.patch('divergulent.cli._detect_release', return_value='trixie'), \
                mock.patch('divergulent.cli._http_client', return_value=http):
            return cli.main(argv)

    def _stored(self):
        return bundle.stored_path(self.cache_dir, 'trixie')

    def test_pull_stores_downloaded_bytes_verbatim(self):
        data = _bundle_bytes(self)
        http = FakeDownloadHttp(data)
        rc = self._run(['cache', 'pull', '--cache-url', 'http://example/b.json.gz'], http)
        self.assertEqual(0, rc)
        self.assertEqual(['http://example/b.json.gz'], http.urls)
        self.assertTrue(self._stored().exists())
        with open(self._stored(), 'rb') as handle:
            self.assertEqual(data, handle.read())  # stored exactly as downloaded
        # And it loads back as a valid bundle.
        self.assertEqual('trixie', bundle.load(self._stored()).release)

    def test_default_url_is_keyed_on_release(self):
        http = FakeDownloadHttp(_bundle_bytes(self))
        self._run(['cache', 'pull'], http)
        self.assertIn('cache-trixie.json.gz', http.urls[0])

    def test_wrong_release_is_not_stored(self):
        http = FakeDownloadHttp(_bundle_bytes(self, release='bookworm'))
        rc = self._run(['cache', 'pull', '--cache-url', 'http://example/b'], http)
        self.assertEqual(1, rc)
        self.assertFalse(self._stored().exists())

    def test_unparseable_download_is_not_stored(self):
        http = FakeDownloadHttp(b'not a gzip bundle')
        rc = self._run(['cache', 'pull', '--cache-url', 'http://example/b'], http)
        self.assertEqual(1, rc)
        self.assertFalse(self._stored().exists())

    def test_failed_download_returns_error(self):
        http = FakeDownloadHttp(None)
        rc = self._run(['cache', 'pull', '--cache-url', 'http://example/b'], http)
        self.assertEqual(1, rc)
        self.assertFalse(self._stored().exists())
