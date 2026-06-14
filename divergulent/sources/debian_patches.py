'''sources.debian.org adapter: the divergence axis.

Reads each installed source package's quilt patch series from the
sources.debian.org patches API, fetches each patch's content, and classifies it
with DEP-3 to count carried Debian-only patches versus forwarded ones.

Native packages have no upstream/Debian split (NATIVE). Packages we cannot
resolve, or whose source format is not a quilt series, are UNKNOWN — never
reported as zero-divergence.

Raw patch content lives under the Debian pool path (e.g.
/data/main/b/bash/<version>/debian/patches/...). Rather than reconstruct that
pool prefix (area + hashed directory) ourselves, we ask the file-info API for
one patch's ``raw_url`` and derive the shared directory base from it.
'''
from __future__ import annotations

import enum
import urllib.parse
from dataclasses import dataclass

from divergulent import dep3
from divergulent.dep3 import PatchClass
from divergulent.http import HttpClient


SOURCES_BASE = 'https://sources.debian.org'
SERIES_NAMESPACE = 'debian-patches-series'
BASE_NAMESPACE = 'debian-patches-base'
PATCH_NAMESPACE = 'debian-patches-file'
PATCHES_MARKER = '/debian/patches/'
# Patch content for a fixed (package, version) is immutable, so cache it for a
# long time.
CACHE_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days


class DivergenceState(enum.Enum):
    PATCHED = 'patched'    # carries one or more patches
    CLEAN = 'clean'        # quilt package with an empty series
    NATIVE = 'native'      # native package: no upstream/Debian split
    UNKNOWN = 'unknown'    # could not be resolved / not a quilt series


@dataclass(frozen=True)
class DivergenceResult:
    source_package: str
    version: str
    source_format: str | None
    total: int
    debian_only: int
    forwarded: int
    unknown: int
    state: DivergenceState


def _unknown(source_package: str, version: str, source_format: str | None = None) -> 'DivergenceResult':
    return DivergenceResult(source_package, version, source_format, 0, 0, 0, 0, DivergenceState.UNKNOWN)


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe='')


class DebianPatchesSource:
    '''Measure carried-patch divergence of a source package via sources.debian.org.'''

    name = 'debian-patches'

    def __init__(self, http_client: HttpClient) -> None:
        self._http = http_client

    def _series_url(self, source_package: str, version: str) -> str:
        return f'{SOURCES_BASE}/patches/api/{_quote(source_package)}/{_quote(version)}/'

    def _file_api_url(self, source_package: str, version: str, patch_name: str) -> str:
        # The file-info API requires a trailing slash. Patch names may contain
        # subdirectories, so keep their slashes.
        name = urllib.parse.quote(patch_name)
        return f'{SOURCES_BASE}/api/src/{_quote(source_package)}/{_quote(version)}/debian/patches/{name}/'

    def lookup(self, source_package: str, version: str) -> dict | None:
        '''Return the patches-API JSON for one source package version, or None.'''
        data = self._http.get_json(
            self._series_url(source_package, version),
            cache_namespace=SERIES_NAMESPACE,
            cache_key=f'{source_package}:{version}',
            ttl_seconds=CACHE_TTL_SECONDS)
        return data if isinstance(data, dict) else None

    def _raw_base(self, source_package: str, version: str, sample_patch: str) -> str | None:
        '''Discover the raw-content directory base via one patch's raw_url.'''
        info = self._http.get_json(
            self._file_api_url(source_package, version, sample_patch),
            cache_namespace=BASE_NAMESPACE,
            cache_key=f'base:{source_package}:{version}',
            ttl_seconds=CACHE_TTL_SECONDS)
        if not isinstance(info, dict):
            return None
        raw_url = info.get('raw_url')
        if not isinstance(raw_url, str) or PATCHES_MARKER not in raw_url:
            return None
        base_path = raw_url[:raw_url.index(PATCHES_MARKER) + len(PATCHES_MARKER)]
        return SOURCES_BASE + base_path

    def _classify_patch(self, base: str | None, source_package: str, version: str, patch_name: str) -> PatchClass:
        if base is None:
            return PatchClass.UNKNOWN
        text = self._http.get_text(
            base + urllib.parse.quote(patch_name),
            cache_namespace=PATCH_NAMESPACE,
            cache_key=f'{source_package}:{version}:{patch_name}',
            ttl_seconds=CACHE_TTL_SECONDS)
        if text is None:
            return PatchClass.UNKNOWN
        return dep3.classify(text, patch_name)

    def divergence(self, source_package: str, version: str) -> DivergenceResult:
        '''Classify the carried patches of an installed source package version.'''
        info = None
        effective = version
        # sources.debian.org may or may not include the epoch in the path; try
        # the version as installed, then with the epoch stripped.
        for candidate in self._candidate_versions(version):
            info = self.lookup(source_package, candidate)
            if info is not None:
                effective = candidate
                break
        if info is None:
            return _unknown(source_package, version)

        source_format = info.get('format')
        fmt = (source_format or '').lower()
        patches = info.get('patches') or []

        if 'native' in fmt:
            return DivergenceResult(source_package, version, source_format, 0, 0, 0, 0, DivergenceState.NATIVE)

        if patches:
            base = self._raw_base(source_package, effective, patches[0])
            counts = {PatchClass.DEBIAN_ONLY: 0, PatchClass.FORWARDED: 0, PatchClass.UNKNOWN: 0}
            for patch_name in patches:
                counts[self._classify_patch(base, source_package, effective, patch_name)] += 1
            return DivergenceResult(
                source_package, version, source_format,
                total=len(patches),
                debian_only=counts[PatchClass.DEBIAN_ONLY],
                forwarded=counts[PatchClass.FORWARDED],
                unknown=counts[PatchClass.UNKNOWN],
                state=DivergenceState.PATCHED)

        if 'quilt' in fmt:
            return DivergenceResult(source_package, version, source_format, 0, 0, 0, 0, DivergenceState.CLEAN)

        # A non-quilt, non-native format (e.g. 1.0): divergence is not captured
        # by a quilt series, so we do not claim it is clean.
        return _unknown(source_package, version, source_format)

    @staticmethod
    def _candidate_versions(version: str):
        yield version
        if ':' in version:
            yield version.split(':', 1)[1]
