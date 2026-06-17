'''The precomputed cache bundle: schema, writer, and loader.

A bundle is the shareable half of a cold run — staleness and divergence for a
whole Debian release, computed centrally so a client downloads it once instead
of hammering Repology and sources.debian.org. Both axes are functions of
``(source_package, version)`` plus the upstream world, never of the user's
machine, so the data is the same for everyone on a given release.

The bundle is a single gzipped JSON document. ``schema`` versions the envelope
and ``cache_schema`` the per-entry value shape, so a client can refuse a bundle
it does not understand and fall back to the live path rather than misread it.
The data is architecture-independent (divergence lives in the arch-independent
source package); ``built_on`` records the build host purely as provenance.

The dataclass core takes ``generated_at`` and the host facts as plain values
rather than reading the clock or ``uname`` itself, so assembling and
round-tripping a bundle stays offline and deterministic in tests.
'''
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Envelope schema: the top-level shape (keys below). Bump when that changes.
SCHEMA_VERSION = 1
# Per-entry value schema: the shape of each divergence value. Bump when the
# staleness/divergence value layout changes without the envelope changing.
CACHE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Bundle:
    '''A precomputed staleness + divergence dataset for one Debian release.

    ``staleness`` maps a source package name to its newest upstream version (a
    bare string, as Repology reports it). ``divergence`` maps a source package
    name to ``{version, format, total, state}`` for one specific version — a
    client uses it only when the installed source version matches, since
    divergence is version-specific.
    '''

    schema: int
    cache_schema: int
    generated_at: str
    release: str
    repology_repo: str
    built_on: dict[str, str]
    staleness: dict[str, str]
    divergence: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            'schema': self.schema,
            'cache_schema': self.cache_schema,
            'generated_at': self.generated_at,
            'release': self.release,
            'repology_repo': self.repology_repo,
            'built_on': self.built_on,
            'staleness': self.staleness,
            'divergence': self.divergence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'Bundle':
        return cls(
            schema=data['schema'],
            cache_schema=data['cache_schema'],
            generated_at=data['generated_at'],
            release=data['release'],
            repology_repo=data['repology_repo'],
            built_on=data['built_on'],
            staleness=data['staleness'],
            divergence=data['divergence'])


def write(bundle: Bundle, path: str | Path) -> None:
    '''Write a bundle to ``path`` as gzipped JSON.'''
    payload = json.dumps(bundle.to_dict(), separators=(',', ':'), sort_keys=True).encode('utf-8')
    with gzip.open(path, 'wb') as handle:
        handle.write(payload)


def load(path: str | Path) -> Bundle:
    '''Read a gzipped-JSON bundle from ``path``.'''
    with gzip.open(path, 'rb') as handle:
        data = json.loads(handle.read().decode('utf-8'))
    return Bundle.from_dict(data)
