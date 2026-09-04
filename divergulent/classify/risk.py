"""The security-risk gate (phase 4) -- a claim-blind, advisory risk score.

A cheap LLM pass that scores each carried patch's SECURITY RISK on a coarse
ordinal (``none < low < elevated < high``) from the diff alone, so the expensive
category pass and the human reviewer reach the scariest patches first. It is a
PRIORITISATION signal, not a verdict: it records a supersedable ``security-risk``
observation and never touches the verdict precedence, so -- unlike the category
tier -- it needs no adversarial verification.

The score carries full provenance: ``observed_by='risk-gate:<model>'`` /
``rule_version=RISK_PROMPT_VERSION``, mirroring the triage decisions, so a model
swap or prompt tweak is a new identity and old scores can be superseded and
re-scored.

The model backend is the SAME injected ``call(system, user, *, model) ->
CallResult`` boundary the triage tier uses (so it runs offline against a fake);
the real backend is ``triage.claude_cli_call``, cost-stripped.

Curation-side only: no client command imports ``classify/``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from divergulent.classify import content as content_mod
from divergulent.classify import generated as generated_mod
from divergulent.classify import injection as injection_mod
from divergulent.classify import ledger as ledger_mod
from divergulent.classify import measure
from divergulent.classify import reviewability as reviewability_mod
from divergulent.classify import triage as triage_mod
from divergulent.classify.triage import Usage

# ``triage_driver`` is imported LAZILY (inside the functions that need it):
# ``triage_driver`` imports this module for the risk-aware work-list ordering, so
# a module-level import here would be a cycle.

# ---------------------------------------------------------------------------
# Versioned constants -- the ledger keys the observation on (model,
# RISK_PROMPT_VERSION), exactly as triage keys decisions on (model, prompt).
# ---------------------------------------------------------------------------

RISK_PROMPT_VERSION = 2

# The coarse ordinal scale (rank order matters; higher == more concerning).
RISK_LEVELS = ('none', 'low', 'elevated', 'high')
RISK_RANK = {level: rank for rank, level in enumerate(RISK_LEVELS)}

# The observation kind and the source-id prefix recorded on the ledger row.
RISK_KIND = 'security-risk'
RISK_OBSERVED_BY_PREFIX = 'risk-gate:'

# The deterministic cull source (provably-benign patches scored 'none' without
# spending an LLM call), versioned independently of the LLM prompt.
RISK_CULL_OBSERVED_BY = 'risk-cull'
RISK_CULL_VERSION = 1

# The deterministic injection-skip source: a diff carrying injection-shaped text
# is never handed to the gate -- the model must not read instructions aimed at
# it -- so its risk is recorded here instead of being scored, and versioned
# independently of the LLM prompt.
RISK_INJECTION_OBSERVED_BY = 'risk-injection-skip'
RISK_INJECTION_VERSION = 1

# Opus was the bake-off pick: 100% recall / 0% false-alarm at the >=elevated cut
# vs Sonnet's 73%/3%. For a security gate, recall is the metric you cannot trade
# away, and the cost-stripped claude -p shape makes it affordable as a one-time
# pass. Sonnet is the cost-sensitive fallback (set --model).
DEFAULT_RISK_MODEL = 'claude-opus-4-8'

# When the gate cannot be scored from a response (no JSON, or an out-of-scale
# level), default to this level -- erring toward review, never burying a patch.
_PARSE_FAILURE_LEVEL = 'elevated'

# Cap the diff sent to the gate. A coarse security read needs only the head, and
# uncapped giant diffs were the run's cost spikes AND its context-overflow error
# (one 5.4 MB diff). ~10k tokens; truncation is recorded, never silent. The
# `oversized` reviewability tier skips the LLM entirely (S3) unless a small
# hand-written residue unlocks it, so this bites the `large` middle and whatever
# residue survives a projection.
RISK_MAX_DIFF_CHARS = 40_000


@dataclass(frozen=True)
class RiskScore:
    """One claim-blind security-risk read of a diff body.

    ``level`` is one of :data:`RISK_LEVELS`; ``rank`` is its 0..3 ordinal.
    ``usage`` is the call's token usage (telemetry); ``raw_response`` is kept as
    auditable evidence, since an LLM score is non-deterministic. ``truncated`` is
    True when the diff was capped before the call (``original_chars`` is then the
    pre-cap length), so the audit trail records that the score read only the head
    -- of whatever was handed to the cap, which for a marked fingerprint is the
    projection, not the raw diff.

    ``projection`` is the residue-first reordering applied BEFORE the cap when the
    fingerprint carried a live ``generated-content`` mark, and ``None`` when it
    carried none -- the unmarked majority, whose input and recorded evidence stay
    byte-identical to before the mark existed.
    """

    level: str
    rank: int
    reason: str
    model: str
    prompt_version: int
    raw_response: str
    usage: Usage = Usage()
    truncated: bool = False
    original_chars: int = 0
    projection: generated_mod.ProjectedDiff | None = None


def risk_system_prompt(*, prompt_version: int = RISK_PROMPT_VERSION) -> str:
    """The static, cacheable security-risk rubric -- the system prompt.

    Version 2 (recalibrated 2026-06): keys on what the CHANGE does, not which
    file or subsystem it sits in. A mechanical change next to security-sensitive
    code is ``low``; ``elevated`` is reserved for a change that plausibly ALTERS
    a security mechanism (input/bounds validation, sizing, auth, crypto,
    privilege, hardening), ``high`` for one that plausibly introduces or weakens
    a vulnerability. v1 keyed on the *surface* ("touches a sensitive area ->
    elevated, generously" + "round up when unsure"), which -- on a representative
    sample -- turned model uncertainty into a pile on ``elevated`` AND still
    missed real ``security`` patches (8/10 recall, scoring two ``low``). v2
    restored recall (10/10 >=elevated) and reserved ``high`` (slice high 8->4)
    without inflating the already well-calibrated middle (doc->none, packaging->
    low, residue ~18% elevated). Constant for a fixed ``prompt_version`` (no
    diff), so it is the cache prefix; the diff is the variable user message.
    """
    return (
        'You assess the SECURITY RISK of a single Debian patch from its DIFF '
        'alone, for triage. (risk prompt version %d)\n'
        '\n'
        "You are given ONLY the diff body, never the author's description; judge "
        'only from the code the diff adds and removes.\n'
        '\n'
        'Score how likely THIS CHANGE is to have a NEGATIVE security impact. '
        'Judge what the change DOES to the code, not merely which file or '
        'subsystem it sits in. A change that sits in security-relevant code but '
        'only renames, refactors, reformats, adds logging, or adjusts build '
        'plumbing is LOW -- proximity to a sensitive area is not itself risk.\n'
        '\n'
        '  none: no security relevance at all (docs, comments, translations, '
        'changelog, copyright, whitespace, metadata).\n'
        '  low: ordinary code or build change whose behaviour has no '
        'security-relevant effect -- INCLUDING mechanical changes (refactor, '
        'rename, formatting, logging, version bump, portability shim, build '
        'plumbing) even when they sit next to security-sensitive code.\n'
        '  elevated: the change PLAUSIBLY ALTERS a security-relevant behaviour -- '
        'it modifies input/bounds/length/format validation, allocation or buffer '
        'sizing, integer/overflow handling, authentication or permission logic, '
        'cryptographic parameters or routines, privilege or sandbox handling, '
        'escaping/quoting of untrusted data, or a build-hardening flag.\n'
        '  high: the change PLAUSIBLY INTRODUCES OR WORSENS a vulnerability, or '
        'removes/weakens an existing check or hardening.\n'
        '\n'
        'Decide on the mechanism the change actually engages. Most patches are '
        'low. Reserve elevated/high for a change that touches a security '
        'MECHANISM, not just a sensitive neighbourhood. When genuinely torn '
        'between two adjacent levels for a change that DOES engage a security '
        'mechanism, pick the higher one.\n'
        '\n'
        'Respond with STRICT JSON only: '
        '{"risk":"none|low|elevated|high","reason":"<=20 words"}\n'
    ) % prompt_version


def risk_user_message(diff_body: str) -> str:
    """The variable per-patch user message: the diff body, framed."""
    return 'Diff body:\n\n%s\n' % diff_body


def _parse_risk(text: str) -> tuple[str, str]:
    """Extract ``(level, reason)`` from a gate response; recall-safe on failure.

    Robust to fences / prose via the shared JSON extractor. A response with no
    usable JSON, or an out-of-scale ``risk`` value, degrades to ``elevated`` (NOT
    ``none``) with a noted reason -- a patch the gate could not score is routed
    for review, never silently buried.
    """
    data = triage_mod._first_json_object(text)
    if data is None:
        return (_PARSE_FAILURE_LEVEL, 'gate response had no usable JSON object; routed for review')
    risk = data.get('risk')
    reason = data.get('reason') if isinstance(data.get('reason'), str) else ''
    if risk not in RISK_RANK:
        return (_PARSE_FAILURE_LEVEL,
                ('gate returned out-of-scale risk %r; routed for review. %s' % (risk, reason)).strip())
    return (risk, reason)


def score_risk(patch_text: str, *, call, model: str = DEFAULT_RISK_MODEL,
               prompt_version: int = RISK_PROMPT_VERSION,
               max_diff_chars: int = RISK_MAX_DIFF_CHARS,
               mark_files: list[dict] | None = None) -> RiskScore:
    """Score one patch's security risk with a claim-blind LLM read.

    Extracts the claim-blind ``diff_body`` (so the author's framing never reaches
    the model), CAPS it to ``max_diff_chars`` (a coarse read needs only the head;
    truncation is recorded, never silent), builds the cacheable rubric system
    prompt + the diff user message, invokes ``call(system, user, *, model) ->
    CallResult`` (the injectable boundary the triage tier uses), parses the JSON,
    and returns a :class:`RiskScore` carrying the level, the raw response
    (evidence), the call's token ``usage`` and whether the diff was truncated.
    ``call`` is required so the function is pure given a fake; the test suite
    never touches the network.

    ``max_diff_chars`` is CLAMPED to ``injection.MAX_SCAN_CHARS``: the model is
    never shown text the injection tripwire did not screen, whatever the operator
    passes.  A non-positive value means the screen bound here, not "no cap".

    ``mark_files`` is the per-file evidence of the fingerprint's live
    ``generated-content`` mark (``generated_marks(conn)[fp]['files']``), passed by
    the driver for a MARKED fingerprint and left ``None`` for every other one.
    When given, the body is projected residue-first
    (:func:`generated.project_residue_first`) BETWEEN the claim strip and the cap,
    so the cap spends its budget on the hand-written residue instead of on
    generator output, and the model is told in the text what it is not being
    shown. Order matters and is the whole point: project, then cap. An unmarked
    call is byte-identical to before the mark existed -- ``mark_files=None`` does
    not even build a projection.
    """
    # The screen bound is not negotiable by a flag. The tripwire guarantees that the
    # first MAX_SCAN_CHARS of a body (and, for a marked one, of its projection) were
    # looked at -- nothing past that. cap_diff treats a non-positive max as "no cap"
    # and the CLI advertised "0 disables", so an operator could hand the model a
    # multi-megabyte tail no scanner ever read, silently turning off the invariant
    # both LLM tiers rely on. 0 now means the screen bound, and a larger value is
    # clamped down to it; the default 40,000 is far below either way.
    if max_diff_chars <= 0 or max_diff_chars > injection_mod.MAX_SCAN_CHARS:
        max_diff_chars = injection_mod.MAX_SCAN_CHARS

    body = triage_mod.diff_body(patch_text)
    projection = None
    if mark_files:
        projection = generated_mod.project_residue_first(body, mark_files)
        body = projection.text
    capped, truncated, original_chars = triage_mod.cap_diff(body, max_diff_chars)
    system = risk_system_prompt(prompt_version=prompt_version)
    user = risk_user_message(capped)

    result = call(system, user, model=model)
    level, reason = _parse_risk(result.text)

    return RiskScore(
        level=level, rank=RISK_RANK[level], reason=reason, model=model,
        prompt_version=prompt_version, raw_response=result.text, usage=result.usage,
        truncated=truncated, original_chars=original_chars, projection=projection)


def record_risk_observation(conn, fingerprint: str, score: RiskScore, *, now: str,
                            commit: bool = True) -> int:
    """Record ``score`` as the fingerprint's live ``security-risk`` observation.

    Supersedes any prior live ``security-risk`` observation for the fingerprint
    (from ANY source) so exactly one is live -- the current risk level -- then
    appends the new one keyed ``observed_by='risk-gate:<model>'`` /
    ``rule_version=<prompt_version>``.  Append-only: superseded rows stay as the
    audit trail.  ``now`` is caller-supplied (this module reads no clock).

    The model's input is never silently modified, so a score built from a
    residue-first projection records ``projected`` and what the projection left
    out.  The key is written for EVERY marked fingerprint, including the one whose
    projection was a no-op (``projected: false``) -- its presence is what tells the
    re-risk selection this score has already been through the projecting gate (see
    :func:`rerisk_candidates`).  An unmarked fingerprint's payload gains no key at
    all: it is byte-identical to what this function wrote before phase 3.
    """
    observed_by = RISK_OBSERVED_BY_PREFIX + score.model
    ledger_mod.supersede_observations_for_fingerprint(
        conn, fingerprint=fingerprint, kind=RISK_KIND, superseded_at=now, commit=False)
    payload = {'level': score.level, 'reason': score.reason, 'raw_response': score.raw_response}
    if score.truncated:
        payload['truncated'] = True
        payload['original_chars'] = score.original_chars
    if score.projection is not None:
        payload['projected'] = score.projection.projected
        if score.projection.projected:
            payload['omitted_files'] = score.projection.omitted_files
            payload['omitted_changed'] = score.projection.omitted_changed
    evidence = json.dumps(payload, sort_keys=True)
    return ledger_mod.append_observation(
        conn, fingerprint=fingerprint, kind=RISK_KIND, detail=score.level,
        evidence=evidence, observed_by=observed_by, rule_version=score.prompt_version,
        observed_at=now, commit=commit)


def risk_rank_by_fingerprint(conn) -> dict[str, int]:
    """``{fingerprint: rank}`` from the live ``security-risk`` observations.

    The current risk rank (0..3) per fingerprint -- the prioritisation input for
    the category pass and the review queue.  A fingerprint with no live
    ``security-risk`` observation is absent (treat as un-scored / lowest priority).
    """
    ranks: dict[str, int] = {}
    for obs in ledger_mod.live_observations(conn):
        if obs['kind'] == RISK_KIND and obs['detail'] in RISK_RANK:
            ranks[obs['fingerprint']] = RISK_RANK[obs['detail']]
    return ranks


def risk_level_by_fingerprint(conn) -> dict[str, str]:
    """``{fingerprint: level}`` from the live ``security-risk`` observations.

    The level string (``none``/``low``/``elevated``/``high``) per fingerprint --
    the human-facing form of :func:`risk_rank_by_fingerprint`, for display (e.g.
    the review-UI badge). A fingerprint with no live score is absent.
    """
    levels: dict[str, str] = {}
    for obs in ledger_mod.live_observations(conn):
        if obs['kind'] == RISK_KIND and obs['detail'] in RISK_RANK:
            levels[obs['fingerprint']] = obs['detail']
    return levels


def rerisk_candidates(conn) -> set[str]:
    """Fingerprints whose live risk score was read off a truncated GENERATED head.

    The bounded re-risk population: a fingerprint carrying a live
    ``generated-content`` mark AND a live ``security-risk`` observation whose
    evidence records ``truncated: true`` -- exactly the scores the cap computed
    from generator output because nothing projected the residue first (gatos
    scored ``elevated`` off 40k characters of ``configure``).  A truncated score
    with no mark is honestly truncated code and is left alone; a marked
    fingerprint whose score was never truncated already read its whole diff.

    The TERMINATION GUARD is the presence of the ``projected`` key, not its value.
    A score that has been through the projecting gate records ``projected``
    whether the projection reordered anything or not, so a mark whose paths do not
    appear in this body (projection is an identity, and the diff may still be long
    enough to cap) drops out of the population after one pass instead of being
    re-selected on every run for ever.  Excluding only ``projected: true`` would
    not terminate.

    A row whose evidence is missing or unparseable is skipped, the same defensive
    posture ``generated_marks`` takes: the ledger is append-only operator data and
    one bad row must not decide to spend LLM calls.
    """
    marks = generated_mod.generated_marks(conn)
    candidates: set[str] = set()
    for obs in ledger_mod.live_observations(conn):
        if obs['kind'] != RISK_KIND or obs['fingerprint'] not in marks:
            continue
        try:
            payload = json.loads(obs['evidence'])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get('truncated') is True and 'projected' not in payload:
            candidates.add(obs['fingerprint'])
    return candidates


# ---------------------------------------------------------------------------
# The security-safe deterministic cull.
#
# Patches a deterministic, conservative check can prove carry no security risk
# are scored 'none' WITHOUT spending an LLM call. The predicate must NEVER cull
# something risky -- it is narrower than the packaging category (a debian/rules
# change can flip a build-hardening flag), and every sub-check is conservative
# (false whenever unsure).
# ---------------------------------------------------------------------------

# Non-code data files that are provably benign by path: translation catalogues
# and changelog/copyright metadata.
_BENIGN_DATA_SUFFIXES = ('.po', '.pot')
_BENIGN_DATA_BASENAMES = ('changelog', 'copyright')


def _benign_data_path(path: str) -> bool:
    lowered = path.lower()
    if lowered.endswith(_BENIGN_DATA_SUFFIXES):
        return True
    return lowered.rsplit('/', 1)[-1] in _BENIGN_DATA_BASENAMES


def provably_benign(patch_text: str) -> str | None:
    """A short reason if the patch is provably security-irrelevant, else ``None``.

    Uses the phase-2 :func:`content.profile` (built from the diff, never the
    claim). Every check is conservative -- a change that does not execute
    (empty/whitespace/comment-only) or touches only documentation or
    translation/changelog/copyright metadata. Anything that touches code, build
    files, or other data is NOT culled -- it goes to the LLM gate.
    """
    profile = content_mod.profile(patch_text)
    if profile.is_empty:
        return 'empty / mode-only change (no executable content)'
    if profile.whitespace_only:
        return 'whitespace-only change'
    if profile.comment_only:
        return 'comment-only change'
    if not profile.touches_code:
        if set(profile.file_types) <= {'doc'}:
            return 'documentation-only change'
        if profile.files and all(_benign_data_path(path) for path, _ in profile.files):
            return 'translation/changelog/copyright-only change'
    return None


def record_cull(conn, fingerprint: str, reason: str, *, now: str, commit: bool = True) -> int:
    """Record a deterministic ``none`` risk for a provably-benign patch.

    Mirrors :func:`record_risk_observation` (supersede any prior live risk row,
    then append) but keyed to the deterministic cull source
    ``observed_by='risk-cull'`` / ``rule_version=RISK_CULL_VERSION`` -- so a
    culled 'none' is distinguishable from an LLM 'none' in the audit trail.
    """
    ledger_mod.supersede_observations_for_fingerprint(
        conn, fingerprint=fingerprint, kind=RISK_KIND, superseded_at=now, commit=False)
    evidence = json.dumps({'level': 'none', 'reason': reason, 'culled': True}, sort_keys=True)
    return ledger_mod.append_observation(
        conn, fingerprint=fingerprint, kind=RISK_KIND, detail='none', evidence=evidence,
        observed_by=RISK_CULL_OBSERVED_BY, rule_version=RISK_CULL_VERSION,
        observed_at=now, commit=commit)


def record_injection_skip(conn, fingerprint: str, families: str, *, now: str,
                          commit: bool = True) -> int:
    """Record the recall-safe risk for a diff the gate must never be shown.

    Mirrors :func:`record_cull` (supersede any prior live risk row, then append)
    but at :data:`_PARSE_FAILURE_LEVEL` -- the same disposition this module
    already gives a patch the gate could not score -- and keyed to the
    deterministic source ``observed_by='risk-injection-skip'`` /
    ``rule_version=RISK_INJECTION_VERSION``, so a skipped score is
    distinguishable from an LLM one in the audit trail.  The evidence names the
    families that fired and says plainly that the model did not score it, exactly
    as the triage driver's needs-human reason string does.

    Superseding matters here rather than being mere hygiene: a prior LIVE score
    on such a fingerprint was read off attacker-authored text aimed at the model
    that produced it, so it is precisely the score not to trust.
    """
    ledger_mod.supersede_observations_for_fingerprint(
        conn, fingerprint=fingerprint, kind=RISK_KIND, superseded_at=now, commit=False)
    reason = ('llm-injection-suspect (%s): not sent to the LLM; the model did not score it, '
              'recorded %s for review' % (families, _PARSE_FAILURE_LEVEL))
    evidence = json.dumps(
        {'level': _PARSE_FAILURE_LEVEL, 'reason': reason, 'injection_suspect': True},
        sort_keys=True)
    return ledger_mod.append_observation(
        conn, fingerprint=fingerprint, kind=RISK_KIND, detail=_PARSE_FAILURE_LEVEL,
        evidence=evidence, observed_by=RISK_INJECTION_OBSERVED_BY,
        rule_version=RISK_INJECTION_VERSION, observed_at=now, commit=commit)


def injection_skipped_fingerprints(conn) -> set[str]:
    """Fingerprints whose LIVE risk row is the deterministic injection skip.

    The termination guard for :func:`run_risk_gate`'s skip pass: once a suspect
    carries this row it is dispositioned, so the next run neither re-records it
    nor re-selects it.  A suspect whose live row came from anywhere else (an LLM
    score written before the skip existed) is absent, and is healed on the next
    run.
    """
    return {obs['fingerprint'] for obs in ledger_mod.live_observations(conn)
            if obs['kind'] == RISK_KIND and obs['observed_by'] == RISK_INJECTION_OBSERVED_BY}


# ---------------------------------------------------------------------------
# The bounded cascade driver.
# ---------------------------------------------------------------------------

@dataclass
class RiskRunStats:
    """What one bounded :func:`run_risk_gate` pass did, honest about the cap."""

    queue_size: int = 0
    scored: int = 0       # went through the LLM gate
    culled: int = 0       # provably-benign, scored 'none' deterministically
    errored: int = 0      # the backend raised -> recorded 'elevated' (recall-safe)
    truncated: int = 0    # diff capped before the call (head-only read)
    skipped_oversized: int = 0  # not line-reviewable -> never sent to the LLM
    skipped_injection: int = 0  # injection-suspect diff -> never sent to the LLM -> elevated
    unlocked_by_residue: int = 0  # oversized, but a small residue -> scored after all
    projected: int = 0    # marked -> scored residue-first, generated files as notes
    re_risked: int = 0    # re-scored by the targeted --re-risk-marked pass
    reprioritised: int = 0      # pending review items re-stamped from current risk
    by_level: dict[str, int] = field(default_factory=dict)
    unscored_remaining: int = 0
    usage: Usage = field(default_factory=Usage)
    model: str = DEFAULT_RISK_MODEL


def run_risk_gate(conn, corpus_dir: str, index_path: str, *, call, now: str, limit: int,
                  model: str = DEFAULT_RISK_MODEL, max_diff_chars: int = RISK_MAX_DIFF_CHARS,
                  progress=None, re_risk_marked: bool = False) -> RiskRunStats:
    """Score a BOUNDED slice of the WHOLE corpus's security risk; record each.

    Scores EVERY fingerprint (``scope='all'``), not just the residue: a patch the
    deterministic tier settled as ``packaging``/``documentation`` can still be
    security-relevant (a ``debian/rules`` hardening-flag change is the classic
    case), so the security axis is independent of the category. Skips fingerprints
    that already carry a live ``security-risk`` observation, takes the first
    ``limit``, and for each: applies the **security-safe cull** (provably-benign ->
    ``none`` deterministically, no LLM -- which does real work here on the settled-
    benign bulk, ~7% of the corpus) or scores it via the injected ``call``. A
    backend failure records ``elevated`` (recall-safe) and is counted.
    ``call``/``now`` are injected so the path is offline and deterministic.

    A fingerprint with a live ``generated-content`` mark is scored on a
    residue-first PROJECTION of its diff (the marks are read once per run, never
    once per call), so the cap reads hand-written residue and the model is told
    what it is not being shown; the projection facts land in the observation's
    evidence. Unmarked fingerprints -- the overwhelming majority -- are unchanged
    down to the byte.

    A fingerprint whose DIFF carries injection-shaped text is never handed to the
    gate: the tripwire's whole point is that instructions aimed at a model are not
    fed to it, and a payload steering the gate to ``none`` would both ship that
    value and sink the patch to the bottom of the human queue.  Such a fingerprint
    leaves the pending set before any call is made and is recorded
    deterministically at the recall-safe level instead
    (:func:`record_injection_skip`), so it is dispositioned rather than left
    un-scored and re-selected on every run -- and any live score already read off
    that text is superseded the first time this runs.  Exactly the triage driver's
    check, through the same helper, so the two consumers can never disagree.

    That disposition is written for EVERY corpus suspect, whatever mode the run is
    in -- it costs no LLM call, so ``limit`` does not bound it, and neither does
    ``re_risk_marked``'s narrower selection.  Deliberate: a suspect is a suspect
    whichever pass happens to notice it, and leaving one un-dispositioned because
    the operator asked for a targeted re-risk would let a payload keep its stale
    score.  Two consequences worth knowing.  A suspect that is ALSO oversized-and-
    locked used to carry no risk row at all (its ``reviewability`` observation was
    its whole disposition) and now carries the ``elevated`` one; and because the
    summary counts each population separately, ``skipped_oversized`` and
    ``skipped_injection`` can both count the same fingerprint, so those lines may
    sum to more than the slice.

    ``re_risk_marked`` switches the run to the TARGETED re-risk population instead
    of the un-scored one: the marked fingerprints whose live score was read off a
    truncated generated head (:func:`rerisk_candidates`), re-scored through this
    same now-projecting flow. It is off by default, and the default run's
    behaviour is unchanged apart from the projection. Nothing is deleted -- a
    re-score supersedes, exactly as any other re-score does.
    """
    from divergulent.classify import triage_driver  # lazy: avoids an import cycle
    work = triage_driver.build_work_list(conn, index_path, scope='all')
    marks = generated_mod.generated_marks(conn)
    # Oversized diffs are not line-reviewable and overflow the model: skip them
    # entirely (the reviewability=oversized observation IS their disposition), so
    # no LLM call is spent and the work list does not re-select them every run --
    # EXCEPT those the shared unlock helper says have a small hand-written residue,
    # which the projection now makes perfectly scorable. Same helper as the triage
    # driver's, so the two consumers can never disagree about who is unlocked.
    oversized = reviewability_mod.oversized_fingerprints(conn)
    unlocked = generated_mod.residue_unlocked_fingerprints(conn)
    locked = oversized - unlocked
    # The prompt-injection tripwire, read through the SAME helper the triage
    # driver uses (diff region only -- the model never reads the header): these
    # never reach the model, whatever else is true of them.  The injection skip
    # outranks the residue unlock, exactly as it does in the driver.
    suspects = injection_mod.injection_suspect_fingerprints(
        conn, region=injection_mod.DIFF_REGION)

    stats = RiskRunStats(queue_size=len(work), model=model)
    corpus = {item.fingerprint for item in work}
    stats.skipped_oversized = len(locked & corpus)
    stats.unlocked_by_residue = len(unlocked & corpus)

    # Disposition every suspect in the corpus that is not already dispositioned,
    # BEFORE the selection below drops them: a bare skip would leave them
    # un-scored, re-selected every run, and -- because risk is the top
    # prioritisation band -- parked at the bottom of the human review queue.
    # No LLM call is spent, so this is not bounded by ``limit``; it terminates
    # because the row it writes is its own guard.
    stats.skipped_injection = len(suspects & corpus)
    if stats.skipped_injection:
        families = injection_mod.injection_by_fingerprint(conn)
        for fingerprint in sorted((suspects & corpus) - injection_skipped_fingerprints(conn)):
            record_injection_skip(conn, fingerprint, families.get(fingerprint, ''),
                                  now=now, commit=False)

    if re_risk_marked:
        targets = rerisk_candidates(conn)
        pending = [item for item in work
                   if item.fingerprint in targets and item.fingerprint not in locked
                   and item.fingerprint not in suspects]
    else:
        scored = set(risk_rank_by_fingerprint(conn))
        pending = [item for item in work
                   if item.fingerprint not in scored and item.fingerprint not in locked
                   and item.fingerprint not in suspects]
    selected = pending[:limit]
    stats.unscored_remaining = max(len(pending) - len(selected), 0)
    if re_risk_marked:
        stats.re_risked = len(selected)
        if progress is not None:
            progress('re-risking %d marked fingerprints whose scores read a truncated '
                     'generated head' % len(selected))

    for position, item in enumerate(selected, start=1):
        body = measure.read_body(corpus_dir, item.representative_sha)
        cull_reason = provably_benign(body)
        if cull_reason is not None:
            record_cull(conn, item.fingerprint, cull_reason, now=now, commit=False)
            stats.culled += 1
            level = 'none'
        else:
            mark = marks.get(item.fingerprint)
            try:
                score = score_risk(body, call=call, model=model, max_diff_chars=max_diff_chars,
                                   mark_files=None if mark is None else mark['files'])
            except Exception as exc:  # noqa: BLE001 -- one bad patch must not abort the run
                score = RiskScore(
                    level='elevated', rank=RISK_RANK['elevated'],
                    reason='risk gate failed: %s' % exc, model=model,
                    prompt_version=RISK_PROMPT_VERSION, raw_response='')
                stats.errored += 1
            record_risk_observation(conn, item.fingerprint, score, now=now, commit=False)
            stats.usage = stats.usage + score.usage
            stats.scored += 1
            if score.truncated:
                stats.truncated += 1
            if score.projection is not None and score.projection.projected:
                stats.projected += 1
            level = score.level
        stats.by_level[level] = stats.by_level.get(level, 0) + 1
        if progress is not None:
            progress('[%d/%d] %s -> %s' % (position, len(selected), item.fingerprint[:12], level))

    conn.commit()
    # Re-stamp the review queue from the now-updated risk scores: a patch scored
    # after it was already queued must reach the queue order, not stay frozen at
    # its enqueue-time (risk_rank 0) priority.  Heals the whole pending queue, not
    # just the items scored this run.
    stats.reprioritised = triage_driver.reprioritise_review_queue(conn, index_path)
    return stats


def print_risk_summary(stats: RiskRunStats) -> None:
    """Print a lean, honest summary of one risk-gate run (the cap is loud)."""
    if stats.re_risked:
        print('re-risking %d marked fingerprints whose scores read a truncated generated head'
              % stats.re_risked)
    print('risk gate: scored %d, culled %d (provably benign), errored %d; %d corpus, %d un-scored remain' % (
        stats.scored, stats.culled, stats.errored, stats.queue_size, stats.unscored_remaining))
    if stats.skipped_oversized:
        print('  (%d oversized skipped -- not line-reviewable, no LLM; see the review UI)'
              % stats.skipped_oversized)
    if stats.skipped_injection:
        print('  (%d injection-suspect skipped -- never sent to the LLM; recorded %s for review)'
              % (stats.skipped_injection, _PARSE_FAILURE_LEVEL))
    if stats.unlocked_by_residue:
        print('  (%d oversized unlocked by a small hand-written residue -- scored residue-first)'
              % stats.unlocked_by_residue)
    if stats.projected:
        print('  (%d scored residue-first -- generated files replaced by a note, recorded in evidence)'
              % stats.projected)
    if stats.truncated:
        print('  (%d scored on a truncated diff -- head only, capped for cost)' % stats.truncated)
    if stats.reprioritised:
        print('  (%d pending review items re-prioritised from current risk)' % stats.reprioritised)
    if stats.by_level:
        order = {level: rank for rank, level in enumerate(RISK_LEVELS)}
        for level in sorted(stats.by_level, key=lambda lvl: order.get(lvl, 99), reverse=True):
            print('  %-9s %d' % (level, stats.by_level[level]))
    if stats.scored:
        from divergulent.classify import triage_driver  # lazy: avoids an import cycle
        ratio = triage_driver.cache_hit_ratio(stats.usage)
        reported = '' if stats.usage.cost_usd is None else ' reported=$%.4f' % stats.usage.cost_usd
        print('cost & cache: out=%d cache-hit=%s;%s est=$%.4f (~$%.4f/scored)' % (
            stats.usage.output_tokens, 'n/a' if ratio is None else '%.0f%%' % (ratio * 100),
            reported, triage_driver.derived_cost_usd(stats.usage, stats.model),
            triage_driver.derived_cost_usd(stats.usage, stats.model) / stats.scored))


# The risk gate is cheap per call, so a larger default slice than triage's is
# reasonable; the operator still caps it.
RISK_DEFAULT_LIMIT = 50


def main(argv=None) -> int:
    """``python -m divergulent.classify.risk``: score the residue's security risk.

    Runs a bounded :func:`run_risk_gate` against the REAL cost-stripped
    ``claude -p`` backend, recording a supersedable ``security-risk`` observation
    per fingerprint. Reads the clock ONCE (this is the only place that does) and
    threads it down. Records no decision and rebuilds no verdict -- the score is
    advisory and only reorders the review/triage queue (highest risk first).

    ``--re-risk-marked`` runs the targeted re-risk pass instead of the ordinary
    un-scored one: the marked fingerprints whose score was read off a truncated
    generated head, re-scored residue-first. Bounded by ``--limit`` like every
    other run, and supersede-only.
    """
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(
        prog='python -m divergulent.classify.risk',
        description="Score the residue's security risk to prioritise triage/review.")
    parser.add_argument('ledger', help='path to the ledger sqlite')
    parser.add_argument('corpus_dir', help='path to the corpus directory (bodies + index)')
    parser.add_argument('--index', default=None,
                        help='path to fingerprints.sqlite (default: <corpus>/fingerprints.sqlite)')
    parser.add_argument('--limit', type=int, default=RISK_DEFAULT_LIMIT,
                        help='how many un-scored residue patches to score (default: %d)'
                             % RISK_DEFAULT_LIMIT)
    parser.add_argument('--model', default=DEFAULT_RISK_MODEL,
                        help='model for the gate (default: %s)' % DEFAULT_RISK_MODEL)
    parser.add_argument('--max-diff-chars', type=int, default=RISK_MAX_DIFF_CHARS,
                        help='cap the diff sent to the gate, head only (default: %d; 0 or any '
                             'value above the injection screen bound of %d means that bound, so '
                             'the model is never shown unscreened text)'
                             % (RISK_MAX_DIFF_CHARS, injection_mod.MAX_SCAN_CHARS))
    parser.add_argument('--re-risk-marked', action='store_true',
                        help='instead of scoring un-scored patches, RE-score the marked ones whose '
                             'current score was read off a truncated generated head (supersedes, '
                             'never deletes)')
    args = parser.parse_args(argv)

    index_path = args.index or os.path.join(args.corpus_dir, 'fingerprints.sqlite')
    conn = ledger_mod.open_ledger(args.ledger)
    try:
        stats = run_risk_gate(
            conn, args.corpus_dir, index_path, call=triage_mod.claude_cli_call,
            now=triage_mod._cli_now(), limit=args.limit, model=args.model,
            max_diff_chars=args.max_diff_chars, re_risk_marked=args.re_risk_marked,
            progress=lambda message: print(message, file=sys.stderr, flush=True))
    finally:
        conn.close()

    print_risk_summary(stats)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
