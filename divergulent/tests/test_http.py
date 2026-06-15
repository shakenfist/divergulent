import tempfile
import threading
import time
import urllib.error
from pathlib import Path

import testtools

from divergulent import http
from divergulent.cache import Cache


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self, amt=None):
        # urllib's response.read(amt) reads at most amt bytes; mirror that so the
        # client's size-cap probe (read(max_bytes + 1)) behaves like the real one.
        if amt is None:
            return self._payload
        return self._payload[:amt]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class HttpClientTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # The cache's own clock is fixed at 0; entries set here stay fresh.
        self.cache = Cache(Path(tmp.name), clock=lambda: 0.0)
        self.now = [1000.0]
        self.slept = []

    def _clock(self):
        return self.now[0]

    def _sleep(self, seconds):
        self.slept.append(seconds)
        self.now[0] += seconds  # sleeping advances the fake clock

    def _client(self, urlopen):
        return http.HttpClient(
            self.cache, urlopen=urlopen, clock=self._clock, sleep=self._sleep,
            min_interval=1.0, timeout=5.0, user_agent='ua/1')

    def test_fetches_and_parses_json(self):
        calls = []

        def urlopen(request, timeout=None):
            calls.append((request, timeout))
            return FakeResponse(b'{"a": 1}')

        client = self._client(urlopen)
        data = client.get_json('https://repology.org/x', cache_namespace='r', cache_key='k', ttl_seconds=100)
        self.assertEqual({'a': 1}, data)
        self.assertEqual(1, len(calls))
        request, timeout = calls[0]
        self.assertEqual('ua/1', request.get_header('User-agent'))
        self.assertEqual(5.0, timeout)

    def test_cache_hit_skips_network(self):
        self.cache.set('r', 'k', {'cached': True}, ttl_seconds=100)

        def urlopen(request, timeout=None):
            raise AssertionError('the network must not be touched on a cache hit')

        client = self._client(urlopen)
        data = client.get_json('https://repology.org/x', cache_namespace='r', cache_key='k', ttl_seconds=100)
        self.assertEqual({'cached': True}, data)
        self.assertEqual([], self.slept)

    def test_rate_limit_spaces_requests(self):
        payloads = [b'{"n": 1}', b'{"n": 2}']

        def urlopen(request, timeout=None):
            return FakeResponse(payloads.pop(0))

        client = self._client(urlopen)
        client.get_json('https://repology.org/a', cache_namespace='r', cache_key='a', ttl_seconds=100)
        client.get_json('https://repology.org/b', cache_namespace='r', cache_key='b', ttl_seconds=100)
        # The second call followed immediately, so the client slept ~1s to space it.
        self.assertEqual(1, len(self.slept))
        self.assertAlmostEqual(1.0, self.slept[0])

    def test_rate_limit_is_per_host(self):
        payloads = [b'{"n": 1}', b'{"n": 2}']

        def urlopen(request, timeout=None):
            return FakeResponse(payloads.pop(0))

        client = self._client(urlopen)
        client.get_json('https://repology.org/a', cache_namespace='r', cache_key='a', ttl_seconds=100)
        client.get_json('https://sources.debian.org/b', cache_namespace='r', cache_key='b', ttl_seconds=100)
        # Different hosts must not wait on each other.
        self.assertEqual([], self.slept)

    def test_per_host_interval_override(self):
        payloads = [b'{"n": 1}', b'{"n": 2}']

        def urlopen(request, timeout=None):
            return FakeResponse(payloads.pop(0))

        client = http.HttpClient(
            self.cache, urlopen=urlopen, clock=self._clock, sleep=self._sleep,
            min_interval=1.0, host_intervals={'fast.example': 0.25}, user_agent='ua/1')
        client.get_json('https://fast.example/a', cache_namespace='r', cache_key='a', ttl_seconds=100)
        client.get_json('https://fast.example/b', cache_namespace='r', cache_key='b', ttl_seconds=100)
        # The override host waits its shorter interval, not the 1.0s default.
        self.assertEqual(1, len(self.slept))
        self.assertAlmostEqual(0.25, self.slept[0])

    def test_url_error_returns_none(self):
        def urlopen(request, timeout=None):
            raise urllib.error.URLError('boom')

        client = self._client(urlopen)
        self.assertIsNone(
            client.get_json('https://repology.org/x', cache_namespace='r', cache_key='k', ttl_seconds=100))

    def test_bad_json_returns_none(self):
        def urlopen(request, timeout=None):
            return FakeResponse(b'not json')

        client = self._client(urlopen)
        self.assertIsNone(
            client.get_json('https://repology.org/x', cache_namespace='r', cache_key='k', ttl_seconds=100))

    def test_failure_is_not_cached(self):
        attempts = []

        def urlopen(request, timeout=None):
            attempts.append(1)
            raise urllib.error.URLError('boom')

        client = self._client(urlopen)
        client.get_json('https://repology.org/x', cache_namespace='r', cache_key='k', ttl_seconds=100)
        client.get_json('https://repology.org/x', cache_namespace='r', cache_key='k', ttl_seconds=100)
        self.assertEqual(2, len(attempts))

    def test_get_text_returns_body(self):
        def urlopen(request, timeout=None):
            return FakeResponse(b'Forwarded: no\n--- a/x\n')

        client = self._client(urlopen)
        text = client.get_text('https://sources.debian.org/p', cache_namespace='d', cache_key='k', ttl_seconds=100)
        self.assertEqual('Forwarded: no\n--- a/x\n', text)

    def test_get_text_cache_hit_skips_network(self):
        self.cache.set('d', 'k', 'cached text', ttl_seconds=100)

        def urlopen(request, timeout=None):
            raise AssertionError('the network must not be touched on a cache hit')

        client = self._client(urlopen)
        self.assertEqual(
            'cached text',
            client.get_text('https://sources.debian.org/p', cache_namespace='d', cache_key='k', ttl_seconds=100))

    def test_get_text_error_returns_none(self):
        def urlopen(request, timeout=None):
            raise urllib.error.URLError('boom')

        client = self._client(urlopen)
        self.assertIsNone(
            client.get_text('https://sources.debian.org/p', cache_namespace='d', cache_key='k', ttl_seconds=100))

    def test_response_at_size_cap_is_accepted(self):
        def urlopen(request, timeout=None):
            return FakeResponse(b'{"a": 1}')

        client = http.HttpClient(
            self.cache, urlopen=urlopen, clock=self._clock, sleep=self._sleep,
            min_interval=1.0, max_bytes=8, user_agent='ua/1')
        data = client.get_json('https://repology.org/x', cache_namespace='r', cache_key='k', ttl_seconds=100)
        self.assertEqual({'a': 1}, data)

    def test_oversized_response_returns_none(self):
        def urlopen(request, timeout=None):
            return FakeResponse(b'{"a": 1, "b": 2}')

        client = http.HttpClient(
            self.cache, urlopen=urlopen, clock=self._clock, sleep=self._sleep,
            min_interval=1.0, max_bytes=8, user_agent='ua/1')
        self.assertIsNone(
            client.get_json('https://repology.org/x', cache_namespace='r', cache_key='k', ttl_seconds=100))


class ThrottleConcurrencyTestCase(testtools.TestCase):
    '''The per-host throttle must stay correct under concurrent fetches.'''

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Cache(Path(tmp.name), clock=lambda: 0.0)

    def _real_client(self, **kwargs):
        # Real clock/sleep so we exercise actual thread timing, with a fast
        # urlopen so only the throttle contributes measurable delay.
        return http.HttpClient(
            self.cache, urlopen=lambda request, timeout=None: FakeResponse(b'{}'),
            user_agent='ua/1', **kwargs)

    def test_same_host_requests_are_spaced_across_threads(self):
        interval = 0.05
        client = self._real_client(min_interval=interval)

        def fetch(index):
            client.get_json(
                'https://repology.org/p', cache_namespace='r', cache_key='k-%d' % index, ttl_seconds=100)

        threads = [threading.Thread(target=fetch, args=(i,)) for i in range(5)]
        start = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.monotonic() - start
        # Five same-host requests reserve sequential slots, so the run takes at
        # least (5 - 1) intervals even though they were launched concurrently.
        self.assertGreaterEqual(elapsed, 4 * interval * 0.8)

    def test_different_hosts_do_not_block_each_other(self):
        interval = 0.2
        client = self._real_client(min_interval=interval)

        def fetch(host):
            client.get_json(
                'https://%s/p' % host, cache_namespace='r', cache_key=host, ttl_seconds=100)

        hosts = ['a.example', 'b.example', 'c.example', 'd.example']
        threads = [threading.Thread(target=fetch, args=(h,)) for h in hosts]
        start = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.monotonic() - start
        # Each host's first request has no predecessor, so distinct hosts run in
        # parallel and the batch finishes well under a single interval.
        self.assertLess(elapsed, interval)
