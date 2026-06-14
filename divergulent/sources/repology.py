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
from divergulent.debversion import DebianVersion
from divergulent.http import HttpClient


REPOLOGY_BASE = 'https://repology.org'
CACHE_NAMESPACE = 'repology'
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours

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
        '''Return the newest stable upstream version among project entries.

        Prefer entries flagged "newest" (the latest stable). Otherwise fall back
        to the maximum valid version. Entries with an ignored/incorrect/
        untrusted/noscheme status are skipped.
        '''
        # Skip entries whose version is not a valid Debian version: other
        # distributions' schemes (e.g. Gentoo's "5.3_p15") cannot be ordered
        # with Debian semantics, and feeding them to the comparator would raise.
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

    def staleness(self, source_package: str, installed_version: DebianVersion) -> StalenessResult:
        '''Decide whether ``installed_version`` of ``source_package`` is behind.'''
        entries = self.lookup(source_package)
        if entries is None:
            return StalenessResult(source_package, installed_version, None, StalenessState.UNKNOWN)

        newest = self.newest_version(entries)
        if newest is None:
            return StalenessResult(source_package, installed_version, None, StalenessState.UNKNOWN)

        # Repology versions are upstream-only; compare against the upstream part
        # of the installed version, not its full epoch:upstream-revision form.
        installed_upstream = installed_version.upstream_version
        if debversion.try_parse(installed_upstream) is None:
            return StalenessResult(source_package, installed_version, newest, StalenessState.UNKNOWN)
        if debversion.compare(installed_upstream, newest) < 0:
            state = StalenessState.BEHIND
        else:
            state = StalenessState.CURRENT
        return StalenessResult(source_package, installed_version, newest, state)
