"""The reviewability axis -- a deterministic, structural size classification.

Patch SIZE is a real classification axis, but a different KIND from category and
security-risk: those are semantic LLM judgments (each one multiplies cost),
whereas this is purely structural -- the changed-line count of the diff -- so it
is computed for free over the whole corpus with no model. It rides ALONGSIDE the
category (a patch can be an ``oversized`` ``bugfix``) as a supersedable
``reviewability`` observation, and it exists to:

* give a human an honest disposition for a diff too large to line-review, and
* short-circuit the expensive LLM passes (the risk gate, triage) on those diffs
  -- which also sidesteps the model-context overflow a multi-MB diff causes (the
  risk run's ``errored 1`` was one such giant recorded ``elevated`` on failure).

Thresholds are on CHANGED (``+``/``-``) lines -- what a human actually reads --
agreed from a full-corpus scan (a smooth power law: 98.4% of fingerprints are
<=500 changed lines; 0.25% are >5000). Provenance mirrors the other tiers:
``observed_by='size-rule'`` / ``rule_version=REVIEWABILITY_VERSION``, so a
threshold change is a new identity and old levels supersede cleanly.

Curation-side only: no client command imports ``classify/``.
"""
from __future__ import annotations

import json

from divergulent.classify.content import ContentProfile

# The rule identity recorded on the observation. Bump REVIEWABILITY_VERSION when
# the thresholds change so a re-record supersedes the old level cleanly.
REVIEWABILITY_VERSION = 1
REVIEWABILITY_KIND = 'reviewability'
REVIEWABILITY_OBSERVED_BY = 'size-rule'

# The coarse ordinal scale (rank order matters; higher == less reviewable).
REVIEWABILITY_LEVELS = ('normal', 'large', 'oversized')
REVIEWABILITY_RANK = {level: rank for rank, level in enumerate(REVIEWABILITY_LEVELS)}

# Changed-line thresholds, agreed 2026-06-27 from the full-corpus distribution.
#   normal:    changed <= LARGE_LINES
#   large:     LARGE_LINES < changed <= OVERSIZED_LINES   (LLM-scored, diff-capped)
#   oversized: changed > OVERSIZED_LINES                  (skip the LLM passes)
REVIEWABILITY_LARGE_LINES = 500
REVIEWABILITY_OVERSIZED_LINES = 5000


def changed_lines(profile: ContentProfile) -> int:
    """The diff's changed-line count: added + removed (the human's read size)."""
    return profile.added_lines + profile.removed_lines


def level_for(changed: int) -> str:
    """Map a changed-line count to a reviewability level (one of the levels)."""
    if changed > REVIEWABILITY_OVERSIZED_LINES:
        return 'oversized'
    if changed > REVIEWABILITY_LARGE_LINES:
        return 'large'
    return 'normal'


def classify(profile: ContentProfile) -> str:
    """The reviewability level of a patch from its content profile."""
    return level_for(changed_lines(profile))


def evidence_for(profile: ContentProfile) -> str:
    """Canonical JSON evidence for a reviewability observation.

    Stable per fingerprint (the body is content-addressed, so the counts never
    change), which is what lets the recorder skip an unchanged re-record rather
    than churn a new row every pass.
    """
    return json.dumps(
        {'changed_lines': changed_lines(profile),
         'added_lines': profile.added_lines,
         'removed_lines': profile.removed_lines},
        sort_keys=True)


def reviewability_by_fingerprint(conn) -> dict[str, str]:
    """``{fingerprint: level}`` from the live ``reviewability`` observations.

    The current reviewability level per fingerprint -- the input the risk gate and
    triage use to skip ``oversized`` diffs, and the review UI to bucket them. A
    fingerprint with no live observation is absent (treat as ``normal``).
    """
    from divergulent.classify import ledger as ledger_mod  # lazy: keep this module import-light
    levels: dict[str, str] = {}
    for obs in ledger_mod.live_observations(conn):
        if obs['kind'] == REVIEWABILITY_KIND and obs['detail'] in REVIEWABILITY_RANK:
            levels[obs['fingerprint']] = obs['detail']
    return levels


def oversized_fingerprints(conn) -> set[str]:
    """Fingerprints currently observed ``oversized`` -- skipped by the LLM passes."""
    return {fp for fp, level in reviewability_by_fingerprint(conn).items() if level == 'oversized'}
