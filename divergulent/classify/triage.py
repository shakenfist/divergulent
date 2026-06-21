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

import json
import os
import re
import subprocess
from dataclasses import dataclass

from divergulent.classify import fingerprint as fp

# ---------------------------------------------------------------------------
# Versioned constants -- the ledger keys on (model, prompt_version) to detect
# stale verdicts, exactly as the deterministic tiers key on their *_VERSION.
# ---------------------------------------------------------------------------

PROMPT_VERSION = 1

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

def build_prompt(diff_body: str, *, prompt_version: int = PROMPT_VERSION) -> str:
    """Build the claim-blind classification prompt for a diff body.

    Deterministic given ``(diff_body, prompt_version)``. The prompt gives the
    model ONLY the diff -- never the author's description -- and asks for
    exactly one ``TRIAGE_CATEGORIES`` value with a confidence and a brief
    reasoning, as strict JSON. ``security`` is framed as a *candidate*
    (independently verified and human-reviewed downstream), never a verdict, so
    the model neither under- nor over-claims it.
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
        '\n'
        'Diff body:\n'
        '\n'
        f'{diff_body}\n'
    )


# ---------------------------------------------------------------------------
# JSON parsing -- robust to surrounding prose / code fences
# ---------------------------------------------------------------------------

def _parse_response(text: str) -> tuple[str, str, str]:
    """Extract ``(category, confidence, reasoning)`` from a model response.

    Robust to a response wrapped in ```json fences or surrounded by prose: the
    first ``{...}`` object is parsed. An unparseable response, or one missing
    fields, degrades to ``('unknown', 'low', ...)`` rather than raising -- a
    malformed answer is treated as an ambiguous one, never as a hard failure of
    the triage run. The category is validated against ``TRIAGE_CATEGORIES`` by
    the caller; confidence is validated here.
    """
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        return ('unknown', 'low', 'model response contained no JSON object')

    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return ('unknown', 'low', 'model response JSON did not parse')

    if not isinstance(data, dict):
        return ('unknown', 'low', 'model response JSON was not an object')

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

    Extracts the claim-blind ``diff_body``, builds the prompt, invokes
    ``call(prompt, model=model) -> str`` (the injectable backend boundary),
    parses the JSON answer, and returns an ``LlmVerdict`` carrying the full raw
    response as evidence.

    ``call`` is required (no default) so the function is pure given an injected
    fake -- the test suite never touches the network. For the real backend,
    build a ``call`` from ``anthropic_call`` (an optional extra).

    A category outside ``TRIAGE_CATEGORIES`` is coerced to ``unknown`` and noted
    in the reasoning; an LLM must not be able to invent a category the rest of
    the system does not understand.
    """
    body = diff_body(patch_text)
    prompt = build_prompt(body, prompt_version=prompt_version)

    raw_response = call(prompt, model=model)

    category, confidence, reasoning = _parse_response(raw_response)

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
        raw_response=raw_response,
    )


# ---------------------------------------------------------------------------
# Real backends. Both perform external I/O and are the only functions that do;
# neither is imported at module top by anything. ``claude_cli_call`` is the
# default (subscription-billed, no Python dependency); ``anthropic_call`` is the
# optional separately-billed API alternative.
# ---------------------------------------------------------------------------

def claude_cli_call(prompt: str, *, model: str = DEFAULT_MODEL, timeout: float = 180) -> str:
    """Triage backend that shells out to the local ``claude`` CLI (print mode).

    The DEFAULT backend: it runs the prompt through ``claude -p`` so triage is
    billed against the operator's Claude subscription rather than separately-
    billed API calls, and it needs NO Python dependency -- only the ``claude``
    CLI on ``PATH``. The prompt is fed on stdin (diffs are large and multi-line,
    so an argv-safe path matters); ``--model`` selects the model and
    ``--output-format text`` keeps stdout to the model's answer, from which
    ``triage`` extracts the JSON. A missing ``claude`` or a non-zero exit raises
    a clear, actionable error. The base install never needs this -- only an
    operator running a curation-side triage pass does.
    """
    try:
        result = subprocess.run(
            ['claude', '-p', '--model', model, '--output-format', 'text'],
            input=prompt, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError(
            'the "claude" CLI was not found on PATH; install Claude Code to use '
            'the default claude -p triage backend, or use the anthropic API '
            'backend (pip install divergulent[triage])') from exc
    if result.returncode != 0:
        raise RuntimeError(
            'claude -p failed (exit %d): %s' % (result.returncode, result.stderr.strip()))
    return result.stdout


def anthropic_call(prompt: str, *, model: str) -> str:
    """Call the Anthropic API with ``prompt`` and return the model's text.

    The default real backend for ``triage``'s injectable ``call``. The
    ``anthropic`` SDK is imported *lazily* inside this function -- exactly as
    ``verify.py`` imports ``sigstore`` -- so the base install never loads it and
    the dependency stays an opt-in curation-side extra. If the SDK is absent a
    clear, actionable error names the ``triage`` extra.

    Reads the API key from ``ANTHROPIC_API_KEY`` in the environment. Issues a
    single ``messages.create`` and returns the concatenated text blocks. This is
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
        messages=[{'role': 'user', 'content': prompt}],
    )
    return ''.join(block.text for block in response.content if block.type == 'text')
