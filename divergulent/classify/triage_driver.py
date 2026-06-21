"""The bounded triage driver + rule discovery (phase 4, step 4d).

This is the curation-side driver that pulls the substantive residue, orders it by
value, triages a *bounded* slice with the claim-blind LLM tier (steps 4a/4b),
records each result into the ledger (step 4c), and -- as a side product --
surfaces **candidate deterministic rules** for human approval.  It is the
machinery the operational sample run exercises; the full 42,907-fingerprint sweep
is the operator's call, taken iteratively with eyes on cost (see the plan's
"Bounded by prioritisation and rule discovery" and operational note).

Three disciplines from the plan are load-bearing here:

* **Prioritised, not blind.**  The work-list is the ``verdict.queue`` residue,
  ordered highest-value first: a live dangerous-construct observation, then by
  occurrence count (a recurring fingerprint stands for many carried patches).
  Only the first ``limit`` are triaged in a run.

* **The cap is VISIBLE.**  :class:`TriageRunStats` reports ``untriaged_remaining``
  -- the queue size minus what this run triaged -- so a budget that did not cover
  the whole residue is reported, never silently truncated.

* **Rule discovery is a REPORT, never auto-applied.**  :func:`candidate_rules`
  clusters the triaged fingerprints by ``(verified LLM category, structural
  key)`` and surfaces a cluster of >= K identical *verified* verdicts as a
  candidate deterministic rule for a human to approve.  Nothing here writes a
  rule; an approved rule is a separate, deliberate phase-2/3 act.

The LLM ``call`` is INJECTED, exactly as in steps 4a/4b, so the driver is pure
given a fake and the test suite runs fully offline and free.  Timestamps are
caller-supplied (``now``); only the CLI entry point (in ``triage.main``) reads a
clock, once, and threads it down.

Curation-side only and import-time clean: it pulls in only ``classify`` siblings
and stdlib; no client command imports it, and ``triage.main`` lazy-imports it to
avoid an import cycle.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from divergulent.classify import content as content_mod
from divergulent.classify import measure
from divergulent.classify import triage as triage_mod
from divergulent.classify import triage_record
from divergulent.classify import verdict as verdict_mod
from divergulent.classify.claim import extract_claim
from divergulent.classify.ledger import live_observations
from divergulent.classify.triage import DEFAULT_MODEL, triage_and_verify

# Diffs larger than this (characters) are routed straight to a human rather than
# sent to the model: a giant auto-generated patch (e.g. debian-changes) overflows
# the context window ("Prompt is too long"), and truncating it would yield a
# partial, misleading classification. The human reviews the full diff anyway.
# Generous so genuine large patches still get an LLM read; the per-item error
# handler backstops anything that slips through.
MAX_DIFF_CHARS_FOR_LLM = 400_000

# The dangerous-construct observation kind the phase-2 scan writes (see
# ``record.py`` / the ledger).  A live observation of this kind on a fingerprint
# both raises its triage priority and forces the result to human review.
_DANGEROUS_CONSTRUCT_KIND = 'dangerous-construct'

# Default cluster threshold for rule discovery: a candidate rule is proposed only
# when at least this many triaged fingerprints share one (verified category,
# structural key).  Three is the smallest cluster that reads as a pattern rather
# than a coincidence; it is a parameter so the operator can tighten it.
DEFAULT_RULE_MIN_MEMBERS = 3


# ---------------------------------------------------------------------------
# The work-list: prioritised residue.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkItem:
    """One prioritised fingerprint to triage, with the facts the driver needs.

    Built from the queue + the phase-1 index + the live observations: the
    representative ``raw_sha256`` (to load the body), the recurrence counts (for
    priority and the report), and ``has_dangerous_construct`` (which both raises
    priority and routes the result to a human).
    """

    fingerprint: str
    representative_sha: str
    representative_patch_name: str
    n_occurrences: int
    n_packages: int
    has_dangerous_construct: bool


def _fingerprints_with_dangerous_construct(conn: sqlite3.Connection) -> set[str]:
    """Fingerprints carrying a LIVE dangerous-construct observation.

    These are the highest-priority residue (a construct worth a look was added to
    a code file) and they force their triage result to human review regardless of
    what the verifier says (the security/malice escape hatch in
    ``triage_and_verify``).
    """
    return {
        obs['fingerprint'] for obs in live_observations(conn)
        if obs['kind'] == _DANGEROUS_CONSTRUCT_KIND}


def _index_groups(index_path: str) -> dict[str, dict]:
    """Group the phase-1 ``patch`` index by fingerprint: rep sha + counts.

    Returns ``{fingerprint: {'rep_sha', 'rep_patch_name', 'n_occurrences',
    'packages'}}``.  The representative is the FIRST row seen for a fingerprint in
    row order (stable for a given index build), matching ``classify._read_
    fingerprint_groups`` so the driver reads the diff exactly as phase 2 did.
    """
    connection = sqlite3.connect(index_path)
    try:
        rows = connection.execute(
            'SELECT fingerprint, raw_sha256, source_package, patch_name FROM patch').fetchall()
    finally:
        connection.close()

    groups: dict[str, dict] = {}
    for fingerprint, raw_sha256, source_package, patch_name in rows:
        group = groups.get(fingerprint)
        if group is None:
            group = {
                'rep_sha': raw_sha256,
                'rep_patch_name': patch_name,
                'n_occurrences': 0,
                'packages': set(),
            }
            groups[fingerprint] = group
        group['n_occurrences'] += 1
        group['packages'].add(source_package)
    return groups


def _priority_key(item: WorkItem) -> tuple:
    """Sort key (higher == triaged first): dangerous-construct, then occurrence.

    A live dangerous-construct observation is the top signal, then a high
    occurrence count (one recurring fingerprint stands for many carried patches),
    then package count, with the fingerprint as a final deterministic tie-break.
    """
    return (
        1 if item.has_dangerous_construct else 0,
        item.n_occurrences,
        item.n_packages,
        item.fingerprint)


def build_work_list(conn: sqlite3.Connection, index_path: str) -> list[WorkItem]:
    """The prioritised residue work-list (the full queue, ordered, NOT capped).

    Joins the ``verdict.queue`` residue against the phase-1 index (for the
    representative body + recurrence counts) and the live dangerous-construct
    observations (for priority + routing), and returns it sorted highest-value
    first.  A queued fingerprint absent from the index (no provenance row) is
    skipped -- it has no body to triage.  The cap is applied by the caller
    (:func:`run_triage`) so the full ordered list is available for the report's
    ``untriaged_remaining``.
    """
    groups = _index_groups(index_path)
    dangerous = _fingerprints_with_dangerous_construct(conn)

    items: list[WorkItem] = []
    for fingerprint in verdict_mod.queue(conn):
        group = groups.get(fingerprint)
        if group is None:
            continue
        items.append(WorkItem(
            fingerprint=fingerprint,
            representative_sha=group['rep_sha'],
            representative_patch_name=group['rep_patch_name'],
            n_occurrences=group['n_occurrences'],
            n_packages=len(group['packages']),
            has_dangerous_construct=fingerprint in dangerous))

    items.sort(key=_priority_key, reverse=True)
    return items


# ---------------------------------------------------------------------------
# The run.
# ---------------------------------------------------------------------------

@dataclass
class TriageRunStats:
    """What one bounded :func:`run_triage` pass did -- honest about the cap.

    ``triaged`` is how many fingerprints this run sent through draft + verify;
    ``untriaged_remaining`` is the queue size minus ``triaged`` -- the residue the
    budget did NOT cover, surfaced so the cap is visible and never a silent
    truncation.  ``by_category`` counts the drafted categories; ``verified`` /
    ``needs_human`` split the routing; ``claim_mismatches`` counts results whose
    routing reason cited a claim/content disagreement.
    """

    queue_size: int = 0
    triaged: int = 0
    verified: int = 0
    needs_human: int = 0
    claim_mismatches: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    untriaged_remaining: int = 0
    # Items in the slice that did NOT go through the model this run.
    skipped_already_triaged: int = 0   # a live decision already existed (resume)
    too_large: int = 0                 # diff too big for the model -> routed to a human
    errored: int = 0                   # the backend raised -> routed to a human


@dataclass(frozen=True)
class TriagedItem:
    """A triaged work item paired with its result -- the rule-discovery input.

    Carries the :class:`WorkItem` (so the report keeps the recurrence counts and
    the representative body reference) and the resulting
    :class:`triage.TriageResult` (the draft + verification + routing).
    """

    item: WorkItem
    result: triage_mod.TriageResult


def run_triage(conn, corpus_dir, index_path, *, call, now, limit,
               model=DEFAULT_MODEL, progress=None):
    """Triage a BOUNDED slice of the prioritised residue; record each result.

    Builds the prioritised work-list (:func:`build_work_list`), takes the first
    ``limit`` items, and for each: loads the representative body, computes the
    claim category (``extract_claim(...).claimed_category``) and the
    dangerous-construct flag, runs ``triage_and_verify`` against the injected
    ``call``, and records the result into the ledger via
    ``triage_record.record_triage_result`` at the caller-supplied ``now``.

    Returns ``(TriageRunStats, list[TriagedItem])``.  The stats make the cap
    VISIBLE: ``untriaged_remaining`` is the queue size minus ``triaged``.  The
    triaged-item list feeds :func:`candidate_rules`.  ``call`` is injected so the
    function is pure given a fake; ``now`` is caller-supplied so the path is
    deterministic.
    """
    work_list = build_work_list(conn, index_path)

    # Filter out already-triaged fingerprints BEFORE the limit, so --limit
    # triages that many NEW items rather than being consumed re-scanning the
    # needs-human backlog (which stays queued until a human reviews it). The
    # budget for a fingerprint is thus spent at most once, even across re-runs.
    done = triage_record.triaged_fingerprints(conn, model=model)
    pending_work = [item for item in work_list if item.fingerprint not in done]
    selected = pending_work[:limit]

    stats = TriageRunStats(queue_size=len(verdict_mod.queue(conn)))
    stats.skipped_already_triaged = len(work_list) - len(pending_work)
    stats.untriaged_remaining = max(len(pending_work) - len(selected), 0)
    triaged: list[TriagedItem] = []

    total = len(selected)
    for position, item in enumerate(selected, start=1):
        body = measure.read_body(corpus_dir, item.representative_sha)
        claim_category = extract_claim(item.representative_patch_name, body).claimed_category

        # A giant diff overflows the model; route it to a human (full diff in
        # review) rather than truncate to a misleading partial classification.
        if len(body) > MAX_DIFF_CHARS_FOR_LLM:
            reason = 'diff too large for LLM triage (%d chars); routed to a human' % len(body)
            triage_record.record_triage_to_human(
                conn, item.fingerprint, reason, now=now, model=model, priority=_priority_key(item)[1])
            stats.too_large += 1
            stats.needs_human += 1
            if progress is not None:
                progress('[%d/%d]   -> too large (%d chars) -> needs_human' % (
                    position, total, len(body)))
            continue

        # Each triage is two (slow) LLM calls; announce the item BEFORE the call
        # so the run is not silent while claude works, then the verdict after.
        if progress is not None:
            flag = ' [dangerous-construct]' if item.has_dangerous_construct else ''
            progress('[%d/%d] triaging %s (%s, %d pkgs)%s ...' % (
                position, total, item.representative_patch_name,
                item.fingerprint[:12], item.n_packages, flag))

        try:
            result = triage_and_verify(
                body, call=call, claim_category=claim_category,
                has_dangerous_construct=item.has_dangerous_construct, model=model)
        except Exception as exc:  # noqa: BLE001 -- one bad patch must not abort the run
            # Route the failing patch to a human and RECORD it, so it is neither
            # lost, re-tried-as-LLM forever, nor allowed to crash the whole batch.
            reason = 'LLM triage failed: %s' % exc
            triage_record.record_triage_to_human(
                conn, item.fingerprint, reason, now=now, model=model, priority=_priority_key(item)[1])
            stats.errored += 1
            stats.needs_human += 1
            if progress is not None:
                progress('[%d/%d]   -> triage error -> needs_human: %s' % (position, total, exc))
            continue

        triage_record.record_triage_result(
            conn, item.fingerprint, result, now=now, priority=_priority_key(item)[1])

        stats.triaged += 1
        category = result.draft.category
        stats.by_category[category] = stats.by_category.get(category, 0) + 1
        if result.routing == 'verified':
            stats.verified += 1
        else:
            stats.needs_human += 1
        if 'claim/content mismatch' in result.reason:
            stats.claim_mismatches += 1

        if progress is not None:
            progress('[%d/%d]   -> %s (%s)' % (position, total, category, result.routing))

        triaged.append(TriagedItem(item=item, result=result))

    return stats, triaged


# ---------------------------------------------------------------------------
# Rule discovery: cluster identical VERIFIED verdicts on look-alike patches.
#
# This is a REPORT for human approval, never an auto-applied rule.  The cluster
# key is cheap and structural: the VERIFIED LLM category plus a structural
# signature of the representative body -- the sorted set of touched file-types
# and whether the change is code-only / doc-only.  Look-alike patches that all
# verified to the same category are exactly the residue an approved deterministic
# rule could peel off, shrinking what the LLM must ever touch again.
# ---------------------------------------------------------------------------

def _structural_key(corpus_dir: str, representative_sha: str) -> str:
    """A cheap structural signature of a body: touched file-types + shape.

    Derived from ``content.profile``: the sorted set of touched file-types (e.g.
    ``code`` / ``doc`` / ``build``) plus a ``code-only`` / ``doc-only`` / ``mixed``
    shape.  Deliberately coarse -- it is a clustering key for "look-alike"
    patches, not a fingerprint -- so a recurring shape of verdicts stands out.
    """
    body = measure.read_body(corpus_dir, representative_sha)
    prof = content_mod.profile(body)
    types = sorted(prof.file_types)
    if not types:
        shape = 'empty'
    elif types == ['code']:
        shape = 'code-only'
    elif types == ['doc']:
        shape = 'doc-only'
    else:
        shape = 'mixed'
    return 'types={%s};shape=%s' % (','.join(types), shape)


@dataclass(frozen=True)
class CandidateRule:
    """A surfaced candidate deterministic rule -- for human approval, never applied.

    A cluster of ``member_count`` triaged fingerprints that ALL verified to
    ``category`` and share the structural ``key``.  ``fingerprints`` and
    ``occurrences`` quantify how much residue an approved rule would peel off.
    Nothing in the driver acts on this; it is a proposal a human reviews.
    """

    category: str
    structural_key: str
    member_count: int
    fingerprints: list[str]
    occurrences: int

    def describe(self) -> str:
        """A one-line human proposal, e.g. for the findings note / printed report."""
        return (
            '%d patches all verified %s, all matching %s '
            '(%d carried occurrences): consider a deterministic rule.' % (
                self.member_count, self.category, self.structural_key, self.occurrences))


def candidate_rules(corpus_dir, triaged, *, min_members=DEFAULT_RULE_MIN_MEMBERS):
    """Cluster VERIFIED triaged items into candidate deterministic rules.

    Considers only items that routed to ``verified`` (an unverified draft is not a
    settled signal worth a rule), groups them by ``(verified category, structural
    key)``, and returns one :class:`CandidateRule` per cluster with at least
    ``min_members`` members.  Sorted by member count then occurrences (most
    impactful first).  Pure apart from reading the representative bodies; it
    NEVER writes a rule -- the proposal goes to a human.
    """
    clusters: dict[tuple[str, str], list[TriagedItem]] = defaultdict(list)
    for triaged_item in triaged:
        if triaged_item.result.routing != 'verified':
            continue
        category = triaged_item.result.draft.category
        key = _structural_key(corpus_dir, triaged_item.item.representative_sha)
        clusters[(category, key)].append(triaged_item)

    candidates: list[CandidateRule] = []
    for (category, key), members in clusters.items():
        if len(members) < min_members:
            continue
        candidates.append(CandidateRule(
            category=category,
            structural_key=key,
            member_count=len(members),
            fingerprints=sorted(m.item.fingerprint for m in members),
            occurrences=sum(m.item.n_occurrences for m in members)))

    candidates.sort(key=lambda c: (c.member_count, c.occurrences), reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# The findings report.
# ---------------------------------------------------------------------------

def _render_counts(title: str, counts: dict[str, int]) -> list[str]:
    """A markdown sub-section: a heading and a ``- key: count`` line each, sorted."""
    lines = ['### %s' % title, '']
    if not counts:
        lines.append('_(none)_')
    else:
        for key in sorted(counts, key=lambda k: (-counts[k], k)):
            lines.append('- %s: %d' % (key, counts[key]))
    lines.append('')
    return lines


def render_run_report(stats: TriageRunStats, candidates: list[CandidateRule]) -> str:
    """Render the markdown findings note for one bounded triage run.

    Reports, honestly and in one place: how big the queue was, how many this run
    triaged, the verified-vs-human split, the claim/content mismatches, the
    drafted-category counts, the candidate deterministic rules (for human
    approval), and -- explicitly -- ``untriaged_remaining``, the residue the
    budget did NOT cover.  The cap is a headline, never a footnote.
    """
    lines: list[str] = ['# Phase 4 triage run findings', '']
    lines.append('- Queue size (phase-4 residue): %d' % stats.queue_size)
    lines.append('- Triaged this run: %d' % stats.triaged)
    lines.append('- Verified: %d' % stats.verified)
    lines.append('- Needs human review: %d' % stats.needs_human)
    lines.append('- Claim/content mismatches: %d' % stats.claim_mismatches)
    lines.append('- Skipped (already triaged on a prior run): %d' % stats.skipped_already_triaged)
    lines.append('- Routed to human, too large for the model: %d' % stats.too_large)
    lines.append('- Routed to human, triage error: %d' % stats.errored)
    lines.append('- **Untriaged remaining (budget did not cover): %d**'
                 % stats.untriaged_remaining)
    lines.append('')
    lines.extend(_render_counts('Drafted categories', stats.by_category))

    lines.append('### Candidate deterministic rules (for human approval, never auto-applied)')
    lines.append('')
    if not candidates:
        lines.append('_No cluster reached the threshold._')
        lines.append('')
    else:
        for candidate in candidates:
            lines.append('- %s' % candidate.describe())
            lines.append('  - fingerprints: %s'
                         % ', '.join(fp[:16] for fp in candidate.fingerprints))
        lines.append('')

    return '\n'.join(lines).rstrip() + '\n'


def print_run_summary(stats: TriageRunStats, candidates: list[CandidateRule]) -> None:
    """Print a lean summary of the run -- the same honest counts as the report.

    The cap is surfaced loudly: ``untriaged_remaining`` is the last headline line
    so a budgeted run never hides what it did not cover.
    """
    print('triaged %d of %d queued; verified=%d needs_human=%d claim_mismatches=%d' % (
        stats.triaged, stats.queue_size, stats.verified, stats.needs_human,
        stats.claim_mismatches))
    if stats.skipped_already_triaged or stats.too_large or stats.errored:
        print('skipped (already triaged)=%d; routed-to-human too-large=%d, errored=%d' % (
            stats.skipped_already_triaged, stats.too_large, stats.errored))
    if stats.by_category:
        print('drafted categories:')
        for category in sorted(stats.by_category, key=lambda k: (-stats.by_category[k], k)):
            print('  %-16s %d' % (category, stats.by_category[category]))
    if candidates:
        print('candidate deterministic rules (for human approval):')
        for candidate in candidates:
            print('  %s' % candidate.describe())
    else:
        print('candidate deterministic rules: none reached the threshold')
    print('untriaged remaining (budget did not cover): %d' % stats.untriaged_remaining)
