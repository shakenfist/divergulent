import argparse
import json
import sys

from divergulent import __version__
from divergulent import inventory


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

    return parser


def _format_table(packages):
    headers = ('BINARY', 'BINARY VERSION', 'SOURCE', 'SOURCE VERSION', 'ARCH')
    rows = [
        (p.binary_name, str(p.binary_version), p.source_name, str(p.source_version), p.architecture)
        for p in packages]
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
        print(_format_table(packages))
    return 0


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == 'inventory':
        return _inventory_command(args)

    parser.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
