"""Record an LLM triage result into the ledger (phase 4, step 4c).

This is the bridge between the claim-blind LLM triage + adversarial verification
(``triage.py``, steps 4a/4b) and the append-only decision ledger (phase 3).  It
takes a single :class:`triage.TriageResult` -- a draft, its independent
verification, and the resulting routing -- and writes it into the ledger,
honouring the same structural promises the deterministic recorder
(``record.py``) does, plus the step-4c precedence rules:

1. **An LLM decision is recorded with an explicit ``verified`` flag.**  A draft
   that the adversarial pass confirmed (``routing == 'verified'``) is appended
   ``verified=True`` -- only then does it outrank a heuristic.  An unverified
   draft is still recorded (audit trail, cache) but ``verified=False``, so the
   precedence in ``verdict.decision_rank`` keeps it BELOW the heuristic: no cry
   wolf from an unreviewed guess.

2. **The MODEL is part of the rule identity, the prompt the rule version.**
   ``decided_by = 'llm-triage:<model>'`` (so changing the model is a NEW rule
   identity whose old decisions can be superseded), while ``rule_version`` is
   the integer ``prompt_version`` (so a prompt bump moves the version and
   re-triages cleanly).  Both drive supersession through the existing phase-3
   machinery, and ``rule_version`` stays the INTEGER the schema requires.

3. **A ``needs_human`` result is enqueued for human review.**  When the routing
   is ``needs_human`` the result is also appended to the ``review_queue`` so the
   local, signed review tool (step 4e) can pull it.  A human's verdict is a
   separate signed ``kind='human'`` decision; this recorder never writes one.

4. **Recording is idempotent.**  A decision is skipped when a LIVE decision
   already exists for ``(fingerprint, decided_by, rule_version)``; a review item
   is skipped when a PENDING one already exists for the fingerprint.  A second
   run over the same result therefore appends nothing.

Timestamps are **caller-supplied**: ``now`` is an ISO-8601 string the caller
passes in.  This module never reads a clock, keeping the path deterministic and
re-runnable.

Curation-side only and import-time clean: it pulls in only ``ledger`` and
``triage`` and stdlib; no client command imports it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from divergulent.classify import ledger as ledger_mod
from divergulent.classify import triage as triage_mod
from divergulent.classify.ledger import CATEGORY_ENUM_VERSION


@dataclass
class TriageRecordStats:
    """What a :func:`record_triage_result` call did.

    Each flag/count is whether a row was newly written (``*_appended``) or
    skipped because an equivalent live/pending one already existed
    (``*_skipped``).  ``verified`` mirrors the recorded decision's verified flag
    for the caller's report.
    """

    decision_appended: bool = False
    decision_skipped: bool = False
    review_appended: bool = False
    review_skipped: bool = False
    verified: bool = False


def _decided_by(model: str) -> str:
    """The rule identity for an LLM triage decision: ``'llm-triage:<model>'``.

    Putting the MODEL in the rule id (not the integer rule_version) means
    changing the model is a new rule identity -- its old decisions can be
    superseded and re-triaged -- while a prompt bump moves the integer
    rule_version.  Both drive supersession cleanly.
    """
    return 'llm-triage:%s' % model


def _registered_rule(model: str, prompt_version: int) -> ledger_mod.RegisteredRule:
    """The :class:`ledger.RegisteredRule` for one ``(model, prompt_version)``.

    ``kind='llm'``, ``purity='pure'`` (the verdict is a function of the diff body
    alone, modulo the model's non-determinism, which is captured in the evidence)
    so the CLI can supersede it by id + version exactly as a heuristic rule.
    """
    return ledger_mod.RegisteredRule(
        rule_id=_decided_by(model),
        version=prompt_version,
        kind='llm',
        purity='pure',
        description='claim-blind LLM triage (%s), verified adversarially' % model,
        category_enum_version=CATEGORY_ENUM_VERSION)


def _evidence(result: triage_mod.TriageResult) -> str:
    """A compact, auditable record of BOTH the draft and the verification.

    An LLM verdict is non-deterministic, so the ledger keeps enough to audit it
    after the fact: the draft's raw response, the verification's agreement +
    raw response, and the routing reason.  JSON so it is machine-readable and
    stable.
    """
    return json.dumps({
        'routing': result.routing,
        'reason': result.reason,
        'draft': {
            'category': result.draft.category,
            'confidence': result.draft.confidence,
            'model': result.draft.model,
            'prompt_version': result.draft.prompt_version,
            'raw_response': result.draft.raw_response,
        },
        'verification': {
            'agrees': result.verification.agrees,
            'confidence': result.verification.confidence,
            'model': result.verification.model,
            'prompt_version': result.verification.prompt_version,
            'raw_response': result.verification.raw_response,
        },
    }, sort_keys=True)


def record_triage_result(conn, fingerprint, result, *, now, register=True,
                         priority=0):
    """Record one :class:`triage.TriageResult` into ``conn``; idempotent.

    Appends an ``llm`` ``decision`` for ``fingerprint`` carrying the draft's
    category/confidence, ``decided_by='llm-triage:<model>'``,
    ``rule_version=<prompt_version>``, ``verified=(routing == 'verified')``, and
    an ``evidence`` JSON of both the draft and the verification.  When the
    routing is ``'needs_human'`` it ALSO appends a pending ``review_queue`` item
    (carrying the routing reason and the draft category/confidence).

    If ``register`` (the default), registers the ``(model, prompt_version)`` rule
    into the ``rule`` table first so the CLI can supersede it by id + version.

    Idempotent: the decision is skipped when a LIVE decision already exists for
    ``(fingerprint, decided_by, rule_version)``; the review item is skipped when
    a PENDING one already exists for the fingerprint.  A second call therefore
    appends nothing.

    Returns a :class:`TriageRecordStats`.  ``now`` is a caller-supplied ISO-8601
    string; this module never reads a clock.
    """
    draft = result.draft
    decided_by = _decided_by(draft.model)
    rule_version = draft.prompt_version
    verified = result.routing == 'verified'

    if register:
        ledger_mod.register_rules(conn, [_registered_rule(draft.model, rule_version)])

    stats = TriageRecordStats(verified=verified)

    if ledger_mod.live_decision_exists(
            conn, fingerprint=fingerprint, decided_by=decided_by,
            rule_version=rule_version):
        stats.decision_skipped = True
    else:
        ledger_mod.append_decision(
            conn, fingerprint=fingerprint, category=draft.category,
            confidence=draft.confidence, decided_by=decided_by,
            rule_version=rule_version, kind='llm', verified=verified,
            evidence=_evidence(result), decided_at=now, commit=False)
        stats.decision_appended = True

    if result.routing == 'needs_human':
        if ledger_mod.pending_review_item_exists(
                conn, fingerprint=fingerprint, decided_by=decided_by):
            stats.review_skipped = True
        else:
            ledger_mod.append_review_item(
                conn, fingerprint=fingerprint, reason=result.reason,
                draft_category=draft.category, draft_confidence=draft.confidence,
                enqueued_at=now, priority=priority, commit=False)
            stats.review_appended = True

    conn.commit()
    return stats


def record_triage_to_human(conn, fingerprint, reason, *, now, model,
                           prompt_version=triage_mod.PROMPT_VERSION, register=True, priority=0):
    """Record a fingerprint we could NOT triage so a human handles it; idempotent.

    For a patch the LLM could not classify -- too large for the model's context,
    or a backend error -- appends an ``llm`` ``decision`` (category ``unknown``,
    ``verified=False``, evidence noting the failure) under the SAME
    ``(model, prompt_version)`` rule identity as a normal triage, plus a pending
    ``review_queue`` item carrying ``reason``. Recording the decision means a
    later run finds a live decision for that triple and SKIPS the fingerprint
    instead of re-calling the failing/expensive model on it -- the budget is
    spent at most once. The human (step 4e) decides the real category from the
    full diff. ``now`` is caller-supplied; the call commits.
    """
    decided_by = _decided_by(model)
    if register:
        ledger_mod.register_rules(conn, [_registered_rule(model, prompt_version)])

    stats = TriageRecordStats(verified=False)
    if ledger_mod.live_decision_exists(
            conn, fingerprint=fingerprint, decided_by=decided_by, rule_version=prompt_version):
        stats.decision_skipped = True
    else:
        ledger_mod.append_decision(
            conn, fingerprint=fingerprint, category='unknown', confidence='low',
            decided_by=decided_by, rule_version=prompt_version, kind='llm', verified=False,
            evidence=json.dumps({'triage_error': reason}, sort_keys=True),
            decided_at=now, commit=False)
        stats.decision_appended = True

    if ledger_mod.pending_review_item_exists(conn, fingerprint=fingerprint, decided_by=decided_by):
        stats.review_skipped = True
    else:
        ledger_mod.append_review_item(
            conn, fingerprint=fingerprint, reason=reason,
            draft_category='unknown', draft_confidence='low',
            enqueued_at=now, priority=priority, commit=False)
        stats.review_appended = True

    conn.commit()
    return stats


def triaged_fingerprints(conn, *, model, prompt_version=triage_mod.PROMPT_VERSION) -> set:
    """Every fingerprint already triaged for this ``(model, prompt_version)``.

    One query, so the driver can filter the work-list BEFORE applying ``--limit``
    -- otherwise the limit is consumed re-scanning items already decided on a
    prior run (chiefly the needs-human backlog, which stays queued until a human
    reviews it). Returns the set of fingerprints with a live LLM decision.
    """
    rows = conn.execute(
        'SELECT DISTINCT fingerprint FROM decision '
        'WHERE decided_by = ? AND rule_version = ? AND superseded_at IS NULL',
        (_decided_by(model), prompt_version)).fetchall()
    return {row[0] for row in rows}


def already_triaged(conn, fingerprint, *, model, prompt_version=triage_mod.PROMPT_VERSION) -> bool:
    """Whether ``fingerprint`` already has a live LLM decision for this model.

    The driver checks this BEFORE calling the (slow, paid) model, so a resumed or
    re-run pass never re-triages a fingerprint that was already decided -- the
    idempotency that ``record_triage_result`` enforces at write time, lifted to
    skip the LLM call itself.
    """
    return ledger_mod.live_decision_exists(
        conn, fingerprint=fingerprint, decided_by=_decided_by(model), rule_version=prompt_version)
