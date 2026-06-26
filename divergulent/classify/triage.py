"""Injectable, claim-blind LLM triage of the substantive patch residue.

Phases 1-3 settle what content can prove deterministically and leave a queue of
*substantive* fingerprints whose category (bugfix / feature / security / ...)
the deterministic rules deliberately do not guess. Phase 4 triages that residue
with an LLM -- the one non-deterministic, non-free, off-box decider in the
system -- under strict discipline:

* **Curation-side, with two backends, neither a runtime import.** The default
  ``claude_cli_call`` shells out to the local ``claude`` CLI (``claude -p``), so
  triage is billed against the operator's Claude subscription and needs NO
  Python dependency -- only the ``claude`` CLI on ``PATH``, which suits
  divergulent's dependency minimalism. ``anthropic_call`` is an optional
  separately-billed alternative whose SDK is imported *lazily* exactly as
  ``verify.py`` imports ``sigstore`` (``pip install divergulent[triage]``). No
  client command imports this module; the runtime never triages -- clients
  consume a signed bundle (phase 5).

* **Injectable.** ``triage`` takes a ``call`` callable (the model backend) so
  the test suite runs offline against a fake and the model is swappable. Given
  an injected ``call``, ``triage`` is pure.

* **Blind to the author's claim.** A quilt patch often opens with a DEP-3 /
  free-text header (``Description:``, ``Subject:`` ...) -- an author-controlled
  *claim*. The LLM is shown only the raw diff body (``diff_body``), so it
  classifies *content*, exactly as the deterministic content rules do. The
  loud signal downstream is the LLM's content read disagreeing with the claim;
  that comparison cannot happen if the LLM has already seen the claim.

* **Auditable despite non-determinism.** An LLM verdict is not reproducible, so
  ``LlmVerdict`` carries the full ``raw_response`` verbatim for the ledger to
  keep as evidence, and ``model`` + ``prompt_version`` form the ``rule_version``
  (bumping either supersedes and re-triages, the phase-3 machinery).

The LLM is the *last* tier and is **always verified** before it counts
(step 4b); a ``security`` verdict here is a *candidate for human confirmation*,
never a final judgement.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass

from divergulent.classify import fingerprint as fp

# ---------------------------------------------------------------------------
# Versioned constants -- the ledger keys on (model, prompt_version) to detect
# stale verdicts, exactly as the deterministic tiers key on their *_VERSION.
# ---------------------------------------------------------------------------

PROMPT_VERSION = 1

# The verification prompt is versioned independently of the triage prompt: the
# adversarial pass can be re-tuned without re-triaging, and the ledger keys on
# both, so a verify-prompt bump supersedes the verification but not the draft.
VERIFY_PROMPT_VERSION = 1

# A capable, cost-conscious default for a large (~43k) residue: Sonnet triages
# the bulk well within budget, leaving Opus available for the riskier
# dangerous-construct / security candidates later (step 4d's prioritised slice).
DEFAULT_MODEL = 'claude-sonnet-4-6'

# The provisional triage enum the LLM may return. Versioned with PROMPT_VERSION;
# mirrors the deterministic claim/content vocabulary so verdicts compare
# directly against the claim. ``unknown`` is the honest answer for a genuinely
# ambiguous diff.
TRIAGE_CATEGORIES = ('packaging', 'documentation', 'bugfix', 'security', 'feature', 'unknown')

_CONFIDENCES = ('high', 'medium', 'low')

# Extract the first JSON object from a model response that may be wrapped in
# prose or a ```json code fence. Non-greedy, brace-balanced enough for the flat
# object the prompt asks for.
_JSON_OBJECT_RE = re.compile(r'\{.*?\}', re.DOTALL)


# ---------------------------------------------------------------------------
# The model-call boundary: usage + result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Usage:
    """Normalised per-call token usage (and cost when a backend reports it).

    Mirrors the Anthropic usage block so cache behaviour is visible: the
    ``cache_read_tokens`` (billed at ~0.1x) versus ``cache_creation_tokens`` split
    is the signal that the cached rubric prefix is landing.  A backend fills what
    it can; a fake or a text-only backend reports zeros.  ``cost_usd`` is the
    backend's own figure (``claude -p`` reports one) or ``None`` when unknown --
    the run report derives an at-rates estimate when it is ``None``.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float | None = None

    def __add__(self, other: 'Usage') -> 'Usage':
        cost = None if self.cost_usd is None and other.cost_usd is None else \
            (self.cost_usd or 0.0) + (other.cost_usd or 0.0)
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cost_usd=cost)


@dataclass(frozen=True)
class CallResult:
    """What a triage backend returns: the model's text plus its token usage.

    The injected ``call(system, user, *, model) -> CallResult`` boundary: the
    cacheable ``system`` rubric and the variable ``user`` diff go in, the answer
    text and a normalised :class:`Usage` come out.
    """

    text: str
    usage: Usage = Usage()


# ---------------------------------------------------------------------------
# LlmVerdict
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LlmVerdict:
    """One claim-blind LLM read of a diff body.

    Every field is the LLM's *content* judgement; nothing here is verified
    (step 4b) and nothing reflects the author's claim. Use alongside the
    ``Claim`` -- their disagreement is the loudest signal.
    """

    category: str
    """One of ``TRIAGE_CATEGORIES``. Coerced to ``unknown`` if the model
    returns anything outside the enum."""

    confidence: str
    """``'high'`` / ``'medium'`` / ``'low'`` -- the model's stated confidence.
    Low confidence routes to human review (step 4b)."""

    reasoning: str
    """The model's one/two-sentence justification, drawn from the diff alone."""

    model: str
    """The model id that produced this verdict (part of the ``rule_version``)."""

    prompt_version: int
    """The ``PROMPT_VERSION`` that produced this verdict (part of the
    ``rule_version``)."""

    raw_response: str
    """The full model text, stored verbatim. An LLM verdict is
    non-deterministic, so the ledger keeps this as evidence -- the verdict must
    be auditable even though it is not reproducible."""

    usage: Usage = Usage()
    """Token usage for the call that produced this draft -- operational telemetry,
    NOT part of the verdict identity or the ledger evidence."""


# ---------------------------------------------------------------------------
# Claim-blind diff extraction
# ---------------------------------------------------------------------------

def diff_body(patch_text: str) -> str:
    """Return the raw diff body of ``patch_text``, blind to the author's claim.

    Reuses ``fingerprint._split_lines`` / ``_diff_start`` so the cut point
    matches the fingerprint and content tiers exactly: everything before the
    first diff marker is an author-controlled DEP-3 / free-text header and is
    dropped. Unlike ``fingerprint.normalise`` this returns the *readable* diff
    (line numbers, context, decoration intact) -- the LLM reads a real diff,
    not the canonical hash form. A patch with no recognisable diff body returns
    ``''``.
    """
    lines = fp._split_lines(patch_text)
    start = fp._diff_start(lines)
    if start >= len(lines):
        return ''
    return '\n'.join(lines[start:])


# ---------------------------------------------------------------------------
# Prompt construction -- deterministic given (diff_body, prompt_version)
# ---------------------------------------------------------------------------

def triage_system_prompt(*, prompt_version: int = PROMPT_VERSION) -> str:
    """The static claim-blind classification rubric -- the CACHEABLE system prompt.

    Constant for a fixed ``prompt_version`` (it holds no diff), so it is sent once
    as the prompt-cache prefix and read back cheaply for every patch in a run. The
    diff is the variable user message (:func:`triage_user_message`). The rubric
    asks for exactly one ``TRIAGE_CATEGORIES`` value with a confidence and a brief
    reasoning, as strict JSON. ``security`` is framed as a *candidate*
    (independently verified and human-reviewed downstream), never a verdict, so
    the model neither under- nor over-claims it.

    Note: this is the SAME rubric text the prior single flat prompt led with --
    relocated verbatim into the system prompt -- so verdict meaning, and the
    ``(model, prompt_version)`` ledger identity, are unchanged.
    """
    categories = ', '.join(TRIAGE_CATEGORIES)
    return (
        f'You are classifying a single Debian patch by what its DIFF does. '
        f'(prompt version {prompt_version})\n'
        '\n'
        'You are given ONLY the diff body below. You are NOT given the patch '
        "author's description, and you must not assume one: classify the change "
        'purely from the code the diff adds and removes.\n'
        '\n'
        f'Choose exactly one category from: {categories}.\n'
        '\n'
        '  - packaging: Debian packaging / build-system / reproducibility plumbing.\n'
        '  - documentation: docs, manpages, comments, typo/spelling fixes only.\n'
        '  - bugfix: corrects incorrect behaviour in existing code.\n'
        '  - security: the change has a PLAUSIBLE security impact. This is a\n'
        '    CANDIDATE only -- it will be independently verified and reviewed by\n'
        '    a human; it is never a final judgement. Use it when the diff\n'
        '    plausibly addresses a vulnerability, memory-safety issue, or other\n'
        '    security-relevant defect.\n'
        '  - feature: adds new behaviour or capability.\n'
        '  - unknown: use this when the diff is genuinely ambiguous and you\n'
        '    cannot confidently choose another category.\n'
        '\n'
        'Respond with STRICT JSON and nothing else, in exactly this shape:\n'
        '{"category": "...", "confidence": "high|medium|low", "reasoning": "..."}\n'
        'Keep "reasoning" to one or two sentences, grounded in the diff.\n'
    )


def triage_user_message(diff_body: str) -> str:
    """The variable per-patch user message: just the diff body, framed.

    This is the only part that changes between patches, so it carries no cache
    breakpoint -- the rubric prefix (:func:`triage_system_prompt`) does.
    """
    return 'Diff body:\n\n%s\n' % diff_body


# ---------------------------------------------------------------------------
# JSON parsing -- robust to surrounding prose / code fences
# ---------------------------------------------------------------------------

def _first_json_object(text: str) -> dict | None:
    """Extract and parse the first ``{...}`` JSON object from a model response.

    Robust to a response wrapped in ```json fences or surrounded by prose: the
    first brace-delimited object is parsed. Returns ``None`` when there is no
    JSON object, it does not parse, or it is not an object -- the callers each
    decide what their own safe degradation is (``triage`` -> ``unknown``;
    ``verify`` -> ``agrees=False``).
    """
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _parse_response(text: str) -> tuple[str, str, str]:
    """Extract ``(category, confidence, reasoning)`` from a model response.

    Robust to a response wrapped in ```json fences or surrounded by prose: the
    first ``{...}`` object is parsed. An unparseable response, or one missing
    fields, degrades to ``('unknown', 'low', ...)`` rather than raising -- a
    malformed answer is treated as an ambiguous one, never as a hard failure of
    the triage run. The category is validated against ``TRIAGE_CATEGORIES`` by
    the caller; confidence is validated here.
    """
    data = _first_json_object(text)
    if data is None:
        return ('unknown', 'low', 'model response had no usable JSON object')

    category = data.get('category')
    confidence = data.get('confidence')
    reasoning = data.get('reasoning')

    category = category if isinstance(category, str) else 'unknown'
    confidence = confidence if confidence in _CONFIDENCES else 'low'
    reasoning = reasoning if isinstance(reasoning, str) else ''

    return (category, confidence, reasoning)


# ---------------------------------------------------------------------------
# The injectable boundary
# ---------------------------------------------------------------------------

def triage(patch_text: str, *, call, model: str = DEFAULT_MODEL,
           prompt_version: int = PROMPT_VERSION) -> LlmVerdict:
    """Triage one patch with a claim-blind LLM read.

    Extracts the claim-blind ``diff_body``, builds the cacheable rubric system
    prompt and the per-patch diff user message, invokes
    ``call(system, user, *, model) -> CallResult`` (the injectable backend
    boundary), parses the JSON answer, and returns an ``LlmVerdict`` carrying the
    full raw response as evidence and the call's token ``usage``.

    ``call`` is required (no default) so the function is pure given an injected
    fake -- the test suite never touches the network. For the real backend,
    build a ``call`` from ``claude_cli_call`` or ``anthropic_call``.

    A category outside ``TRIAGE_CATEGORIES`` is coerced to ``unknown`` and noted
    in the reasoning; an LLM must not be able to invent a category the rest of
    the system does not understand.
    """
    body = diff_body(patch_text)
    system = triage_system_prompt(prompt_version=prompt_version)
    user = triage_user_message(body)

    result = call(system, user, model=model)

    category, confidence, reasoning = _parse_response(result.text)

    if category not in TRIAGE_CATEGORIES:
        reasoning = (
            'coerced to unknown: model returned out-of-enum category %r. %s'
            % (category, reasoning)).strip()
        category = 'unknown'

    return LlmVerdict(
        category=category,
        confidence=confidence,
        reasoning=reasoning,
        model=model,
        prompt_version=prompt_version,
        raw_response=result.text,
        usage=result.usage,
    )


# ---------------------------------------------------------------------------
# The adversarial verifier (step 4b)
#
# An LLM draft does not count until an INDEPENDENT pass confirms it. The verify
# pass is itself claim-blind (it sees only the diff body + the proposed
# category, never the author's claim) and adversarial (prompted to try to break
# the draft, defaulting to REFUTE when unsure). Agreement at sufficient
# confidence is the only path to ``verified``; everything else routes to a human.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Verification:
    """One independent, claim-blind adversarial read of a proposed category.

    The verifier confirms or refutes a category that ``triage`` drafted, from
    the diff alone. ``agrees=False`` is the safe default: a garbled or missing
    answer is treated as *unverified*, never as confirmation.
    """

    agrees: bool
    """Whether the verifier CONFIRMS the proposed category. ``False`` is the
    safe default -- a disagreement, or an unparseable answer, leaves the draft
    unverified and routes it to a human."""

    confidence: str
    """``'high'`` / ``'medium'`` / ``'low'`` -- the verifier's stated
    confidence. Low confidence routes to human review even when it agrees."""

    reasoning: str
    """The verifier's one/two-sentence justification, drawn from the diff and
    the proposed category alone."""

    model: str
    """The model id that produced this verification (part of the
    ``rule_version``)."""

    prompt_version: int
    """The ``VERIFY_PROMPT_VERSION`` that produced this verification (part of
    the ``rule_version``)."""

    raw_response: str
    """The full verifier text, stored verbatim as auditable evidence -- the
    verification is non-deterministic and must be inspectable after the fact."""

    usage: Usage = Usage()
    """Token usage for the verify call -- operational telemetry, not evidence."""


def verify_system_prompt(*, prompt_version: int = VERIFY_PROMPT_VERSION) -> str:
    """The static adversarial verification rubric -- the CACHEABLE system prompt.

    Constant for a fixed ``prompt_version``: it states the adversarial discipline
    and the output schema, but NOT the specific proposed category or diff (those
    vary per patch and live in :func:`verify_user_message`), so it caches as a
    stable prefix. It is given ONLY the diff body and the proposed category --
    NEVER the author's claim -- so this is a genuinely independent second read,
    not a rubber stamp of the same evidence the draft already had plus the
    author's framing.

    The discipline is adversarial: the model is told to default to REFUTE when
    unsure, and that confirming a wrong category is worse than refuting a right
    one. The cost asymmetry is deliberate -- a false ``verified`` lets a wrong
    LLM call finalise unreviewed, whereas a false refutal merely sends a correct
    draft to a human, who can still accept it.
    """
    categories = ', '.join(TRIAGE_CATEGORIES)
    return (
        f'You are an adversarial reviewer checking a proposed classification of '
        f'a single Debian patch. (verify prompt version {prompt_version})\n'
        '\n'
        'You will be given a diff body and a single PROPOSED category for it. '
        'You are given ONLY the diff body and that proposed category. You are '
        "NOT given the patch author's description, and you must not assume one: "
        'judge the proposal purely from the code the diff adds and removes.\n'
        '\n'
        'Your job is to try to BREAK the proposal. Decide whether the diff '
        f'genuinely supports the proposed category (chosen from: {categories}).\n'
        '\n'
        'Be adversarial and skeptical:\n'
        '  - DEFAULT TO REFUTE when you are unsure. Do not give the proposal '
        'the benefit of the doubt.\n'
        '  - Confirming a WRONG category is worse than refuting a RIGHT one: a '
        'wrong confirmation lets a bad call stand unreviewed, while a wrong '
        'refusal merely sends a correct call to a human who can still accept '
        'it.\n'
        '  - Only set "agrees" to true if the diff clearly and specifically '
        'supports the proposed category.\n'
        '\n'
        'Respond with STRICT JSON and nothing else, in exactly this shape:\n'
        '{"agrees": true|false, "confidence": "high|medium|low", "reasoning": "..."}\n'
        'Keep "reasoning" to one or two sentences, grounded in the diff.\n'
    )


def verify_user_message(diff_body: str, proposed_category: str) -> str:
    """The variable per-patch verify user message: the proposed category + diff.

    The proposed category varies per patch, so it rides here (not in the cached
    system prefix). States it plainly, then the diff to judge it against.
    """
    return 'Proposed category: %s\n\nDiff body:\n\n%s\n' % (proposed_category, diff_body)


def _parse_verification(text: str) -> tuple[bool, str, str]:
    """Extract ``(agrees, confidence, reasoning)`` from a verifier response.

    Robust to fences / surrounding prose via ``_first_json_object``. A missing
    or non-boolean ``agrees``, or an unparseable response, degrades to
    ``(False, 'low', ...)`` -- ``agrees=False`` is the SAFE default: an
    unreadable answer leaves the draft unverified, which routes it to a human,
    rather than silently confirming it. Confidence is validated against
    ``_CONFIDENCES`` and degrades to ``'low'``.
    """
    data = _first_json_object(text)
    if data is None:
        return (False, 'low', 'verifier response had no usable JSON object')

    agrees = data.get('agrees')
    confidence = data.get('confidence')
    reasoning = data.get('reasoning')

    if not isinstance(agrees, bool):
        return (False, 'low',
                'verifier response had no boolean "agrees" field; '
                'defaulting to unverified')

    confidence = confidence if confidence in _CONFIDENCES else 'low'
    reasoning = reasoning if isinstance(reasoning, str) else ''

    return (agrees, confidence, reasoning)


def verify(patch_text: str, proposed_category: str, *, call,
           model: str = DEFAULT_MODEL,
           prompt_version: int = VERIFY_PROMPT_VERSION) -> Verification:
    """Independently, adversarially verify a proposed category for one patch.

    Extracts the claim-blind ``diff_body``, builds the cacheable adversarial
    rubric system prompt and the per-patch (proposed category + diff) user
    message -- seeing only the diff and the proposed category, never the claim --
    invokes ``call(system, user, *, model) -> CallResult`` (the same injectable
    backend boundary ``triage`` uses), parses the JSON robustly, and returns a
    ``Verification`` carrying the full raw response as evidence and the call's
    token ``usage``.

    ``call`` is required (no default) so the function is pure given an injected
    fake -- the test suite never touches the network. A missing or garbled
    ``agrees`` degrades to ``agrees=False`` at low confidence: unverified is the
    safe default, never a silent confirmation.
    """
    body = diff_body(patch_text)
    system = verify_system_prompt(prompt_version=prompt_version)
    user = verify_user_message(body, proposed_category)

    result = call(system, user, model=model)

    agrees, confidence, reasoning = _parse_verification(result.text)

    return Verification(
        agrees=agrees,
        confidence=confidence,
        reasoning=reasoning,
        model=model,
        prompt_version=prompt_version,
        raw_response=result.text,
        usage=result.usage,
    )


# ---------------------------------------------------------------------------
# Routing: draft + verify -> verified | needs_human (step 4b)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TriageResult:
    """A draft, its adversarial verification, and the resulting routing.

    The whole point of step 4b: the ``draft`` (an ``LlmVerdict``) does not count
    on its own -- only a ``routing`` of ``'verified'`` lets it stand as a
    verified LLM decision (step 4c). ``'needs_human'`` enqueues it for the local
    interactive review tool (step 4e), with ``reason`` recording why.
    """

    draft: LlmVerdict
    """The claim-blind LLM draft from ``triage``."""

    verification: Verification
    """The independent adversarial check of ``draft.category``."""

    routing: str
    """``'verified'`` or ``'needs_human'`` -- whether the draft may stand or
    must go to a human."""

    reason: str
    """The deciding reason(s). For ``'needs_human'`` this lists every condition
    that fired (verifier disagreed, low confidence, claim mismatch, dangerous
    construct); for ``'verified'`` it states the draft passed."""

    usage: Usage = Usage()
    """Total token usage for this patch -- the draft call plus the verify call --
    summed for the driver's per-run cost/cache telemetry."""


def triage_and_verify(patch_text: str, *, call, claim_category: str | None = None,
                      has_dangerous_construct: bool = False,
                      model: str = DEFAULT_MODEL) -> TriageResult:
    """Draft a category, independently verify it, and route the result.

    Runs ``triage`` (the claim-blind draft) then ``verify`` (the independent
    adversarial check of the drafted category). The result routes to
    ``'needs_human'`` when ANY of these fire, otherwise ``'verified'``:

      * the verifier does not agree;
      * the draft OR the verification confidence is ``'low'``;
      * a claim/content mismatch -- ``claim_category`` is given, is not ``None``
        or ``'unknown'``, and differs from the drafted category (the loud signal:
        the author's claim and the content read disagree);
      * a live dangerous-construct observation (``has_dangerous_construct``).

    ``claim_category`` and ``has_dangerous_construct`` are supplied by the 4d
    driver from the ledger/classification, and are parameters (not re-derived
    here) so this function stays pure and testable.

    Note the security/malice escape hatch: a ``security`` draft that the verifier
    confirms STILL routes to ``'needs_human'`` if it carries a dangerous-construct
    flag or a claim mismatch. The LLM never finalises a security or malice call
    on its own -- a human does (step 4e). ``security`` from the LLM is only ever
    a *candidate for human confirmation*.
    """
    draft = triage(patch_text, call=call, model=model)
    verification = verify(patch_text, draft.category, call=call, model=model)

    reasons: list[str] = []

    if not verification.agrees:
        reasons.append('verifier refuted the drafted category')
    if draft.confidence == 'low':
        reasons.append('draft confidence is low')
    if verification.confidence == 'low':
        reasons.append('verification confidence is low')
    if (claim_category is not None and claim_category != 'unknown'
            and claim_category != draft.category):
        reasons.append(
            'claim/content mismatch: author claims %r but content reads %r'
            % (claim_category, draft.category))
    if has_dangerous_construct:
        reasons.append('a dangerous-construct observation is present')

    usage = draft.usage + verification.usage

    if reasons:
        return TriageResult(
            draft=draft, verification=verification,
            routing='needs_human', reason='; '.join(reasons), usage=usage)

    return TriageResult(
        draft=draft, verification=verification, routing='verified',
        reason='verifier confirmed the drafted category at sufficient confidence',
        usage=usage)


# ---------------------------------------------------------------------------
# Real backends. Both perform external I/O and are the only functions that do;
# neither is imported at module top by anything. ``claude_cli_call`` is the
# default (subscription-billed, no Python dependency); ``anthropic_call`` is the
# optional separately-billed API alternative.
# ---------------------------------------------------------------------------

def claude_cli_call(system: str, user: str, *, model: str = DEFAULT_MODEL,
                    timeout: float = 180) -> CallResult:
    """Triage backend that shells out to the local ``claude`` CLI (print mode).

    The DEFAULT backend: it runs the prompt through ``claude -p`` so triage is
    billed against the operator's Claude subscription rather than separately-
    billed API calls, and it needs NO Python dependency -- only the ``claude``
    CLI on ``PATH``. The cacheable rubric ``system`` is passed via
    ``--system-prompt`` (replacing Claude Code's default system prompt, so our
    rubric is the whole stable cache prefix), and the variable ``user`` diff is
    fed on stdin (diffs are large and multi-line). ``--output-format json``
    returns the answer text alongside a token-usage block and a reported cost,
    which become the call's :class:`Usage`. A missing ``claude`` or a non-zero
    exit raises a clear, actionable error. Only a curation-side triage pass uses
    this; the base install never does.
    """
    cmd = ['claude', '-p', '--model', model, '--system-prompt', system,
           '--output-format', 'json']
    try:
        result = subprocess.run(
            cmd, input=user, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError(
            'the "claude" CLI was not found on PATH; install Claude Code to use '
            'the default claude -p triage backend, or use the anthropic API '
            'backend (pip install divergulent[triage])') from exc
    if result.returncode != 0:
        # claude writes its failure reason (auth, usage limit, ...) to stdout, so
        # surface BOTH streams and the command -- an exit code alone is undebuggable.
        detail = result.stderr.strip() or result.stdout.strip() or '(no output on stdout or stderr)'
        raise RuntimeError(
            'claude -p failed (exit %d).\n  command: %s\n  output: %s'
            % (result.returncode, ' '.join(cmd[:5]), detail[:4000]))
    if not result.stdout.strip():
        raise RuntimeError(
            'claude -p returned an empty response (exit 0).\n  command: %s\n  stderr: %s'
            % (' '.join(cmd[:5]), result.stderr.strip() or '(empty)'))
    return _parse_claude_cli_json(result.stdout)


def _parse_claude_cli_json(stdout: str) -> CallResult:
    """Parse a ``claude -p --output-format json`` result into a :class:`CallResult`.

    The result object carries the answer in ``result`` and a ``usage`` block with
    the token counts (including the ``cache_creation``/``cache_read`` split) plus a
    ``total_cost_usd``.  A non-JSON or schema-surprising payload raises a clear
    error rather than silently triaging on garbage.
    """
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise RuntimeError(
            'claude -p did not return parseable JSON (expected --output-format '
            'json):\n  %s' % stdout[:2000]) from exc

    text = data.get('result')
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError(
            'claude -p JSON had no usable "result" text:\n  %s' % stdout[:2000])

    usage_block = data.get('usage') or {}
    usage = Usage(
        input_tokens=int(usage_block.get('input_tokens', 0) or 0),
        output_tokens=int(usage_block.get('output_tokens', 0) or 0),
        cache_creation_tokens=int(usage_block.get('cache_creation_input_tokens', 0) or 0),
        cache_read_tokens=int(usage_block.get('cache_read_input_tokens', 0) or 0),
        cost_usd=data.get('total_cost_usd'))
    return CallResult(text=text, usage=usage)


def anthropic_call(system: str, user: str, *, model: str) -> CallResult:
    """Call the Anthropic API with the cached rubric ``system`` + ``user`` diff.

    The metered (API-key) backend for ``triage``'s injectable ``call``. The
    ``anthropic`` SDK is imported *lazily* inside this function -- exactly as
    ``verify.py`` imports ``sigstore`` -- so the base install never loads it and
    the dependency stays an opt-in curation-side extra. If the SDK is absent a
    clear, actionable error names the ``triage`` extra.

    Reads the API key from ``ANTHROPIC_API_KEY``. The ``system`` rubric is sent as
    a cached content block (a 1-hour ephemeral ``cache_control`` breakpoint), so it
    is written to cache once and read back cheaply for every patch in a run; the
    ``user`` diff varies and is not cached. Returns the model text and the
    response's token ``usage`` (including the cache-read/creation split). This is
    the only function in the module that performs network I/O.
    """
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            'the anthropic SDK is not installed; run '
            '"pip install divergulent[triage]" to use the LLM triage backend') from exc

    client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=[{'type': 'text', 'text': system,
                 'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}],
        messages=[{'role': 'user', 'content': user}],
    )
    text = ''.join(block.text for block in response.content if block.type == 'text')
    raw = response.usage
    usage = Usage(
        input_tokens=getattr(raw, 'input_tokens', 0) or 0,
        output_tokens=getattr(raw, 'output_tokens', 0) or 0,
        cache_creation_tokens=getattr(raw, 'cache_creation_input_tokens', 0) or 0,
        cache_read_tokens=getattr(raw, 'cache_read_input_tokens', 0) or 0)
    return CallResult(text=text, usage=usage)


# ---------------------------------------------------------------------------
# The bounded triage-run CLI (``python -m divergulent.classify.triage``).
#
# This is the ONLY place in the triage stack that reads a wall clock and that
# selects a REAL backend: ``main`` captures one ``now`` ISO-8601 string and a
# ``call`` (``claude_cli_call`` by default, ``anthropic_call`` with
# ``--backend api``) and threads them into the driver, which runs the bounded,
# prioritised triage slice and records each result.  The run is curation-side and
# off-box; clients never reach this path.
#
# ``triage_driver`` is LAZY-imported inside ``main``, not at module top: the
# driver imports this module (for ``triage_and_verify`` / the backends), so
# importing it here would be a cycle.  Keeping this module import-time clean (only
# its own deps + stdlib) is what lets the driver import it freely.  The tests
# inject a fake ``call`` and never reach a real backend.
# ---------------------------------------------------------------------------

# A small, deliberate default cap: a budgeted run never sweeps the whole ~43k
# residue by accident.  The plan is explicit that the full sweep is the
# operator's call, taken iteratively; ``--limit`` makes each slice a choice.
DEFAULT_LIMIT = 50


def _cli_now() -> str:
    """The single clock read of the triage stack: an ISO-8601 UTC timestamp.

    Only the CLI entry point reads the clock; the value is threaded down as the
    recorder's ``decided_at`` so every deterministic module path stays
    re-runnable, exactly as the ledger CLI's ``_cli_now`` does.
    """
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m divergulent.classify.triage',
        description='Run a BOUNDED, prioritised LLM triage pass over the phase-4 '
                    'residue (curation-side; off-box). Drafts a category blind to the '
                    "author's claim, verifies it adversarially, records each result in "
                    'the ledger, and surfaces candidate deterministic rules for human '
                    'approval. No malice is ever pronounced; the LLM never self-certifies.')
    parser.add_argument('ledger', help='path to a ledger sqlite built by classify.ledger')
    parser.add_argument('corpus_dir', help='directory of a corpus built by classify.corpus')
    parser.add_argument('--index', default=None,
                        help='path to the phase-1 sqlite fingerprint index (default: '
                             '<corpus_dir>/fingerprints.sqlite)')
    parser.add_argument('--limit', type=int, default=DEFAULT_LIMIT,
                        help='max fingerprints to triage this run -- the budget cap, so '
                             'the whole residue is never swept by accident (default: %d). '
                             'untriaged_remaining reports what the cap did not cover.'
                             % DEFAULT_LIMIT)
    parser.add_argument('--model', default=DEFAULT_MODEL,
                        help='the model id to triage and verify with (default: %s)' % DEFAULT_MODEL)
    parser.add_argument('--backend', choices=('claude', 'api'), default='claude',
                        help='LLM backend: "claude" shells out to the claude CLI '
                             '(subscription-billed, the default), "api" uses the Anthropic '
                             'API (pip install divergulent[triage])')
    parser.add_argument('--findings', default=None,
                        help='path for the markdown findings note (default: '
                             '<corpus_dir>/triage-findings.md)')
    return parser


def main(argv: list[str] | None = None) -> int:
    """``python -m divergulent.classify.triage``: a bounded, prioritised triage run.

    Opens the ledger, selects the real backend (``claude_cli_call`` by default,
    ``anthropic_call`` with ``--backend api``), runs the bounded triage slice via
    the driver (recording each result), rebuilds the current-verdict cache, writes
    the findings note, and prints the honest summary -- including
    ``untriaged_remaining``, the residue the budget did not cover.  The single
    clock read of the triage stack lives here (:func:`_cli_now`).
    """
    from divergulent.classify import triage_driver
    from divergulent.classify import verdict as verdict_mod

    parser = _build_parser()
    args = parser.parse_args(argv)

    index_path = args.index or os.path.join(args.corpus_dir, 'fingerprints.sqlite')
    findings_path = args.findings or os.path.join(args.corpus_dir, 'triage-findings.md')

    call = anthropic_call if args.backend == 'api' else claude_cli_call

    conn = sqlite3.connect(args.ledger)
    try:
        stats, triaged = triage_driver.run_triage(
            conn, args.corpus_dir, index_path, call=call, now=_cli_now(),
            limit=args.limit, model=args.model,
            progress=lambda msg: print(msg, file=sys.stderr, flush=True))
        # Cluster across the WHOLE ledger (every settled verdict from every batch),
        # not just this run -- a pattern accumulates over batches -- gated so an
        # ambiguous structure is refused, not proposed.
        scan = triage_driver.candidate_rules_from_ledger(conn, args.corpus_dir, index_path)
        verdict_mod.rebuild_current_verdict(conn)
    finally:
        conn.close()

    directory = os.path.dirname(findings_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(findings_path, 'w', encoding='utf-8') as handle:
        handle.write(triage_driver.render_run_report(stats, scan))

    triage_driver.print_run_summary(stats, scan)
    print('wrote findings note: %s' % findings_path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
