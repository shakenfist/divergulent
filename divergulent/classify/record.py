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

from dataclasses import dataclass

from divergulent.classify import ledger as ledger_mod
from divergulent.classify import reviewability as reviewability_mod
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
    fingerprints: int = 0


def _category_rule_versions(registry: list[ledger_mod.RegisteredRule]) -> dict[str, int]:
    """Map each content-category rule id to its registered version.

    Built from the registry so a decision records the WINNING rule's own
    version.  Only ``kind='heuristic'``, ``purity='pure'`` content rules are
    relevant here, but the map is keyed by id over the whole registry; the
    recorder only ever looks up a content-category ``decided_by`` in it.
    """
    return {rule.rule_id: rule.version for rule in registry}


def record_to_ledger(conn, corpus_dir, index_path, *, now, registry=None, progress=None,
                     reconcile=False):
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

    if progress is not None:
        progress.finish()

    # One commit for the whole batch: the appends above ran ``commit=False`` so
    # ~60k inserts are a single transaction (one fsync) rather than one per row,
    # which turns an ~11-minute build into seconds. Same-connection reads during
    # the loop still saw the uncommitted rows, so idempotency was unaffected.
    conn.commit()
    return stats
