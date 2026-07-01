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
_FORWARDING_VERBS = ('record', 'triage', 'risk', 'review', 'requeue', 'history', 'web',
                     'report', 'export', 'bundle')

# Verbs that forward but operate on the CORPUS only (no ledger needed) -- e.g.
# pulling a popcon snapshot for the reach axis, which writes corpus/popcon.sqlite.
_CORPUS_VERBS = ('popcon',)

# Verbs that CREATE the ledger, so must not require one to already exist -- e.g.
# rebuilding it from a committed JSONL export on a fresh CI checkout.
_LEDGER_WRITING_VERBS = ('import',)

# Verbs that need a built ledger to do anything; checked up front so the operator
# gets one clear message instead of a deeper failure.
_NEEDS_LEDGER = (*_FORWARDING_VERBS, 'status')

_ALL_VERBS = (*_FORWARDING_VERBS, *_CORPUS_VERBS, *_LEDGER_WRITING_VERBS, 'status', 'init')

# A stored published bundle older than this nags the operator to re-pull it.
CACHE_STALE_DAYS = 14


def _age_days(generated_at: str, *, now=None) -> int | None:
    """Whole days between ``generated_at`` (ISO-8601) and ``now``, or ``None``."""
    import datetime
    try:
        stamp = datetime.datetime.fromisoformat(generated_at.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (now - stamp).days


def _cache_report(*, now=None) -> tuple[str, bool]:
    """A one-line cache-freshness summary and a ``stale`` flag; best-effort.

    Looks at the stored published bundle for this Debian release (where the client
    keeps it) and reports its age. Never raises -- a command must not fail because
    the cache could not be inspected; an un-inspectable cache reports as such.
    """
    try:
        from divergulent import bundle, cli as client_cli
        from divergulent.cache import default_cache_dir
        from pathlib import Path

        release = client_cli._detect_release()
        if not release:
            return ('cache: (Debian release not detected)', False)
        path = bundle.stored_path(default_cache_dir(), release)
        if not Path(path).exists():
            return ('cache: ⚠ no bundle stored for %s -- run `divergulent cache pull`'
                    % release, True)
        age = _age_days(bundle.load(path).generated_at, now=now)
        stale = age is not None and age > CACHE_STALE_DAYS
        age_str = '%d days old' % age if age is not None else 'age unknown'
        flag = '  ⚠ STALE -- run `divergulent cache pull`' if stale else ''
        return ('cache: %s bundle (%s)%s' % (release, age_str, flag), stale)
    except Exception as exc:  # noqa: BLE001 -- best-effort; never break a command
        return ('cache: (unavailable: %s)' % exc, False)


def _record_due_reasons(conn, verdicts) -> list[str]:
    """Why a ``record`` run is due, or ``[]`` if the ledger reflects current rules.

    Two signals that the live ledger no longer matches what the deterministic pass
    would now produce: a COVERAGE gap (a fingerprint with a verdict but no live
    reviewability observation -- e.g. a ledger built before the size axis existed),
    and rule-VERSION drift (the code's registry carries a rule id/version the
    ledger has not registered -- a bumped or newly-added rule since the last
    build/record). Either is the forgetful operator's cue to re-run ``record``.
    """
    from divergulent.classify import ledger as ledger_mod
    from divergulent.classify import reviewability as reviewability_mod

    reasons: list[str] = []
    review_levels = reviewability_mod.reviewability_by_fingerprint(conn)
    missing = sum(1 for fingerprint in verdicts if fingerprint not in review_levels)
    if missing:
        reasons.append('%d fingerprints have no size tier (reviewability not recorded)' % missing)

    registered = {(r['rule_id'], r['version']) for r in ledger_mod.registered_rules(conn)}
    current = {(rule.rule_id, rule.version) for rule in ledger_mod.default_registry()}
    drifted = sorted(rid for (rid, ver) in current if (rid, ver) not in registered)
    if drifted:
        reasons.append('rules changed since the last record: %s' % ', '.join(drifted))
    return reasons


def _status(ws: workspace.Workspace) -> int:
    """Print a one-screen orientation for the data root before a session."""
    from collections import Counter

    from divergulent.classify import ledger as ledger_mod
    from divergulent.classify import risk as risk_mod
    from divergulent.classify import verdict as verdict_mod

    conn = ledger_mod.open_ledger(str(ws.ledger))
    try:
        verdicts = verdict_mod.current_verdict(conn)
        residue = set(verdict_mod.queue(conn))
        by_category = Counter(v.category for v in verdicts.values())
        ranks = risk_mod.risk_rank_by_fingerprint(conn)
        pending = len(ledger_mod.pending_review_items(conn))
        record_due = _record_due_reasons(conn, verdicts)
    finally:
        conn.close()

    print('data root: %s' % ws.root)
    print('residue (un-settled fingerprints): %d' % len(residue))
    print('verdicts by category:')
    for category, count in by_category.most_common():
        print('  %-14s %d' % (category, count))
    print('security-risk scored: %d' % len(ranks))
    for level in reversed(risk_mod.RISK_LEVELS):
        count = sum(1 for rank in ranks.values() if rank == risk_mod.RISK_RANK[level])
        if count:
            print('  %-9s %d' % (level, count))
    hot = sum(1 for fp, rank in ranks.items()
              if rank >= risk_mod.RISK_RANK['elevated'] and fp in residue)
    print('elevated+ still in the residue (review these first): %d' % hot)
    print('pending human review: %d' % pending)
    if record_due:
        print()
        print('a `record` run is due (the ledger is behind the current rules):')
        for reason in record_due:
            print('  - %s' % reason)
        print('  run: divergulent-classify record')
    print(_cache_report()[0])
    return 0


def _forward(verb: str, ws: workspace.Workspace, rest: list[str]) -> int:
    ledger, corpus = str(ws.ledger), str(ws.corpus_dir)
    if verb == 'record':
        # Re-apply the current deterministic rules to the existing ledger
        # (non-destructive: preserves llm/human decisions). The recurring
        # "I changed a rule, re-apply it" pass -- e.g. backfilling a new
        # observation like reviewability onto an already-built ledger.
        from divergulent.classify import ledger as ledger_mod
        return ledger_mod.main(['record', ledger, corpus, *rest])
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
    if verb == 'popcon':
        # Pull + pin a Debian popcon snapshot into corpus/popcon.sqlite (the reach
        # axis input). Corpus-only: no ledger required.
        from divergulent.classify import popcon
        return popcon.main([corpus, *rest])
    if verb == 'export':
        # Serialise the ledger to canonical JSONL -- the committed source of truth
        # CI publishes from. Default output is the ledger with a .jsonl suffix.
        from divergulent.classify import export
        return export.main(['export', ledger, *rest])
    if verb == 'import':
        # Rebuild the ledger sqlite from a committed JSONL export (the CI side).
        from divergulent.classify import export
        return export.main(['import', *rest, '--ledger', ledger])
    if verb == 'bundle':
        # Build the lean, publishable classification bundle from the ledger.
        from divergulent.classify import classification_bundle
        return classification_bundle.main([ledger, *rest])
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

    # ``--data``/``--no-pull`` are global, but REMAINDER swallows them if they
    # follow the verb; recover them so position does not matter (a forgetful-
    # operator smoothing). Anything else stays as the forwarded verb args.
    data, no_pull, rest = args.data, args.no_pull, []
    index = 0
    while index < len(args.rest):
        token = args.rest[index]
        if token == '--data' and index + 1 < len(args.rest):
            data, index = args.rest[index + 1], index + 2
        elif token.startswith('--data='):
            data, index = token.split('=', 1)[1], index + 1
        elif token == '--no-pull':
            no_pull, index = True, index + 1
        else:
            rest.append(token)
            index += 1

    if args.verb == 'init':
        target = rest[0] if rest else (data or os.getcwd())
        ws = workspace.init(target)
        print('initialised divergulent data root at %s' % ws.root)
        print('  corpus -> %s   cache -> %s' % (ws.corpus_dir, ws.cache_dir))
        return 0

    try:
        ws = workspace.find(data)
    except workspace.WorkspaceNotFound as exc:
        print('error: %s' % exc, file=sys.stderr)
        return 2

    if args.verb in _NEEDS_LEDGER and not ws.ledger_exists():
        print('error: no ledger at %s\n'
              '  build the corpus + ledger first (classify.corpus / classify.measure / '
              'classify.ledger build), then re-run.' % ws.ledger, file=sys.stderr)
        return 2

    if args.verb == 'status':
        return _status(ws)

    # Guardrail: nag (loudly, but do not block) if the published cache looks stale
    # before a data-consuming command, so a forgetful operator notices.
    if not no_pull:
        line, stale = _cache_report()
        if stale:
            print('%s' % line, file=sys.stderr)

    return _forward(args.verb, ws, rest)


if __name__ == '__main__':
    raise SystemExit(main())
