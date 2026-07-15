"""Deterministic prompt-injection tripwire for LLM-bound patch text.

Divergulent's triage tier sends attacker-authorable text -- the diff body of a
carried patch -- to a language model.  A patch is exactly the place a supply-
chain attacker controls, so patch text aimed at the *classifier* rather than the
*compiler* is a live concern: an embedded instruction that nudges the triage LLM
toward a benign category is cheap to attempt and invisible in a large diff.

This module is the deterministic gate in front of that surface.  It scans a diff
body for injection-shaped text and returns evidence-bearing
:class:`~divergulent.classify.rules.Flag` candidates of kind
``llm-injection-suspect``.  A hit is recorded as a ledger observation
(``record.py``) and causes the triage driver to **skip the LLM and route the
patch to a human** -- attacker-authored instructions are never fed to the model
they target.

Posture (see ``docs/plans/PLAN-prompt-injection-screening.md``):

* **Tripwire, not shield.**  The patterns are public, so a targeted attacker can
  iterate offline until a payload scores clean.  What the tripwire still buys:
  every lazy, untargeted, or copy-pasted payload, and a forced increase in
  attacker effort.  We claim no more than that.
* **A candidate, never a verdict.**  A hit never settles a category and never
  pronounces malice; it routes a patch to a human, which is the system's
  designed behaviour.  A false positive costs one human review; a false
  negative leaves us where we are today.

The family set is deliberately TUNED (phase-3 findings): the raw evaluation
prototype's ``large-base64-blob`` family (embedded assets, not instruction-
shaped) is dropped, and the invisible-Unicode family is split into the strong
``invisible-tag-block`` (near-zero legitimate use in a Debian patch) and a
``zero-width`` family narrowed to a RUN of zero-width characters so an emoji
ZWJ (U+200D between emoji) does not fire.  Each family is separately versioned
and separately reportable so a noisy family can be retired without disturbing
the quiet ones.

Patterns are written with ``\\u`` / ``\\U`` escapes so this source stays pure
ASCII: literal invisible or bidi characters in code are hostile to review and
to the editing tools.
"""
from __future__ import annotations

import re

from divergulent.classify.rules import Flag

# The observation kind a hit records.  A single string shared by the recorder
# (``record.py``), the triage driver (which skips the LLM on it), and the review
# UI (which badges it), so the wire name is defined in exactly one place.
INJECTION_KIND = 'llm-injection-suspect'

# Folds the family set + tuning into the observation's ``rule_version``; bumping
# it (e.g. retiring a family) supersedes prior observations and re-scans, exactly
# like every other deterministic rule.
INJECTION_RULES_VERSION = 1

# The two regions a patch splits into.  Only a DIFF-region hit skips the LLM
# (the model reads the diff body, blind to the header); a HEADER-region hit is
# recorded for human provenance but never diverts triage -- routing on text the
# model never reads would be crying wolf.
DIFF_REGION = 'diff'
HEADER_REGION = 'header'

# Each family is a separately versioned, separately reportable list of patterns.
# Patterns match the WHOLE region text (everything in a patch is attacker-
# authored; restricting to '+' lines would just move the payload to context
# lines).
FAMILIES: dict[str, list[re.Pattern]] = {
    # Imperatives aimed at a model rather than a compiler.
    'instruction-phrase': [
        re.compile(r'(ignore|disregard|forget)\s+(all\s+|any\s+)?(previous|prior|above|earlier|preceding|your)\s+'
                   r'(instructions?|prompts?|rules|directives|context)', re.IGNORECASE),
        re.compile(r'\bsystem\s+prompt\b', re.IGNORECASE),
        re.compile(r'\bnew\s+instructions?\s*:', re.IGNORECASE),
        re.compile(r'\brespond\s+only\s+with\b', re.IGNORECASE),
        re.compile(r'\bclassify\s+this\s+(patch|diff|change|file)\s+as\b', re.IGNORECASE),
        re.compile(r'\b(print|reveal|repeat|output)\s+(your|the)\s+(system\s+)?(prompt|instructions)\b',
                   re.IGNORECASE),
        re.compile(r'\byou\s+are\s+now\s+(a|an|in)\b', re.IGNORECASE),
    ],
    # Chat / instruction-tuning turn structure smuggled into a diff.
    'chat-template-marker': [
        re.compile(r'<\|im_start\|>|<\|im_end\|>|<\|endoftext\|>|<\|system\|>'),
        re.compile(r'\[/?INST\]|<<SYS>>|<</SYS>>'),
        re.compile(r'"role"\s*:\s*"(system|assistant)"'),
        re.compile(r'^\s*(Human|Assistant):\s', re.MULTILINE),
    ],
    # The Unicode tag block (U+E0000-E007F): the invisible-instruction vector,
    # with effectively zero legitimate use in a Debian patch.  Kept whole.
    'invisible-tag-block': [
        re.compile('[\\U000e0000-\\U000e007f]'),
    ],
    # A RUN of four or more zero-width characters -- the shape of a hidden-text or
    # data-smuggling payload (which packs many).  U+200D (the emoji zero-width
    # joiner) is deliberately EXCLUDED so multi-codepoint emoji never fire.  The
    # threshold is 4, not 2: measured against the corpus, legitimate typography
    # and locale data (Khmer word-boundary ZWSPs, doubled editing artifacts)
    # produce short runs of 2-3, while a steganographic payload produces long
    # runs -- so 4+ separates intent from typography without weakening detection.
    'zero-width': [
        re.compile('[\\u200b\\u200c\\u2060\\ufeff]{4,}'),
    ],
    # Bidi embedding / override controls: the Trojan Source vector
    # (CVE-2021-42574).  Legitimate RTL text uses these too, but one benign hit
    # in the whole corpus (a translation file) does not justify a carve-out.
    'bidi-control': [
        re.compile('[\\u202a-\\u202e\\u2066-\\u2069]'),
    ],
}


def _visible(text: str) -> str:
    """Render ``text`` with invisible / control characters shown as ``<U+XXXX>``.

    Evidence snippets frequently contain exactly the invisible or bidi characters
    that fired -- which would be unreadable (and re-introduce hostile characters)
    if stored and displayed raw.  This makes the snippet legible to a human
    adjudicator and keeps the ledger and the review UI free of literal invisible
    text.  Newlines are shown as ``\\n`` so a snippet stays one line.
    """
    out = []
    for char in text:
        if char == '\n':
            out.append('\\n')
        elif char == '\t':
            out.append('\\t')
        elif char.isprintable():
            out.append(char)
        else:
            out.append('<U+%04X>' % ord(char))
    return ''.join(out)


def scan_text(text: str) -> list[tuple[str, str]]:
    """Every ``(family, snippet)`` match in ``text``; one entry per firing pattern.

    Pure and region-agnostic.  The snippet is a short, made-visible window around
    the match, suitable as observation evidence a human can read.
    """
    hits = []
    for family, patterns in FAMILIES.items():
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                start = max(match.start() - 40, 0)
                snippet = _visible(text[start:match.end() + 40])
                hits.append((family, snippet[:160]))
    return hits


def scan_injection(text: str, *, region: str) -> list[Flag]:
    """Scan one region's ``text``, returning ``llm-injection-suspect`` flags.

    ``region`` is :data:`DIFF_REGION` or :data:`HEADER_REGION`; it is folded into
    each flag's ``detail`` as ``'<family>/<region>'`` so the recorder,
    the triage skip, and the review UI can weigh a diff-region hit (skips the
    LLM) differently from a header-region one (recorded only).  Each flag is a
    candidate for a human, never a verdict.
    """
    return [
        Flag(kind=INJECTION_KIND, detail='%s/%s' % (family, region), evidence=snippet)
        for family, snippet in scan_text(text)]


def family_of(detail: str) -> str:
    """The family name from an observation ``detail`` (``'<family>/<region>'``)."""
    return detail.split('/', 1)[0]


def region_of(detail: str) -> str:
    """The region from an observation ``detail`` (``'<family>/<region>'``)."""
    parts = detail.split('/', 1)
    return parts[1] if len(parts) == 2 else ''


def injection_suspect_fingerprints(conn, *, region: str = DIFF_REGION) -> set[str]:
    """Fingerprints carrying a LIVE injection observation in ``region``.

    The set the triage driver skips (default: the diff region -- the text the LLM
    actually reads).  A header-only hit is intentionally NOT in the default set:
    it is recorded for provenance but does not divert triage.
    """
    from divergulent.classify import ledger as ledger_mod  # lazy: keep the scanner import-light
    return {
        obs['fingerprint'] for obs in ledger_mod.live_observations(conn)
        if obs['kind'] == INJECTION_KIND and region_of(obs['detail']) == region}


def injection_by_fingerprint(conn) -> dict[str, str]:
    """Map each fingerprint with a LIVE injection observation to a family summary.

    The value is the comma-joined sorted set of families that fired (across both
    regions), e.g. ``'instruction-phrase'`` -- a compact badge string for the
    review worklist and detail page.  Fingerprints with no injection observation
    are absent.
    """
    from divergulent.classify import ledger as ledger_mod  # lazy: keep the scanner import-light
    families: dict[str, set[str]] = {}
    for obs in ledger_mod.live_observations(conn):
        if obs['kind'] == INJECTION_KIND:
            families.setdefault(obs['fingerprint'], set()).add(family_of(obs['detail']))
    return {fingerprint: ', '.join(sorted(names)) for fingerprint, names in families.items()}
