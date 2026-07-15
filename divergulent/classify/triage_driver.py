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
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from divergulent.classify import content as content_mod
from divergulent.classify import injection as injection_mod
from divergulent.classify import measure
from divergulent.classify import triage as triage_mod
from divergulent.classify import triage_record
from divergulent.classify import verdict as verdict_mod
from divergulent.classify import ledger as ledger_mod
from divergulent.classify import reviewability as reviewability_mod
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

# The prompt-injection tripwire observation kind (``record.py`` / the ledger). A
# live DIFF-region observation of this kind means the diff carries injection-
# shaped text aimed at the classifier: the driver SKIPS the LLM entirely (the
# whole point is not to feed attacker instructions to the model they target) and
# routes the patch to a human with a priority bump. A header-only hit does NOT
# skip -- the LLM never reads the header.
_INJECTION_KIND = injection_mod.INJECTION_KIND

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
    priority and the report), ``has_dangerous_construct`` (which both raises
    priority and routes the result to a human), ``risk_rank`` -- the security-risk
    gate's level (0..3) for this fingerprint, the TOP priority component so the
    scariest carried patches are triaged and reviewed first -- and ``reach_rank``
    -- the install-base t-shirt level (0..4, XS..XL), a SECONDARY key WITHIN a risk
    tier so a widely-run risky patch sorts ahead of an obscure one, but reach never
    crosses a risk boundary.
    """

    fingerprint: str
    representative_sha: str
    representative_patch_name: str
    n_occurrences: int
    n_packages: int
    has_dangerous_construct: bool
    risk_rank: int = 0
    reach_rank: int = 0
    has_claim_unconfirmed: bool = False
    """A live phase-6 ``claim-unconfirmed`` provenance observation -- the patch
    declared a CVE/bug that did not survive cross-reference. A review nudge (a
    band below risk, above reach): a failed provenance claim is worth a human look
    ahead of a merely widely-run patch, but never crosses a risk boundary."""

    has_injection_suspect: bool = False
    """A live DIFF-region ``llm-injection-suspect`` observation -- the diff carries
    injection-shaped text aimed at the classifier. Skips the LLM (routed to a
    human) and takes a priority band just below risk, above provenance: a possible
    attack on the classifier is worth a human look ahead of a failed provenance
    claim, but never crosses a risk boundary."""


def _fingerprints_with_injection_suspect(conn: sqlite3.Connection) -> set[str]:
    """Fingerprints carrying a LIVE DIFF-region ``llm-injection-suspect`` observation.

    These SKIP the LLM: the diff body holds injection-shaped text, and feeding it
    to the model is exactly what we avoid. A header-only hit is deliberately NOT
    in this set (the LLM never reads the header). Delegates to the injection
    module so the diff-region filter lives in one place.
    """
    return injection_mod.injection_suspect_fingerprints(conn, region=injection_mod.DIFF_REGION)


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


def _fingerprints_with_claim_unconfirmed(conn: sqlite3.Connection) -> set[str]:
    """Fingerprints carrying a LIVE ``claim-unconfirmed`` provenance observation.

    The phase-6 contradiction set: a declared CVE/bug that failed cross-reference.
    A review nudge (below risk, above reach) -- not a verdict of malice.
    """
    from divergulent.classify import cross_reference as xref_mod  # lazy: import hygiene
    return {
        obs['fingerprint'] for obs in live_observations(conn)
        if obs['kind'] == xref_mod.PROVENANCE_KIND
        and obs['detail'] == xref_mod.DETAIL_CLAIM_UNCONFIRMED}


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


# Priority is a single integer with non-overlapping bands so the riskiest patches
# always sort first: risk_rank is the TOP band (scaled past any realistic reach +
# occurrence), then an injection-suspect band (a possible attack on the classifier),
# then provenance, then reach a SECONDARY band within a risk tier, and occurrence the
# within-reach tie-break. The bands cannot overlap (injection + provenance + reach +
# occurrence all sum below one risk step, and injection sits above provenance+reach+
# occurrence), so neither injection, provenance nor reach can ever promote a patch
# across a risk boundary -- the one hard rule of the axis.
RISK_PRIORITY_WEIGHT = 1_000_000_000
INJECTION_PRIORITY_WEIGHT = 500_000_000
PROVENANCE_PRIORITY_WEIGHT = 100_000_000
REACH_PRIORITY_WEIGHT = 1_000_000


def _priority_key(item: WorkItem) -> tuple:
    """Sort key (higher == first): risk, injection, dangerous, provenance, reach, occurrence.

    The security-risk gate's level is the top signal -- the scariest patches are
    triaged and reviewed first -- then a live injection-suspect observation (a
    possible attack on the classifier, routed to a human ahead of the rest), then a
    live dangerous-construct observation, then a phase-6 provenance contradiction (a
    declared CVE/bug that failed cross-reference), then the install-base reach (a
    widely-run patch ahead of an obscure one), then a high occurrence count (one
    recurring fingerprint stands for many carried patches), then package count, with
    the fingerprint as a final tie-break.
    """
    return (
        item.risk_rank,
        1 if item.has_injection_suspect else 0,
        1 if item.has_dangerous_construct else 0,
        1 if item.has_claim_unconfirmed else 0,
        item.reach_rank,
        item.n_occurrences,
        item.n_packages,
        item.fingerprint)


def _stored_priority(item: WorkItem) -> int:
    """The single integer ``review_queue.priority`` for a needs-human item.

    Encodes risk-first, then injection-within-risk, then provenance-within-injection,
    then reach-within-provenance ordering into one int (``risk_rank * RISK_WEIGHT +
    injection * INJECTION_WEIGHT + provenance * PROV_WEIGHT + reach_rank * REACH_WEIGHT
    + occurrence``) so the review tool's ``ORDER BY priority DESC`` pulls the
    highest-risk patches first -- an injection-suspect breaking ties within a risk
    tier, a failed provenance claim within that, reach within that, occurrence within
    that -- no schema change. The bands cannot overlap (injection = INJECTION_WEIGHT >
    PROV_WEIGHT + reach + occurrence, and injection + provenance + reach + occurrence <
    a risk step), so none of injection, provenance nor reach can ever promote a patch
    across a risk boundary.
    """
    return (item.risk_rank * RISK_PRIORITY_WEIGHT
            + (1 if item.has_injection_suspect else 0) * INJECTION_PRIORITY_WEIGHT
            + (1 if item.has_claim_unconfirmed else 0) * PROVENANCE_PRIORITY_WEIGHT
            + item.reach_rank * REACH_PRIORITY_WEIGHT
            + min(item.n_occurrences, REACH_PRIORITY_WEIGHT - 1))


def reprioritise_review_queue(conn, index_path: str) -> int:
    """Re-stamp pending review items' priority from CURRENT risk + occurrence.

    ``review_queue.priority`` is frozen at enqueue (triage) time, so a risk or
    reach signal that lands AFTER a patch was queued never reaches the queue order
    -- the worklist's "review next" walks ``priority`` DESC and would otherwise stay
    in occurrence order even as scary patches get scored. This recomputes the full
    :func:`_stored_priority` formula (``risk_rank * RISK_WEIGHT + reach_rank *
    REACH_WEIGHT + occurrence``) from the live risk + reach observations + the index
    for every pending item, updates those that changed, and returns the count
    changed. Run at the tail of a risk-gate pass so the queue self-heals as scores
    land; safe to run standalone.
    """
    from divergulent.classify import reach as reach_mod  # lazy: avoids an import cycle
    from divergulent.classify import risk as risk_mod  # lazy: avoids an import cycle

    risk_ranks = risk_mod.risk_rank_by_fingerprint(conn)
    reach_ranks = reach_mod.reach_rank_by_fingerprint(conn)
    unconfirmed = _fingerprints_with_claim_unconfirmed(conn)
    suspects = _fingerprints_with_injection_suspect(conn)
    groups = _index_groups(index_path)
    changed = 0
    for item in ledger_mod.pending_review_items(conn):
        fingerprint = item['fingerprint']
        occurrences = groups.get(fingerprint, {}).get('n_occurrences', 0)
        new_priority = (risk_ranks.get(fingerprint, 0) * RISK_PRIORITY_WEIGHT
                        + (1 if fingerprint in suspects else 0) * INJECTION_PRIORITY_WEIGHT
                        + (1 if fingerprint in unconfirmed else 0) * PROVENANCE_PRIORITY_WEIGHT
                        + reach_ranks.get(fingerprint, 0) * REACH_PRIORITY_WEIGHT
                        + min(occurrences, REACH_PRIORITY_WEIGHT - 1))
        if new_priority != item['priority']:
            ledger_mod.reprioritise_review_item(
                conn, item_id=item['id'], priority=new_priority, commit=False)
            changed += 1
    conn.commit()
    return changed


def build_work_list(conn: sqlite3.Connection, index_path: str, *,
                    scope: str = 'residue') -> list[WorkItem]:
    """A prioritised work-list (ordered, NOT capped).

    ``scope='residue'`` (default) is the ``verdict.queue`` residue -- the patches
    triage works on. ``scope='all'`` is EVERY fingerprint in the phase-1 index --
    used by the security-risk gate, which scores the whole corpus (a settled
    ``packaging`` patch can still be security-relevant, e.g. a ``debian/rules``
    hardening-flag change), not just the residue.

    Joins the source against the index (for the representative body + recurrence
    counts), the live dangerous-construct observations, the live security-risk
    levels, and the live reach levels, and returns it sorted highest-value first
    (risk, then dangerous, then reach, then occurrence). The cap is applied by the
    caller so the full ordered list is available for the report's remainder count.
    """
    from divergulent.classify import reach as reach_mod  # lazy: avoids an import cycle
    from divergulent.classify import risk as risk_mod  # lazy: avoids an import cycle

    groups = _index_groups(index_path)
    dangerous = _fingerprints_with_dangerous_construct(conn)
    unconfirmed = _fingerprints_with_claim_unconfirmed(conn)
    suspects = _fingerprints_with_injection_suspect(conn)
    risk_ranks = risk_mod.risk_rank_by_fingerprint(conn)
    reach_ranks = reach_mod.reach_rank_by_fingerprint(conn)
    fingerprints = groups.keys() if scope == 'all' else verdict_mod.queue(conn)

    items: list[WorkItem] = []
    for fingerprint in fingerprints:
        group = groups.get(fingerprint)
        if group is None:
            continue
        items.append(WorkItem(
            fingerprint=fingerprint,
            representative_sha=group['rep_sha'],
            representative_patch_name=group['rep_patch_name'],
            n_occurrences=group['n_occurrences'],
            n_packages=len(group['packages']),
            has_dangerous_construct=fingerprint in dangerous,
            risk_rank=risk_ranks.get(fingerprint, 0),
            reach_rank=reach_ranks.get(fingerprint, 0),
            has_claim_unconfirmed=fingerprint in unconfirmed,
            has_injection_suspect=fingerprint in suspects))

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
    skipped_injection: int = 0         # injection-suspect diff -> never sent to the LLM -> human
    skipped_oversized: int = 0         # not line-reviewable (reviewability axis) -> human
    too_large: int = 0                 # diff too big for the model -> routed to a human
    errored: int = 0                   # the backend raised -> routed to a human
    # Token usage summed across every model call this run (draft + verify per
    # triaged patch). The cache_read vs cache_creation split is the signal that
    # the cached rubric prefix is landing; cost is the backend's reported figure.
    usage: triage_mod.Usage = field(default_factory=triage_mod.Usage)
    # The model the run used (uniform per run) -- for the at-rates cost estimate.
    model: str = triage_mod.DEFAULT_MODEL


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
    oversized_fps = reviewability_mod.oversized_fingerprints(conn)
    injection_fps = _fingerprints_with_injection_suspect(conn)

    stats = TriageRunStats(queue_size=len(verdict_mod.queue(conn)), model=model)
    stats.skipped_already_triaged = len(work_list) - len(pending_work)
    stats.untriaged_remaining = max(len(pending_work) - len(selected), 0)
    triaged: list[TriagedItem] = []

    total = len(selected)
    for position, item in enumerate(selected, start=1):
        body = measure.read_body(corpus_dir, item.representative_sha)
        claim_category = extract_claim(item.representative_patch_name, body).claimed_category

        # Injection-suspect diff: the body carries injection-shaped text aimed at
        # the classifier. NEVER send it to the model -- that is the attack we are
        # guarding against -- so skip the LLM and route to a human, checked BEFORE
        # every other skip. A tripwire, not a shield: it catches lazy/untargeted
        # payloads and forces attacker effort up; it does not claim to stop an
        # adaptive attacker iterating offline against the public patterns.
        if item.fingerprint in injection_fps:
            families = injection_mod.injection_by_fingerprint(conn).get(item.fingerprint, '')
            reason = 'llm-injection-suspect (%s): not sent to the LLM; routed to a human' % families
            triage_record.record_triage_to_human(
                conn, item.fingerprint, reason, now=now, model=model, priority=_stored_priority(item))
            stats.skipped_injection += 1
            stats.needs_human += 1
            if progress is not None:
                progress('[%d/%d]   -> injection-suspect (%s) -> needs_human' % (position, total, families))
            continue

        # Oversized by changed-line count (the reviewability axis): not
        # line-reviewable, so route to a human disposition without an LLM call --
        # its full diff is in review. The principled, changed-line-based superset
        # of the raw-char backstop below.
        if item.fingerprint in oversized_fps:
            reason = ('oversized: not line-reviewable (>%d changed lines); routed to a human'
                      % reviewability_mod.REVIEWABILITY_OVERSIZED_LINES)
            triage_record.record_triage_to_human(
                conn, item.fingerprint, reason, now=now, model=model, priority=_stored_priority(item))
            stats.skipped_oversized += 1
            stats.needs_human += 1
            if progress is not None:
                progress('[%d/%d]   -> oversized (not line-reviewable) -> needs_human' % (position, total))
            continue

        # A giant diff overflows the model; route it to a human (full diff in
        # review) rather than truncate to a misleading partial classification.
        if len(body) > MAX_DIFF_CHARS_FOR_LLM:
            reason = 'diff too large for LLM triage (%d chars); routed to a human' % len(body)
            triage_record.record_triage_to_human(
                conn, item.fingerprint, reason, now=now, model=model, priority=_stored_priority(item))
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
                conn, item.fingerprint, reason, now=now, model=model, priority=_stored_priority(item))
            stats.errored += 1
            stats.needs_human += 1
            if progress is not None:
                progress('[%d/%d]   -> triage error -> needs_human: %s' % (position, total, exc))
            continue

        triage_record.record_triage_result(
            conn, item.fingerprint, result, now=now, priority=_stored_priority(item))

        stats.triaged += 1
        stats.usage = stats.usage + result.usage
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


@dataclass(frozen=True)
class RejectedCluster:
    """A cluster big enough to tempt a rule, REFUSED by the counterexample gate.

    Its structural ``key`` reached ``min_members`` for ``category``, but the SAME
    key also carries other categories across the settled population
    (``category_counts``): the structure does not determine the category, so it is
    not a sound deterministic rule.  Surfaced -- with the conflicting spread -- so
    the operator sees WHY a large-looking cluster is not a rule, rather than being
    tempted to approve it.
    """

    structural_key: str
    category_counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.category_counts.values())

    def describe(self) -> str:
        """A one-line explanation of why this tempting cluster is not a rule."""
        spread = ', '.join(
            '%s %d' % (category, count) for category, count in
            sorted(self.category_counts.items(), key=lambda kv: (-kv[1], kv[0])))
        return (
            '%s spans %d categories (%s): NOT a rule -- structure does not '
            'determine category.' % (self.structural_key, len(self.category_counts), spread))


@dataclass(frozen=True)
class RuleScan:
    """The outcome of scanning for candidate deterministic rules.

    ``candidates`` are SOUND proposals -- their structural key carries exactly one
    category across the settled population (the counterexample gate passed).
    ``rejected`` are clusters that met the size threshold but whose key also
    carries other categories, kept (with the spread) so the report can explain the
    refusal instead of silently dropping a large cluster.
    """

    candidates: list[CandidateRule]
    rejected: list[RejectedCluster]


def _gate_clusters(clusters: dict, key_categories: dict, min_members: int) -> RuleScan:
    """Apply the counterexample gate to ``(category, key) -> [(fingerprint, occ)]``.

    A ``(category, key)`` cluster of at least ``min_members`` becomes a candidate
    ONLY when ``key`` maps to a single category in ``key_categories`` (a
    ``key -> Counter(category)`` over the whole settled population).  A key that
    spans more than one category yields a :class:`RejectedCluster` instead (one per
    key, recorded once), so the same structure can never be proposed as a rule for
    one category while it demonstrably carries others.
    """
    candidates: list[CandidateRule] = []
    rejected: dict[str, RejectedCluster] = {}
    for (category, key), members in clusters.items():
        if len(members) < min_members:
            continue
        if len(key_categories[key]) == 1:
            candidates.append(CandidateRule(
                category=category, structural_key=key, member_count=len(members),
                fingerprints=sorted(fingerprint for fingerprint, _ in members),
                occurrences=sum(occurrences for _, occurrences in members)))
        elif key not in rejected:
            rejected[key] = RejectedCluster(
                structural_key=key, category_counts=dict(key_categories[key]))

    candidates.sort(key=lambda c: (c.member_count, c.occurrences), reverse=True)
    ordered_rejected = sorted(rejected.values(), key=lambda r: r.total, reverse=True)
    return RuleScan(candidates=candidates, rejected=ordered_rejected)


def candidate_rules(corpus_dir, triaged, *, min_members=DEFAULT_RULE_MIN_MEMBERS):
    """Cluster VERIFIED triaged items into candidate deterministic rules.

    Considers only items that routed to ``verified`` (an unverified draft is not a
    settled signal worth a rule), groups them by ``(verified category, structural
    key)``, and applies the counterexample gate: a cluster of at least
    ``min_members`` is a candidate only when its structural key carries ONE
    category across this run's verified items.  Returns a :class:`RuleScan`
    (sound candidates plus the gate's rejections, each with its category spread).
    Pure apart from reading the representative bodies; NEVER writes a rule.
    """
    clusters: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
    key_categories: dict[str, Counter] = defaultdict(Counter)
    for triaged_item in triaged:
        if triaged_item.result.routing != 'verified':
            continue
        category = triaged_item.result.draft.category
        key = _structural_key(corpus_dir, triaged_item.item.representative_sha)
        clusters[(category, key)].append(
            (triaged_item.item.fingerprint, triaged_item.item.n_occurrences))
        key_categories[key][category] += 1

    return _gate_clusters(clusters, key_categories, min_members)


def candidate_rules_from_ledger(conn, corpus_dir, index_path, *,
                                min_members=DEFAULT_RULE_MIN_MEMBERS):
    """Cluster the settled ledger verdicts into candidate rules, gated.

    The cross-batch view: rule discovery is a property of the accumulated ledger,
    not one run.  A pattern that builds up over several triage batches (two
    matching verdicts this run, two more next run) only reaches the threshold
    when clustered over the whole ledger -- clustering a single run's ``triaged``
    list (:func:`candidate_rules`) would miss it.

    Uses each fingerprint's CURRENT verdict (so a human override counts as the
    fingerprint's category, and supersedes the LLM draft), keeping only confident
    settled signals -- ``human`` or verified ``llm`` (an unverified draft or a
    bare heuristic baseline is not a signal worth a rule).  Groups by
    ``(category, structural key)`` and applies the counterexample gate: a key that
    also carries a different category is refused, not proposed.  Returns a
    :class:`RuleScan`.  NEVER writes a rule -- the proposal goes to a human.
    """
    groups = _index_groups(index_path)
    verdicts = verdict_mod.current_verdict(conn)

    clusters: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
    key_categories: dict[str, Counter] = defaultdict(Counter)
    for fingerprint, verdict in verdicts.items():
        if not (verdict.kind == 'human' or (verdict.kind == 'llm' and verdict.verified)):
            continue
        group = groups.get(fingerprint)
        if group is None:  # queued fingerprint with no provenance row -- skip
            continue
        key = _structural_key(corpus_dir, group['rep_sha'])
        clusters[(verdict.category, key)].append((fingerprint, group['n_occurrences']))
        key_categories[key][verdict.category] += 1

    return _gate_clusters(clusters, key_categories, min_members)


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


# ---------------------------------------------------------------------------
# Cost & cache telemetry.
#
# The claude -p backend reports its own ``total_cost_usd`` (authoritative for the
# subscription path); for the metered API path, and as a "what would this cost
# at standard rates" estimate even on subscription, we derive a cost from a small
# indicative rate table. Update the rates when Anthropic pricing changes.
# ---------------------------------------------------------------------------

# (input, output) US$ per million tokens. Indicative, as of 2026-06.
_API_RATES_PER_MTOK = {
    'claude-sonnet-4-6': (3.0, 15.0),
    'claude-opus-4-8': (5.0, 25.0),
    'claude-haiku-4-5-20251001': (1.0, 5.0),
}
_DEFAULT_RATE_PER_MTOK = (3.0, 15.0)
_CACHE_READ_MULTIPLIER = 0.1    # cache reads bill at ~10% of input
_CACHE_WRITE_MULTIPLIER = 2.0   # 1h ephemeral cache writes bill at ~2x input


def derived_cost_usd(usage: triage_mod.Usage, model: str) -> float:
    """An at-standard-rates cost estimate for one run's ``usage``.

    The "what would this cost metered" figure -- shown even on subscription so the
    prototype→pivot decision has a number. Applies the cache read/write
    multipliers; the model is uniform per run.
    """
    in_rate, out_rate = _API_RATES_PER_MTOK.get(model, _DEFAULT_RATE_PER_MTOK)
    million = 1_000_000
    return (
        usage.input_tokens / million * in_rate
        + usage.cache_read_tokens / million * in_rate * _CACHE_READ_MULTIPLIER
        + usage.cache_creation_tokens / million * in_rate * _CACHE_WRITE_MULTIPLIER
        + usage.output_tokens / million * out_rate)


def cache_hit_ratio(usage: triage_mod.Usage) -> float | None:
    """Fraction of input tokens served from cache -- the caching-landed signal.

    ``cache_read / (cache_read + cache_creation + input)``. ``None`` when nothing
    ran (no input at all). Climbs toward 1 across a run as the cached rubric
    prefix is read back instead of re-sent.
    """
    cacheable = usage.cache_read_tokens + usage.cache_creation_tokens + usage.input_tokens
    if cacheable == 0:
        return None
    return usage.cache_read_tokens / cacheable


def _render_cost_and_cache(stats: TriageRunStats) -> list[str]:
    """The Cost & cache report block: tokens, cache-hit ratio, reported + derived cost."""
    usage = stats.usage
    lines = ['### Cost & cache', '']
    if stats.triaged == 0:
        lines.extend(['_(no model calls this run)_', ''])
        return lines
    ratio = cache_hit_ratio(usage)
    lines.append('- Input tokens: %d (cache read %d, cache write %d)' % (
        usage.input_tokens, usage.cache_read_tokens, usage.cache_creation_tokens))
    lines.append('- Output tokens: %d' % usage.output_tokens)
    lines.append('- Cache-hit ratio: %s' % ('n/a' if ratio is None else '%.1f%%' % (ratio * 100)))
    if usage.cost_usd is not None:
        lines.append('- Cost (backend-reported): $%.4f' % usage.cost_usd)
    lines.append('- Cost (estimated at API rates for %s): $%.4f' % (
        stats.model, derived_cost_usd(usage, stats.model)))
    lines.append('- Per triaged patch (estimated): $%.4f' % (
        derived_cost_usd(usage, stats.model) / stats.triaged))
    lines.append('')
    return lines


def render_run_report(stats: TriageRunStats, scan: RuleScan) -> str:
    """Render the markdown findings note for one bounded triage run.

    Reports, honestly and in one place: how big the queue was, how many this run
    triaged, the verified-vs-human split, the claim/content mismatches, the
    drafted-category counts, the candidate deterministic rules (for human
    approval) and the clusters the counterexample gate REFUSED (with the
    conflicting spread, so a tempting-but-unsound cluster is explained not hidden),
    and -- explicitly -- ``untriaged_remaining``, the residue the budget did NOT
    cover.  The cap is a headline, never a footnote.
    """
    lines: list[str] = ['# Phase 4 triage run findings', '']
    lines.append('- Queue size (phase-4 residue): %d' % stats.queue_size)
    lines.append('- Triaged this run: %d' % stats.triaged)
    lines.append('- Verified: %d' % stats.verified)
    lines.append('- Needs human review: %d' % stats.needs_human)
    lines.append('- Claim/content mismatches: %d' % stats.claim_mismatches)
    lines.append('- Skipped (already triaged on a prior run): %d' % stats.skipped_already_triaged)
    lines.append('- Routed to human, injection-suspect (never sent to the LLM): %d' % stats.skipped_injection)
    lines.append('- Routed to human, oversized (not line-reviewable): %d' % stats.skipped_oversized)
    lines.append('- Routed to human, too large for the model: %d' % stats.too_large)
    lines.append('- Routed to human, triage error: %d' % stats.errored)
    lines.append('- **Untriaged remaining (budget did not cover): %d**'
                 % stats.untriaged_remaining)
    lines.append('')
    lines.extend(_render_counts('Drafted categories', stats.by_category))
    lines.extend(_render_cost_and_cache(stats))

    lines.append('### Candidate deterministic rules (for human approval, never auto-applied)')
    lines.append('')
    if not scan.candidates:
        lines.append('_No sound cluster: none reached the threshold with a structural '
                     'key unique to one category._')
        lines.append('')
    else:
        for candidate in scan.candidates:
            lines.append('- %s' % candidate.describe())
            lines.append('  - fingerprints: %s'
                         % ', '.join(fp[:16] for fp in candidate.fingerprints))
        lines.append('')

    if scan.rejected:
        lines.append('### Refused by the counterexample gate (structure does not '
                     'determine category)')
        lines.append('')
        for rejected in scan.rejected:
            lines.append('- %s' % rejected.describe())
        lines.append('')

    return '\n'.join(lines).rstrip() + '\n'


def print_run_summary(stats: TriageRunStats, scan: RuleScan) -> None:
    """Print a lean summary of the run -- the same honest counts as the report.

    The cap is surfaced loudly: ``untriaged_remaining`` is the last headline line
    so a budgeted run never hides what it did not cover.
    """
    print('triaged %d of %d queued; verified=%d needs_human=%d claim_mismatches=%d' % (
        stats.triaged, stats.queue_size, stats.verified, stats.needs_human,
        stats.claim_mismatches))
    if (stats.skipped_already_triaged or stats.skipped_injection or stats.skipped_oversized
            or stats.too_large or stats.errored):
        print('skipped (already triaged)=%d; routed-to-human injection-suspect=%d, oversized=%d, '
              'too-large=%d, errored=%d' % (
                  stats.skipped_already_triaged, stats.skipped_injection, stats.skipped_oversized,
                  stats.too_large, stats.errored))
    if stats.by_category:
        print('drafted categories:')
        for category in sorted(stats.by_category, key=lambda k: (-stats.by_category[k], k)):
            print('  %-16s %d' % (category, stats.by_category[category]))
    if stats.triaged:
        ratio = cache_hit_ratio(stats.usage)
        reported = ('' if stats.usage.cost_usd is None
                    else ' reported=$%.4f' % stats.usage.cost_usd)
        print('cost & cache: in=%d (cache-read=%d) out=%d; cache-hit=%s;%s est=$%.4f (~$%.4f/patch)' % (
            stats.usage.input_tokens, stats.usage.cache_read_tokens, stats.usage.output_tokens,
            'n/a' if ratio is None else '%.1f%%' % (ratio * 100), reported,
            derived_cost_usd(stats.usage, stats.model),
            derived_cost_usd(stats.usage, stats.model) / stats.triaged))
    if scan.candidates:
        print('candidate deterministic rules (for human approval):')
        for candidate in scan.candidates:
            print('  %s' % candidate.describe())
    else:
        print('candidate deterministic rules: none sound (no key unique to one category)')
    if scan.rejected:
        print('refused by the counterexample gate (structure != category):')
        for rejected in scan.rejected:
            print('  %s' % rejected.describe())
    print('untriaged remaining (budget did not cover): %d' % stats.untriaged_remaining)
