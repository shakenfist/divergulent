"""Deterministic content rules — the no-cry-wolf core of classification.

This module maps a patch to a *content verdict*: a deterministic, evidence-
bearing statement about what the diff DOES, derived from the ``ContentProfile``
(step 2b) and never from the author's ``Claim`` (step 2a).  Two design promises
govern everything here:

1. **Deterministic rules settle only the easy categories.**  ``packaging`` and
   ``documentation`` are confirmable from content alone; everything substantive
   is left as ``unknown`` with a ``substantive`` signal for phase 4 to triage.
   We deliberately do **not** split ``bugfix``/``feature``/``security`` from
   content — claiming otherwise would cry wolf.

2. **``security`` and ``malicious`` are never deterministic verdicts.**  A
   dangerous construct added in code is an evidence-bearing *candidate flag*
   (a ``Flag``), ranked for human/LLM review — explicitly NOT a category and
   NOT a malice judgement.  The category of a patch that adds ``system(...)``
   is still ``unknown``/substantive; the construct is surfaced *alongside* it.

The code-vs-prose distinction is the load-bearing guarantee: the dangerous-
construct scan runs only over ``content.code_added_lines`` (added lines in
*code*-typed files), so a construct mentioned in a manpage or other prose file
cannot flag.  See ``scan_dangerous_constructs``.

Rules are a small **registry of pure functions**, each with an ``id`` and
``version``, applied in a documented precedence order — the seed of phase 3's
registry.  The content verdict must NOT depend on the claim; the claim/content
comparison is step 2d's job (``classify_content`` accepts the claim for
symmetry but does not consult it — see its docstring).

Curation-side only: no client command imports ``classify/``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from divergulent.classify import content as content_mod
from divergulent.classify.claim import Claim
from divergulent.classify.content import ContentProfile

# ---------------------------------------------------------------------------
# Version tag — phase-3 ledger keys on this to detect stale verdicts.
# ---------------------------------------------------------------------------

RULES_VERSION = 1

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Flag:
    """An evidence-bearing candidate for review — explicitly NOT a verdict.

    A ``Flag`` records that a potentially-interesting construct was added to a
    code file.  It is a pointer for a human or the phase-4 LLM tier to look at,
    never a pronouncement of malice or even of "this is a security patch".  Most
    flags will turn out benign; the value is that the few worth a look are
    surfaced rather than lost in the residue.
    """

    kind: str
    """The flag family, e.g. ``'dangerous-construct'``."""

    detail: str
    """Which pattern fired, e.g. ``'shell-out'`` or ``'fetch-piped-to-shell'``."""

    evidence: str
    """The offending added line, trimmed.  The exact text a reviewer reads."""


@dataclass(frozen=True)
class ContentVerdict:
    """Deterministic content verdict for a patch.

    ``content_category`` is one of ``packaging`` / ``documentation`` /
    ``unknown`` — never ``security``/``bugfix``/``feature``, which are not
    deterministic from content.  ``flags`` carries any dangerous-construct
    candidates found by the code-aware scan; they are orthogonal to the
    category (a flagged patch is still ``unknown``/substantive, not
    ``security``).
    """

    content_category: str
    """One of ``packaging`` / ``documentation`` / ``unknown``."""

    confidence: str
    """``'high'`` / ``'medium'`` / ``'low'``.  High for confirmable easy
    categories; low for the substantive ``unknown`` residue."""

    signals: list[str]
    """Human-readable evidence for the chosen category, in rule order."""

    flags: list[Flag]
    """Dangerous-construct candidate flags (may be empty).  NOT verdicts."""

    rule_ids: list[str]
    """The ``id`` of every rule that fired, in precedence order."""

    rule_version: int = field(default=RULES_VERSION)
    """The ``RULES_VERSION`` that produced this verdict."""


# ---------------------------------------------------------------------------
# Content-category rules.
#
# Each rule is a pure function ``(ContentProfile) -> _RuleHit | None``.  Rules
# are applied in the PRECEDENCE order of ``_CATEGORY_RULES`` below; the first
# rule that returns a hit wins and its category becomes the verdict.  Every
# rule carries an ``id`` and ``version`` so phase 3 can track provenance.
#
# Precedence (first match wins), most-specific / most-confidently-trivial
# first:
#
#   1. empty            (mode-only / normalises-to-empty)      -> packaging  high
#   2. ignore_file_only (.pc/_build added to .gitignore-like)  -> packaging  high
#   3. whitespace_only  (cosmetic reindent, no semantic change)-> packaging  high
#   4. comment_only     (prose-in-code only)                   -> documentation high
#   5. doc_only         (all touched files typed doc)          -> documentation high
#   6. build_only       (build, or build+data, no code/doc)    -> packaging  high
#   7. substantive      (the residue — anything else)          -> unknown    low
#
# Order rationale: the trivial-only flags (1-4) describe a change with no real
# semantic content and so are the most confidently-settled; they precede the
# file-type rules so a whitespace-only edit to a ``.c`` file is packaging, not
# substantive code.  ``comment_only`` is documentation (prose), distinct from
# whitespace.  ``doc_only`` precedes ``build_only`` only by convention; they are
# mutually exclusive (a diff cannot be all-doc and all-build at once).  The
# final ``substantive`` rule always matches, producing the phase-4 residue.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RuleHit:
    """Internal: a category rule's result before assembly into a verdict."""

    category: str
    confidence: str
    signal: str


def _rule_empty(profile: ContentProfile) -> _RuleHit | None:
    """Mode-only / empty-after-normalisation → packaging (high)."""
    if profile.is_empty:
        return _RuleHit(
            'packaging', 'high',
            'change normalises to empty (mode-only / pure-decoration)')
    return None


def _rule_ignore_file_only(profile: ContentProfile) -> _RuleHit | None:
    """Only ignore files, only ignore patterns → packaging (high)."""
    if profile.ignore_file_only:
        return _RuleHit(
            'packaging', 'high',
            'touches only ignore files with ignore-pattern lines')
    return None


def _rule_whitespace_only(profile: ContentProfile) -> _RuleHit | None:
    """Whitespace-only change (cosmetic, no semantic change) → packaging (high)."""
    if profile.whitespace_only:
        return _RuleHit(
            'packaging', 'high',
            'change differs only in whitespace (no semantic change)')
    return None


def _rule_comment_only(profile: ContentProfile) -> _RuleHit | None:
    """Comment-only change (prose-in-code) → documentation (high)."""
    if profile.comment_only:
        return _RuleHit(
            'documentation', 'high',
            'every changed line is blank or a comment (prose in code)')
    return None


def _rule_doc_only(profile: ContentProfile) -> _RuleHit | None:
    """All touched files typed ``doc`` → documentation (high)."""
    types = profile.file_types
    if types and set(types) == {'doc'}:
        return _RuleHit(
            'documentation', 'high',
            'all touched files are documentation')
    return None


def _rule_build_only(profile: ContentProfile) -> _RuleHit | None:
    """All touched files ``build`` (or ``build``+``data``, no code/doc) → packaging (high)."""
    types = set(profile.file_types)
    if 'build' in types and types <= {'build', 'data'}:
        return _RuleHit(
            'packaging', 'high',
            'all touched files are build-system / packaging (build, data)')
    return None


def _rule_substantive(profile: ContentProfile) -> _RuleHit | None:
    """The residue: anything not settled above → unknown / substantive (low).

    Always matches, so it is last.  This is the genuine work handed to phase 4;
    deterministic rules deliberately do NOT guess bugfix/feature/security here.
    """
    return _RuleHit(
        'unknown', 'low',
        'substantive: not settled by deterministic content rules')


# (id, version, fn) in precedence order — first hit wins.
_CATEGORY_RULES: tuple[tuple[str, int, object], ...] = (
    ('empty', 1, _rule_empty),
    ('ignore-file-only', 1, _rule_ignore_file_only),
    ('whitespace-only', 1, _rule_whitespace_only),
    ('comment-only', 1, _rule_comment_only),
    ('doc-only', 1, _rule_doc_only),
    ('build-only', 1, _rule_build_only),
    ('substantive', 1, _rule_substantive),
)


# ---------------------------------------------------------------------------
# Dangerous-construct scan.
#
# Runs ONLY over ``content.code_added_lines`` (added lines in code files), so a
# construct in prose cannot flag.  The pattern set is deliberately NARROW (the
# plan says start narrow and grow from real findings) and precise: each regex
# requires a syntactic shape that genuine source uses to *invoke* behaviour, not
# merely mention a word.  Each match yields a ``Flag`` with ``detail`` naming the
# pattern and ``evidence`` the trimmed line — never a category.
#
# Precision choices (to avoid false positives):
#   * ``system(`` / ``popen(`` require the trailing ``(`` so the word "system"
#     in a comment or identifier does not fire.
#   * ``subprocess`` requires ``shell=True`` on the same line — the dangerous
#     shape — not bare ``subprocess`` use.
#   * fetch-piped-to-shell requires both the fetch tool AND a pipe into a shell
#     on the same line, so a bare ``curl`` invocation does not fire.
#   * base64/decode-to-exec requires the decode AND a pipe-to-shell or an
#     ``exec``/``eval`` wrapper, so ordinary base64 use does not fire.
#   * reverse-shell patterns (`/dev/tcp/`, ``nc -e``) are themselves rare and
#     specific enough to match directly.
#
# TODO(future, lower confidence): newly-added bare URLs/IPs in code are a weaker
# signal worth surfacing once we can rank them — left out here because matching
# them precisely without crying wolf (vendored test data, doc links copied into
# code comments) needs more care than the narrow start allows.
# ---------------------------------------------------------------------------

_DANGEROUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # --- shell-out from code ---
    ('shell-out', re.compile(r'\bos\.system\s*\(')),
    ('shell-out', re.compile(r'\bsystem\s*\(')),
    ('shell-out', re.compile(r'\b(?:os\.)?popen\s*\(')),
    ('shell-out', re.compile(r'Runtime\.getRuntime\(\)\.exec\b')),
    ('shell-out', re.compile(r'\bsubprocess\b.*\bshell\s*=\s*True\b')),
    # Backtick command substitution in shell: `...` containing a non-trivial
    # command.  Require at least one space inside to avoid matching a bare
    # backtick pair or markdown-style inline code.
    ('shell-out', re.compile(r'`[^`]* [^`]*`')),
    # --- fetch piped to a shell ---
    ('fetch-piped-to-shell',
     re.compile(r'\b(?:curl|wget)\b.*\|\s*(?:sudo\s+)?(?:ba)?sh\b')),
    # --- dynamic exec of decoded data ---
    ('decode-piped-to-shell',
     re.compile(r'\bbase64\b.*(?:-d|--decode)\b.*\|\s*(?:ba)?sh\b')),
    ('decode-exec', re.compile(r'\beval\s*\(.*\b(?:base64|atob)\b')),
    ('decode-exec', re.compile(r'\bexec\s*\(.*\bdecode\b')),
    # --- embedded private key material ---
    ('embedded-private-key',
     re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----')),
    # --- reverse-shell-ish ---
    ('reverse-shell', re.compile(r'/dev/tcp/')),
    ('reverse-shell', re.compile(r'\bnc\b.*\s-e\b')),
)


def scan_dangerous_constructs(text: str) -> list[Flag]:
    """Scan a patch's *code* added lines for dangerous constructs.

    Runs only over ``content.code_added_lines(text)`` — added lines in
    code-typed files.  This is the no-cry-wolf guarantee: a construct that only
    appears in a manpage or other prose file is never seen, so it never flags.

    Each matched line yields at most one ``Flag`` per distinct ``detail`` (so a
    line matching several shell-out variants flags ``shell-out`` once).  Returns
    candidate flags for review — never a verdict.
    """
    flags: list[Flag] = []
    for line in content_mod.code_added_lines(text):
        trimmed = line.strip()
        if not trimmed:
            continue
        seen: set[str] = set()
        for detail, pattern in _DANGEROUS_PATTERNS:
            if detail in seen:
                continue
            if pattern.search(line):
                flags.append(Flag(kind='dangerous-construct', detail=detail,
                                  evidence=trimmed))
                seen.add(detail)
    return flags


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_content(claim: Claim, profile: ContentProfile, text: str) -> ContentVerdict:
    """Produce the deterministic ``ContentVerdict`` for a patch.

    Runs the content-category rules in precedence order to choose
    (category, confidence, signals, rule_ids), then attaches any
    dangerous-construct flags from ``scan_dangerous_constructs(text)``.

    ``claim`` is accepted for symmetry with step 2d and possible future use, but
    is deliberately NOT consulted: the content verdict must depend on the diff
    alone.  Comparing the claim against the content (the loudest signal in the
    pipeline) is step 2d's job, not this module's.  Keeping content independent
    here is what lets 2d treat a claim/content disagreement as meaningful.

    ``text`` is the full patch body; ``profile`` must be ``content.profile(text)``
    for the same body.

    Pure: no I/O, no network.
    """
    del claim  # intentionally unused — content must not depend on the claim.

    signals: list[str] = []
    rule_ids: list[str] = []
    category = 'unknown'
    confidence = 'low'

    for rule_id, _version, fn in _CATEGORY_RULES:
        hit = fn(profile)  # type: ignore[operator]
        if hit is None:
            continue
        category = hit.category
        confidence = hit.confidence
        signals.append(hit.signal)
        rule_ids.append(rule_id)
        break

    flags = scan_dangerous_constructs(text)

    return ContentVerdict(
        content_category=category,
        confidence=confidence,
        signals=signals,
        flags=flags,
        rule_ids=rule_ids,
        rule_version=RULES_VERSION,
    )
