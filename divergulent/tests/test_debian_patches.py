import testtools

from divergulent.dep3 import PatchClass
from divergulent.sources import debian_patches
from divergulent.sources.base import Source
from divergulent.sources.debian_patches import DebianPatchesSource, DivergenceState


DEBIAN_ONLY = 'Description: distro tweak\nForwarded: not-needed\nOrigin: vendor\n\n--- a/x\n'
FORWARDED = 'Description: upstreamable\nForwarded: https://lists.example/1\n\n--- a/x\n'
WITH_BUG = 'Description: fix thing\nForwarded: yes\nBug-Debian: https://bugs.debian.org/123\n\n--- a/x\n'
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


def _counts(package):
    debian_only = sum(1 for p in package.patches if p.patch_class == PatchClass.DEBIAN_ONLY)
    forwarded = sum(1 for p in package.patches if p.patch_class == PatchClass.FORWARDED)
    unknown = sum(1 for p in package.patches if p.patch_class == PatchClass.UNKNOWN)
    return debian_only, forwarded, unknown


class DetailsClassificationTestCase(testtools.TestCase):

    def test_patched_classes(self):
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
        package = DebianPatchesSource(http).details('foo', '1.2-1')
        self.assertEqual(DivergenceState.PATCHED, package.state)
        self.assertEqual(3, len(package.patches))
        self.assertEqual((1, 1, 1), _counts(package))

    def test_epoch_stripped_fallback(self):
        # The full epoch version 404s; the epoch-stripped one resolves.
        http = FakeHttp(
            json_by_key={
                'foo:2.3-4': {'format': '3.0 (quilt)', 'patches': ['a.patch']},
                'base:foo:2.3-4': _raw_url('foo', '2.3-4', 'a.patch'),
            },
            text_by_key={'foo:2.3-4:a.patch': DEBIAN_ONLY})
        package = DebianPatchesSource(http).details('foo', '1:2.3-4')
        self.assertEqual(DivergenceState.PATCHED, package.state)
        self.assertEqual(PatchClass.DEBIAN_ONLY, package.patches[0].patch_class)
        # The reported version remains the installed one, including the epoch.
        self.assertEqual('1:2.3-4', package.version)

    def test_unreadable_patch_is_unknown(self):
        http = FakeHttp(
            json_by_key={
                'foo:1.2-1': {'format': '3.0 (quilt)', 'patches': ['a.patch']},
                'base:foo:1.2-1': _raw_url('foo', '1.2-1', 'a.patch'),
            },
            text_by_key={})  # patch content fetch returns None
        package = DebianPatchesSource(http).details('foo', '1.2-1')
        self.assertEqual(PatchClass.UNKNOWN, package.patches[0].patch_class)

    def test_undiscoverable_base_all_unknown(self):
        # The series resolves but the raw-content base cannot be discovered.
        http = FakeHttp(
            json_by_key={'foo:1.2-1': {'format': '3.0 (quilt)', 'patches': ['a.patch', 'b.patch']}})
        package = DebianPatchesSource(http).details('foo', '1.2-1')
        self.assertEqual(2, len(package.patches))
        self.assertTrue(all(p.patch_class == PatchClass.UNKNOWN for p in package.patches))

    def test_filename_heuristic_marks_debian_only(self):
        # A bare diff with no DEP-3, but a deb-* filename, is Debian-only.
        http = FakeHttp(
            json_by_key={
                'foo:1.2-1': {'format': '3.0 (quilt)', 'patches': ['deb-tweak.diff']},
                'base:foo:1.2-1': _raw_url('foo', '1.2-1', 'deb-tweak.diff'),
            },
            text_by_key={'foo:1.2-1:deb-tweak.diff': BARE})
        package = DebianPatchesSource(http).details('foo', '1.2-1')
        self.assertEqual(PatchClass.DEBIAN_ONLY, package.patches[0].patch_class)

    def test_is_a_source(self):
        self.assertIsInstance(DebianPatchesSource(FakeHttp()), Source)
        self.assertEqual('debian-patches', debian_patches.DebianPatchesSource.name)


class SummaryTestCase(testtools.TestCase):

    def test_non_quilt_without_series_is_unknown(self):
        http = FakeHttp(json_by_key={'foo:1.2-1': {'format': '1.0', 'patches': []}})
        self.assertEqual(DivergenceState.UNKNOWN, DebianPatchesSource(http).summary('foo', '1.2-1').state)

    def test_patched_counts_in_one_request(self):
        http = FakeHttp(json_by_key={'foo:1.2-1': {'format': '3.0 (quilt)', 'patches': ['a.patch', 'b.patch']}})
        summary = DebianPatchesSource(http).summary('foo', '1.2-1')
        self.assertEqual(DivergenceState.PATCHED, summary.state)
        self.assertEqual(2, summary.total)
        # Only the series request: no base discovery, no patch bodies.
        self.assertEqual(1, len(http.json_calls))
        self.assertEqual([], http.text_calls)

    def test_native(self):
        http = FakeHttp(json_by_key={'foo:1.2': {'format': '3.0 (native)', 'patches': []}})
        summary = DebianPatchesSource(http).summary('foo', '1.2')
        self.assertEqual(DivergenceState.NATIVE, summary.state)
        self.assertEqual(0, summary.total)

    def test_clean(self):
        http = FakeHttp(json_by_key={'foo:1.2-1': {'format': '3.0 (quilt)', 'patches': []}})
        self.assertEqual(DivergenceState.CLEAN, DebianPatchesSource(http).summary('foo', '1.2-1').state)

    def test_unresolved(self):
        self.assertEqual(
            DivergenceState.UNKNOWN, DebianPatchesSource(FakeHttp()).summary('foo', '1.2-1').state)


class DetailsTestCase(testtools.TestCase):

    def test_per_patch_detail_with_bug(self):
        http = FakeHttp(
            json_by_key={
                'foo:1.2-1': {'format': '3.0 (quilt)', 'patches': ['a.patch', 'b.patch']},
                'base:foo:1.2-1': _raw_url('foo', '1.2-1', 'a.patch'),
            },
            text_by_key={'foo:1.2-1:a.patch': WITH_BUG, 'foo:1.2-1:b.patch': BARE})
        package = DebianPatchesSource(http).details('foo', '1.2-1')
        self.assertEqual(DivergenceState.PATCHED, package.state)
        self.assertEqual(2, len(package.patches))

        first = package.patches[0]
        self.assertEqual('a.patch', first.name)
        self.assertEqual(PatchClass.FORWARDED, first.patch_class)
        self.assertEqual('fix thing', first.description)
        self.assertEqual(1, len(first.bugs))
        self.assertEqual('debian', first.bugs[0].tracker)

        second = package.patches[1]
        self.assertEqual(PatchClass.UNKNOWN, second.patch_class)
        self.assertEqual([], second.bugs)

    def test_native_has_no_patches(self):
        http = FakeHttp(json_by_key={'foo:1.2': {'format': '3.0 (native)', 'patches': []}})
        package = DebianPatchesSource(http).details('foo', '1.2')
        self.assertEqual(DivergenceState.NATIVE, package.state)
        self.assertEqual([], package.patches)

    def test_unresolved_has_no_patches(self):
        package = DebianPatchesSource(FakeHttp()).details('foo', '1.2-1')
        self.assertEqual(DivergenceState.UNKNOWN, package.state)
        self.assertEqual([], package.patches)
