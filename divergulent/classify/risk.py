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
from dataclasses import dataclass

from divergulent.classify import ledger as ledger_mod
from divergulent.classify import triage as triage_mod
from divergulent.classify.triage import Usage

# ---------------------------------------------------------------------------
# Versioned constants -- the ledger keys the observation on (model,
# RISK_PROMPT_VERSION), exactly as triage keys decisions on (model, prompt).
# ---------------------------------------------------------------------------

RISK_PROMPT_VERSION = 1

# The coarse ordinal scale (rank order matters; higher == more concerning).
RISK_LEVELS = ('none', 'low', 'elevated', 'high')
RISK_RANK = {level: rank for rank, level in enumerate(RISK_LEVELS)}

# The observation kind and the source-id prefix recorded on the ledger row.
RISK_KIND = 'security-risk'
RISK_OBSERVED_BY_PREFIX = 'risk-gate:'

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

    Validated against the corpus (bake-off 2026-06): ``elevated`` fires on any
    security-sensitive *surface* even when the change looks benign, ``high`` is
    reserved for a *plausible* vulnerability, and the gate is recall-biased
    ("prefer the higher level when unsure"). Constant for a fixed
    ``prompt_version`` (no diff), so it is the cache prefix; the diff is the
    variable user message.
    """
    return (
        'You assess the SECURITY RISK of a single Debian patch from its DIFF '
        'alone, for triage. (risk prompt version %d)\n'
        '\n'
        "You are given ONLY the diff body, never the author's description; judge "
        'only from the code the diff adds and removes.\n'
        '\n'
        'How likely is this change to have a NEGATIVE security impact: to '
        'introduce or worsen a vulnerability, weaken a security control or '
        'hardening flag, or mishandle memory, input, privilege, crypto, '
        'authentication, or untrusted data?\n'
        '\n'
        'Most patches are routine (none/low). Use the upper levels deliberately:\n'
        '  none: no security relevance (docs, comments, translations, whitespace, '
        'changelog, metadata).\n'
        '  low: ordinary code/build change with no security-sensitive surface.\n'
        '  elevated: TOUCHES a security-sensitive surface (memory/buffer ops, '
        'input/format parsing, auth, crypto, privilege, network, sandbox, or a '
        'build-hardening flag) -- use this GENEROUSLY whenever such a surface is '
        'involved, even if the change looks benign.\n'
        '  high: PLAUSIBLY creates or worsens a vulnerability, or removes/weakens '
        'a check or hardening.\n'
        '\n'
        'Bias toward the HIGHER level when uncertain (missing a risky patch is '
        'worse than over-flagging a benign one).\n'
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
