"""Fingerprint, deduplicate, and measure a carried-patch corpus (curation-side).

This is the measurement half of phase 1. It reads a corpus built by
``corpus.py`` -- the content-addressed bodies plus the ``patches.jsonl`` /
``packages.jsonl`` manifests -- applies ``fingerprint.py`` ACROSS THE
NORMALISATION MATRIX, deduplicates, and emits the headline number that reframes
"60k carried patches" as "N distinct patches". It does NO classification: no
categories, no rules, no DEP-3 verdicts. Only fingerprint, dedup, and measure.

It reads the corpus OFFLINE (no network), so the sensitivity matrix can be
re-run and re-measured without re-crawling the archive.

The deliverables, all produced by ``measure_corpus`` / the CLI:

* THE SENSITIVITY MATRIX -- the distinct count under all four variants of
  ``(strip_path, drop_context)``. This shows how much the path/context choice
  moves the headline number before the canonical v1 is frozen. The canonical
  variant is ``strip_path=True, drop_context=False``.
* For the canonical variant: the MULTIPLICITY HISTOGRAM (how many fingerprints
  recur across exactly k distinct source packages -- the long tail is the
  recurring boilerplate), the TOP-RECURRING list with sample bodies, and the
  PER-PACKAGE patches-vs-distinct comparison.
* A self-describing sqlite FINGERPRINT INDEX (the canonical variant) that later
  phases join on by ``fingerprint``.
* A markdown FINDINGS NOTE summarising all of the above plus the honest
  accounting (packages by state, fetch failures, non-quilt skips).

Builder-only: no client command imports it, and heavy work stays out of import
time.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from divergulent.classify import fingerprint as fingerprint_module

# The canonical/default v1 variant; frozen by step 1d once the real corpus has
# been measured. Until then the matrix below reports all four so the choice is
# made by measuring, not a priori.
NORMALISATION_VERSION = 1
CANONICAL_STRIP_PATH = True
CANONICAL_DROP_CONTEXT = False

# The full sensitivity matrix: (strip_path, drop_context). Ordered so the
# canonical variant is first.
MATRIX_VARIANTS: tuple[tuple[bool, bool], ...] = (
    (True, False),
    (True, True),
    (False, False),
    (False, True),
)

DEFAULT_TOP_N = 25
# Lines of normalised body shown as a human-eyeballable sample of a recurring
# fingerprint.
SAMPLE_LINES = 12


def _variant_label(strip_path: bool, drop_context: bool) -> str:
    """A short stable label for a matrix variant, e.g. ``strip_path,keep_context``."""
    path = 'strip_path' if strip_path else 'keep_path'
    context = 'drop_context' if drop_context else 'keep_context'
    return '%s,%s' % (path, context)


@dataclass
class VariantMeasurement:
    """Headline counts for one ``(strip_path, drop_context)`` matrix cell."""

    strip_path: bool
    drop_context: bool
    patch_rows: int = 0
    distinct_bodies: int = 0
    distinct_fingerprints: int = 0

    @property
    def label(self) -> str:
        return _variant_label(self.strip_path, self.drop_context)

    @property
    def dedup_ratio(self) -> float:
        """Patch rows per distinct fingerprint (1.0 means no dedup)."""
        if self.distinct_fingerprints == 0:
            return 0.0
        return self.patch_rows / self.distinct_fingerprints


@dataclass
class TopRecurring:
    """One of the most cross-package-recurring canonical fingerprints."""

    fingerprint: str
    package_count: int
    row_count: int
    sample: str


@dataclass
class PackageDedup:
    """A package whose patch count exceeds its distinct-fingerprint count."""

    source_package: str
    patches: int
    distinct_fingerprints: int


@dataclass
class Accounting:
    """The honest accounting: package outcomes that are NOT silently dropped."""

    packages_total: int = 0
    by_state: dict[str, int] = field(default_factory=dict)
    fetch_failures: int = 0
    non_quilt_skipped: int = 0


@dataclass
class Measurement:
    """The full phase-1 measurement of a corpus (canonical + matrix)."""

    patch_rows: int
    distinct_bodies: int
    canonical_label: str
    matrix: list[VariantMeasurement]
    multiplicity_histogram: list[tuple[str, int]]
    top_recurring: list[TopRecurring]
    intra_package_dedup: list[PackageDedup]
    accounting: Accounting

    @property
    def canonical(self) -> VariantMeasurement:
        for variant in self.matrix:
            if variant.label == self.canonical_label:
                return variant
        raise KeyError(self.canonical_label)


def _read_jsonl(path: str):
    """Yield decoded rows from a JSONL manifest; empty if the file is absent."""
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_body(corpus_dir: str, sha: str) -> str:
    """Read a raw patch body from the content-addressed store by its sha256.

    A small public reader over the corpus' content-addressed scheme so callers
    do not reach into ``corpus._body_path`` (private). Mirrors how the corpus
    lays bodies down under ``bodies/<sha[:2]>/<sha>``.
    """
    path = os.path.join(corpus_dir, 'bodies', sha[:2], sha)
    with open(path, encoding='utf-8') as handle:
        return handle.read()


# Histogram buckets for fingerprint multiplicity (the number of DISTINCT source
# packages a fingerprint recurs across). Ordered, non-overlapping, covering
# [1, inf). The long tail is bucketed so the recurring-boilerplate mass reads at
# a glance rather than as a flat list of every k.
_HISTOGRAM_BUCKETS: tuple[tuple[str, int, "int | None"], ...] = (
    ('1', 1, 1),
    ('2', 2, 2),
    ('3-5', 3, 5),
    ('6-10', 6, 10),
    ('11-50', 11, 50),
    ('51+', 51, None),
)


def _bucket_label(count: int) -> str:
    for label, low, high in _HISTOGRAM_BUCKETS:
        if count >= low and (high is None or count <= high):
            return label
    return _HISTOGRAM_BUCKETS[-1][0]


def _fingerprint_body(raw_text: str, *, strip_path: bool, drop_context: bool) -> str:
    """Just the hex digest for a body under a matrix variant (version pinned)."""
    _version, digest = fingerprint_module.fingerprint(
        raw_text, version=NORMALISATION_VERSION, strip_path=strip_path, drop_context=drop_context)
    return digest


def measure_corpus(corpus_dir: str, *, top_n: int = DEFAULT_TOP_N) -> Measurement:
    """Read ``corpus_dir`` and compute the full phase-1 measurement.

    Bodies are fingerprinted ONCE per distinct raw body per matrix variant (the
    content-addressed store means thousands of provenance rows collapse to far
    fewer body reads), then results are projected back over the provenance rows.
    """
    patch_rows = list(_read_jsonl(os.path.join(corpus_dir, 'patches.jsonl')))
    package_rows = list(_read_jsonl(os.path.join(corpus_dir, 'packages.jsonl')))

    # Distinct raw bodies referenced by provenance, read once each.
    distinct_shas = sorted({row['raw_sha256'] for row in patch_rows})
    bodies = {sha: read_body(corpus_dir, sha) for sha in distinct_shas}

    # Per variant: map each raw sha -> fingerprint hex. One normalise per body.
    sha_to_fp: dict[tuple[bool, bool], dict[str, str]] = {}
    for strip_path, drop_context in MATRIX_VARIANTS:
        sha_to_fp[(strip_path, drop_context)] = {
            sha: _fingerprint_body(body, strip_path=strip_path, drop_context=drop_context)
            for sha, body in bodies.items()}

    matrix: list[VariantMeasurement] = []
    for strip_path, drop_context in MATRIX_VARIANTS:
        mapping = sha_to_fp[(strip_path, drop_context)]
        distinct_fps = {mapping[row['raw_sha256']] for row in patch_rows}
        matrix.append(VariantMeasurement(
            strip_path=strip_path, drop_context=drop_context,
            patch_rows=len(patch_rows), distinct_bodies=len(distinct_shas),
            distinct_fingerprints=len(distinct_fps)))

    canonical_key = (CANONICAL_STRIP_PATH, CANONICAL_DROP_CONTEXT)
    canonical_map = sha_to_fp[canonical_key]

    # Canonical-variant aggregates over the provenance rows.
    fp_packages: dict[str, set[str]] = defaultdict(set)
    fp_rows: Counter[str] = Counter()
    pkg_patch_count: Counter[str] = Counter()
    pkg_fps: dict[str, set[str]] = defaultdict(set)
    for row in patch_rows:
        digest = canonical_map[row['raw_sha256']]
        pkg = row['source_package']
        fp_packages[digest].add(pkg)
        fp_rows[digest] += 1
        pkg_patch_count[pkg] += 1
        pkg_fps[pkg].add(digest)

    # Multiplicity histogram: bucket fingerprints by their distinct-package count.
    bucket_counts: Counter[str] = Counter()
    for digest, packages in fp_packages.items():
        bucket_counts[_bucket_label(len(packages))] += 1
    histogram = [(label, bucket_counts.get(label, 0)) for label, _low, _high in _HISTOGRAM_BUCKETS]

    # Top-recurring: most distinct packages, tie-broken by row count then digest.
    ranked = sorted(
        fp_packages, key=lambda d: (len(fp_packages[d]), fp_rows[d], d), reverse=True)
    top_recurring: list[TopRecurring] = []
    for digest in ranked[:top_n]:
        # The sample is the canonical-normalised body; find any sha mapping here.
        sample_sha = next(sha for sha in distinct_shas if canonical_map[sha] == digest)
        normalised = fingerprint_module.normalise(
            bodies[sample_sha], version=NORMALISATION_VERSION,
            strip_path=CANONICAL_STRIP_PATH, drop_context=CANONICAL_DROP_CONTEXT)
        sample = '\n'.join(normalised.splitlines()[:SAMPLE_LINES])
        top_recurring.append(TopRecurring(
            fingerprint=digest, package_count=len(fp_packages[digest]),
            row_count=fp_rows[digest], sample=sample))

    # Per-package intra-package dedup: report only where patches > distinct.
    intra = []
    for pkg in sorted(pkg_patch_count):
        patches = pkg_patch_count[pkg]
        distinct = len(pkg_fps[pkg])
        if patches != distinct:
            intra.append(PackageDedup(source_package=pkg, patches=patches, distinct_fingerprints=distinct))

    accounting = _account(package_rows)

    return Measurement(
        patch_rows=len(patch_rows), distinct_bodies=len(distinct_shas),
        canonical_label=_variant_label(CANONICAL_STRIP_PATH, CANONICAL_DROP_CONTEXT),
        matrix=matrix, multiplicity_histogram=histogram, top_recurring=top_recurring,
        intra_package_dedup=intra, accounting=accounting)


def _account(package_rows: list[dict]) -> Accounting:
    """The honest accounting from ``packages.jsonl``: states, failures, skips."""
    by_state: Counter[str] = Counter()
    fetch_failures = 0
    non_quilt = 0
    for row in package_rows:
        by_state[row['state']] += 1
        error = row.get('error') or ''
        if error.startswith('fetch-failed') or error.startswith('fetch-error'):
            fetch_failures += 1
        elif error.startswith('non-quilt-format'):
            non_quilt += 1
    return Accounting(
        packages_total=len(package_rows), by_state=dict(sorted(by_state.items())),
        fetch_failures=fetch_failures, non_quilt_skipped=non_quilt)


def write_index(corpus_dir: str, index_path: str) -> int:
    """Write the self-describing sqlite fingerprint index for the canonical variant.

    One ``patch`` row per patch-provenance row, carrying the canonical
    fingerprint that later phases join on; a ``meta`` table records the
    normalisation version and the variant knobs so the index is self-describing.
    Returns the number of patch rows written.
    """
    patch_rows = list(_read_jsonl(os.path.join(corpus_dir, 'patches.jsonl')))
    distinct_shas = sorted({row['raw_sha256'] for row in patch_rows})
    bodies = {sha: read_body(corpus_dir, sha) for sha in distinct_shas}
    sha_to_fp = {
        sha: _fingerprint_body(body, strip_path=CANONICAL_STRIP_PATH, drop_context=CANONICAL_DROP_CONTEXT)
        for sha, body in bodies.items()}

    # Overwrite any stale index so a re-run is deterministic.
    if os.path.exists(index_path):
        os.unlink(index_path)
    directory = os.path.dirname(index_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    connection = sqlite3.connect(index_path)
    try:
        connection.execute(
            'CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        connection.executemany(
            'INSERT INTO meta (key, value) VALUES (?, ?)',
            [('normalisation_version', str(NORMALISATION_VERSION)),
             ('strip_path', str(CANONICAL_STRIP_PATH)),
             ('drop_context', str(CANONICAL_DROP_CONTEXT)),
             ('variant', _variant_label(CANONICAL_STRIP_PATH, CANONICAL_DROP_CONTEXT))])
        connection.execute(
            'CREATE TABLE patch ('
            'source_package TEXT NOT NULL, '
            'version TEXT NOT NULL, '
            'patch_name TEXT NOT NULL, '
            'raw_sha256 TEXT NOT NULL, '
            'normalisation_version INTEGER NOT NULL, '
            'fingerprint TEXT NOT NULL)')
        connection.executemany(
            'INSERT INTO patch (source_package, version, patch_name, raw_sha256, '
            'normalisation_version, fingerprint) VALUES (?, ?, ?, ?, ?, ?)',
            [(row['source_package'], row['version'], row['patch_name'], row['raw_sha256'],
              NORMALISATION_VERSION, sha_to_fp[row['raw_sha256']]) for row in patch_rows])
        connection.execute('CREATE INDEX idx_patch_fingerprint ON patch (fingerprint)')
        connection.execute('CREATE INDEX idx_patch_source_package ON patch (source_package)')
        connection.commit()
    finally:
        connection.close()
    return len(patch_rows)


def render_findings(measurement: Measurement) -> str:
    """Render the markdown findings note from a measurement."""
    canonical = measurement.canonical
    lines: list[str] = []
    lines.append('# Phase 1 findings: the distinct-patch count')
    lines.append('')
    lines.append(
        'Headline: **≈%d carried patches → %d distinct** '
        '(canonical normalisation v%d, %s; dedup ratio %.2fx).' % (
            measurement.patch_rows, canonical.distinct_fingerprints,
            NORMALISATION_VERSION, canonical.label, canonical.dedup_ratio))
    lines.append('')
    lines.append('Distinct raw bodies (pre-normalisation): %d.' % measurement.distinct_bodies)
    lines.append('')

    lines.append('## Sensitivity matrix (path in/out x context in/out)')
    lines.append('')
    lines.append('| variant | patch rows | distinct bodies | distinct fingerprints | dedup ratio |')
    lines.append('| --- | ---: | ---: | ---: | ---: |')
    for variant in measurement.matrix:
        marker = ' (canonical)' if variant.label == measurement.canonical_label else ''
        lines.append('| %s%s | %d | %d | %d | %.2fx |' % (
            variant.label, marker, variant.patch_rows, variant.distinct_bodies,
            variant.distinct_fingerprints, variant.dedup_ratio))
    lines.append('')

    lines.append('## Multiplicity histogram (distinct fingerprints by package recurrence)')
    lines.append('')
    lines.append('| distinct packages | fingerprints |')
    lines.append('| --- | ---: |')
    for label, count in measurement.multiplicity_histogram:
        lines.append('| %s | %d |' % (label, count))
    lines.append('')

    lines.append('## Top-recurring fingerprints (canonical)')
    lines.append('')
    if not measurement.top_recurring:
        lines.append('_None._')
        lines.append('')
    for entry in measurement.top_recurring:
        lines.append('### `%s` — %d packages, %d rows' % (
            entry.fingerprint[:16], entry.package_count, entry.row_count))
        lines.append('')
        lines.append('```')
        lines.append(entry.sample)
        lines.append('```')
        lines.append('')

    lines.append('## Per-package intra-package dedup (patches != distinct)')
    lines.append('')
    if not measurement.intra_package_dedup:
        lines.append('_No package carries duplicate fingerprints internally._')
    else:
        lines.append('| source package | patches | distinct fingerprints |')
        lines.append('| --- | ---: | ---: |')
        for entry in measurement.intra_package_dedup:
            lines.append('| %s | %d | %d |' % (
                entry.source_package, entry.patches, entry.distinct_fingerprints))
    lines.append('')

    accounting = measurement.accounting
    lines.append('## Honest accounting')
    lines.append('')
    lines.append('Packages processed: %d.' % accounting.packages_total)
    lines.append('')
    lines.append('| state | packages |')
    lines.append('| --- | ---: |')
    for state, count in accounting.by_state.items():
        lines.append('| %s | %d |' % (state, count))
    lines.append('')
    lines.append('Fetch failures: %d. Non-quilt skipped: %d.' % (
        accounting.fetch_failures, accounting.non_quilt_skipped))
    lines.append('')
    return '\n'.join(lines)


def write_findings(measurement: Measurement, findings_path: str) -> None:
    """Write the markdown findings note to ``findings_path``."""
    directory = os.path.dirname(findings_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(findings_path, 'w', encoding='utf-8') as handle:
        handle.write(render_findings(measurement))


def _print_summary(measurement: Measurement) -> None:
    canonical = measurement.canonical
    print('headline: ~%d carried patches -> %d distinct (canonical %s, dedup %.2fx)' % (
        measurement.patch_rows, canonical.distinct_fingerprints,
        canonical.label, canonical.dedup_ratio))
    print('sensitivity matrix:')
    for variant in measurement.matrix:
        marker = ' *' if variant.label == measurement.canonical_label else '  '
        print('%s %-26s rows=%d bodies=%d fingerprints=%d ratio=%.2fx' % (
            marker, variant.label, variant.patch_rows, variant.distinct_bodies,
            variant.distinct_fingerprints, variant.dedup_ratio))
    print('accounting: %d packages; fetch failures=%d; non-quilt skipped=%d' % (
        measurement.accounting.packages_total, measurement.accounting.fetch_failures,
        measurement.accounting.non_quilt_skipped))


def main(argv: list[str] | None = None) -> int:
    """``python -m divergulent.classify.measure``: measure a corpus offline."""
    parser = argparse.ArgumentParser(
        prog='python -m divergulent.classify.measure',
        description='Fingerprint, deduplicate, and measure a carried-patch corpus '
                    '(curation-side; offline). Emits a sqlite fingerprint index and a '
                    'markdown findings note. No classification.')
    parser.add_argument('corpus_dir', help='directory of a corpus built by classify.corpus')
    parser.add_argument('--index', default=None,
                        help='path for the sqlite fingerprint index (default: '
                             '<corpus_dir>/fingerprints.sqlite)')
    parser.add_argument('--findings', default=None,
                        help='path for the markdown findings note (default: '
                             '<corpus_dir>/findings.md)')
    parser.add_argument('--top', type=int, default=DEFAULT_TOP_N,
                        help='size of the top-recurring list (default: %d)' % DEFAULT_TOP_N)
    args = parser.parse_args(argv)

    index_path = args.index or os.path.join(args.corpus_dir, 'fingerprints.sqlite')
    findings_path = args.findings or os.path.join(args.corpus_dir, 'findings.md')

    measurement = measure_corpus(args.corpus_dir, top_n=args.top)
    rows = write_index(args.corpus_dir, index_path)
    write_findings(measurement, findings_path)

    _print_summary(measurement)
    print('wrote sqlite index (%d patch rows): %s' % (rows, index_path))
    print('wrote findings note: %s' % findings_path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
