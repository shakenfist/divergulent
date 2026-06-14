'''HTTP client with the politeness external sources require.

This wraps fetching, rate limiting and caching in one place so every
network-backed source behaves well: an identifying User-Agent, a request
timeout, at most one request per second, on-disk caching, and graceful
degradation (failures return None rather than raising). It uses the standard
library ``urllib`` to keep divergulent's runtime dependency surface minimal.
'''
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from divergulent import __version__
from divergulent.cache import Cache


DEFAULT_USER_AGENT = f'divergulent/{__version__} (+https://github.com/shakenfist/divergulent)'


class HttpClient:
    '''Fetch resources politely, caching results and rate-limiting the network.'''

    def __init__(self, cache: Cache, *, user_agent: str = DEFAULT_USER_AGENT,
                 timeout: float = 10.0, min_interval: float = 1.0,
                 urlopen: Callable[..., Any] = urllib.request.urlopen,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._cache = cache
        self._user_agent = user_agent
        self._timeout = timeout
        self._min_interval = min_interval
        self._urlopen = urlopen
        self._clock = clock
        self._sleep = sleep
        self._last_request: float | None = None

    def get_json(self, url: str, *, cache_namespace: str, cache_key: str,
                 ttl_seconds: float) -> Any:
        '''Return parsed JSON for ``url``, or None on any failure.'''
        return self._cached(url, cache_namespace, cache_key, ttl_seconds, json.loads)

    def get_text(self, url: str, *, cache_namespace: str, cache_key: str,
                 ttl_seconds: float) -> str | None:
        '''Return the decoded text body for ``url``, or None on any failure.'''
        return self._cached(
            url, cache_namespace, cache_key, ttl_seconds,
            lambda payload: payload.decode('utf-8', 'replace'))

    def _cached(self, url: str, namespace: str, key: str, ttl_seconds: float,
                decode: Callable[[bytes], Any]) -> Any:
        '''Return a cached value, or fetch, decode, cache and return it.

        A cache hit returns immediately without touching the network or the
        rate limiter. Failures return None and are not cached, so a later run
        retries.
        '''
        cached = self._cache.get(namespace, key)
        if cached is not None:
            return cached

        payload = self._fetch(url)
        if payload is None:
            return None
        try:
            value = decode(payload)
        except ValueError:
            return None

        self._cache.set(namespace, key, value, ttl_seconds)
        return value

    def _fetch(self, url: str) -> bytes | None:
        self._throttle()
        try:
            request = urllib.request.Request(url, headers={'User-Agent': self._user_agent})
            with self._urlopen(request, timeout=self._timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError):
            return None

    def _throttle(self) -> None:
        now = self._clock()
        if self._last_request is not None:
            wait = self._min_interval - (now - self._last_request)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_request = now
