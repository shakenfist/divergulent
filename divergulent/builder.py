'''Central cache builder: enumerate the archive and sweep staleness.

This is the *builder* side of the published cache (see
``docs/plans/PLAN-published-cache.md``) — code that runs once, centrally, in
CI, not on a user's machine. It does the two expensive, fully shareable
computations:

* **Enumeration** — read the Debian release's deb-src ``Sources`` indices to
  learn every ``(source, version, format)`` in the archive, with no network.
* **Bulk staleness** — page through Repology's whole-repo project set once to
  build ``{srcname: newest upstream version}``. Doing this per-package per-user
  was reverted as a cold-run regression (see
  ``PLAN-faster-full-run-phase-04-revert-bulk.md``); centrally it is the right
  tool — one polite sweep feeds every user's bundle.

The per-package client path in ``sources/repology.py`` is deliberately left
untouched; this module imports its version-selection helper so the two agree.
'''
from __future__ import annotations

import glob
import os
import urllib.parse

from debian import deb822  # type: ignore[import-untyped]

from divergulent import debversion
from divergulent.http import HttpClient
from divergulent.sources.apt_patches import deb_src_available
from divergulent.sources.repology import (
    REPOLOGY_BASE, _select_newest)


APT_LISTS_DIR = '/var/lib/apt/lists'

# Bulk staleness (whole-repo sweep) page cache. Pages are fetched through the
# HTTP client, so they honour its TTL and its --refresh mode uniformly; there is
# no separate assembled-map cache (it bought nothing at a daily build cadence
# and would have sidestepped --refresh).
BULK_PAGE_NAMESPACE = 'repology-bulk-page'
BULK_TTL_SECONDS = 24 * 60 * 60
PROJECTS_PER_PAGE = 200
# Safety bound on the bulk sweep. debian_unstable is ~175 pages at 200/page;
# this caps an unbounded loop if a server response keeps the pager moving but
# never signals the end (the no-forward-progress check handles the stuck case).
BULK_MAX_PAGES = 1000


def sources_index_paths(lists_dir: str = APT_LISTS_DIR) -> list[str]:
    '''Return the apt deb-src ``Sources`` index files, sorted.'''
    return sorted(glob.glob(os.path.join(lists_dir, '*_Sources')))


def enumerate_archive(paths: list[str]) -> list[tuple[str, str, str | None]]:
    '''Parse deb-src ``Sources`` indices into ``[(source, version, format)]``.

    Uses ``debian.deb822.Sources`` rather than shelling out. A given source may
    appear more than once (multiple components or suites); callers that want one
    version per source use ``latest_versions``.
    '''
    items: list[tuple[str, str, str | None]] = []
    for path in paths:
        with open(path) as handle:
            for para in deb822.Sources.iter_paragraphs(handle):
                name = para.get('Package')
                version = para.get('Version')
                if not name or not version:
                    continue
                items.append((name, version, para.get('Format')))
    return items


def latest_versions(items: list[tuple[str, str, str | None]]) -> dict[str, tuple[str, str | None]]:
    '''Collapse enumerated sources to the newest ``(version, format)`` each.

    The bundle keys divergence by source package with a single version, so we
    crawl (and publish) only the highest version seen per source — which is also
    the politer choice.
    '''
    latest: dict[str, tuple[str, str | None]] = {}
    for name, version, fmt in items:
        current = latest.get(name)
        if current is None or debversion.compare(version, current[0]) > 0:
            latest[name] = (version, fmt)
    return latest


def _projects_url(repo: str, start: str | None) -> str:
    if start:
        base = '%s/api/v1/projects/%s/' % (REPOLOGY_BASE, urllib.parse.quote(start))
    else:
        base = '%s/api/v1/projects/' % REPOLOGY_BASE
    return '%s?inrepo=%s' % (base, urllib.parse.quote(repo))


def build_staleness_map(http_client: HttpClient, repo: str = 'debian_unstable',
                        page_size: int = PROJECTS_PER_PAGE,
                        max_pages: int = BULK_MAX_PAGES) -> dict[str, str]:
    '''Return ``{Debian srcname: newest version}`` for the whole repo.

    Page through the repo's project set once (cheap per-archive, not
    per-machine). Repology mandates <=1 req/s, so this is the slow half of a
    build, but it runs once and feeds every user's bundle. Each page is cached
    through ``http_client``, so a daily build re-fetches only expired pages and
    ``--refresh`` re-fetches all of them.
    '''
    mapping: dict[str, str] = {}
    start = None
    for _ in range(max_pages):
        page = http_client.get_json(
            _projects_url(repo, start),
            cache_namespace=BULK_PAGE_NAMESPACE,
            cache_key='%s:%s' % (repo, start or ''),
            ttl_seconds=BULK_TTL_SECONDS)
        if not isinstance(page, dict) or not page:
            break
        for entries in page.values():
            # External data is untrusted: a project value that is not a list of
            # entry dicts is skipped rather than crashing the whole sweep.
            if not isinstance(entries, list):
                continue
            newest = _select_newest(entries)
            if newest is None:
                continue
            for entry in entries:
                if isinstance(entry, dict) and entry.get('repo') == repo and entry.get('srcname'):
                    mapping[entry['srcname']] = newest
        if len(page) < page_size:
            break
        next_start = sorted(page)[-1]
        if next_start == start:  # safety: no forward progress
            break
        start = next_start
    return mapping


def require_deb_src() -> None:
    '''Raise ``RuntimeError`` if apt has no deb-src source indices configured.'''
    if not deb_src_available():
        raise RuntimeError(
            'deb-src source indices are not configured; enable deb-src and run '
            "'apt-get update' before building the cache.")
