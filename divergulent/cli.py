import argparse
import concurrent.futures
import json
import sys

from divergulent import __version__
from divergulent import inventory
from divergulent import score
from divergulent.cache import Cache, default_cache_dir
from divergulent.dep3 import PatchClass
from divergulent.http import HttpClient
from divergulent.progress import Progress
from divergulent.sources.apt_patches import AptSourcePatches
from divergulent.sources.debian_patches import DebianPatchesSource, DivergenceState, DivergenceSummary
from divergulent.sources.repology import RepologySource, StalenessState


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
    scorecmd.add_argument('--quiet', action='store_true', help='Suppress progress output.')

    showcmd = subparsers.add_parser(
        'show', help='Show per-patch detail (with Debian bug references) for one installed package.')
    showcmd.add_argument('package', help='A binary or source package name that is installed.')
    showcmd.add_argument('--json', action='store_true', help='Emit the detail as JSON.')

    return parser


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
    source = _repology()
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
    source = DebianPatchesSource(_http_client())
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
    repology = _repology()
    patches = DebianPatchesSource(_http_client())

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


def _render_show(source, version, staleness, package, drift):
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
        for patch in package.patches:
            lines.append('')
            lines.append('  %s  [%s]' % (patch.name, patch.patch_class.value))
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
                    'bugs': [{'tracker': b.tracker, 'ref': b.ref, 'url': _bug_link(b)} for b in p.bugs],
                }
                for p in package.patches],
        }, indent=2))
    else:
        print(_render_show(source_name, str(source_version), staleness, package, drift))
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

    parser.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
