import argparse
import json
import sys

from divergulent import __version__
from divergulent import inventory
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


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == 'inventory':
        return _inventory_command(args)
    if args.command == 'staleness':
        return _staleness_command(args)
    if args.command == 'divergence':
        return _divergence_command(args)

    parser.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
