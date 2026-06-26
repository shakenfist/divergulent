"""``divergulent-classify`` -- one dispatcher for the curation-side commands.

Each curation command (``triage``, ``risk``, ``review``, ``web``, ``report``, ...)
keeps its own tested ``main`` in its own module; this is a thin shim that resolves
the data root (:mod:`divergulent.classify.workspace`) and forwards to that ``main``
with the ledger/corpus paths spliced in -- so the operator types a verb and its
own flags, never the paths. ``status`` and the cache guardrail (added alongside)
are the orientation/protection layer.

The old ``python -m divergulent.classify.<x>`` forms keep working; this is the
friendlier front, not a replacement.

Curation-side only: no client command imports ``classify/``.
"""
from __future__ import annotations

import argparse
import os
import sys

from divergulent.classify import workspace

# Verbs that forward to an existing module ``main``, and how to splice the
# resolved paths into that main's argv (everything the operator typed after the
# verb is appended verbatim as ``rest``).
_FORWARDING_VERBS = ('triage', 'risk', 'review', 'requeue', 'history', 'web', 'report')

# Verbs that need a built ledger to do anything; checked up front so the operator
# gets one clear message instead of a deeper failure.
_NEEDS_LEDGER = _FORWARDING_VERBS

_ALL_VERBS = (*_FORWARDING_VERBS, 'init')


def _forward(verb: str, ws: workspace.Workspace, rest: list[str]) -> int:
    ledger, corpus = str(ws.ledger), str(ws.corpus_dir)
    if verb == 'triage':
        from divergulent.classify import triage
        return triage.main([ledger, corpus, *rest])
    if verb == 'risk':
        from divergulent.classify import risk
        return risk.main([ledger, corpus, *rest])
    if verb == 'review':
        from divergulent.classify import review
        return review.main(['review', ledger, corpus, *rest])
    if verb == 'requeue':
        from divergulent.classify import review
        return review.main(['requeue', ledger, *rest])
    if verb == 'history':
        from divergulent.classify import review
        return review.main(['history', ledger, *rest])
    if verb == 'web':
        from divergulent.classify import review_web
        return review_web.main(['--ledger', ledger, '--corpus', corpus, *rest])
    if verb == 'report':
        from divergulent.classify import ledger as ledger_mod
        return ledger_mod.main(['report', ledger, *rest])
    raise AssertionError('unhandled verb %r' % verb)  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    """Resolve the data root and dispatch a curation verb to its module."""
    parser = argparse.ArgumentParser(
        prog='divergulent-classify',
        description='Curation-side commands for the divergulent patch classifier. '
                    'Run from inside a data root (a directory with a .divergulent '
                    'marker holding corpus/ and cache/); the ledger and corpus paths '
                    'are discovered, never typed.')
    parser.add_argument('--data', default=None, metavar='ROOT',
                        help='data root (default: discovered via DIVERGULENT_DATA or the cwd)')
    parser.add_argument('--no-pull', action='store_true',
                        help='do not auto-pull a stale published cache before running')
    parser.add_argument('verb', choices=sorted(_ALL_VERBS), help='what to do')
    parser.add_argument('rest', nargs=argparse.REMAINDER,
                        help='arguments forwarded to the verb (e.g. --limit 50)')
    args = parser.parse_args(argv)

    if args.verb == 'init':
        target = args.rest[0] if args.rest else (args.data or os.getcwd())
        ws = workspace.init(target)
        print('initialised divergulent data root at %s' % ws.root)
        print('  corpus -> %s   cache -> %s' % (ws.corpus_dir, ws.cache_dir))
        return 0

    try:
        ws = workspace.find(args.data)
    except workspace.WorkspaceNotFound as exc:
        print('error: %s' % exc, file=sys.stderr)
        return 2

    if args.verb in _NEEDS_LEDGER and not ws.ledger_exists():
        print('error: no ledger at %s\n'
              '  build the corpus + ledger first (classify.corpus / classify.measure / '
              'classify.ledger build), then re-run.' % ws.ledger, file=sys.stderr)
        return 2

    return _forward(args.verb, ws, args.rest)


if __name__ == '__main__':
    raise SystemExit(main())
