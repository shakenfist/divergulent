import tempfile
import urllib.error
from pathlib import Path

import testtools

from divergulent import http
from divergulent.cache import Cache


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

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
