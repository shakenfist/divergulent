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
from divergulent.classify import ledger as ledger_mod
from divergulent.classify import measure
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

# Opus was the bake-off pick: 100% recall / 0% false-alarm at the >=elevated cut
# vs Sonnet's 73%/3%. For a security gate, recall is the metric you cannot trade
# away, and the cost-stripped claude -p shape makes it affordable as a one-time
# pass. Sonnet is the cost-sensitive fallback (set --model).
DEFAULT_RISK_MODEL = 'claude-opus-4-8'

# When the gate cannot be scored from a response (no JSON, or an out-of-scale
# level), default to this level -- erring toward review, never burying a patch.
_PARSE_FAILURE_LEVEL = 'elevated'


@dataclass(frozen=True)
class RiskScore:
    """One claim-blind security-risk read of a diff body.

    ``level`` is one of :data:`RISK_LEVELS`; ``rank`` is its 0..3 ordinal.
    ``usage`` is the call's token usage (telemetry); ``raw_response`` is kept as
    auditable evidence, since an LLM score is non-deterministic.
    """

    level: str
    rank: int
    reason: str
    model: str
    prompt_version: int
    raw_response: str
    usage: Usage = Usage()


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
               prompt_version: int = RISK_PROMPT_VERSION) -> RiskScore:
    """Score one patch's security risk with a claim-blind LLM read.

    Extracts the claim-blind ``diff_body`` (so the author's framing never reaches
    the model), builds the cacheable rubric system prompt + the diff user message,
    invokes ``call(system, user, *, model) -> CallResult`` (the injectable
    boundary the triage tier uses), parses the JSON, and returns a
    :class:`RiskScore` carrying the level, the raw response (evidence) and the
    call's token ``usage``.  ``call`` is required so the function is pure given a
    fake; the test suite never touches the network.
    """
    body = triage_mod.diff_body(patch_text)
    system = risk_system_prompt(prompt_version=prompt_version)
    user = risk_user_message(body)

    result = call(system, user, model=model)
    level, reason = _parse_risk(result.text)

    return RiskScore(
        level=level, rank=RISK_RANK[level], reason=reason, model=model,
        prompt_version=prompt_version, raw_response=result.text, usage=result.usage)


def record_risk_observation(conn, fingerprint: str, score: RiskScore, *, now: str,
                            commit: bool = True) -> int:
    """Record ``score`` as the fingerprint's live ``security-risk`` observation.

    Supersedes any prior live ``security-risk`` observation for the fingerprint
    (from ANY source) so exactly one is live -- the current risk level -- then
    appends the new one keyed ``observed_by='risk-gate:<model>'`` /
    ``rule_version=<prompt_version>``.  Append-only: superseded rows stay as the
    audit trail.  ``now`` is caller-supplied (this module reads no clock).
    """
    observed_by = RISK_OBSERVED_BY_PREFIX + score.model
    ledger_mod.supersede_observations_for_fingerprint(
        conn, fingerprint=fingerprint, kind=RISK_KIND, superseded_at=now, commit=False)
    evidence = json.dumps(
        {'level': score.level, 'reason': score.reason, 'raw_response': score.raw_response},
        sort_keys=True)
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
    by_level: dict[str, int] = field(default_factory=dict)
    unscored_remaining: int = 0
    usage: Usage = field(default_factory=Usage)
    model: str = DEFAULT_RISK_MODEL


def run_risk_gate(conn, corpus_dir: str, index_path: str, *, call, now: str, limit: int,
                  model: str = DEFAULT_RISK_MODEL, progress=None) -> RiskRunStats:
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
    """
    from divergulent.classify import triage_driver  # lazy: avoids an import cycle
    work = triage_driver.build_work_list(conn, index_path, scope='all')
    scored = set(risk_rank_by_fingerprint(conn))
    pending = [item for item in work if item.fingerprint not in scored]
    selected = pending[:limit]

    stats = RiskRunStats(queue_size=len(work), model=model)
    stats.unscored_remaining = max(len(pending) - len(selected), 0)

    for position, item in enumerate(selected, start=1):
        body = measure.read_body(corpus_dir, item.representative_sha)
        cull_reason = provably_benign(body)
        if cull_reason is not None:
            record_cull(conn, item.fingerprint, cull_reason, now=now, commit=False)
            stats.culled += 1
            level = 'none'
        else:
            try:
                score = score_risk(body, call=call, model=model)
            except Exception as exc:  # noqa: BLE001 -- one bad patch must not abort the run
                score = RiskScore(
                    level='elevated', rank=RISK_RANK['elevated'],
                    reason='risk gate failed: %s' % exc, model=model,
                    prompt_version=RISK_PROMPT_VERSION, raw_response='')
                stats.errored += 1
            record_risk_observation(conn, item.fingerprint, score, now=now, commit=False)
            stats.usage = stats.usage + score.usage
            stats.scored += 1
            level = score.level
        stats.by_level[level] = stats.by_level.get(level, 0) + 1
        if progress is not None:
            progress('[%d/%d] %s -> %s' % (position, len(selected), item.fingerprint[:12], level))

    conn.commit()
    return stats


def print_risk_summary(stats: RiskRunStats) -> None:
    """Print a lean, honest summary of one risk-gate run (the cap is loud)."""
    print('risk gate: scored %d, culled %d (provably benign), errored %d; %d corpus, %d un-scored remain' % (
        stats.scored, stats.culled, stats.errored, stats.queue_size, stats.unscored_remaining))
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
    args = parser.parse_args(argv)

    index_path = args.index or os.path.join(args.corpus_dir, 'fingerprints.sqlite')
    conn = ledger_mod.open_ledger(args.ledger)
    try:
        stats = run_risk_gate(
            conn, args.corpus_dir, index_path, call=triage_mod.claude_cli_call,
            now=triage_mod._cli_now(), limit=args.limit, model=args.model,
            progress=lambda message: print(message, file=sys.stderr, flush=True))
    finally:
        conn.close()

    print_risk_summary(stats)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
