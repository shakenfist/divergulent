'''On-disk cache for data fetched from external sources.

Cache keys are hashed (sha256) to form the on-disk filename, so a key derived
from an external source can never escape the cache directory via path
traversal. Entries are stored as JSON ``{stored_at, ttl, value}`` and written
atomically. The clock is injectable so TTL behaviour is testable.
'''
from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


def default_cache_dir() -> Path:
    '''Resolve the cache directory, honouring an explicit override then XDG.'''
    override = os.environ.get('DIVERGULENT_CACHE_DIR')
    if override:
        return Path(override)
    xdg = os.environ.get('XDG_CACHE_HOME')
    base = Path(xdg) if xdg else Path.home() / '.cache'
    return base / 'divergulent'


class Cache:
    '''A minimal filesystem cache with per-entry TTL.'''

    def __init__(self, root: Path, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self._clock = clock

    def _path(self, namespace: str, key: str) -> Path:
        # Hashing the key means a malicious or surprising key (e.g. one
        # containing "../") cannot escape self.root.
        digest = hashlib.sha256(f'{namespace}\x00{key}'.encode('utf-8')).hexdigest()
        return self.root / f'{digest}.json'

    def get(self, namespace: str, key: str) -> Any:
        '''Return the cached value, or None if absent or expired.'''
        path = self._path(namespace, key)
        try:
            with open(path, 'r') as handle:
                entry = json.load(handle)
        except (FileNotFoundError, ValueError):
            return None
        if self._clock() - entry['stored_at'] > entry['ttl']:
            return None
        return entry['value']

    def set(self, namespace: str, key: str, value: Any, ttl_seconds: float) -> None:
        '''Store a JSON-serialisable value under (namespace, key) with a TTL.'''
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(namespace, key)
        entry = {'stored_at': self._clock(), 'ttl': ttl_seconds, 'value': value}
        tmp = path.with_name(path.name + '.tmp')
        with open(tmp, 'w') as handle:
            json.dump(entry, handle)
        os.replace(tmp, path)
