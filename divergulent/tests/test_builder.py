import os

import testtools

from divergulent import builder


FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'sources-index-sample.txt')


def _entry(repo, version, status, srcname='foo'):
    return {'repo': repo, 'version': version, 'status': status, 'srcname': srcname, 'visiblename': srcname}


class FakePagedHttp:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = 0

    def get_json(self, url, *, cache_namespace, cache_key, ttl_seconds):
        page = self.pages[self.calls] if self.calls < len(self.pages) else None
        self.calls += 1
        return page


class EnumerateTestCase(testtools.TestCase):

    def test_parses_every_paragraph(self):
        items = builder.enumerate_archive([FIXTURE])
        names = [name for name, _version, _fmt in items]
        self.assertEqual(['bash', 'hello', 'bash', 'zlib'], names)
        self.assertIn(('hello', '2.10-3', '3.0 (native)'), items)

    def test_skips_paragraphs_without_name_or_version(self):
        # An empty path list yields nothing; a real index never includes a
        # paragraph missing Package/Version, but the guard must hold.
        self.assertEqual([], builder.enumerate_archive([]))


class LatestVersionsTestCase(testtools.TestCase):

    def test_keeps_newest_version_per_source(self):
        latest = builder.latest_versions(builder.enumerate_archive([FIXTURE]))
        self.assertEqual(('5.2.15-3', '3.0 (quilt)'), latest['bash'])
        self.assertEqual(('2.10-3', '3.0 (native)'), latest['hello'])
        self.assertEqual(('1:1.2.13.dfsg-1', '3.0 (quilt)'), latest['zlib'])

    def test_orders_versions_with_debian_semantics(self):
        items = [('p', '1.0-1', 'q'), ('p', '1.0-10', 'q'), ('p', '1.0-2', 'q')]
        self.assertEqual(('1.0-10', 'q'), builder.latest_versions(items)['p'])


class BuildStalenessMapTestCase(testtools.TestCase):

    def test_paging_assembles_map(self):
        page0 = {
            'aaa': [_entry('debian_unstable', '1.0', 'outdated', 'aaa'), _entry('arch', '2.0', 'newest', 'aaa')],
            'bbb': [_entry('debian_unstable', '3.0', 'newest', 'bbb')],
        }
        page1 = {
            'ccc': [_entry('debian_unstable', '1.0', 'outdated', 'ccc'), _entry('arch', '1.5', 'newest', 'ccc')],
        }
        mapping = builder.build_staleness_map(
            FakePagedHttp([page0, page1]), repo='debian_unstable', page_size=2)
        self.assertEqual('2.0', mapping['aaa'])
        self.assertEqual('3.0', mapping['bbb'])
        self.assertEqual('1.5', mapping['ccc'])

    def test_malformed_page_values_are_skipped(self):
        # External data is untrusted: project values that are not lists of dicts,
        # and non-dict entries within a list, must be skipped, not crash.
        page = {
            'good': [_entry('debian_unstable', '3.0', 'newest', 'good')],
            'not_a_list': 'oops',
            'null': None,
            'mixed': ['junk', _entry('debian_unstable', '4.0', 'newest', 'mixed')],
        }
        mapping = builder.build_staleness_map(
            FakePagedHttp([page]), repo='debian_unstable', page_size=10)
        self.assertEqual('3.0', mapping['good'])
        self.assertEqual('4.0', mapping['mixed'])
        self.assertNotIn('not_a_list', mapping)
        self.assertNotIn('null', mapping)

    def test_max_pages_caps_the_sweep(self):
        # A server that always returns a full page and keeps the pager moving
        # must not loop forever; max_pages bounds it.
        class EndlessHttp:
            def __init__(self):
                self.calls = 0

            def get_json(self, url, *, cache_namespace, cache_key, ttl_seconds):
                self.calls += 1
                name = 'pkg%05d' % self.calls
                return {name: [_entry('debian_unstable', '1.0', 'newest', name)]}

        http = EndlessHttp()
        builder.build_staleness_map(http, repo='debian_unstable', page_size=1, max_pages=5)
        self.assertEqual(5, http.calls)

    def test_stops_on_no_forward_progress(self):
        # A page whose greatest key equals the current start would loop forever;
        # the no-progress guard breaks instead.
        page = {'same': [_entry('debian_unstable', '1.0', 'newest', 'same')]}
        http = FakePagedHttp([page, page, page])
        builder.build_staleness_map(http, repo='debian_unstable', page_size=1)
        self.assertEqual(2, http.calls)
