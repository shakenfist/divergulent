'''Bundle-backed source adapters: serve staleness and divergence from a
published bundle, falling back to the live sources for anything it does not
cover.

A bundle (``divergulent.bundle``) holds whole-release staleness and divergence
computed centrally, so for a covered package these adapters answer from a dict
in memory with no network. The fallback wrappers preserve "no cry wolf": a
package the bundle cannot answer is resolved live, so UNKNOWN means neither the
bundle nor the live source could resolve it — never that the bundle simply did
not include it.

Bundle-backed staleness is ``RepologyBulkSource`` (in ``repology.py``, where it
reuses the shared version classification). This module adds the divergence
consumer and the two fallback wrappers.
'''
from __future__ import annotations

from typing import Any

from divergulent.debversion import DebianVersion
from divergulent.sources.debian_patches import DivergenceState, DivergenceSummary
from divergulent.sources.repology import StalenessResult


class BundleDivergenceSource:
    '''Divergence from a bundle's ``{pkg: {version, format, total, state}}`` map.'''

    name = 'debian-patches'

    def __init__(self, divergence_map: dict[str, dict[str, Any]]) -> None:
        self._map = divergence_map

    def summary(self, source_package: str, version: str) -> DivergenceSummary | None:
        '''Published summary for an installed version, or None on a miss.

        Divergence is version-specific, so the bundle entry is used only when its
        version matches the installed version exactly; an absent package, a
        version mismatch, or an unrecognised state string is a miss (None) that
        the fallback wrapper resolves live.
        '''
        entry = self._map.get(source_package)
        if entry is None or entry.get('version') != version:
            return None
        try:
            state = DivergenceState(entry['state'])
        except (KeyError, ValueError):
            return None
        return DivergenceSummary(
            source_package, version, entry.get('format'), entry.get('total', 0), state)


class FallbackStaleness:
    '''Staleness from the bundle, falling back to the live source on a miss.

    A miss is a srcname absent from the bundle's staleness map; the bundle
    covers nearly every installed source, so the live (rate-limited) path is
    reached only for the rare third-party or locally-built package.
    '''

    name = 'repology'

    def __init__(self, bundle_source, live_source) -> None:
        self._bundle = bundle_source
        self._live = live_source

    def staleness(self, source_package: str, installed_version: DebianVersion) -> StalenessResult:
        if self._bundle.lookup(source_package) is not None:
            return self._bundle.staleness(source_package, installed_version)
        return self._live.staleness(source_package, installed_version)


class FallbackDivergence:
    '''Divergence from the bundle, falling back to the live source on a miss.

    A miss is an absent package or a version mismatch (the installed version is
    not the one the bundle described); the live source then resolves it on the
    unthrottled, concurrent sources.debian.org path.
    '''

    name = 'debian-patches'

    def __init__(self, bundle_source, live_source) -> None:
        self._bundle = bundle_source
        self._live = live_source

    def summary(self, source_package: str, version: str) -> DivergenceSummary:
        hit = self._bundle.summary(source_package, version)
        if hit is not None:
            return hit
        return self._live.summary(source_package, version)
