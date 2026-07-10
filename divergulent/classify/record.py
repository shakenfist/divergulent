"""The decision recorder — drive the registered rules into the ledger (step 3b).

This is the bridge between the deterministic rules (phase 2) and the append-only
decision ledger (phase 3, step 3a).  It runs the shared per-fingerprint pass
(``classify.iter_classified``) over the phase-1 index and writes its verdicts
into the ledger, honouring the two structural promises the ledger encodes:

1. **A category verdict is a decision; a dangerous-construct flag is an
   observation.**  For each distinct fingerprint the recorder appends exactly
   ONE category ``decision`` (the winning content-category rule's verdict) and,
   separately, an ``observation`` per dangerous-construct flag.  A flagged patch
   is still ``unknown``/substantive -- the flag rides alongside the category
   decision, never becomes one.

2. **Recording is idempotent.**  A pure decision is reproducible from
   ``(fingerprint, decided_by, rule_version)``, so the recorder never appends a
   decision that already exists LIVE for that triple; likewise an observation is
   skipped when an identical live one already exists.  A second run over the
   same corpus therefore appends nothing and duplicates no rows -- the
   append-only ledger stays a clean audit trail.

A decision records the WINNING category rule's OWN version (looked up from the
registry), not the module-level ``RULES_VERSION``: superseding that rule later
must key on the exact ``(rule_id, version)`` it decided under.  The
dangerous-construct observations, which come from the single whole-scan rule,
carry ``RULES_VERSION``.

Timestamps are **caller-supplied**: ``now`` is an ISO-8601 string the caller
(e.g. the step-3d CLI) passes in.  This module never reads a clock, keeping the
build deterministic and re-runnable.

Curation-side only: no client command imports ``classify/``; nothing here runs
an LLM.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from divergulent.classify import bts as bts_mod
from divergulent.classify import cross_reference as xref_mod
from divergulent.classify import ledger as ledger_mod
from divergulent.classify import reach as reach_mod
from divergulent.classify import reviewability as reviewability_mod
from divergulent.classify import security_tracker as tracker_mod
from divergulent.classify.classify import iter_classified
from divergulent.classify.rules import RULES_VERSION

# The observation source: the single dangerous-construct scan rule.  All
# dangerous-construct flags are recorded under this id at ``RULES_VERSION``.
_SCAN_RULE_ID = 'dangerous-construct-scan'


@dataclass
class RecordStats:
    """What a :func:`record_to_ledger` run did.

    ``*_appended`` rows were newly written; ``*_skipped`` rows already existed
    live and were left untouched (idempotency).  ``decisions_superseded`` counts
    heuristic decisions a ``reconcile`` run retired because the winning rule for
    that fingerprint changed.  ``reviewability_*`` counts the deterministic size
    (reviewability) observation, one per fingerprint.  ``fingerprints`` is the
    number of distinct fingerprints classified.
    """

    decisions_appended: int = 0
    decisions_skipped: int = 0
    decisions_superseded: int = 0
    observations_appended: int = 0
    observations_skipped: int = 0
    reviewability_appended: int = 0
    reviewability_skipped: int = 0
    reach_appended: int = 0
    reach_skipped: int = 0
    reach_unknown: int = 0
    # The phase-6 external CVE cross-reference (only when a Security Tracker
    # snapshot is supplied). ``external_decisions_*`` count the settled ``security``
    # category decision; ``external_obs_*`` count the provenance observation
    # (confirmed / unconfirmed) recorded alongside it.
    external_decisions_appended: int = 0
    external_decisions_skipped: int = 0
    external_decisions_superseded: int = 0
    external_obs_appended: int = 0
    external_obs_skipped: int = 0
    fingerprints: int = 0


def _category_rule_versions(registry: list[ledger_mod.RegisteredRule]) -> dict[str, int]:
    """Map each content-category rule id to its registered version.

    Built from the registry so a decision records the WINNING rule's own
    version.  Only ``kind='heuristic'``, ``purity='pure'`` content rules are
    relevant here, but the map is keyed by id over the whole registry; the
    recorder only ever looks up a content-category ``decided_by`` in it.
    """
    return {rule.rule_id: rule.version for rule in registry}


def _record_external_cve(conn, record, tracker_conn, tracker_date, now, stats,
                         bts_conn=None, bts_date=None):
    """Record the phase-6 CVE (and BTS bug) cross-reference for one fingerprint.

    Two outputs, both supersede-on-change so a re-run over an unchanged snapshot
    writes nothing:

    * a ``provenance`` observation -- ``cve-confirmed`` or ``claim-unconfirmed`` --
      whenever the fingerprint carries a CVE reference (retracted when it no longer
      does, or when there is nothing to say);
    * a settled ``security`` DECISION, but only for a code-touching confirmed CVE
      over the ``unknown`` residue (the deference + code-touch guards). It carries
      the compact ``input_snapshot`` and the ``input_fresh_until`` horizon; a stale
      (past-horizon) or changed verdict supersedes the prior one, and a corroboration
      the tracker no longer supports is retracted.
    """
    verdict = (xref_mod.verify_cve(record.claim.cves, record.representative_package,
                                   tracker_conn, snapshot_date=tracker_date)
               if tracker_conn is not None else None)

    # 1. The provenance observation (annotates every CVE/bug-referencing fingerprint).
    # The CVE signal (from the Security Tracker) is preferred; the BTS bug check is
    # a fallback for a patch that cites a bug but no CVE.
    if verdict is not None and verdict.outcome == xref_mod.CONFIRMED:
        detail, evidence = xref_mod.DETAIL_CVE_CONFIRMED, verdict.reason
    elif verdict is not None and verdict.outcome == xref_mod.CONTRADICTED:
        detail, evidence = xref_mod.DETAIL_CLAIM_UNCONFIRMED, verdict.reason
    else:
        detail, evidence = None, None
        # No CVE signal: fall back to the BTS bug check (E5). A bug reference maps
        # to no category, so this only ever produces a provenance signal.
        if bts_conn is not None:
            bug_verdict = xref_mod.verify_bugs(
                record.claim.bugs, record.representative_package, bts_conn, snapshot_date=bts_date)
            if bug_verdict.outcome != xref_mod.UNKNOWN:
                detail, evidence = bug_verdict.detail, bug_verdict.reason
    if detail is None:
        # No signal now: retract any stale provenance observation (no-op if none).
        ledger_mod.supersede_observations_for_fingerprint(
            conn, fingerprint=record.fingerprint, kind=xref_mod.PROVENANCE_KIND,
            observed_by=xref_mod.PROVENANCE_OBSERVED_BY, superseded_at=now, commit=False)
    elif ledger_mod.live_observation_exists(
            conn, fingerprint=record.fingerprint, observed_by=xref_mod.PROVENANCE_OBSERVED_BY,
            rule_version=xref_mod.EXTERNAL_CVE_VERSION, detail=detail, evidence=evidence):
        stats.external_obs_skipped += 1
    else:
        ledger_mod.supersede_observations_for_fingerprint(
            conn, fingerprint=record.fingerprint, kind=xref_mod.PROVENANCE_KIND,
            observed_by=xref_mod.PROVENANCE_OBSERVED_BY, superseded_at=now, commit=False)
        ledger_mod.append_observation(
            conn, fingerprint=record.fingerprint, kind=xref_mod.PROVENANCE_KIND,
            detail=detail, evidence=evidence, observed_by=xref_mod.PROVENANCE_OBSERVED_BY,
            rule_version=xref_mod.EXTERNAL_CVE_VERSION, observed_at=now, commit=False)
        stats.external_obs_appended += 1

    # 2. The settled ``security`` decision (confirmed + deference + code-touch).
    # Only the CVE tier settles a category, so this is skipped entirely without a
    # Security Tracker snapshot (a BTS-only run never touches security decisions).
    if tracker_conn is None:
        return
    settle = (verdict.outcome == xref_mod.CONFIRMED and xref_mod.should_settle_security(
        record.verdict.content_category, record.profile.touches_code))
    live = ledger_mod.live_decision_for_rule(
        conn, fingerprint=record.fingerprint, decided_by=xref_mod.EXTERNAL_CVE_RULE_ID,
        rule_version=xref_mod.EXTERNAL_CVE_VERSION)
    if settle:
        snapshot_json = json.dumps(verdict.input_snapshot, sort_keys=True)
        stale = live is not None and now[:10] >= (live['input_fresh_until'] or '')
        changed = live is not None and (live['input_snapshot'] or '') != snapshot_json
        if live is not None and not stale and not changed:
            stats.external_decisions_skipped += 1
            return
        if live is not None:
            ledger_mod.supersede_rule_decisions_for_fingerprint(
                conn, fingerprint=record.fingerprint, decided_by=xref_mod.EXTERNAL_CVE_RULE_ID,
                rule_version=xref_mod.EXTERNAL_CVE_VERSION, superseded_at=now, commit=False)
            stats.external_decisions_superseded += 1
        ledger_mod.append_decision(
            conn, fingerprint=record.fingerprint, category=xref_mod.EXTERNAL_CVE_CATEGORY,
            confidence=verdict.confidence, decided_by=xref_mod.EXTERNAL_CVE_RULE_ID,
            rule_version=xref_mod.EXTERNAL_CVE_VERSION, kind=xref_mod.EXTERNAL_CVE_KIND,
            evidence=verdict.reason, decided_at=now, input_snapshot=snapshot_json,
            input_fresh_until=verdict.fresh_until, commit=False)
        stats.external_decisions_appended += 1
    elif live is not None:
        # No longer a settle-able corroboration: retract the stale security verdict.
        ledger_mod.supersede_rule_decisions_for_fingerprint(
            conn, fingerprint=record.fingerprint, decided_by=xref_mod.EXTERNAL_CVE_RULE_ID,
            rule_version=xref_mod.EXTERNAL_CVE_VERSION, superseded_at=now, commit=False)
        stats.external_decisions_superseded += 1


def record_to_ledger(conn, corpus_dir, index_path, *, now, registry=None, progress=None,
                     reconcile=False, popcon_path=None, security_tracker_path=None,
                     bts_path=None):
    """Record the deterministic verdicts for a corpus into ``conn``; idempotent.

    Registers ``registry`` (or :func:`ledger.default_registry`) into the
    ledger, then for every distinct fingerprint in ``index_path``:

    * appends ONE category ``decision`` -- ``category`` is the content verdict,
      ``decided_by`` the winning content-category rule (``verdict.rule_ids[0]``),
      ``rule_version`` that rule's own registered version, ``confidence`` the
      verdict's confidence, ``evidence`` the ``' | '``-joined verdict signals,
      ``kind='heuristic'``, ``decided_at=now``;
    * appends one ``observation`` per ``verdict.flags`` entry --
      ``kind=flag.kind`` (e.g. ``'dangerous-construct'``), ``detail``/``evidence``
      from the flag, ``observed_by='dangerous-construct-scan'``,
      ``rule_version=RULES_VERSION``, ``observed_at=now``.

    Idempotent: a decision is skipped when a LIVE decision already exists for
    ``(fingerprint, decided_by, rule_version)``; an observation is skipped when a
    LIVE observation already exists for ``(fingerprint, observed_by,
    rule_version, detail, evidence)``.  A second run therefore appends nothing.

    ``reconcile`` (the ``ledger record`` path, default off so ``build``'s
    behaviour is unchanged): when the WINNING rule for a fingerprint has changed
    -- e.g. a newly-added ``test-only`` rule now outranks ``substantive``, or a
    rule's version bumped -- the fingerprint's existing live ``heuristic``
    decision is from a DIFFERENT ``(rule, version)``, so a plain append would
    leave two live heuristic decisions.  With ``reconcile`` on, that stale
    heuristic decision is SUPERSEDED before the new one is appended, keeping
    exactly one live deterministic decision per fingerprint.  ``llm`` / ``human``
    decisions are never touched (different ``kind``), so review work is
    preserved.  On a fresh ledger (``build``) there is no prior decision, so
    ``reconcile`` is a no-op.

    Returns a :class:`RecordStats`.  ``now`` is a caller-supplied ISO-8601
    string; this module never reads a clock.
    """
    rules = registry if registry is not None else ledger_mod.default_registry()
    ledger_mod.register_rules(conn, rules)
    rule_versions = _category_rule_versions(rules)

    # The deterministic reach (install-base) levels, joined once over the whole
    # corpus from the popcon snapshot + the index's binary lists. Empty (the reach
    # pass is skipped) when no snapshot is supplied -- reach is opt-in on a pinned
    # snapshot, so an operator who has not pulled one records exactly as before.
    # ``existing_reach`` is the prior run's levels: the skip key is the BUCKET, not
    # the evidence, so a fresh snapshot whose counts drift but whose bucket is
    # unchanged does NOT churn ~60k rows -- reach only re-records when a bucket moves.
    reach_levels = (reach_mod.reach_levels_for_index(index_path, popcon_path)
                    if popcon_path else {})
    existing_reach = reach_mod.reach_by_fingerprint(conn) if popcon_path else {}

    # The phase-6 external CVE cross-reference is opt-in on a pinned Security
    # Tracker snapshot, exactly like reach is opt-in on a popcon snapshot: with no
    # snapshot the pass is skipped and the recorder behaves exactly as before. When
    # supplied, open the snapshot once and register the external rule so its
    # decisions reference a known (rule_id, version).
    tracker_conn = None
    tracker_date = None
    if security_tracker_path:
        tracker_conn = tracker_mod.open_snapshot(security_tracker_path)
        tracker_date = tracker_mod.snapshot_meta(tracker_conn).get('snapshot_date', now[:10])
        ledger_mod.register_rules(conn, [xref_mod.registered_rule()])

    # The BTS bug index (E5) is opt-in on its own pinned snapshot, layered onto the
    # CVE pass: it only ever adds a provenance signal (a bug maps to no category),
    # for a patch that cites a Debian bug but no CVE.
    bts_conn = None
    bts_date = None
    if bts_path:
        bts_conn = bts_mod.open_snapshot(bts_path)
        bts_date = bts_mod.snapshot_meta(bts_conn).get('snapshot_date', now[:10])

    stats = RecordStats()
    for record in iter_classified(corpus_dir, index_path):
        stats.fingerprints += 1
        if progress is not None:
            progress.step(record.fingerprint[:12])
        verdict = record.verdict

        # The winning content-category rule decided this fingerprint; record its
        # id and its OWN registered version so a later supersede keys exactly.
        decided_by = verdict.rule_ids[0]
        rule_version = rule_versions[decided_by]

        if ledger_mod.live_decision_exists(
                conn, fingerprint=record.fingerprint, decided_by=decided_by,
                rule_version=rule_version):
            stats.decisions_skipped += 1
        else:
            if reconcile:
                # The winning rule changed: retire the fingerprint's stale
                # heuristic decision(s) so exactly one stays live. Only heuristic
                # rows -- llm/human verdicts are a different tier and outrank.
                stats.decisions_superseded += ledger_mod.supersede_decisions_for_fingerprint(
                    conn, fingerprint=record.fingerprint, kind='heuristic',
                    superseded_at=now, commit=False)
            ledger_mod.append_decision(
                conn, fingerprint=record.fingerprint,
                category=verdict.content_category, confidence=verdict.confidence,
                decided_by=decided_by, rule_version=rule_version, kind='heuristic',
                evidence=' | '.join(verdict.signals), decided_at=now, commit=False)
            stats.decisions_appended += 1

        for flag in verdict.flags:
            if ledger_mod.live_observation_exists(
                    conn, fingerprint=record.fingerprint, observed_by=_SCAN_RULE_ID,
                    rule_version=RULES_VERSION, detail=flag.detail,
                    evidence=flag.evidence):
                stats.observations_skipped += 1
            else:
                ledger_mod.append_observation(
                    conn, fingerprint=record.fingerprint, kind=flag.kind,
                    detail=flag.detail, evidence=flag.evidence,
                    observed_by=_SCAN_RULE_ID, rule_version=RULES_VERSION,
                    observed_at=now, commit=False)
                stats.observations_appended += 1

        # The deterministic reviewability (size) observation -- its own rule
        # identity, recorded once per fingerprint. The level is stable (the body
        # is content-addressed), so an unchanged re-record skips; a threshold
        # (version) change supersedes the prior level before appending the new.
        level = reviewability_mod.classify(record.profile)
        review_evidence = reviewability_mod.evidence_for(record.profile)
        if ledger_mod.live_observation_exists(
                conn, fingerprint=record.fingerprint,
                observed_by=reviewability_mod.REVIEWABILITY_OBSERVED_BY,
                rule_version=reviewability_mod.REVIEWABILITY_VERSION,
                detail=level, evidence=review_evidence):
            stats.reviewability_skipped += 1
        else:
            ledger_mod.supersede_observations_for_fingerprint(
                conn, fingerprint=record.fingerprint,
                kind=reviewability_mod.REVIEWABILITY_KIND, superseded_at=now, commit=False)
            ledger_mod.append_observation(
                conn, fingerprint=record.fingerprint,
                kind=reviewability_mod.REVIEWABILITY_KIND, detail=level, evidence=review_evidence,
                observed_by=reviewability_mod.REVIEWABILITY_OBSERVED_BY,
                rule_version=reviewability_mod.REVIEWABILITY_VERSION,
                observed_at=now, commit=False)
            stats.reviewability_appended += 1

        # The deterministic reach (install-base) observation -- its own rule
        # identity, recorded only when a popcon snapshot was supplied. A
        # fingerprint with no binary list to rank is counted as a gap, not
        # recorded (unknown is unrankable, so a row would be pure churn). An
        # unchanged level skips; a changed level (new snapshot/binaries or a
        # version bump) supersedes the prior before appending.
        if popcon_path:
            reach_level, reach_evidence = reach_levels.get(
                record.fingerprint, (reach_mod.REACH_UNKNOWN, None))
            if reach_level == reach_mod.REACH_UNKNOWN:
                stats.reach_unknown += 1
            elif existing_reach.get(record.fingerprint) == reach_level:
                stats.reach_skipped += 1
            else:
                ledger_mod.supersede_observations_for_fingerprint(
                    conn, fingerprint=record.fingerprint,
                    kind=reach_mod.REACH_KIND, superseded_at=now, commit=False)
                ledger_mod.append_observation(
                    conn, fingerprint=record.fingerprint,
                    kind=reach_mod.REACH_KIND, detail=reach_level, evidence=reach_evidence,
                    observed_by=reach_mod.REACH_OBSERVED_BY,
                    rule_version=reach_mod.REACH_VERSION, observed_at=now, commit=False)
                stats.reach_appended += 1

        # The phase-6 external CVE cross-reference -- only when a Security Tracker
        # snapshot was supplied. Verifies the fingerprint's claimed CVEs against the
        # snapshot and records the outcome as a provenance observation (always) plus,
        # for a code-touching confirmed CVE over the unknown residue, a settled
        # ``security`` decision carrying the input snapshot + freshness horizon.
        if tracker_conn is not None or bts_conn is not None:
            _record_external_cve(conn, record, tracker_conn, tracker_date, now, stats,
                                 bts_conn=bts_conn, bts_date=bts_date)

    if tracker_conn is not None:
        tracker_conn.close()
    if bts_conn is not None:
        bts_conn.close()

    if progress is not None:
        progress.finish()

    # One commit for the whole batch: the appends above ran ``commit=False`` so
    # ~60k inserts are a single transaction (one fsync) rather than one per row,
    # which turns an ~11-minute build into seconds. Same-connection reads during
    # the loop still saw the uncommitted rows, so idempotency was unaffected.
    conn.commit()
    return stats
