'''HTTP client with the politeness external sources require.

This wraps fetching, rate limiting and caching in one place so every
network-backed source behaves well: an identifying User-Agent, a request
timeout, at most one request per second, on-disk caching, and graceful
degradation (failures return None rather than raising). It uses the standard
library ``urllib`` to keep divergulent's runtime dependency surface minimal.
'''
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from divergulent import __version__
from divergulent.cache import Cache


DEFAULT_USER_AGENT = f'divergulent/{__version__} (+https://github.com/shakenfist/divergulent)'

# Refuse responses larger than this; external data is untrusted and we never
# need a body this big.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class HttpClient:
    '''Fetch resources politely, caching results and rate-limiting the network.'''

    def __init__(self, cache: Cache, *, user_agent: str = DEFAULT_USER_AGENT,
                 timeout: float = 10.0, min_interval: float = 1.0,
                 host_intervals: dict[str, float] | None = None,
                 max_bytes: int = MAX_RESPONSE_BYTES, refresh: bool = False,
                 urlopen: Callable[..., Any] = urllib.request.urlopen,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._cache = cache
        self._user_agent = user_agent
        self._timeout = timeout
        self._min_interval = min_interval
        self._max_bytes = max_bytes
        # In refresh mode every request skips the cache read but still writes the
        # fresh result back, so a builder can force a clean recompute that also
        # repopulates the cache. A purely incremental builder would otherwise
        # republish a once-bad cached value forever.
        self._refresh = refresh
        # Optional per-host minimum interval overriding min_interval, so a host
        # with no documented rate limit can run faster than the conservative
        # default while a rate-limited host (e.g. Repology) stays slow.
        self._host_intervals = host_intervals or {}
        self._urlopen = urlopen
        self._clock = clock
        self._sleep = sleep
        # Next allowed start time per host, so different hosts do not serialise
        # against each other while each still gets its own interval. Guarded by
        # a lock so the throttle is safe under concurrent fetches: a thread
        # reserves its slot under the lock, then sleeps to it outside the lock.
        self._next_allowed: dict[str, float] = {}
        self._lock = threading.Lock()

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
        retries. In refresh mode the cache read is skipped (but the write is
        not), forcing a fresh fetch that also repopulates the cache.
        '''
        if not self._refresh:
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
        self._throttle(url)
        try:
            request = urllib.request.Request(url, headers={'User-Agent': self._user_agent})
            with self._urlopen(request, timeout=self._timeout) as response:
                # Read one byte past the cap so we can tell "exactly at the cap"
                # from "over it"; an over-cap body is treated as a failure.
                data = response.read(self._max_bytes + 1)
                if len(data) > self._max_bytes:
                    return None
                return data
        except (urllib.error.URLError, TimeoutError, OSError):
            return None

    def _throttle(self, url: str) -> None:
        host = urllib.parse.urlparse(url).netloc
        interval = self._host_intervals.get(host, self._min_interval)
        # Reserve this host's next start slot atomically, then sleep to it
        # outside the lock. Under concurrency this keeps per-host spacing (e.g.
        # Repology stays <=1 req/s in aggregate) while other hosts and a
        # zero-interval host proceed without blocking on this one.
        with self._lock:
            now = self._clock()
            scheduled = max(now, self._next_allowed.get(host, now))
            self._next_allowed[host] = scheduled + interval
        wait = scheduled - self._clock()
        if wait > 0:
            self._sleep(wait)
