'''Repology source adapter: the staleness axis.

Maps an installed Debian source package to its Repology project (via the
project-by resolver, which is more robust than assuming the project name equals
the source name), finds the newest stable upstream version, and compares it
against the installed upstream version to decide whether the package is behind.

Two correctness points:

* Repology's ``version`` field is upstream-only (epoch and Debian revision
  stripped), so comparisons use the *upstream portion* of the installed
  version, never the full Debian version.
* The newest *stable* version is what we measure against (Repology's "newest"
  status), so a development/pre-release does not make every package look behind.

Anything that cannot be resolved is reported as UNKNOWN, never as BEHIND.
'''
from __future__ import annotations

import enum
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from divergulent import debversion
from divergulent.cache import Cache
from divergulent.debversion import DebianVersion
from divergulent.http import HttpClient


REPOLOGY_BASE = 'https://repology.org'
CACHE_NAMESPACE = 'repology'
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours

# Bulk staleness (whole-repo sweep) caches.
BULK_NAMESPACE = 'repology-bulk'
BULK_PAGE_NAMESPACE = 'repology-bulk-page'
BULK_TTL_SECONDS = 24 * 60 * 60
PROJECTS_PER_PAGE = 200

# Repology statuses that do not represent a usable, trusted version.
_IGNORED_STATUSES = frozenset({'ignored', 'incorrect', 'untrusted', 'noscheme'})


class StalenessState(enum.Enum):
    CURRENT = 'current'
    BEHIND = 'behind'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class StalenessResult:
    source_package: str
    installed_version: DebianVersion
    newest_version: str | None
    state: StalenessState


class RepologySource:
    '''Determine staleness of a source package against Repology.'''

    name = 'repology'

    def __init__(self, http_client: HttpClient, resolver_repo: str = 'debian_unstable') -> None:
        self._http = http_client
        self._resolver_repo = resolver_repo

    def _project_by_url(self, source_package: str) -> str:
        query = urllib.parse.urlencode({
            'repo': self._resolver_repo,
            'name_type': 'srcname',
            'target_page': 'api_v1_project',
            'name': source_package,
        })
        return f'{REPOLOGY_BASE}/tools/project-by?{query}'

    def lookup(self, source_package: str) -> list[dict] | None:
        '''Return the Repology project entries for a source package, or None.'''
        data = self._http.get_json(
            self._project_by_url(source_package),
            cache_namespace=CACHE_NAMESPACE,
            cache_key=f'{self._resolver_repo}:{source_package}',
            ttl_seconds=CACHE_TTL_SECONDS)
        if not isinstance(data, list) or not data:
            return None
        return data

    def newest_version(self, entries: Sequence[dict[str, Any]]) -> str | None:
        '''Return the newest stable upstream version among project entries.'''
        return _select_newest(entries)

    def staleness(self, source_package: str, installed_version: DebianVersion) -> StalenessResult:
        '''Decide whether ``installed_version`` of ``source_package`` is behind.'''
        entries = self.lookup(source_package)
        newest = _select_newest(entries) if entries is not None else None
        return StalenessResult(
            source_package, installed_version, newest, _state_for(installed_version, newest))


def _select_newest(entries: Sequence[dict[str, Any]]) -> str | None:
    '''The newest stable upstream version among a project's Repology entries.

    Prefer entries flagged "newest" (the latest stable); else the maximum valid
    version. Entries with ignored/incorrect/untrusted/noscheme status, or a
    version that is not a valid Debian version (other distros' schemes, e.g.
    Gentoo's "5.3_p15", which cannot be ordered with Debian semantics), are
    skipped.
    '''
    usable = [
        entry for entry in entries
        if entry.get('version')
        and entry.get('status') not in _IGNORED_STATUSES
        and debversion.try_parse(entry['version']) is not None]
    if not usable:
        return None

    stable = [entry['version'] for entry in usable if entry.get('status') == 'newest']
    candidates = stable or [entry['version'] for entry in usable]

    best = candidates[0]
    for version in candidates[1:]:
        if debversion.compare(version, best) > 0:
            best = version
    return best


def _state_for(installed_version: DebianVersion, newest: str | None) -> StalenessState:
    '''Classify staleness of an installed version against a newest version.

    Repology versions are upstream-only, so compare against the upstream part of
    the installed version, not its full epoch:upstream-revision form. Anything
    not comparable is UNKNOWN, never a false BEHIND.
    '''
    if newest is None:
        return StalenessState.UNKNOWN
    installed_upstream = installed_version.upstream_version
    if debversion.try_parse(installed_upstream) is None:
        return StalenessState.UNKNOWN
    if debversion.compare(installed_upstream, newest) < 0:
        return StalenessState.BEHIND
    return StalenessState.CURRENT


def _projects_url(repo: str, start: str | None) -> str:
    if start:
        base = '%s/api/v1/projects/%s/' % (REPOLOGY_BASE, urllib.parse.quote(start))
    else:
        base = '%s/api/v1/projects/' % REPOLOGY_BASE
    return '%s?inrepo=%s' % (base, urllib.parse.quote(repo))


def build_staleness_map(http_client: HttpClient, cache: Cache, repo: str = 'debian_unstable',
                        page_size: int = PROJECTS_PER_PAGE) -> dict[str, str]:
    '''Return {Debian srcname: newest version} for the whole repo, cached ~24h.

    Instead of one Repology request per source package, page through the entire
    repo's project set once (cheap per-archive, not per-machine) and cache the
    assembled map. Repology mandates <=1 req/s, so this is the slow part of a
    cold run, but it is built once and reused.
    '''
    cached = cache.get(BULK_NAMESPACE, repo)
    if cached is not None:
        return cached

    mapping: dict[str, str] = {}
    start = None
    while True:
        page = http_client.get_json(
            _projects_url(repo, start),
            cache_namespace=BULK_PAGE_NAMESPACE,
            cache_key='%s:%s' % (repo, start or ''),
            ttl_seconds=BULK_TTL_SECONDS)
        if not isinstance(page, dict) or not page:
            break
        for entries in page.values():
            newest = _select_newest(entries)
            if newest is None:
                continue
            for entry in entries:
                if entry.get('repo') == repo and entry.get('srcname'):
                    mapping[entry['srcname']] = newest
        if len(page) < page_size:
            break
        next_start = sorted(page)[-1]
        if next_start == start:  # safety: no forward progress
            break
        start = next_start

    cache.set(BULK_NAMESPACE, repo, mapping, ttl_seconds=BULK_TTL_SECONDS)
    return mapping


class RepologyBulkSource:
    '''Staleness from a prebuilt {srcname: newest} map (bulk Repology data).

    Drop-in for RepologySource in the whole-machine commands: a source absent
    from the map is UNKNOWN (no per-package fallback request).
    '''

    name = 'repology'

    def __init__(self, staleness_map: dict[str, str]) -> None:
        self._map = staleness_map

    def staleness(self, source_package: str, installed_version: DebianVersion) -> StalenessResult:
        newest = self._map.get(source_package)
        return StalenessResult(
            source_package, installed_version, newest, _state_for(installed_version, newest))
