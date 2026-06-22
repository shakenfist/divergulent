"""Drive the extractors over the phase-1 index and measure the classification.

This is step 2d: the driver that ties together the claim (``claim.py``),
content (``content.py``), and rule (``rules.py``) extractors over the phase-1
fingerprint index (``measure.py``) and emits the measurement that matters now
that dedup gave nothing -- *of the distinct patches, how many settle
deterministically (packaging/documentation) and how big is the substantive
residue handed to phase 4, and how many carry a review flag.*

For each DISTINCT fingerprint in the index it:

* picks one representative provenance row (any ``raw_sha256`` sharing the
  fingerprint) and counts ``n_occurrences`` (provenance rows) and
  ``n_packages`` (distinct source packages) carrying it;
* loads the representative body via ``measure.read_body`` and runs
  ``claim.extract_claim`` + ``content.profile`` + ``rules.classify_content``;
* derives a claim/content ``consistency`` and a ``review_flag`` (the loudest
  signal -- a benign claim over a substantive code change, or any
  dangerous-construct flag).

**Simplification (an open question in the plan).**  Content is well-defined per
fingerprint -- the normalised diff is byte-identical across every occurrence --
so we classify content exactly once per fingerprint.  The CLAIM, however, is
taken from the *representative occurrence's* body only.  The DEP-3 header can
differ across occurrences of one fingerprint because canonical normalisation
strips it before fingerprinting, so two packages can carry the same diff under
different descriptions/filenames.  A fuller per-occurrence claim pass (claim
summarised across occurrences with the mismatch noted) is future work; the
plan's open question ``One body per fingerprint`` records this.  We surface the
representative's ``patch_name`` and ``source_package`` so a reviewer can tell
which occurrence the claim came from.

Curation-side only: no client command imports ``classify/``.  Heavy work and any
I/O stay out of import time.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from divergulent.classify import content as content_mod
from divergulent.classify import measure
from divergulent.classify.claim import CLAIM_RULE_VERSION, extract_claim
from divergulent.classify.content import CONTENT_RULE_VERSION
from divergulent.classify.rules import RULES_VERSION, classify_content

# Benign claimed categories: an author SAYING the patch is one of these while
# the content is a substantive code change is the "claims trivial / docs but
# actually changes code" case the threat model centres on.
_BENIGN_CLAIM_CATEGORIES = frozenset({'documentation', 'packaging'})

# How many sample fingerprints to show per bucket in the printed summary and
# findings note.
DEFAULT_SAMPLES = 5


# ---------------------------------------------------------------------------
# Per-fingerprint classification record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Classification:
    """The deterministic classification of one distinct fingerprint.

    Content is classified once per fingerprint (the normalised diff is
    identical across occurrences); the claim is taken from the representative
    occurrence only (see the module docstring -- an open question in the plan).
    """

    fingerprint: str
    representative_sha: str
    representative_package: str
    representative_patch_name: str

    content_category: str
    claim_category: str
    confidence: str
    consistency: str
    review_flag: bool

    n_occurrences: int
    n_packages: int

    flag_count: int
    flag_details: list[str]
    """Distinct dangerous-construct flag details, sorted (e.g. ``['shell-out']``)."""

    signals: list[str]
    """The content verdict's evidence signals, in rule order."""

    rule_ids: list[str]
    """The content rule ids that fired, in precedence order."""

    flag_evidence: list[str] = field(default_factory=list)
    """The trimmed offending lines for each flag (review context; not persisted
    as a column but used in the findings samples)."""

    touches_code: bool = False
    """Whether the content touches a code file (drives the review flag)."""


@dataclass
class ClassifyResult:
    """The full set of per-fingerprint classifications plus the aggregate counts."""

    classifications: list[Classification]

    @property
    def total_fingerprints(self) -> int:
        return len(self.classifications)

    @property
    def total_occurrences(self) -> int:
        return sum(c.n_occurrences for c in self.classifications)


# ---------------------------------------------------------------------------
# Consistency and review-flag logic
# ---------------------------------------------------------------------------

def _consistency(claim_category: str, content_category: str) -> str:
    """Compare the author's CLAIM against the content verdict.

    * ``'claim-unknown'`` -- the author gave no usable category signal, so there
      is nothing to compare against.
    * ``'content-substantive'`` -- the content verdict is ``unknown``: content
      could not confirm a category.  This is NOT itself a mismatch (the claim
      may well be right); it just means content cannot corroborate it.  The
      review flag, not consistency, carries the "benign claim over real code"
      alarm.
    * ``'agree'`` / ``'disagree'`` -- both sides named a category; they match or
      they do not.

    ``claim-unknown`` is checked first: if the author said nothing, we report
    that rather than the (vacuous) content-substantive observation.
    """
    if claim_category == 'unknown':
        return 'claim-unknown'
    if content_category == 'unknown':
        return 'content-substantive'
    return 'agree' if claim_category == content_category else 'disagree'


def _review_flag(claim_category: str, content_category: str,
                 touches_code: bool, has_dangerous_flag: bool) -> bool:
    """The loudest signal: does a human need to look at this fingerprint?

    True when EITHER:

    * a dangerous-construct flag fired (a construct worth a look was added to a
      code file -- never a malice verdict, just a candidate); OR
    * the author claims something benign (documentation/packaging) but the
      content is substantive AND touches code.  This is the "claims trivial /
      docs but actually changes code" case at the heart of the threat model: a
      malicious diff hiding behind a ``docs/typo.patch`` name and a "fix
      spelling" description must not get a free pass.

    A benign claim over substantive NON-code content (e.g. a large data-file
    change) does not flag here -- the code-touch gate keeps this from crying
    wolf on big translation/po updates.
    """
    if has_dangerous_flag:
        return True
    if claim_category in _BENIGN_CLAIM_CATEGORIES and content_category == 'unknown' and touches_code:
        return True
    return False


# ---------------------------------------------------------------------------
# Driving the index
# ---------------------------------------------------------------------------

def _read_fingerprint_groups(index_path: str) -> dict[str, dict]:
    """Group the index's ``patch`` rows by fingerprint.

    Returns ``{fingerprint: {'rep_sha', 'rep_package', 'rep_patch_name',
    'n_occurrences', 'packages'}}``.  The representative is the FIRST row seen
    for a fingerprint in row order, which is stable for a given index build.
    """
    connection = sqlite3.connect(index_path)
    try:
        rows = connection.execute(
            'SELECT fingerprint, raw_sha256, source_package, patch_name FROM patch').fetchall()
    finally:
        connection.close()

    groups: dict[str, dict] = {}
    for fingerprint, raw_sha256, source_package, patch_name in rows:
        group = groups.get(fingerprint)
        if group is None:
            group = {
                'rep_sha': raw_sha256,
                'rep_package': source_package,
                'rep_patch_name': patch_name,
                'n_occurrences': 0,
                'packages': set(),
            }
            groups[fingerprint] = group
        group['n_occurrences'] += 1
        group['packages'].add(source_package)
    return groups


@dataclass(frozen=True)
class ClassifiedFingerprint:
    """The shared per-fingerprint pass result: counts + the raw extractor output.

    One record per distinct fingerprint, carrying everything BOTH callers (the
    measurement in ``classify_index`` and the phase-3 recorder in ``record.py``)
    need: the recurrence counts and representative provenance, plus the raw
    ``claim`` / ``profile`` / ``verdict`` objects so neither caller re-runs the
    extractors.  Factoring the per-fingerprint work here keeps the
    measurement and the ledger recorder reading the diff exactly once and in
    exactly the same way -- no duplication, no drift.
    """

    fingerprint: str
    n_occurrences: int
    n_packages: int
    representative_sha: str
    representative_package: str
    representative_patch_name: str

    claim: object
    """The ``claim.Claim`` for the representative occurrence."""

    profile: object
    """The ``content.ContentProfile`` for the (fingerprint-stable) body."""

    verdict: object
    """The ``rules.ContentVerdict`` for the body (category + signals + flags)."""


def iter_classified(corpus_dir: str, index_path: str):
    """Yield one :class:`ClassifiedFingerprint` per distinct fingerprint.

    The single shared per-fingerprint pass over the phase-1 index: for each
    distinct fingerprint (sorted for determinism) it reads the representative
    body once via ``measure.read_body`` and runs ``extract_claim`` +
    ``content.profile`` + ``classify_content``, yielding the raw extractor
    objects alongside the recurrence counts.  Both ``classify_index`` (the
    phase-2 measurement) and the phase-3 ledger recorder consume this generator,
    so the diff is read and classified exactly once per fingerprint and in one
    place.  Pure apart from reading the index and the body files (no network, no
    subprocess).
    """
    groups = _read_fingerprint_groups(index_path)
    for fingerprint in sorted(groups):
        group = groups[fingerprint]
        rep_sha = group['rep_sha']
        body = measure.read_body(corpus_dir, rep_sha)

        claim = extract_claim(group['rep_patch_name'], body)
        prof = content_mod.profile(body)
        verdict = classify_content(claim, prof, body)

        yield ClassifiedFingerprint(
            fingerprint=fingerprint,
            n_occurrences=group['n_occurrences'],
            n_packages=len(group['packages']),
            representative_sha=rep_sha,
            representative_package=group['rep_package'],
            representative_patch_name=group['rep_patch_name'],
            claim=claim,
            profile=prof,
            verdict=verdict,
        )


def classify_index(corpus_dir: str, index_path: str) -> ClassifyResult:
    """Classify every distinct fingerprint in ``index_path`` against ``corpus_dir``.

    Builds the phase-2 ``Classification`` list from the shared
    :func:`iter_classified` pass, deriving consistency + review flag from each
    record's raw extractor output.  Pure apart from reading the index and the
    body files (no network, no subprocess).
    """
    classifications: list[Classification] = []
    for record in iter_classified(corpus_dir, index_path):
        claim = record.claim
        prof = record.profile
        verdict = record.verdict

        flag_details = sorted({flag.detail for flag in verdict.flags})
        flag_evidence = [flag.evidence for flag in verdict.flags]
        has_dangerous_flag = bool(verdict.flags)

        consistency = _consistency(claim.claimed_category, verdict.content_category)
        review_flag = _review_flag(
            claim.claimed_category, verdict.content_category,
            prof.touches_code, has_dangerous_flag)

        classifications.append(Classification(
            fingerprint=record.fingerprint,
            representative_sha=record.representative_sha,
            representative_package=record.representative_package,
            representative_patch_name=record.representative_patch_name,
            content_category=verdict.content_category,
            claim_category=claim.claimed_category,
            confidence=verdict.confidence,
            consistency=consistency,
            review_flag=review_flag,
            n_occurrences=record.n_occurrences,
            n_packages=record.n_packages,
            flag_count=len(verdict.flags),
            flag_details=flag_details,
            signals=list(verdict.signals),
            rule_ids=list(verdict.rule_ids),
            flag_evidence=flag_evidence,
            touches_code=prof.touches_code,
        ))

    return ClassifyResult(classifications=classifications)


# ---------------------------------------------------------------------------
# The classification sqlite table
# ---------------------------------------------------------------------------

def write_classification(result: ClassifyResult, out_path: str) -> int:
    """Write the ``classification`` sqlite table; overwrite any stale file.

    One row per distinct fingerprint.  A ``meta`` table records the
    claim/content/rules versions so phase 3 can detect stale verdicts.  Returns
    the number of rows written.
    """
    if os.path.exists(out_path):
        os.unlink(out_path)
    directory = os.path.dirname(out_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    connection = sqlite3.connect(out_path)
    try:
        connection.execute(
            'CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        connection.executemany(
            'INSERT INTO meta (key, value) VALUES (?, ?)',
            [('claim_rule_version', str(CLAIM_RULE_VERSION)),
             ('content_rule_version', str(CONTENT_RULE_VERSION)),
             ('rules_version', str(RULES_VERSION))])
        connection.execute(
            'CREATE TABLE classification ('
            'fingerprint TEXT NOT NULL, '
            'representative_sha TEXT NOT NULL, '
            'content_category TEXT NOT NULL, '
            'claim_category TEXT NOT NULL, '
            'confidence TEXT NOT NULL, '
            'consistency TEXT NOT NULL, '
            'review_flag INTEGER NOT NULL, '
            'n_occurrences INTEGER NOT NULL, '
            'n_packages INTEGER NOT NULL, '
            'flag_count INTEGER NOT NULL, '
            'flag_details TEXT NOT NULL, '
            'signals TEXT NOT NULL, '
            'rule_ids TEXT NOT NULL)')
        connection.executemany(
            'INSERT INTO classification ('
            'fingerprint, representative_sha, content_category, claim_category, '
            'confidence, consistency, review_flag, n_occurrences, n_packages, '
            'flag_count, flag_details, signals, rule_ids) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            [(c.fingerprint, c.representative_sha, c.content_category, c.claim_category,
              c.confidence, c.consistency, int(c.review_flag), c.n_occurrences, c.n_packages,
              c.flag_count, ', '.join(c.flag_details), ' | '.join(c.signals),
              ', '.join(c.rule_ids)) for c in result.classifications])
        connection.execute(
            'CREATE INDEX idx_classification_fingerprint ON classification (fingerprint)')
        connection.execute(
            'CREATE INDEX idx_classification_content_category '
            'ON classification (content_category)')
        connection.commit()
    finally:
        connection.close()
    return len(result.classifications)


# ---------------------------------------------------------------------------
# The measurement (summary)
# ---------------------------------------------------------------------------

@dataclass
class Summary:
    """The headline measurement over a ``ClassifyResult``.

    Weighted two ways throughout: by DISTINCT fingerprint (how much of the
    distinct work settles) and by OCCURRENCE (how much of the carried-patch mass
    settles), since one recurring fingerprint can stand for many carried
    patches.
    """

    total_fingerprints: int
    total_occurrences: int

    category_fingerprints: dict[str, int]
    category_occurrences: dict[str, int]

    consistency_fingerprints: dict[str, int]

    review_flag_fingerprints: int
    review_flag_occurrences: int

    # Dangerous-construct flag counts BY detail (so we can see how noisy each
    # pattern is -- especially whether the backtick/shell-out pattern over-fires).
    flag_detail_fingerprints: dict[str, int]
    flag_detail_occurrences: dict[str, int]

    samples_by_category: dict[str, list[Classification]]
    review_samples: list[Classification]

    def settled_fraction(self) -> float:
        """Fraction of DISTINCT fingerprints settled (packaging + documentation)."""
        if not self.total_fingerprints:
            return 0.0
        settled = (self.category_fingerprints.get('packaging', 0)
                   + self.category_fingerprints.get('documentation', 0))
        return settled / self.total_fingerprints


def summarise(result: ClassifyResult, *, samples: int = DEFAULT_SAMPLES) -> Summary:
    """Compute the headline measurement from a ``ClassifyResult``."""
    category_fp: Counter[str] = Counter()
    category_occ: Counter[str] = Counter()
    consistency_fp: Counter[str] = Counter()
    flag_detail_fp: Counter[str] = Counter()
    flag_detail_occ: Counter[str] = Counter()
    review_fp = 0
    review_occ = 0
    samples_by_category: dict[str, list[Classification]] = defaultdict(list)
    review_samples: list[Classification] = []

    # Rank samples by occurrence so the most representative examples surface.
    ranked = sorted(result.classifications, key=lambda c: (c.n_occurrences, c.fingerprint), reverse=True)

    for c in ranked:
        category_fp[c.content_category] += 1
        category_occ[c.content_category] += c.n_occurrences
        consistency_fp[c.consistency] += 1
        for detail in c.flag_details:
            flag_detail_fp[detail] += 1
            flag_detail_occ[detail] += c.n_occurrences
        if c.review_flag:
            review_fp += 1
            review_occ += c.n_occurrences
            if len(review_samples) < samples:
                review_samples.append(c)
        if len(samples_by_category[c.content_category]) < samples:
            samples_by_category[c.content_category].append(c)

    return Summary(
        total_fingerprints=result.total_fingerprints,
        total_occurrences=result.total_occurrences,
        category_fingerprints=dict(category_fp),
        category_occurrences=dict(category_occ),
        consistency_fingerprints=dict(consistency_fp),
        review_flag_fingerprints=review_fp,
        review_flag_occurrences=review_occ,
        flag_detail_fingerprints=dict(flag_detail_fp),
        flag_detail_occurrences=dict(flag_detail_occ),
        samples_by_category=dict(samples_by_category),
        review_samples=review_samples,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

# The content categories a deterministic verdict can produce, in report order.
# ``unknown`` is the substantive phase-4 residue.
_REPORT_CATEGORIES = ('packaging', 'documentation', 'test', 'unknown')


def _pct(part: int, whole: int) -> float:
    return 100.0 * part / whole if whole else 0.0


def _sorted_by_count(counts: dict[str, int]) -> list[str]:
    """Keys of ``counts`` ordered by descending count, ties broken by key."""
    return sorted(counts, key=lambda key: (-counts[key], key))


def _category_label(category: str) -> str:
    """Human label; ``unknown`` is the substantive residue handed to phase 4."""
    if category == 'unknown':
        return 'unknown-substantive (phase-4 residue)'
    return category


def render_findings(summary: Summary) -> str:
    """Render the markdown findings note: the headline deterministic-settlement
    measurement plus the review-flag and dangerous-construct accounting."""
    lines: list[str] = []
    lines.append('# Phase 2 findings: deterministic patch classification')
    lines.append('')
    lines.append(
        'Headline: of **%d distinct fingerprints** (%d carried-patch occurrences), '
        '**%.1f%% settle deterministically** (packaging + documentation); the rest '
        'is the substantive residue handed to phase 4.' % (
            summary.total_fingerprints, summary.total_occurrences,
            100.0 * summary.settled_fraction()))
    lines.append('')

    lines.append('## Content category (how much settles vs the phase-4 residue)')
    lines.append('')
    lines.append('| content category | fingerprints | % fp | occurrences | % occ |')
    lines.append('| --- | ---: | ---: | ---: | ---: |')
    for category in _REPORT_CATEGORIES:
        fp_count = summary.category_fingerprints.get(category, 0)
        occ_count = summary.category_occurrences.get(category, 0)
        lines.append('| %s | %d | %.1f%% | %d | %.1f%% |' % (
            _category_label(category), fp_count,
            _pct(fp_count, summary.total_fingerprints), occ_count,
            _pct(occ_count, summary.total_occurrences)))
    lines.append('')

    lines.append('## Review flag (the loudest signal)')
    lines.append('')
    lines.append(
        'Review-flagged: **%d fingerprints** (%.1f%%), **%d occurrences** (%.1f%%). '
        'A review flag fires on any dangerous-construct candidate, or on a benign '
        'claim (documentation/packaging) over substantive content that touches code.' % (
            summary.review_flag_fingerprints,
            _pct(summary.review_flag_fingerprints, summary.total_fingerprints),
            summary.review_flag_occurrences,
            _pct(summary.review_flag_occurrences, summary.total_occurrences)))
    lines.append('')

    lines.append('## Dangerous-construct flags by detail (how noisy each pattern is)')
    lines.append('')
    if not summary.flag_detail_fingerprints:
        lines.append('_No dangerous-construct candidates fired._')
        lines.append('')
    else:
        lines.append('| detail | fingerprints | occurrences |')
        lines.append('| --- | ---: | ---: |')
        for detail in _sorted_by_count(summary.flag_detail_fingerprints):
            lines.append('| %s | %d | %d |' % (
                detail, summary.flag_detail_fingerprints[detail],
                summary.flag_detail_occurrences.get(detail, 0)))
        lines.append('')

    lines.append('## Claim/content consistency')
    lines.append('')
    lines.append('| consistency | fingerprints |')
    lines.append('| --- | ---: |')
    for consistency in _sorted_by_count(summary.consistency_fingerprints):
        lines.append('| %s | %d |' % (consistency, summary.consistency_fingerprints[consistency]))
    lines.append('')

    lines.append('## Sample review-flagged fingerprints (with evidence)')
    lines.append('')
    if not summary.review_samples:
        lines.append('_None._')
        lines.append('')
    for c in summary.review_samples:
        lines.append('### `%s` — claims %s, content %s (%d occ, %d pkgs)' % (
            c.fingerprint[:16], c.claim_category, c.content_category,
            c.n_occurrences, c.n_packages))
        lines.append('')
        lines.append('- representative: `%s` in `%s`' % (
            c.representative_patch_name, c.representative_package))
        lines.append('- consistency: %s' % c.consistency)
        if c.flag_details:
            lines.append('- dangerous-construct: %s' % ', '.join(c.flag_details))
        for evidence in c.flag_evidence:
            lines.append('  - evidence: `%s`' % evidence)
        lines.append('')

    lines.append('## Sample of each content category')
    lines.append('')
    for category in _REPORT_CATEGORIES:
        lines.append('### %s' % _category_label(category))
        lines.append('')
        category_samples = summary.samples_by_category.get(category, [])
        if not category_samples:
            lines.append('_None._')
            lines.append('')
            continue
        for c in category_samples:
            signal = c.signals[0] if c.signals else ''
            lines.append('- `%s` (%d occ, %d pkgs) — claims %s; %s' % (
                c.fingerprint[:16], c.n_occurrences, c.n_packages, c.claim_category, signal))
        lines.append('')

    return '\n'.join(lines)


def write_findings(summary: Summary, findings_path: str) -> None:
    """Write the markdown findings note to ``findings_path``."""
    directory = os.path.dirname(findings_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(findings_path, 'w', encoding='utf-8') as handle:
        handle.write(render_findings(summary))


def _print_summary(summary: Summary) -> None:
    print('classified %d distinct fingerprints (%d occurrences); '
          '%.1f%% settle deterministically' % (
              summary.total_fingerprints, summary.total_occurrences,
              100.0 * summary.settled_fraction()))
    print('content category (fingerprints / occurrences):')
    for category in _REPORT_CATEGORIES:
        fp_count = summary.category_fingerprints.get(category, 0)
        occ_count = summary.category_occurrences.get(category, 0)
        print('  %-32s fp=%d (%.1f%%)  occ=%d (%.1f%%)' % (
            _category_label(category), fp_count,
            _pct(fp_count, summary.total_fingerprints), occ_count,
            _pct(occ_count, summary.total_occurrences)))
    print('review-flagged: fp=%d (%.1f%%)  occ=%d (%.1f%%)' % (
        summary.review_flag_fingerprints,
        _pct(summary.review_flag_fingerprints, summary.total_fingerprints),
        summary.review_flag_occurrences,
        _pct(summary.review_flag_occurrences, summary.total_occurrences)))
    if summary.flag_detail_fingerprints:
        print('dangerous-construct flags by detail:')
        for detail in _sorted_by_count(summary.flag_detail_fingerprints):
            print('  %-28s fp=%d  occ=%d' % (
                detail, summary.flag_detail_fingerprints[detail],
                summary.flag_detail_occurrences.get(detail, 0)))
    else:
        print('dangerous-construct flags: none')
    print('consistency (fingerprints):')
    for consistency in _sorted_by_count(summary.consistency_fingerprints):
        print('  %-20s %d' % (consistency, summary.consistency_fingerprints[consistency]))


def main(argv: list[str] | None = None) -> int:
    """``python -m divergulent.classify.classify``: classify a corpus offline."""
    parser = argparse.ArgumentParser(
        prog='python -m divergulent.classify.classify',
        description='Classify each distinct fingerprint in a phase-1 index by what its '
                    'diff does (curation-side; offline). Emits a classification sqlite '
                    'table and a markdown findings note. No malice is ever pronounced.')
    parser.add_argument('corpus_dir', help='directory of a corpus built by classify.corpus')
    parser.add_argument('--index', default=None,
                        help='path to the phase-1 sqlite fingerprint index (default: '
                             '<corpus_dir>/fingerprints.sqlite)')
    parser.add_argument('--out', default=None,
                        help='path for the classification sqlite table (default: '
                             '<corpus_dir>/classification.sqlite)')
    parser.add_argument('--findings', default=None,
                        help='path for the markdown findings note (default: '
                             '<corpus_dir>/classification-findings.md)')
    parser.add_argument('--samples', type=int, default=DEFAULT_SAMPLES,
                        help='samples shown per bucket in the report (default: %d)' % DEFAULT_SAMPLES)
    args = parser.parse_args(argv)

    index_path = args.index or os.path.join(args.corpus_dir, 'fingerprints.sqlite')
    out_path = args.out or os.path.join(args.corpus_dir, 'classification.sqlite')
    findings_path = args.findings or os.path.join(args.corpus_dir, 'classification-findings.md')

    result = classify_index(args.corpus_dir, index_path)
    rows = write_classification(result, out_path)
    summary = summarise(result, samples=args.samples)
    write_findings(summary, findings_path)

    _print_summary(summary)
    print('wrote classification table (%d fingerprints): %s' % (rows, out_path))
    print('wrote findings note: %s' % findings_path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
