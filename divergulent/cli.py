import argparse
import json
import sys

from divergulent import __version__
from divergulent import inventory
from divergulent import score
from divergulent.cache import Cache, default_cache_dir
from divergulent.http import HttpClient
from divergulent.sources.debian_patches import DebianPatchesSource, DivergenceState
from divergulent.sources.repology import RepologySource, StalenessState


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

    diverge = subparsers.add_parser(
        'divergence', help='Report packages carrying Debian-only patches (via sources.debian.org).')
    diverge.add_argument(
        '--json', action='store_true', help='Emit the report as JSON.')
    diverge.add_argument(
        '--all', action='store_true', dest='show_all',
        help='Include packages with no Debian-only patches (clean/native/unknown).')
    diverge.add_argument(
        '--limit', type=int, default=None,
        help='Process at most this many source packages (each is one or more network requests).')

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


def _gather_staleness(source, packages):
    return [source.staleness(name, version) for name, version in _dedup_sources(packages).items()]


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
    source = RepologySource(HttpClient(Cache(default_cache_dir())))
    results = _gather_staleness(source, packages)
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


def _gather_divergence(source, packages, limit=None):
    items = list(_dedup_sources(packages).items())
    if limit is not None:
        items = items[:limit]
    return [source.divergence(name, str(version)) for name, version in items]


def _select_divergence(results, show_all):
    chosen = results if show_all else [r for r in results if r.debian_only > 0]
    return sorted(chosen, key=lambda r: (-r.debian_only, -r.total, r.source_package))


def _summarise_divergence(results):
    patched = sum(1 for r in results if r.state == DivergenceState.PATCHED)
    debian_only = sum(r.debian_only for r in results)
    print(
        '%d source packages: %d carry patches, %d Debian-only patches total' % (
            len(results), patched, debian_only),
        file=sys.stderr)


def _divergence_command(args):
    packages = inventory.list_installed()
    source = DebianPatchesSource(HttpClient(Cache(default_cache_dir())))
    results = _gather_divergence(source, packages, limit=args.limit)
    _summarise_divergence(results)

    selected = _select_divergence(results, args.show_all)
    if args.json:
        data = [
            {
                'source': r.source_package,
                'version': r.version,
                'format': r.source_format,
                'total': r.total,
                'debian_only': r.debian_only,
                'forwarded': r.forwarded,
                'unknown': r.unknown,
                'state': r.state.value,
            }
            for r in selected]
        print(json.dumps(data, indent=2))
    else:
        rows = [
            (r.source_package, r.version, str(r.total), str(r.debian_only),
             str(r.forwarded), str(r.unknown), r.state.value)
            for r in selected]
        print(_table(
            ('SOURCE', 'VERSION', 'TOTAL', 'DEBIAN-ONLY', 'FORWARDED', 'UNKNOWN', 'STATE'), rows))
    return 0


def _gather_score(repology, patches, packages, limit=None):
    items = list(_dedup_sources(packages).items())
    if limit is not None:
        items = items[:limit]
    drifts = []
    for name, version in items:
        staleness = repology.staleness(name, version)
        divergence = patches.divergence(name, str(version))
        drifts.append(score.combine(staleness, divergence))
    return drifts


def _select_score(drifts, show_all):
    chosen = drifts if show_all else [d for d in drifts if d.score > 0]
    return sorted(chosen, key=lambda d: (-d.score, -d.divergence.debian_only, d.source_package))


def _summarise_score(drifts):
    behind = sum(1 for d in drifts if d.staleness.state == StalenessState.BEHIND)
    carrying = sum(1 for d in drifts if d.divergence.debian_only > 0)
    both = sum(
        1 for d in drifts
        if d.staleness.state == StalenessState.BEHIND and d.divergence.debian_only > 0)
    total_debian_only = sum(d.divergence.debian_only for d in drifts)
    stale_unknown = sum(1 for d in drifts if d.staleness.state == StalenessState.UNKNOWN)
    diverge_unknown = sum(1 for d in drifts if d.divergence.state == DivergenceState.UNKNOWN)
    print(
        '%d source packages assessed: %d behind upstream, %d carry Debian-only patches, '
        '%d both; %d Debian-only patches total' % (
            len(drifts), behind, carrying, both, total_debian_only),
        file=sys.stderr)
    print(
        'could not assess: staleness for %d, divergence for %d' % (stale_unknown, diverge_unknown),
        file=sys.stderr)


def _score_command(args):
    packages = inventory.list_installed()
    http = HttpClient(Cache(default_cache_dir()))
    repology = RepologySource(http)
    patches = DebianPatchesSource(http)

    drifts = _gather_score(repology, patches, packages, limit=args.limit)
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
                'debian_only': d.divergence.debian_only,
                'forwarded': d.divergence.forwarded,
                'unknown_patches': d.divergence.unknown,
                'total_patches': d.divergence.total,
            }
            for d in selected]
        print(json.dumps(data, indent=2))
    else:
        rows = [
            (d.source_package, d.version, d.staleness.state.value, d.staleness.newest_version or '?',
             str(d.divergence.debian_only), str(d.divergence.total), str(d.score))
            for d in selected]
        print(_table(
            ('SOURCE', 'VERSION', 'STALENESS', 'NEWEST', 'DEB-ONLY', 'PATCHES', 'SCORE'), rows))
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


def _render_show(source, version, staleness, package, divergence, drift):
    newest = ' -> newest %s' % staleness.newest_version if staleness.newest_version else ''
    lines = [
        '%s %s' % (source, version),
        '  staleness: %s%s' % (staleness.state.value, newest),
        '  drift score: %d' % drift.score,
    ]
    if package.patches:
        lines.append('  patches: %d total (%d Debian-only, %d forwarded, %d unknown)' % (
            divergence.total, divergence.debian_only, divergence.forwarded, divergence.unknown))
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

    http = HttpClient(Cache(default_cache_dir()))
    staleness = RepologySource(http).staleness(source_name, source_version)
    patches = DebianPatchesSource(http)
    package = patches.details(source_name, str(source_version))
    divergence = patches.divergence(source_name, str(source_version))
    drift = score.combine(staleness, divergence)

    if args.json:
        print(json.dumps({
            'source': source_name,
            'version': str(source_version),
            'score': drift.score,
            'staleness': {'state': staleness.state.value, 'newest': staleness.newest_version},
            'divergence': {
                'state': package.state.value,
                'total': divergence.total,
                'debian_only': divergence.debian_only,
                'forwarded': divergence.forwarded,
                'unknown': divergence.unknown,
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
        print(_render_show(source_name, str(source_version), staleness, package, divergence, drift))
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
