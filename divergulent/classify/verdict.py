"""The derived current-verdict view, the work queue, and the ledger report.

This module is step 3c of the ledger: the *read side*.  The append-only
``decision`` table (step 3a/3b) is the source of truth; the **current verdict**
for a fingerprint is **never stored** there — it is *computed* from the live
(non-superseded) decisions every time it is needed.  This is the load-bearing
design choice: because the view is derived, it cannot drift from the ledger, and
a supersession (a future rule retirement or an LLM/human override) is picked up
automatically the next time the view is computed.

Three things live here:

1. :func:`current_verdict` — per fingerprint, the single *winning* live decision,
   chosen by an explicit precedence (see below).  Pure read; the selection is
   done in Python over :func:`ledger.live_decisions` so it is easy to read and
   to test.  60k fingerprints is comfortably in range.

2. :func:`rebuild_current_verdict` — *materialises* the view into a
   ``current_verdict`` cache table (DROP/CREATE then one row per fingerprint).
   The cache is **always rebuilt from** :func:`current_verdict`, never
   hand-edited, so it stays a faithful snapshot for fast export (phase 5).

3. :func:`queue`, :func:`summarise_ledger`, :func:`render_report` — the phase-4
   residue (fingerprints still needing work) and a lean markdown report of how
   the current view splits and how big the audit trail is.

Curation-side only: no client command imports ``classify/``; nothing here runs
an LLM.  Import-time clean — it pulls in only ``ledger`` and stdlib.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from divergulent.classify import ledger as ledger_mod

# ---------------------------------------------------------------------------
# Precedence for the derived view.
#
# Per fingerprint we pick the single winning LIVE decision.  The ordering, in
# strict priority, is:
#
#   1. kind          — human > llm > heuristic (``ledger.kind_rank``)
#   2. decided_at    — most recent wins (a fresher verdict of the same kind)
#   3. confidence    — high > medium > low
#   4. decision id   — most recent insert wins (full determinism on ties)
#
# Implemented as a sort key returning a tuple of "higher is better" components,
# so ``max(..., key=...)`` selects the winner.  ``decided_at`` is an ISO-8601
# string and sorts correctly lexically; we guard ``None`` to the bottom.
# ---------------------------------------------------------------------------

_CONFIDENCE_RANK: dict[str, int] = {'low': 0, 'medium': 1, 'high': 2}


def _confidence_rank(confidence: str | None) -> int:
    """Rank a ``confidence`` string (``high > medium > low``); unknown sorts low.

    An unrecognised or ``NULL`` confidence ranks below ``low`` so it can never
    beat a real confidence on the tie-break; the kind and recency tiers above it
    still apply first.
    """
    if confidence is None:
        return -1
    return _CONFIDENCE_RANK.get(confidence, -1)


def _selection_key(row: sqlite3.Row) -> tuple[int, str, int, int]:
    """The precedence sort key for one live decision (higher tuple wins).

    Ordered: ``kind_rank`` (human > llm > heuristic), then ``decided_at`` (most
    recent), then ``confidence`` rank (high > medium > low), then ``id`` (most
    recent insert).  ``decided_at`` is normalised to ``''`` when ``NULL`` so it
    sorts to the bottom rather than raising.
    """
    return (
        ledger_mod.kind_rank(row['kind']),
        row['decided_at'] or '',
        _confidence_rank(row['confidence']),
        int(row['id']))


@dataclass(frozen=True)
class Verdict:
    """The winning live decision for one fingerprint (the derived current view).

    Carries enough of the decision to export and to reason about ``why``: the
    fingerprint, the chosen category, who decided and under which rule version,
    the decision ``kind`` and ``confidence``, the evidence string, the
    ``decided_at`` timestamp, and the source ``decision_id`` (so the view points
    back into the immutable ledger row it came from).
    """

    fingerprint: str
    category: str
    decided_by: str
    rule_version: int
    kind: str
    confidence: str
    evidence: str | None
    decided_at: str | None
    decision_id: int


def _verdict_from_row(row: sqlite3.Row) -> Verdict:
    return Verdict(
        fingerprint=row['fingerprint'],
        category=row['category'],
        decided_by=row['decided_by'],
        rule_version=int(row['rule_version']),
        kind=row['kind'],
        confidence=row['confidence'],
        evidence=row['evidence'],
        decided_at=row['decided_at'],
        decision_id=int(row['id']))


def current_verdict(conn: sqlite3.Connection) -> dict[str, Verdict]:
    """The derived current verdict per fingerprint (never stored, always computed).

    Reads every LIVE (``superseded_at IS NULL``) decision and selects, per
    fingerprint, the single winning one by :func:`_selection_key` precedence:
    kind (human > llm > heuristic) first, then ``decided_at`` (most recent), then
    confidence (high > medium > low), then decision ``id``.

    Returns ``{fingerprint: Verdict}``.  A fingerprint with no live decision (all
    superseded) is absent from the map — it has *no* current verdict and is
    re-queued by :func:`queue`.  The selection runs in Python so it is clear and
    directly testable; 60k fingerprints is fine.
    """
    winners: dict[str, sqlite3.Row] = {}
    for row in ledger_mod.live_decisions(conn):
        fingerprint = row['fingerprint']
        incumbent = winners.get(fingerprint)
        if incumbent is None or _selection_key(row) > _selection_key(incumbent):
            winners[fingerprint] = row
    return {fp: _verdict_from_row(row) for fp, row in winners.items()}


# ---------------------------------------------------------------------------
# The materialised cache.
#
# ``current_verdict`` (the table) is a CACHE of the derived view for fast export
# (phase 5).  It is always DROP/CREATE-d and rebuilt from ``current_verdict()``
# (the function) — never hand-edited — so it is guaranteed to mirror the ledger
# at rebuild time.  Columns mirror the winning decision.
# ---------------------------------------------------------------------------


def rebuild_current_verdict(conn: sqlite3.Connection) -> int:
    """(Re)materialise the ``current_verdict`` cache table; returns the row count.

    DROP/CREATE the table, then insert exactly one row per fingerprint from
    :func:`current_verdict`.  Because it is rebuilt from the query every time, a
    second rebuild over an unchanged ledger reproduces the same rows — the cache
    can never silently drift from the source-of-truth decisions.  This is the
    fast-export snapshot for phase 5; it is never the source of truth.
    """
    verdicts = current_verdict(conn)
    conn.execute('DROP TABLE IF EXISTS current_verdict')
    conn.execute(
        'CREATE TABLE current_verdict ('
        'fingerprint TEXT PRIMARY KEY, '
        'category TEXT NOT NULL, '
        'decided_by TEXT NOT NULL, '
        'rule_version INTEGER NOT NULL, '
        'kind TEXT NOT NULL, '
        'confidence TEXT NOT NULL, '
        'evidence TEXT, '
        'decided_at TEXT, '
        'decision_id INTEGER NOT NULL)')
    conn.executemany(
        'INSERT INTO current_verdict '
        '(fingerprint, category, decided_by, rule_version, kind, confidence, '
        'evidence, decided_at, decision_id) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [(v.fingerprint, v.category, v.decided_by, v.rule_version, v.kind,
          v.confidence, v.evidence, v.decided_at, v.decision_id)
         for v in verdicts.values()])
    conn.commit()
    return len(verdicts)


# ---------------------------------------------------------------------------
# The work queue.
# ---------------------------------------------------------------------------


def queue(conn: sqlite3.Connection) -> list[str]:
    """Fingerprints that still need work — the phase-4 residue.  Sorted, unique.

    Two sources, unioned:

    * fingerprints whose current (winning live) verdict ``category == 'unknown'``
      — the substantive residue the LLM tier (phase 4) consumes; and
    * fingerprints that have decisions in the ledger but **none live** (every
      decision superseded) — re-queued, because a supersession (a retired rule)
      left them with no verdict until something new is appended.

    Returned sorted and de-duplicated.
    """
    verdicts = current_verdict(conn)
    queued: set[str] = {fp for fp, v in verdicts.items() if v.category == 'unknown'}

    # Every fingerprint that appears anywhere in the ledger; those absent from
    # the derived view have no live decision and must be re-queued.
    all_fingerprints = {
        fp for (fp,) in conn.execute('SELECT DISTINCT fingerprint FROM decision')}
    queued |= (all_fingerprints - set(verdicts))

    return sorted(queued)


# ---------------------------------------------------------------------------
# The report.
# ---------------------------------------------------------------------------


@dataclass
class LedgerSummary:
    """A lean, fingerprint-weighted snapshot of the ledger's current state.

    ``verdicts_by_category`` / ``decisions_by_rule`` / ``observations_by_detail``
    are ordered dicts (most-frequent first) so the report is stable and readable.
    """

    fingerprints_with_verdict: int = 0
    verdicts_by_category: dict[str, int] = field(default_factory=dict)
    queue_size: int = 0
    decisions_by_rule: dict[str, int] = field(default_factory=dict)
    observations_by_detail: dict[str, int] = field(default_factory=dict)
    superseded_decisions: int = 0


def _counts_descending(pairs: list[tuple[str, int]]) -> dict[str, int]:
    """Order ``(key, count)`` pairs by count descending, then key, into a dict."""
    return {key: count for key, count in sorted(pairs, key=lambda kv: (-kv[1], kv[0]))}


def summarise_ledger(conn: sqlite3.Connection) -> LedgerSummary:
    """Summarise the current derived view and the audit trail; pure read.

    Covers, weighted by fingerprint:

    * ``fingerprints_with_verdict`` — fingerprints with a live (winning) verdict;
    * ``verdicts_by_category`` — how the current view splits (should reproduce
      the phase-2 packaging / documentation / unknown distribution);
    * ``queue_size`` — the phase-4 residue size (see :func:`queue`);
    * ``decisions_by_rule`` — live decisions counted by ``decided_by`` rule;
    * ``observations_by_detail`` — live observations counted by ``detail``;
    * ``superseded_decisions`` — the size of the superseded audit trail.
    """
    verdicts = current_verdict(conn)

    by_category: dict[str, int] = {}
    for verdict in verdicts.values():
        by_category[verdict.category] = by_category.get(verdict.category, 0) + 1

    by_rule: dict[str, int] = {}
    for verdict in verdicts.values():
        by_rule[verdict.decided_by] = by_rule.get(verdict.decided_by, 0) + 1

    by_detail: dict[str, int] = {}
    for obs in ledger_mod.live_observations(conn):
        by_detail[obs['detail']] = by_detail.get(obs['detail'], 0) + 1

    (superseded,) = conn.execute(
        'SELECT COUNT(*) FROM decision WHERE superseded_at IS NOT NULL').fetchone()

    return LedgerSummary(
        fingerprints_with_verdict=len(verdicts),
        verdicts_by_category=_counts_descending(list(by_category.items())),
        queue_size=len(queue(conn)),
        decisions_by_rule=_counts_descending(list(by_rule.items())),
        observations_by_detail=_counts_descending(list(by_detail.items())),
        superseded_decisions=int(superseded))


def _render_counts(title: str, counts: dict[str, int]) -> list[str]:
    """A markdown sub-section: a heading and a ``- key: count`` line each."""
    lines = ['### %s' % title, '']
    if not counts:
        lines.append('_(none)_')
    else:
        for key, count in counts.items():
            lines.append('- %s: %d' % (key, count))
    lines.append('')
    return lines


def render_report(summary: LedgerSummary) -> str:
    """Render a :class:`LedgerSummary` as a markdown report string.

    A lean, fingerprint-weighted report: the headline counts, the
    current-view category split (the phase-2 distribution reproduced through the
    ledger), the queue size, decisions by rule, live observations by detail, and
    the superseded-audit-trail size.
    """
    lines = ['# Ledger report', '']
    lines.append('- Fingerprints with a live verdict: %d'
                 % summary.fingerprints_with_verdict)
    lines.append('- Queue size (phase-4 residue): %d' % summary.queue_size)
    lines.append('- Superseded decisions (audit trail): %d'
                 % summary.superseded_decisions)
    lines.append('')
    lines.extend(_render_counts('Verdicts by category', summary.verdicts_by_category))
    lines.extend(_render_counts('Decisions by rule', summary.decisions_by_rule))
    lines.extend(_render_counts('Live observations by detail', summary.observations_by_detail))
    return '\n'.join(lines).rstrip() + '\n'
