import argparse
import sys

from divergulent import __version__


def _build_parser():
    parser = argparse.ArgumentParser(
        prog='divergulent',
        description='Measure how far a Debian machine has drifted from pure upstream.')
    parser.add_argument(
        '--version', action='version', version=f'divergulent {__version__}')

    subparsers = parser.add_subparsers(dest='command')

    inventory = subparsers.add_parser(
        'inventory', help='List installed packages and their source packages.')
    inventory.add_argument(
        '--json', action='store_true', help='Emit the inventory as JSON.')

    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == 'inventory':
        # Implemented in phase 1 (see docs/plans/PLAN-initial-phase-01-inventory.md).
        print('divergulent: inventory is not implemented yet', file=sys.stderr)
        return 1

    parser.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
