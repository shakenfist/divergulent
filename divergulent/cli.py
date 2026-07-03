import argparse
import concurrent.futures
import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from divergulent import __version__
from divergulent import builder
from divergulent import bundle
from divergulent import inventory
from divergulent import score
from divergulent import verify
from divergulent.classify import classification_bundle
from divergulent.cache import Cache, default_cache_dir
from divergulent.dep3 import PatchClass
from divergulent.http import HttpClient
from divergulent.progress import Progress
from divergulent.sources.apt_patches import AptSourcePatches
from divergulent.sources.bundle_backed import (
    BundleDivergenceSource, FallbackDivergence, FallbackStaleness)
from divergulent.sources.debian_patches import DebianPatchesSource, DivergenceState, DivergenceSummary
from divergulent.sources.repology import RepologyBulkSource, RepologySource, StalenessState


_CLASSIFY_UNAVAILABLE = (
    'divergulent: --classify needs deb-src source indices; enable deb-src and run '
    "'apt-get update'. Falling back to patch counts.")

# sources.debian.org has no documented rate limit (unlike Repology, which
# mandates <=1 req/s). We do not space its requests at all; concurrency
# (--workers) is the politeness bound instead, so the per-request interval is 0
# and at most DEFAULT_WORKERS requests are in flight at once.
SOURCES_DEBIAN_INTERVAL = 0.0

# Default number of concurrent fetch workers for the commands that query
# sources.debian.org. Kept moderate to stay a polite single client; Repology
# requests still self-limit to <=1 req/s via the per-host throttle regardless.
DEFAULT_WORKERS = 8

# Where `cache pull` fetches the bundle from when no --cache-url is given. The
# release codename is substituted in. The publisher (build-cache.yml) uploads
# the bundle to the rolling, in-place 'cache' prerelease, so this is a stable
# URL that is not the repository's software "latest" release. Override with
# --cache-url to use a mirror or a hand-hosted bundle.
DEFAULT_CACHE_URL_TEMPLATE = (
    'https://github.com/shakenfist/divergulent/releases/download/cache/cache-%s.json.gz')

# Where `cache pull-classification` fetches the classification bundle from. Like
# the divergence cache it is uploaded to a rolling, in-place prerelease tag
# ('classification') by its publisher (build-classification.yml), so this is a
# stable URL. Override with --cache-url.
DEFAULT_CLASSIFICATION_URL_TEMPLATE = (
    'https://github.com/shakenfist/divergulent/releases/download/'
    'classification/classification-%s.json.gz')

# How recent a stored bundle's staleness data must be to be trusted. Divergence
# is immutable and never expires; staleness ages, but a stale "newest" can only
# under-report BEHIND (newest versions only increase), never cry wolf, so the
# window is generous. Past it, staleness is queried live to catch packages that
# have since fallen behind.
BUNDLE_STALENESS_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


def _cache_and_client():
    cache = Cache(default_cache_dir())
    return cache, HttpClient(cache, host_intervals={'sources.debian.org': SOURCES_DEBIAN_INTERVAL})


def _http_client():
    return _cache_and_client()[1]


def _repology():
    '''A per-package Repology staleness source.

    Resolves each installed source via the project-by API (small, fast, and
    cached per package for ~24h). This fetches only what is installed; a
    whole-archive bulk sweep was tried (phase 2) and reverted as a cold-run
    regression -- see docs/plans/PLAN-faster-full-run-phase-04-revert-bulk.md.
    '''
    return RepologySource(_http_client())


def _usable_bundle(path):
    '''Load a local bundle if it is present, recognised, and for this release.

    Returns the Bundle, or None (so the command runs fully live) when the path
    is unset or missing, the file is unreadable, the envelope/entry schema is
    unrecognised, or the bundle describes a different Debian release. A warning
    is printed when a bundle is present but unusable, so the fall back to live is
    visible rather than silent.
    '''
    if not path:
        return None
    if not os.path.exists(path):
        print("divergulent: bundle '%s' not found; querying live." % path, file=sys.stderr)
        return None
    try:
        loaded = bundle.load(path)
    except (OSError, ValueError, KeyError):
        print("divergulent: bundle '%s' could not be read; querying live." % path, file=sys.stderr)
        return None
    if (loaded.schema, loaded.cache_schema) != (bundle.SCHEMA_VERSION, bundle.CACHE_SCHEMA_VERSION):
        print('divergulent: bundle schema not recognised; querying live.', file=sys.stderr)
        return None
    release = _detect_release()
    if release is not None and loaded.release != release:
        print(
            "divergulent: bundle is for '%s' but this system is '%s'; querying live." % (
                loaded.release, release),
            file=sys.stderr)
        return None
    return loaded


def _select_bundle(args):
    '''The bundle to use: an explicit --bundle, else the stored one if present.

    An explicit --bundle that is unusable warns (via _usable_bundle); an absent
    stored bundle is the normal pre-pull state and is silent.
    '''
    explicit = getattr(args, 'bundle', None)
    if explicit:
        return _usable_bundle(explicit)
    release = _detect_release()
    if release is None:
        return None
    stored = bundle.stored_path(default_cache_dir(), release)
    if not stored.exists():
        return None
    return _usable_bundle(str(stored))


def _staleness_fresh(loaded):
    '''True if a bundle's staleness is recent enough to trust (else query live).'''
    try:
        generated = datetime.datetime.fromisoformat(loaded.generated_at)
    except (TypeError, ValueError):
        return False
    try:
        age = (_utc_now() - generated).total_seconds()
    except TypeError:  # naive vs aware datetime: treat as stale, the safe choice
        return False
    return age <= BUNDLE_STALENESS_TTL_SECONDS


def _resolve_sources(args):
    '''Return (staleness_source, divergence_source), bundle-backed if available.

    With a usable bundle the sources answer covered packages from it and fall
    back to the live sources only for misses. Divergence is served from any
    bundle (immutable); staleness only while the bundle is fresh, else it is
    queried live. Without a bundle the sources are the live ones, as before.
    '''
    loaded = _select_bundle(args)
    staleness_live = _repology()
    divergence_live = DebianPatchesSource(_http_client())
    if loaded is None:
        return staleness_live, divergence_live

    divergence = FallbackDivergence(BundleDivergenceSource(loaded.divergence), divergence_live)
    if _staleness_fresh(loaded):
        staleness = FallbackStaleness(RepologyBulkSource(loaded.staleness), staleness_live)
    else:
        print(
            'divergulent: bundle staleness is older than the freshness window '
            '(built %s); querying Repology live for staleness.' % loaded.generated_at,
            file=sys.stderr)
        staleness = staleness_live
    return staleness, divergence


def _build_parser():
    parser = argparse.ArgumentParser(
        prog='divergulent',
        description='Measure how far a Debian machine has drifted from pure upstream.')
    parser.add_argument(
        '--version', action='version', version=f'divergulent {__version__}')

    subparsers = parser.add_subparsers(dest='command')

    inv = subparsers.add_parser(
        'inventory', help='List installed packages and their source packages.')
    inv.add_argument(
        '--json', action='store_true', help='Emit the inventory as JSON.')

    stale = subparsers.add_parser(
        'staleness', help='Report packages that are behind upstream (via Repology).')
    stale.add_argument(
        '--json', action='store_true', help='Emit the report as JSON.')
    stale.add_argument(
        '--all', action='store_true', dest='show_all',
        help='Include current and unknown packages, not just those behind.')
    stale.add_argument(
        '--bundle', default=None,
        help='Resolve staleness from a precomputed cache bundle (gzipped JSON), '
             'falling back to live Repology lookups for anything it does not cover.')
    stale.add_argument('--quiet', action='store_true', help='Suppress progress output.')

    diverge = subparsers.add_parser(
        'divergence',
        help='Report how many patches each package carries (via sources.debian.org). '
             'Use `show` for the per-patch classification.')
    diverge.add_argument(
        '--json', action='store_true', help='Emit the report as JSON.')
    diverge.add_argument(
        '--all', action='store_true', dest='show_all',
        help='Include packages carrying no patches (clean/native/unknown).')
    diverge.add_argument(
        '--limit', type=int, default=None,
        help='Process at most this many source packages (each is one or more network requests).')
    diverge.add_argument(
        '--classify', action='store_true',
        help='Classify patches (Debian-only/forwarded/unknown) by fetching source packages '
             'via apt. Needs deb-src indices.')
    diverge.add_argument(
        '--workers', type=int, default=DEFAULT_WORKERS,
        help='Concurrent requests to sources.debian.org (default %d; 1 = serial).' % DEFAULT_WORKERS)
    diverge.add_argument(
        '--bundle', default=None,
        help='Resolve divergence from a precomputed cache bundle (gzipped JSON), falling back to '
             'live sources.debian.org lookups for misses. Ignored with --classify.')
    diverge.add_argument('--quiet', action='store_true', help='Suppress progress output.')

    scorecmd = subparsers.add_parser(
        'score', help='Combine staleness and divergence into a ranked, whole-machine drift report.')
    scorecmd.add_argument(
        '--json', action='store_true', help='Emit the report as JSON.')
    scorecmd.add_argument(
        '--all', action='store_true', dest='show_all',
        help='Include packages with no detected drift (score 0).')
    scorecmd.add_argument(
        '--limit', type=int, default=None,
        help='Process at most this many source packages (this command queries both axes per package).')
    scorecmd.add_argument(
        '--classify', action='store_true',
        help='Classify carried patches (via apt source packages) and weight Debian-only patches. '
             'Needs deb-src indices.')
    scorecmd.add_argument(
        '--workers', type=int, default=DEFAULT_WORKERS,
        help='Concurrent requests to sources.debian.org (default %d; 1 = serial). '
             'Repology stays <=1 req/s regardless.' % DEFAULT_WORKERS)
    scorecmd.add_argument(
        '--bundle', default=None,
        help='Resolve both axes from a precomputed cache bundle (gzipped JSON) where it covers a '
             'package, falling back to live lookups for misses. Ignored with --classify.')
    scorecmd.add_argument('--quiet', action='store_true', help='Suppress progress output.')

    showcmd = subparsers.add_parser(
        'show', help='Show per-patch detail (with Debian bug references) for one installed package.')
    showcmd.add_argument('package', help='A binary or source package name that is installed.')
    showcmd.add_argument('--json', action='store_true', help='Emit the detail as JSON.')

    cachecmd = subparsers.add_parser(
        'cache', help='Build the precomputed cache bundle (central builder).')
    cachesub = cachecmd.add_subparsers(dest='cache_command')
    buildcmd = cachesub.add_parser(
        'build', help='Sweep the whole archive and write a gzipped staleness/divergence bundle.')
    buildcmd.add_argument('--output', required=True, help='Path to write the gzipped bundle to.')
    buildcmd.add_argument(
        '--release', default=None,
        help='Debian release codename the bundle describes (default: detect from /etc/os-release).')
    buildcmd.add_argument(
        '--workers', type=int, default=DEFAULT_WORKERS,
        help='Concurrent requests to sources.debian.org (default %d; 1 = serial). '
             'Repology stays <=1 req/s regardless.' % DEFAULT_WORKERS)
    buildcmd.add_argument(
        '--refresh', action='store_true',
        help='Ignore cached results and recompute from the origins (still repopulates the cache).')
    buildcmd.add_argument('--quiet', action='store_true', help='Suppress progress output.')

    pullcmd = cachesub.add_parser(
        'pull', help='Download, verify and store the precomputed cache bundle for this Debian release.')
    pullcmd.add_argument(
        '--cache-url', default=None,
        help='URL to download the bundle from (default: the GitHub Releases asset for this release).')
    _add_verify_arguments(pullcmd)

    pullclasscmd = cachesub.add_parser(
        'pull-classification',
        help='Download, verify and store the patch classification bundle for this Debian release.')
    pullclasscmd.add_argument(
        '--cache-url', default=None,
        help='URL to download the classification bundle from (default: the GitHub Releases asset).')
    _add_verify_arguments(pullclasscmd)

    verifycmd = cachesub.add_parser(
        'verify', help='Re-verify a stored or given cache bundle (signature and spot-check).')
    verifycmd.add_argument(
        '--bundle', default=None,
        help='Bundle to verify (default: the stored bundle for this release).')
    _add_verify_arguments(verifycmd)

    return parser


def _add_verify_arguments(parser):
    parser.add_argument(
        '--spot-check', type=int, default=verify.DEFAULT_SPOT_CHECK, metavar='N',
        help='Verify N random bundle entries against the live origin (default %d; 0 disables).'
             % verify.DEFAULT_SPOT_CHECK)
    parser.add_argument(
        '--require-signature', action='store_true',
        help='Fail if the Sigstore signature cannot be verified (needs the "verify" extra).')
    parser.add_argument(
        '--insecure', action='store_true',
        help='Skip all verification (signature and spot-check). Not recommended.')


def _table(headers, rows):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = '  '.join('{:<%d}' % w for w in widths)
    lines = [fmt.format(*headers).rstrip()]
    lines.extend(fmt.format(*row).rstrip() for row in rows)
    return '\n'.join(lines)


def _inventory_command(args):
    packages = sorted(
        inventory.list_installed(),
        key=lambda p: (p.source_name, p.binary_name, p.architecture))

    if args.json:
        data = [
            {
                'binary': p.binary_name,
                'binary_version': str(p.binary_version),
                'source': p.source_name,
                'source_version': str(p.source_version),
                'architecture': p.architecture,
            }
            for p in packages]
        print(json.dumps(data, indent=2))
    else:
        rows = [
            (p.binary_name, str(p.binary_version), p.source_name, str(p.source_version), p.architecture)
            for p in packages]
        print(_table(('BINARY', 'BINARY VERSION', 'SOURCE', 'SOURCE VERSION', 'ARCH'), rows))
    return 0


def _dedup_sources(packages):
    '''Collapse installed packages to one entry per source package.

    Many binary packages share a source; we query each source once.
    '''
    seen = {}
    for package in packages:
        seen.setdefault(package.source_name, package.source_version)
    return seen


def _concurrent_map(items, fn, workers, progress):
    '''Apply ``fn(name, version)`` over deduped sources, preserving input order.

    Results keep the order of ``items``; the progress reporter steps as each
    task completes. ``workers <= 1`` runs a plain serial loop (used by tests and
    ``--workers 1``). Concurrency only helps unthrottled hosts: a per-host
    throttle (e.g. Repology at <=1 req/s) still serialises that host's requests
    across workers.
    '''
    results = [None] * len(items)
    if workers <= 1:
        for index, (name, version) in enumerate(items):
            progress.step(name)
            results[index] = fn(name, version)
        progress.finish()
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fn, name, version): (index, name)
            for index, (name, version) in enumerate(items)}
        for future in concurrent.futures.as_completed(futures):
            index, name = futures[future]
            results[index] = future.result()
            progress.step(name)
    progress.finish()
    return results


def _gather_staleness(source, packages, progress_enabled=False):
    items = list(_dedup_sources(packages).items())
    progress = Progress(len(items), enabled=progress_enabled)
    results = []
    for name, version in items:
        progress.step(name)
        results.append(source.staleness(name, version))
    progress.finish()
    return results


_STATE_ORDER = {
    StalenessState.BEHIND: 0,
    StalenessState.UNKNOWN: 1,
    StalenessState.CURRENT: 2,
}


def _select(results, show_all):
    chosen = results if show_all else [r for r in results if r.state == StalenessState.BEHIND]
    return sorted(chosen, key=lambda r: (_STATE_ORDER[r.state], r.source_package))


def _summarise(results):
    counts = {state: 0 for state in StalenessState}
    for result in results:
        counts[result.state] += 1
    print(
        '%d source packages: %d behind, %d unknown, %d current' % (
            len(results),
            counts[StalenessState.BEHIND],
            counts[StalenessState.UNKNOWN],
            counts[StalenessState.CURRENT]),
        file=sys.stderr)


def _staleness_command(args):
    packages = inventory.list_installed()
    source, _ = _resolve_sources(args)
    results = _gather_staleness(source, packages, progress_enabled=not args.quiet)
    _summarise(results)

    selected = _select(results, args.show_all)
    if args.json:
        data = [
            {
                'source': r.source_package,
                'installed': str(r.installed_version),
                'newest': r.newest_version,
                'state': r.state.value,
            }
            for r in selected]
        print(json.dumps(data, indent=2))
    else:
        rows = [
            (r.source_package, str(r.installed_version), r.newest_version or '?', r.state.value)
            for r in selected]
        print(_table(('SOURCE', 'INSTALLED', 'NEWEST', 'STATE'), rows))
    return 0


def _gather_divergence(source, packages, limit=None, progress_enabled=False, workers=DEFAULT_WORKERS):
    items = list(_dedup_sources(packages).items())
    if limit is not None:
        items = items[:limit]
    progress = Progress(len(items), enabled=progress_enabled)
    return _concurrent_map(items, lambda name, version: source.summary(name, str(version)), workers, progress)


def _select_divergence(results, show_all):
    chosen = results if show_all else [r for r in results if r.total > 0]
    return sorted(chosen, key=lambda r: (-r.total, r.source_package))


def _summarise_divergence(results):
    patched = sum(1 for r in results if r.state == DivergenceState.PATCHED)
    total = sum(r.total for r in results)
    print(
        '%d source packages: %d carry patches, %d patches carried total' % (
            len(results), patched, total),
        file=sys.stderr)


def _divergence_classified(apt, packages, args):
    items = list(_dedup_sources(packages).items())
    if args.limit is not None:
        items = items[:args.limit]
    progress = Progress(len(items), enabled=not args.quiet)
    results = []
    for name, version in items:
        progress.step(name)
        package = apt.details(name, str(version))
        results.append((package, _patch_class_counts(package)))
    progress.finish()

    patched = sum(1 for p, _ in results if p.state == DivergenceState.PATCHED)
    total_debian_only = sum(counts[0] for _, counts in results)
    print(
        '%d source packages: %d carry patches, %d Debian-only patches total' % (
            len(results), patched, total_debian_only),
        file=sys.stderr)

    selected = results if args.show_all else [pc for pc in results if pc[1][0] > 0]
    selected = sorted(selected, key=lambda pc: (-pc[1][0], -len(pc[0].patches), pc[0].source_package))

    if args.json:
        data = [
            {
                'source': p.source_package,
                'version': p.version,
                'format': p.source_format,
                'total': len(p.patches),
                'debian_only': counts[0],
                'forwarded': counts[1],
                'unknown': counts[2],
                'state': p.state.value,
            }
            for p, counts in selected]
        print(json.dumps(data, indent=2))
    else:
        rows = [
            (p.source_package, p.version, str(len(p.patches)),
             str(counts[0]), str(counts[1]), str(counts[2]), p.state.value)
            for p, counts in selected]
        print(_table(
            ('SOURCE', 'VERSION', 'TOTAL', 'DEBIAN-ONLY', 'FORWARDED', 'UNKNOWN', 'STATE'), rows))
    return 0


def _divergence_command(args):
    packages = inventory.list_installed()
    if args.classify:
        apt = AptSourcePatches()
        if apt.available():
            return _divergence_classified(apt, packages, args)
        print(_CLASSIFY_UNAVAILABLE, file=sys.stderr)
    _, source = _resolve_sources(args)
    results = _gather_divergence(
        source, packages, limit=args.limit, progress_enabled=not args.quiet, workers=args.workers)
    _summarise_divergence(results)

    selected = _select_divergence(results, args.show_all)
    if args.json:
        data = [
            {
                'source': r.source_package,
                'version': r.version,
                'format': r.source_format,
                'total': r.total,
                'state': r.state.value,
            }
            for r in selected]
        print(json.dumps(data, indent=2))
    else:
        rows = [
            (r.source_package, r.version, str(r.total), r.state.value)
            for r in selected]
        print(_table(('SOURCE', 'VERSION', 'PATCHES', 'STATE'), rows))
    return 0


def _gather_score(repology, patches, packages, limit=None, progress_enabled=False, workers=DEFAULT_WORKERS):
    items = list(_dedup_sources(packages).items())
    if limit is not None:
        items = items[:limit]
    progress = Progress(len(items), enabled=progress_enabled)

    def assess(name, version):
        # Repology self-limits to <=1 req/s via the throttle, so concurrent
        # workers overlap the sources.debian.org fetch under that wait.
        staleness = repology.staleness(name, version)
        divergence = patches.summary(name, str(version))
        return score.combine(staleness, divergence)

    return _concurrent_map(items, assess, workers, progress)


def _select_score(drifts, show_all):
    chosen = drifts if show_all else [d for d in drifts if d.score > 0]
    return sorted(chosen, key=lambda d: (-d.score, -d.divergence.total, d.source_package))


def _summarise_score(drifts):
    behind = sum(1 for d in drifts if d.staleness.state == StalenessState.BEHIND)
    carrying = sum(1 for d in drifts if d.divergence.state == DivergenceState.PATCHED)
    both = sum(
        1 for d in drifts
        if d.staleness.state == StalenessState.BEHIND and d.divergence.state == DivergenceState.PATCHED)
    total_patches = sum(d.divergence.total for d in drifts)
    stale_unknown = sum(1 for d in drifts if d.staleness.state == StalenessState.UNKNOWN)
    diverge_unknown = sum(1 for d in drifts if d.divergence.state == DivergenceState.UNKNOWN)
    print(
        '%d source packages assessed: %d behind upstream, %d carry patches, '
        '%d both; %d patches carried total' % (
            len(drifts), behind, carrying, both, total_patches),
        file=sys.stderr)
    print(
        'could not assess: staleness for %d, divergence for %d' % (stale_unknown, diverge_unknown),
        file=sys.stderr)


def _score_classified(apt, packages, args):
    repology = _repology()
    items = list(_dedup_sources(packages).items())
    if args.limit is not None:
        items = items[:args.limit]

    progress = Progress(len(items), enabled=not args.quiet)
    rows = []
    for name, version in items:
        progress.step(name)
        staleness = repology.staleness(name, version)
        package = apt.details(name, str(version))
        rows.append((staleness, package, score.classified_score(staleness, package), _patch_class_counts(package)))
    progress.finish()

    behind = sum(1 for staleness, _, _, _ in rows if staleness.state == StalenessState.BEHIND)
    carrying = sum(1 for _, package, _, _ in rows if package.state == DivergenceState.PATCHED)
    total_debian_only = sum(counts[0] for _, _, _, counts in rows)
    print(
        '%d source packages assessed: %d behind upstream, %d carry patches, '
        '%d Debian-only patches total' % (len(rows), behind, carrying, total_debian_only),
        file=sys.stderr)

    selected = rows if args.show_all else [r for r in rows if r[2] > 0]
    selected = sorted(selected, key=lambda r: (-r[2], -r[3][0], r[1].source_package))

    if args.json:
        data = [
            {
                'source': package.source_package,
                'version': package.version,
                'score': drift_score,
                'staleness': staleness.state.value,
                'newest': staleness.newest_version,
                'debian_only': counts[0],
                'forwarded': counts[1],
                'unknown': counts[2],
                'total_patches': len(package.patches),
            }
            for staleness, package, drift_score, counts in selected]
        print(json.dumps(data, indent=2))
    else:
        out_rows = [
            (package.source_package, package.version, staleness.state.value, staleness.newest_version or '?',
             str(counts[0]), str(counts[1]), str(counts[2]), str(drift_score))
            for staleness, package, drift_score, counts in selected]
        print(_table(
            ('SOURCE', 'VERSION', 'STALENESS', 'NEWEST', 'DEB-ONLY', 'FORWARDED', 'UNKNOWN', 'SCORE'), out_rows))
    return 0


def _score_command(args):
    packages = inventory.list_installed()
    if args.classify:
        apt = AptSourcePatches()
        if apt.available():
            return _score_classified(apt, packages, args)
        print(_CLASSIFY_UNAVAILABLE, file=sys.stderr)
    repology, patches = _resolve_sources(args)

    drifts = _gather_score(
        repology, patches, packages, limit=args.limit, progress_enabled=not args.quiet, workers=args.workers)
    _summarise_score(drifts)

    selected = _select_score(drifts, args.show_all)
    if args.json:
        data = [
            {
                'source': d.source_package,
                'version': d.version,
                'score': d.score,
                'staleness': d.staleness.state.value,
                'newest': d.staleness.newest_version,
                'divergence': d.divergence.state.value,
                'total_patches': d.divergence.total,
            }
            for d in selected]
        print(json.dumps(data, indent=2))
    else:
        rows = [
            (d.source_package, d.version, d.staleness.state.value, d.staleness.newest_version or '?',
             str(d.divergence.total), str(d.score))
            for d in selected]
        print(_table(
            ('SOURCE', 'VERSION', 'STALENESS', 'NEWEST', 'PATCHES', 'SCORE'), rows))
    return 0


def build_bundle(http, paths, *, release, repology_repo, arch, generated_at,
                 workers=DEFAULT_WORKERS, progress_enabled=False):
    '''Assemble a precomputed bundle from a fixed set of deb-src indices.

    Pure orchestration with no clock, ``uname`` or apt detection inside:
    ``generated_at`` and the host facts are passed in, so a test can drive the
    whole build offline with a fake HTTP client. Enumerates the archive (1
    version per source, the newest), sweeps Repology for staleness once, and
    gathers a divergence ``summary()`` per source concurrently.
    '''
    enumerated = builder.enumerate_archive(paths)
    latest = builder.latest_versions(enumerated)
    staleness = builder.build_staleness_map(http, repo=repology_repo)

    patches = DebianPatchesSource(http)
    items = sorted(latest.items())
    div_items = [(name, version) for name, (version, _fmt) in items]
    progress = Progress(len(div_items), enabled=progress_enabled)
    summaries = _concurrent_map(
        div_items, lambda name, version: patches.summary(name, version), workers, progress)

    divergence = {
        name: {
            'version': version,
            'format': summary.source_format,
            'total': summary.total,
            'state': summary.state.value,
        }
        for (name, (version, _fmt)), summary in zip(items, summaries)}

    return bundle.Bundle(
        schema=bundle.SCHEMA_VERSION,
        cache_schema=bundle.CACHE_SCHEMA_VERSION,
        generated_at=generated_at,
        release=release,
        repology_repo=repology_repo,
        built_on={'arch': arch, 'release': release},
        staleness=staleness,
        divergence=divergence)


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _utc_now_iso():
    return _utc_now().isoformat()


def _detect_release():
    '''The Debian release codename from /etc/os-release, or None.'''
    try:
        with open('/etc/os-release') as handle:
            for line in handle:
                if line.startswith('VERSION_CODENAME='):
                    return line.split('=', 1)[1].strip().strip('"') or None
    except OSError:
        return None
    return None


def _detect_arch():
    '''The dpkg architecture (provenance only), or 'unknown'.'''
    try:
        result = subprocess.run(['dpkg', '--print-architecture'], capture_output=True, text=True)
    except OSError:
        return 'unknown'
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return 'unknown'


# Repology's published per-repo project set; "newest" is upstream-global, so the
# choice mainly affects srcname coverage. debian_unstable is a superset of the
# stable releases and is recorded in the bundle (repology_repo).
BUILDER_REPOLOGY_REPO = 'debian_unstable'


def _cache_build_command(args):
    try:
        builder.require_deb_src()
    except RuntimeError as exc:
        print('divergulent: %s' % exc, file=sys.stderr)
        return 1

    release = args.release or _detect_release()
    if release is None:
        print('divergulent: could not detect the Debian release; pass --release.', file=sys.stderr)
        return 1

    cache = Cache(default_cache_dir())
    http = HttpClient(
        cache, host_intervals={'sources.debian.org': SOURCES_DEBIAN_INTERVAL}, refresh=args.refresh)

    bundle_obj = build_bundle(
        http, builder.sources_index_paths(), release=release, repology_repo=BUILDER_REPOLOGY_REPO,
        arch=_detect_arch(), generated_at=_utc_now_iso(), workers=args.workers,
        progress_enabled=not args.quiet)
    bundle.write(bundle_obj, args.output)

    size = os.path.getsize(args.output)
    print(
        'divergulent: wrote %s (%d staleness, %d divergence entries, %d bytes gzipped)' % (
            args.output, len(bundle_obj.staleness), len(bundle_obj.divergence), size),
        file=sys.stderr)
    return 0


def _atomic_write_bytes(path, data):
    '''Write bytes to ``path`` via a unique temp file, then an atomic rename.'''
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.stem + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _verify_bundle(data, signature, loaded, args):
    '''Run signature and spot-check verification, printing notices.

    Returns True if the bundle may be trusted. A missing or unverifiable
    signature is fatal only under --require-signature; a spot-check mismatch is
    always fatal. --insecure skips everything (with a loud notice).
    '''
    if args.insecure:
        print('divergulent: --insecure: skipping signature and spot-check verification.', file=sys.stderr)
        return True

    ok = True

    if signature is None:
        print('divergulent: no signature found for the bundle.', file=sys.stderr)
        if args.require_signature:
            print('divergulent: --require-signature set; refusing.', file=sys.stderr)
            ok = False
    else:
        result = verify.verify_signature(data, signature)
        if result.status == verify.SignatureStatus.VERIFIED:
            print('divergulent: signature verified (%s).' % result.detail, file=sys.stderr)
        elif result.status == verify.SignatureStatus.SKIPPED:
            print('divergulent: signature not checked: %s' % result.detail, file=sys.stderr)
            if args.require_signature:
                print('divergulent: --require-signature set; refusing.', file=sys.stderr)
                ok = False
        else:
            print('divergulent: signature verification FAILED: %s' % result.detail, file=sys.stderr)
            ok = False

    if args.spot_check > 0:
        sc = verify.spot_check(loaded, DebianPatchesSource(_http_client()), sample=args.spot_check)
        if sc.status == verify.SpotCheckStatus.MISMATCH:
            print(
                'divergulent: spot-check FAILED: %d of %d checked entries disagree with live data:' % (
                    len(sc.mismatches), sc.checked),
                file=sys.stderr)
            for mismatch in sc.mismatches:
                print('  %s' % mismatch, file=sys.stderr)
            ok = False
        else:
            print(
                'divergulent: spot-check passed (%d checked, %d inconclusive).' % (
                    sc.checked, sc.inconclusive),
                file=sys.stderr)

    return ok


def _validate_bundle(data, release):
    '''Parse and validate downloaded bytes; return the Bundle or None (with a notice).'''
    try:
        loaded = bundle.loads(data)
    except (OSError, ValueError, KeyError):
        print('divergulent: bundle could not be read.', file=sys.stderr)
        return None
    if (loaded.schema, loaded.cache_schema) != (bundle.SCHEMA_VERSION, bundle.CACHE_SCHEMA_VERSION):
        print('divergulent: bundle schema not recognised.', file=sys.stderr)
        return None
    if release is not None and loaded.release != release:
        print(
            "divergulent: bundle is for '%s' but this system is '%s'." % (loaded.release, release),
            file=sys.stderr)
        return None
    return loaded


def _cache_pull_command(args):
    release = _detect_release()
    if release is None:
        print('divergulent: could not detect the Debian release; cannot choose a bundle.', file=sys.stderr)
        return 1

    url = args.cache_url or (DEFAULT_CACHE_URL_TEMPLATE % release)
    http = _http_client()
    data = http.get_bytes(url)
    if data is None:
        print('divergulent: could not download a bundle from %s' % url, file=sys.stderr)
        return 1

    loaded = _validate_bundle(data, release)
    if loaded is None:
        print('divergulent: not stored.', file=sys.stderr)
        return 1

    signature = http.get_bytes(url + verify.SIGNATURE_SUFFIX)
    if not _verify_bundle(data, signature, loaded, args):
        print('divergulent: verification failed; not stored.', file=sys.stderr)
        return 1

    path = bundle.stored_path(default_cache_dir(), release)
    _atomic_write_bytes(path, data)
    if signature is not None:
        _atomic_write_bytes(Path(str(path) + verify.SIGNATURE_SUFFIX), signature)
    print(
        'divergulent: stored %s (%d bytes, %d staleness, %d divergence entries, built %s)' % (
            path, len(data), len(loaded.staleness), len(loaded.divergence), loaded.generated_at),
        file=sys.stderr)
    return 0


def _cache_verify_command(args):
    release = _detect_release()
    if args.bundle:
        path = Path(args.bundle)
    elif release is not None:
        path = bundle.stored_path(default_cache_dir(), release)
    else:
        print('divergulent: could not detect the Debian release; pass --bundle.', file=sys.stderr)
        return 1

    if not path.exists():
        print("divergulent: bundle '%s' not found." % path, file=sys.stderr)
        return 1
    with open(path, 'rb') as handle:
        data = handle.read()

    loaded = _validate_bundle(data, release)
    if loaded is None:
        return 1

    signature_path = Path(str(path) + verify.SIGNATURE_SUFFIX)
    signature = None
    if signature_path.exists():
        with open(signature_path, 'rb') as handle:
            signature = handle.read()

    if _verify_bundle(data, signature, loaded, args):
        print('divergulent: %s verified.' % path, file=sys.stderr)
        return 0
    print('divergulent: %s failed verification.' % path, file=sys.stderr)
    return 1


def _usable_classification(release):
    '''Load the stored classification bundle for ``release``, or None.

    Returns the ClassificationBundle when one is stored, readable, and of a
    recognised schema for this release; otherwise None (so ``show`` simply omits
    the classification, never guesses). A present-but-unusable bundle warns.
    '''
    if release is None:
        return None
    path = classification_bundle.stored_path(default_cache_dir(), release)
    if not path.exists():
        return None
    try:
        loaded = classification_bundle.load(path)
    except (OSError, ValueError, KeyError):
        print('divergulent: classification bundle could not be read; omitting.', file=sys.stderr)
        return None
    if (loaded.schema, loaded.entry_schema) != (
            classification_bundle.CLASSIFICATION_SCHEMA_VERSION,
            classification_bundle.ENTRY_SCHEMA_VERSION):
        print('divergulent: classification bundle schema not recognised; omitting.', file=sys.stderr)
        return None
    return loaded


def _validate_classification(data):
    '''Parse downloaded bytes into a ClassificationBundle, or None (with a notice).'''
    try:
        loaded = classification_bundle.loads(data)
    except (OSError, ValueError, KeyError):
        print('divergulent: classification bundle could not be read.', file=sys.stderr)
        return None
    if (loaded.schema, loaded.entry_schema) != (
            classification_bundle.CLASSIFICATION_SCHEMA_VERSION,
            classification_bundle.ENTRY_SCHEMA_VERSION):
        print('divergulent: classification bundle schema not recognised.', file=sys.stderr)
        return None
    return loaded


def _cache_pull_classification_command(args):
    release = _detect_release()
    if release is None:
        print('divergulent: could not detect the Debian release; cannot choose a bundle.', file=sys.stderr)
        return 1

    url = args.cache_url or (DEFAULT_CLASSIFICATION_URL_TEMPLATE % release)
    http = _http_client()
    data = http.get_bytes(url)
    if data is None:
        print('divergulent: could not download a classification bundle from %s' % url, file=sys.stderr)
        return 1

    loaded = _validate_classification(data)
    if loaded is None:
        print('divergulent: not stored.', file=sys.stderr)
        return 1

    # The classification bundle is signed exactly like the divergence cache, but by
    # its own workflow identity; a spot-check does not apply (there is no live
    # oracle for a verdict), so we verify the signature only.
    signature = http.get_bytes(url + verify.SIGNATURE_SUFFIX)
    if not _verify_signature_only(data, signature, args,
                                  identity=verify.CLASSIFICATION_SIGNER_IDENTITY):
        print('divergulent: verification failed; not stored.', file=sys.stderr)
        return 1

    path = classification_bundle.stored_path(default_cache_dir(), release)
    _atomic_write_bytes(path, data)
    if signature is not None:
        _atomic_write_bytes(Path(str(path) + verify.SIGNATURE_SUFFIX), signature)
    print(
        'divergulent: stored %s (%d bytes, %d verdicts, built %s)' % (
            path, len(data), len(loaded.verdicts), loaded.generated_at),
        file=sys.stderr)
    return 0


def _verify_signature_only(data, signature, args, *, identity):
    '''Signature-only verification for a bundle with no live spot-check oracle.

    Mirrors _verify_bundle's signature half: --insecure skips; a missing or
    unverifiable signature is fatal only under --require-signature; a positive
    verification failure is always fatal.
    '''
    if args.insecure:
        print('divergulent: --insecure: skipping signature verification.', file=sys.stderr)
        return True
    if signature is None:
        print('divergulent: no signature found for the bundle.', file=sys.stderr)
        return not args.require_signature
    result = verify.verify_signature(data, signature, identity=identity)
    if result.status == verify.SignatureStatus.VERIFIED:
        print('divergulent: signature verified (%s).' % result.detail, file=sys.stderr)
        return True
    if result.status == verify.SignatureStatus.SKIPPED:
        print('divergulent: signature not checked: %s' % result.detail, file=sys.stderr)
        return not args.require_signature
    print('divergulent: signature verification FAILED: %s' % result.detail, file=sys.stderr)
    return False


def _cache_command(args):
    if args.cache_command == 'build':
        return _cache_build_command(args)
    if args.cache_command == 'pull':
        return _cache_pull_command(args)
    if args.cache_command == 'pull-classification':
        return _cache_pull_classification_command(args)
    if args.cache_command == 'verify':
        return _cache_verify_command(args)
    print("divergulent: 'cache' needs a subcommand (build, pull, pull-classification, verify)",
          file=sys.stderr)
    return 1


def _resolve_package(name, packages):
    by_binary = {}
    by_source = {}
    for package in packages:
        by_binary.setdefault(package.binary_name, (package.source_name, package.source_version))
        by_source.setdefault(package.source_name, (package.source_name, package.source_version))
    if name in by_binary:
        return by_binary[name]
    if name in by_source:
        return by_source[name]
    return None


def _bug_link(bug):
    ref = bug.ref.strip()
    if ref.startswith('http://') or ref.startswith('https://'):
        return ref
    if bug.tracker == 'debian':
        number = ref.lstrip('#')
        if number.isdigit():
            return 'https://bugs.debian.org/%s' % number
    return ref


_PATCH_STATE_NOTE = {
    DivergenceState.NATIVE: 'native package (no upstream/Debian split)',
    DivergenceState.CLEAN: 'no carried patches',
    DivergenceState.UNKNOWN: 'could not assess patches',
}


def _patch_class_counts(package):
    return (
        sum(1 for p in package.patches if p.patch_class == PatchClass.DEBIAN_ONLY),
        sum(1 for p in package.patches if p.patch_class == PatchClass.FORWARDED),
        sum(1 for p in package.patches if p.patch_class == PatchClass.UNKNOWN))


def _classification_breakdown(package, verdicts):
    '''``[(category, count), ...]`` over the package's fingerprinted patches.

    Counts only patches whose fingerprint is present in the bundle; ordered most
    common first, category name as the tiebreak so the line is deterministic.
    '''
    from collections import Counter
    counts = Counter(
        verdicts[p.fingerprint]['category']
        for p in package.patches
        if p.fingerprint and p.fingerprint in verdicts)
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _verdict_note(entry):
    '''One-line "why" for a patch's verdict: category, axes, and the reason.'''
    axes = []
    for key in ('risk', 'reach', 'reviewability'):
        if entry.get(key):
            axes.append('%s %s' % (key, entry[key]))
    axis_str = ' (%s)' % ', '.join(axes) if axes else ''
    return 'class: %s%s -- %s' % (entry['category'], axis_str, entry.get('reason', ''))


def _render_show(source, version, staleness, package, drift, classification=None):
    verdicts = classification.verdicts if classification else {}
    newest = ' -> newest %s' % staleness.newest_version if staleness.newest_version else ''
    lines = [
        '%s %s' % (source, version),
        '  staleness: %s%s' % (staleness.state.value, newest),
        '  drift score: %d' % drift.score,
    ]
    if package.patches:
        debian_only, forwarded, unknown = _patch_class_counts(package)
        lines.append('  patches: %d total (%d Debian-only, %d forwarded, %d unknown)' % (
            len(package.patches), debian_only, forwarded, unknown))
        if verdicts:
            breakdown = _classification_breakdown(package, verdicts)
            if breakdown:
                lines.append('  classification: %s (bundle built %s)' % (
                    ', '.join('%d %s' % (count, category) for category, count in breakdown),
                    classification.generated_at))
        for patch in package.patches:
            lines.append('')
            lines.append('  %s  [%s]' % (patch.name, patch.patch_class.value))
            entry = verdicts.get(patch.fingerprint) if patch.fingerprint else None
            if entry:
                lines.append('      %s' % _verdict_note(entry))
            if patch.description:
                lines.append('      %s' % patch.description)
            if patch.bugs:
                for bug in patch.bugs:
                    lines.append('      bug (%s): %s' % (bug.tracker, _bug_link(bug)))
            else:
                lines.append('      bug: none declared')
    else:
        lines.append('  patches: %s' % _PATCH_STATE_NOTE.get(package.state, 'none'))
    return '\n'.join(lines)


def _show_command(args):
    resolved = _resolve_package(args.package, inventory.list_installed())
    if resolved is None:
        print("divergulent: '%s' is not an installed package" % args.package, file=sys.stderr)
        return 1
    source_name, source_version = resolved

    http = _http_client()
    staleness = RepologySource(http).staleness(source_name, source_version)
    patches = DebianPatchesSource(http)
    package = patches.details(source_name, str(source_version))
    summary = DivergenceSummary(
        source_name, str(source_version), package.source_format, len(package.patches), package.state)
    drift = score.combine(staleness, summary)

    # The classification is an optional, locally-stored bundle (pulled via
    # `cache pull-classification`); absent, `show` behaves exactly as before.
    classification = _usable_classification(_detect_release())
    verdicts = classification.verdicts if classification else {}

    if args.json:
        debian_only, forwarded, unknown = _patch_class_counts(package)
        print(json.dumps({
            'source': source_name,
            'version': str(source_version),
            'score': drift.score,
            'staleness': {'state': staleness.state.value, 'newest': staleness.newest_version},
            'divergence': {
                'state': package.state.value,
                'total': len(package.patches),
                'debian_only': debian_only,
                'forwarded': forwarded,
                'unknown': unknown,
            },
            'patches': [
                {
                    'name': p.name,
                    'class': p.patch_class.value,
                    'description': p.description,
                    'forwarded': p.forwarded,
                    'fingerprint': p.fingerprint,
                    'classification': verdicts.get(p.fingerprint) if p.fingerprint else None,
                    'bugs': [{'tracker': b.tracker, 'ref': b.ref, 'url': _bug_link(b)} for b in p.bugs],
                }
                for p in package.patches],
        }, indent=2))
    else:
        print(_render_show(source_name, str(source_version), staleness, package, drift, classification))
    return 0


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == 'inventory':
        return _inventory_command(args)
    if args.command == 'staleness':
        return _staleness_command(args)
    if args.command == 'divergence':
        return _divergence_command(args)
    if args.command == 'score':
        return _score_command(args)
    if args.command == 'show':
        return _show_command(args)
    if args.command == 'cache':
        return _cache_command(args)

    parser.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
