import tempfile
import threading
from pathlib import Path

import testtools

from divergulent import cache


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class CacheTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.clock = FakeClock()
        self.cache = cache.Cache(self.root, clock=self.clock)

    def test_set_get_roundtrip(self):
        self.cache.set('repology', 'pngtools', {'version': '1.3'}, ttl_seconds=100)
        self.assertEqual({'version': '1.3'}, self.cache.get('repology', 'pngtools'))

    def test_miss_returns_none(self):
        self.assertIsNone(self.cache.get('repology', 'absent'))

    def test_stores_list_values(self):
        self.cache.set('patches', 'bash', ['a.patch', 'b.patch'], ttl_seconds=100)
        self.assertEqual(['a.patch', 'b.patch'], self.cache.get('patches', 'bash'))

    def test_ttl_not_yet_expired(self):
        self.cache.set('ns', 'k', 'v', ttl_seconds=100)
        self.clock.now += 50
        self.assertEqual('v', self.cache.get('ns', 'k'))

    def test_ttl_expired(self):
        self.cache.set('ns', 'k', 'v', ttl_seconds=100)
        self.clock.now += 101
        self.assertIsNone(self.cache.get('ns', 'k'))

    def test_namespaces_are_isolated(self):
        self.cache.set('a', 'k', 'va', ttl_seconds=100)
        self.cache.set('b', 'k', 'vb', ttl_seconds=100)
        self.assertEqual('va', self.cache.get('a', 'k'))
        self.assertEqual('vb', self.cache.get('b', 'k'))

    def test_key_cannot_escape_root(self):
        evil = '../../../../etc/passwd'
        path = self.cache._path('ns', evil)
        self.assertEqual(self.root, path.parent)
        # And a round-trip with the evil key stays inside the cache dir.
        self.cache.set('ns', evil, 'safe', ttl_seconds=100)
        self.assertEqual('safe', self.cache.get('ns', evil))
        for child in self.root.iterdir():
            self.assertEqual(self.root, child.parent)

    def test_concurrent_writes_do_not_corrupt(self):
        # Many threads writing distinct keys, plus several hammering one shared
        # key, must all land as valid JSON with no leftover temp files (the
        # unique-temp-file write makes this safe under concurrency).
        def writer(index):
            self.cache.set('ns', 'key-%d' % index, {'n': index}, ttl_seconds=100)

        def shared_writer(index):
            self.cache.set('ns', 'shared', {'n': index}, ttl_seconds=100)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(40)]
        threads += [threading.Thread(target=shared_writer, args=(i,)) for i in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        for index in range(40):
            self.assertEqual({'n': index}, self.cache.get('ns', 'key-%d' % index))
        # The shared key resolved to exactly one of the written values.
        self.assertIn(self.cache.get('ns', 'shared')['n'], range(10))
        # No stray temp files were left behind.
        self.assertEqual([], [p.name for p in self.root.iterdir() if p.name.endswith('.tmp')])
