import json
import os

import testtools

from divergulent import debversion
from divergulent.sources import repology
from divergulent.sources.repology import RepologySource, StalenessState


FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'repology-project-sample.json')


def _entry(repo, version, status, srcname='foo'):
    return {'repo': repo, 'version': version, 'status': status, 'srcname': srcname, 'visiblename': srcname}


class FakeHttp:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def get_json(self, url, *, cache_namespace, cache_key, ttl_seconds):
        self.calls.append((url, cache_key))
        return self.result


def _source(result):
    return RepologySource(FakeHttp(result))


class RepologyUrlTestCase(testtools.TestCase):

    def test_project_by_url_encodes_name(self):
        http = FakeHttp([_entry('arch', '1.0', 'newest')])
        source = RepologySource(http)
        source.lookup('g++')
        url = http.calls[0][0]
        self.assertIn('/tools/project-by?', url)
        self.assertIn('name_type=srcname', url)
        self.assertIn('target_page=api_v1_project', url)
        self.assertIn('repo=debian_unstable', url)
        self.assertIn('name=g%2B%2B', url)


class NewestVersionTestCase(testtools.TestCase):

    def test_prefers_newest_status(self):
        entries = [_entry('debian', '1.2', 'outdated'), _entry('arch', '1.3', 'newest')]
        self.assertEqual('1.3', _source(entries).newest_version(entries))

    def test_devel_does_not_count(self):
        entries = [_entry('arch', '1.3', 'newest'), _entry('aur', '2.0', 'devel')]
        self.assertEqual('1.3', _source(entries).newest_version(entries))

    def test_ignored_statuses_skipped(self):
        entries = [_entry('arch', '1.3', 'newest'), _entry('bad', '9.9', 'ignored')]
        self.assertEqual('1.3', _source(entries).newest_version(entries))

    def test_falls_back_to_max_when_no_newest(self):
        entries = [_entry('a', '1.1', 'outdated'), _entry('b', '1.4', 'unique'), _entry('c', '1.2', 'outdated')]
        self.assertEqual('1.4', _source(entries).newest_version(entries))

    def test_none_when_no_usable_entries(self):
        entries = [_entry('a', '9.9', 'ignored'), _entry('b', '5.5', 'incorrect')]
        self.assertIsNone(_source(entries).newest_version(entries))

    def test_skips_versions_that_are_not_valid_debian_versions(self):
        # '5.3_p15' (Gentoo scheme) cannot be ordered with Debian semantics.
        entries = [_entry('gentoo', '5.3_p15', 'newest'), _entry('debian', '5.2', 'outdated')]
        self.assertEqual('5.2', _source(entries).newest_version(entries))

    def test_none_when_only_unparseable_versions(self):
        entries = [_entry('gentoo', '5.3_p15', 'newest')]
        self.assertIsNone(_source(entries).newest_version(entries))


class StalenessTestCase(testtools.TestCase):

    def test_behind(self):
        entries = [_entry('debian', '1.2', 'outdated'), _entry('arch', '1.3', 'newest')]
        result = _source(entries).staleness('foo', debversion.parse('1.2-1'))
        self.assertEqual(StalenessState.BEHIND, result.state)
        self.assertEqual('1.3', result.newest_version)

    def test_current(self):
        entries = [_entry('arch', '1.3', 'newest')]
        result = _source(entries).staleness('foo', debversion.parse('1.3-1'))
        self.assertEqual(StalenessState.CURRENT, result.state)

    def test_not_behind_a_devel_release(self):
        entries = [_entry('arch', '1.3', 'newest'), _entry('aur', '2.0', 'devel')]
        result = _source(entries).staleness('foo', debversion.parse('1.3-1'))
        self.assertEqual(StalenessState.CURRENT, result.state)

    def test_unknown_when_unresolved(self):
        result = _source(None).staleness('foo', debversion.parse('1.0-1'))
        self.assertEqual(StalenessState.UNKNOWN, result.state)
        self.assertIsNone(result.newest_version)

    def test_unknown_when_no_usable_version(self):
        entries = [_entry('a', '9.9', 'ignored')]
        result = _source(entries).staleness('foo', debversion.parse('1.0-1'))
        self.assertEqual(StalenessState.UNKNOWN, result.state)

    def test_epoch_and_revision_do_not_cause_false_behind(self):
        # Installed 1:2.3.4-2 has upstream 2.3.4; Repology newest is also 2.3.4.
        entries = [_entry('arch', '2.3.4', 'newest')]
        result = _source(entries).staleness('foo', debversion.parse('1:2.3.4-2'))
        self.assertEqual(StalenessState.CURRENT, result.state)

    def test_epoch_version_still_detected_as_behind(self):
        entries = [_entry('arch', '2.3.5', 'newest')]
        result = _source(entries).staleness('foo', debversion.parse('1:2.3.4-2'))
        self.assertEqual(StalenessState.BEHIND, result.state)

    def test_unparseable_repology_version_does_not_crash(self):
        # Regression: a Gentoo-style "5.3_p15" newest entry must be skipped, not
        # crash the comparison. The usable newest is 5.2; installed 5.2.15 is
        # current against it.
        entries = [_entry('gentoo', '5.3_p15', 'newest'), _entry('debian', '5.2', 'outdated')]
        result = _source(entries).staleness('bash', debversion.parse('5.2.15-2'))
        self.assertEqual(StalenessState.CURRENT, result.state)


class FixtureTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        with open(FIXTURE, 'r') as handle:
            self.entries = json.load(handle)

    def test_newest_from_recorded_json(self):
        # Stable newest is 1.3; the 2.0 devel and 9.9 ignored entries are excluded.
        self.assertEqual('1.3', _source(self.entries).newest_version(self.entries))

    def test_behind_from_recorded_json(self):
        result = _source(self.entries).staleness('foo', debversion.parse('1.2-3'))
        self.assertEqual(StalenessState.BEHIND, result.state)
        self.assertEqual('1.3', result.newest_version)


class ProtocolTestCase(testtools.TestCase):

    def test_is_a_source(self):
        from divergulent.sources.base import Source
        self.assertIsInstance(_source([_entry('a', '1.0', 'newest')]), Source)
        self.assertEqual('repology', repology.RepologySource.name)


class FakeCache:
    def __init__(self):
        self.store = {}

    def get(self, namespace, key):
        return self.store.get((namespace, key))

    def set(self, namespace, key, value, ttl_seconds):
        self.store[(namespace, key)] = value


class FakePagedHttp:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = 0

    def get_json(self, url, *, cache_namespace, cache_key, ttl_seconds):
        page = self.pages[self.calls] if self.calls < len(self.pages) else None
        self.calls += 1
        return page


class BulkMapTestCase(testtools.TestCase):

    def test_paging_assembles_map(self):
        page0 = {
            'aaa': [_entry('debian_unstable', '1.0', 'outdated', 'aaa'), _entry('arch', '2.0', 'newest', 'aaa')],
            'bbb': [_entry('debian_unstable', '3.0', 'newest', 'bbb')],
        }
        page1 = {
            'ccc': [_entry('debian_unstable', '1.0', 'outdated', 'ccc'), _entry('arch', '1.5', 'newest', 'ccc')],
        }
        mapping = repology.build_staleness_map(
            FakePagedHttp([page0, page1]), FakeCache(), repo='debian_unstable', page_size=2)
        self.assertEqual('2.0', mapping['aaa'])
        self.assertEqual('3.0', mapping['bbb'])
        self.assertEqual('1.5', mapping['ccc'])

    def test_cached_map_skips_network(self):
        cache = FakeCache()
        cache.set('repology-bulk', 'debian_unstable', {'x': '9'}, ttl_seconds=1)
        http = FakePagedHttp([])
        self.assertEqual({'x': '9'}, repology.build_staleness_map(http, cache, repo='debian_unstable'))
        self.assertEqual(0, http.calls)

    def test_malformed_page_values_are_skipped(self):
        # External data is untrusted: project values that are not lists of dicts,
        # and non-dict entries within a list, must be skipped, not crash.
        page = {
            'good': [_entry('debian_unstable', '3.0', 'newest', 'good')],
            'not_a_list': 'oops',
            'null': None,
            'mixed': ['junk', _entry('debian_unstable', '4.0', 'newest', 'mixed')],
        }
        mapping = repology.build_staleness_map(
            FakePagedHttp([page]), FakeCache(), repo='debian_unstable', page_size=10)
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
                # A fresh full page each time, keyed so next_start always advances.
                name = 'pkg%05d' % self.calls
                return {name: [_entry('debian_unstable', '1.0', 'newest', name)]}

        http = EndlessHttp()
        repology.build_staleness_map(http, FakeCache(), repo='debian_unstable', page_size=1, max_pages=5)
        self.assertEqual(5, http.calls)


class RepologyBulkSourceTestCase(testtools.TestCase):

    def test_states(self):
        source = repology.RepologyBulkSource({'foo': '2.0'})
        self.assertEqual(StalenessState.BEHIND, source.staleness('foo', debversion.parse('1.0-1')).state)
        self.assertEqual(StalenessState.CURRENT, source.staleness('foo', debversion.parse('2.0-1')).state)
        self.assertEqual(StalenessState.UNKNOWN, source.staleness('absent', debversion.parse('1.0-1')).state)

    def test_agrees_with_per_package(self):
        entries = [_entry('debian_unstable', '1.0', 'outdated', 'foo'), _entry('arch', '2.0', 'newest', 'foo')]
        installed = debversion.parse('1.0-1')
        per_package = RepologySource(FakeHttp(entries)).staleness('foo', installed)
        bulk = repology.RepologyBulkSource({'foo': repology._select_newest(entries)}).staleness('foo', installed)
        self.assertEqual(per_package.state, bulk.state)
        self.assertEqual(per_package.newest_version, bulk.newest_version)
