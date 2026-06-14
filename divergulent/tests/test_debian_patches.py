import testtools

from divergulent.sources import debian_patches
from divergulent.sources.base import Source
from divergulent.sources.debian_patches import DebianPatchesSource, DivergenceState


DEBIAN_ONLY = 'Description: distro tweak\nForwarded: not-needed\nOrigin: vendor\n\n--- a/x\n'
FORWARDED = 'Description: upstreamable\nForwarded: https://lists.example/1\n\n--- a/x\n'
BARE = '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n'


def _raw_url(pkg, version, name, area='main', prefix=None):
    prefix = prefix or pkg[0]
    return {'raw_url': f'/data/{area}/{prefix}/{pkg}/{version}/debian/patches/{name}'}


class FakeHttp:
    def __init__(self, json_by_key=None, text_by_key=None):
        self.json_by_key = json_by_key or {}
        self.text_by_key = text_by_key or {}
        self.json_calls = []
        self.text_calls = []

    def get_json(self, url, *, cache_namespace, cache_key, ttl_seconds):
        self.json_calls.append(cache_key)
        return self.json_by_key.get(cache_key)

    def get_text(self, url, *, cache_namespace, cache_key, ttl_seconds):
        self.text_calls.append(cache_key)
        return self.text_by_key.get(cache_key)


class UrlTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.source = DebianPatchesSource(FakeHttp())

    def test_series_url_encodes_epoch_and_plus(self):
        url = self.source._series_url('foo', '1:2.3+dfsg-4')
        self.assertEqual('https://sources.debian.org/patches/api/foo/1%3A2.3%2Bdfsg-4/', url)

    def test_file_api_url_has_trailing_slash(self):
        url = self.source._file_api_url('foo', '1.2-1', 'fix.patch')
        self.assertEqual('https://sources.debian.org/api/src/foo/1.2-1/debian/patches/fix.patch/', url)

    def test_raw_base_derived_from_pool_raw_url(self):
        http = FakeHttp(json_by_key={'base:bash:5.2-1': _raw_url('bash', '5.2-1', 'a.patch', prefix='b')})
        base = DebianPatchesSource(http)._raw_base('bash', '5.2-1', 'a.patch')
        self.assertEqual('https://sources.debian.org/data/main/b/bash/5.2-1/debian/patches/', base)


class DivergenceTestCase(testtools.TestCase):

    def test_patched_counts_classes(self):
        http = FakeHttp(
            json_by_key={
                'foo:1.2-1': {'format': '3.0 (quilt)', 'patches': ['a.patch', 'b.patch', 'c.patch']},
                'base:foo:1.2-1': _raw_url('foo', '1.2-1', 'a.patch'),
            },
            text_by_key={
                'foo:1.2-1:a.patch': DEBIAN_ONLY,
                'foo:1.2-1:b.patch': FORWARDED,
                'foo:1.2-1:c.patch': BARE,
            })
        result = DebianPatchesSource(http).divergence('foo', '1.2-1')
        self.assertEqual(DivergenceState.PATCHED, result.state)
        self.assertEqual(3, result.total)
        self.assertEqual(1, result.debian_only)
        self.assertEqual(1, result.forwarded)
        self.assertEqual(1, result.unknown)

    def test_native_is_native(self):
        http = FakeHttp(json_by_key={'foo:1.2': {'format': '3.0 (native)', 'patches': []}})
        result = DebianPatchesSource(http).divergence('foo', '1.2')
        self.assertEqual(DivergenceState.NATIVE, result.state)
        self.assertEqual(0, result.total)

    def test_quilt_empty_series_is_clean(self):
        http = FakeHttp(json_by_key={'foo:1.2-1': {'format': '3.0 (quilt)', 'patches': []}})
        result = DebianPatchesSource(http).divergence('foo', '1.2-1')
        self.assertEqual(DivergenceState.CLEAN, result.state)

    def test_unresolved_is_unknown(self):
        result = DebianPatchesSource(FakeHttp()).divergence('foo', '1.2-1')
        self.assertEqual(DivergenceState.UNKNOWN, result.state)

    def test_non_quilt_without_series_is_unknown(self):
        http = FakeHttp(json_by_key={'foo:1.2-1': {'format': '1.0', 'patches': []}})
        result = DebianPatchesSource(http).divergence('foo', '1.2-1')
        self.assertEqual(DivergenceState.UNKNOWN, result.state)

    def test_epoch_stripped_fallback(self):
        # The full epoch version 404s; the epoch-stripped one resolves.
        http = FakeHttp(
            json_by_key={
                'foo:2.3-4': {'format': '3.0 (quilt)', 'patches': ['a.patch']},
                'base:foo:2.3-4': _raw_url('foo', '2.3-4', 'a.patch'),
            },
            text_by_key={'foo:2.3-4:a.patch': DEBIAN_ONLY})
        result = DebianPatchesSource(http).divergence('foo', '1:2.3-4')
        self.assertEqual(DivergenceState.PATCHED, result.state)
        self.assertEqual(1, result.debian_only)
        # The reported version remains the installed one, including the epoch.
        self.assertEqual('1:2.3-4', result.version)

    def test_unreadable_patch_counts_as_unknown(self):
        http = FakeHttp(
            json_by_key={
                'foo:1.2-1': {'format': '3.0 (quilt)', 'patches': ['a.patch']},
                'base:foo:1.2-1': _raw_url('foo', '1.2-1', 'a.patch'),
            },
            text_by_key={})  # patch content fetch returns None
        result = DebianPatchesSource(http).divergence('foo', '1.2-1')
        self.assertEqual(1, result.total)
        self.assertEqual(1, result.unknown)

    def test_undiscoverable_base_counts_all_unknown(self):
        # The series resolves but the raw-content base cannot be discovered.
        http = FakeHttp(
            json_by_key={'foo:1.2-1': {'format': '3.0 (quilt)', 'patches': ['a.patch', 'b.patch']}})
        result = DebianPatchesSource(http).divergence('foo', '1.2-1')
        self.assertEqual(DivergenceState.PATCHED, result.state)
        self.assertEqual(2, result.total)
        self.assertEqual(2, result.unknown)

    def test_is_a_source(self):
        self.assertIsInstance(DebianPatchesSource(FakeHttp()), Source)
        self.assertEqual('debian-patches', debian_patches.DebianPatchesSource.name)
