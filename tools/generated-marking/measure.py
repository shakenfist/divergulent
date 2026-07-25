#!/usr/bin/env python3
"""Prototype: measure ``divergulent.classify.generated.scan`` over a whole patch corpus.

Part of docs/plans/PLAN-generated-marking-phase-01-scanner.md (phase 1, step S2). This
is an EVALUATION prototype, not shipped classifier code: it walks a fingerprint-indexed
corpus, runs the scanner (and its measurement-only candidate-banner sweep) over one
representative body per fingerprint, and reports the numbers phase 1's findings document
adjudicates -- per-name match/banner rates, the name-set gap list, the patch coverage
distribution, a low-frequency-name adjudication list, the ``acinclude.m4`` check, a
multi-banner report, and the candidate do-not-edit family. No ledger, no routing, no
network: this is measurement only, feeding
docs/plans/PLAN-generated-marking-phase-01-findings.md.

Must be run from THIS worktree checkout with ``PYTHONPATH=.`` -- the operator's editable
install of divergulent tracks ``main``, not this branch, and would silently import the
wrong (or absent) ``generated`` module otherwise.

Usage:

    PYTHONPATH=. python3 tools/generated-marking/measure.py <corpus_dir> \\
        [--output results.json] [--limit N]

Corpus layout (read-only, never written):

* ``<corpus_dir>/fingerprints.sqlite``, table ``patch`` (source_package, version,
  patch_name, raw_sha256, normalisation_version, fingerprint).
* ``<corpus_dir>/bodies/<sha[:2]>/<sha>`` -- raw patch bodies, UTF-8 with
  ``errors='replace'`` on decode (a handful of bodies carry non-UTF-8 bytes).

One representative body is scanned per fingerprint: ``MIN(raw_sha256)`` within each
fingerprint group, which is deterministic and re-runs identically. Fingerprints whose
representative body file is missing from disk are skipped and counted, never treated as
a scan miss.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict

from divergulent.classify import content
from divergulent.classify import generated

# Caps on how many examples/samples each report section carries -- enough for a human to
# eyeball, small enough that the printed summary and the JSON stay readable.
_GAP_EXAMPLES = 5
_MULTI_BANNER_EXAMPLES = 10
_CANDIDATE_SAMPLES = 10
_CANDIDATE_TOP_N = 20
_LOW_FREQUENCY_MAX_TOTAL = 20


# ---------------------------------------------------------------------------
# Corpus reading
# ---------------------------------------------------------------------------

def representative_rows(corpus_dir: str, limit: int | None = None) -> list[tuple[str, str]]:
    """``(fingerprint, raw_sha256)`` for one representative body per fingerprint.

    ``MIN(raw_sha256)`` per fingerprint, ordered by fingerprint so the whole walk (and
    every "first N" sample it takes) is deterministic across re-runs.
    """
    connection = sqlite3.connect(os.path.join(corpus_dir, 'fingerprints.sqlite'))
    try:
        rows = connection.execute(
            'SELECT fingerprint, MIN(raw_sha256) FROM patch GROUP BY fingerprint ORDER BY fingerprint'
        ).fetchall()
    finally:
        connection.close()
    if limit:
        rows = rows[:limit]
    return [(fingerprint, sha) for fingerprint, sha in rows]


def read_body(corpus_dir: str, sha: str) -> str:
    """Read one raw patch body by its sha256, tolerating non-UTF-8 bytes."""
    path = os.path.join(corpus_dir, 'bodies', sha[:2], sha)
    with open(path, encoding='utf-8', errors='replace') as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# Per-body accumulation
# ---------------------------------------------------------------------------

def _banner_label(generator_name: str, version: str | None) -> str:
    """A human-readable ``'generator version'`` (or bare ``generator``) label."""
    return '%s %s' % (generator_name, version) if version else generator_name


def _all_banner_hits(lines: list[str]) -> set[tuple[str, str | None]]:
    """Every distinct ``(generator, version)`` claimed anywhere in ``lines``.

    Unlike ``generated._banner_hit`` (first hit wins, one call per file), this sweeps
    every pattern against every line so a file carrying more than one distinct banner
    claim can be reported -- the multi-banner report exists to surface exactly that.
    """
    hits: set[tuple[str, str | None]] = set()
    for line in lines:
        for generator_name, _family, pattern in generated.BANNERS:
            match = pattern.search(line)
            if match:
                version = match.group(1) if pattern.groups else None
                hits.add((generator_name, version))
    return hits


class _Accumulator:
    """Mutable per-corpus tallies for every report section; one instance, one walk."""

    def __init__(self) -> None:
        self.name_totals: dict[str, int] = {name: 0 for name in generated.GENERATED_NAME_FAMILY}
        self.name_banner: dict[str, int] = {name: 0 for name in generated.GENERATED_NAME_FAMILY}
        self.name_occurrences: dict[str, list[tuple[str, str]]] = defaultdict(list)

        self.gap_counts: dict[str, int] = defaultdict(int)
        self.gap_examples: dict[str, list[str]] = defaultdict(list)
        self.gap_generators: dict[str, set[str]] = defaultdict(set)

        self.coverage_zero = 0
        self.coverage_lt_0_5 = 0
        self.coverage_ge_0_5_lt_0_9 = 0
        self.coverage_ge_0_9 = 0
        self.ge_0_5_list: list[dict] = []

        self.acinclude_touched = 0
        self.acinclude_banner = 0
        self.acinclude_generators: set[str] = set()

        self.multi_banner_count = 0
        self.multi_banner_examples: list[dict] = []

        self.candidate_total = 0
        self.candidate_basename_counts: dict[str, int] = defaultdict(int)
        self.candidate_samples: list[dict] = []

        self.scanned = 0
        self.missing = 0

    def add_body(self, fingerprint: str, text: str) -> None:
        self.scanned += 1
        scan = generated.scan(text)

        for entry in scan.files:
            basename = generated.strip_backup_suffixes(content._basename(entry.path))

            if generated.NAME_SIGNAL in entry.signals:
                self.name_totals[basename] += 1
                self.name_occurrences[basename].append((entry.path, fingerprint))
                if generated.BANNER_SIGNAL in entry.signals:
                    self.name_banner[basename] += 1

            if entry.signals == (generated.BANNER_SIGNAL,):
                self._add_gap_hit(basename, entry)

        sections = content._parse_sections(text)
        for section in sections:
            basename = generated.strip_backup_suffixes(content._basename(section.path))
            if basename == 'acinclude.m4':
                self.acinclude_touched += 1

        regions = generated._region_lines(text)
        for index, section in enumerate(sections):
            lines = regions[index] if index < len(regions) else []
            hits = _all_banner_hits(lines)
            if len(hits) > 1:
                self._add_multi_banner_hit(fingerprint, section.path, hits)

        self._add_coverage(fingerprint, scan)

        for path, snippet in generated.candidate_banner_hits(text):
            self._add_candidate_hit(fingerprint, path, snippet)

    def _add_gap_hit(self, basename: str, entry) -> None:
        self.gap_counts[basename] += 1
        examples = self.gap_examples[basename]
        if entry.path not in examples and len(examples) < _GAP_EXAMPLES:
            examples.append(entry.path)
        if entry.generator:
            label = _banner_label(entry.generator, entry.version)
            self.gap_generators[basename].add(label)
            if basename == 'acinclude.m4':
                self.acinclude_generators.add(label)
        if basename == 'acinclude.m4':
            self.acinclude_banner += 1

    def _add_multi_banner_hit(self, fingerprint: str, path: str, hits: set[tuple[str, str | None]]) -> None:
        self.multi_banner_count += 1
        if len(self.multi_banner_examples) < _MULTI_BANNER_EXAMPLES:
            self.multi_banner_examples.append({
                'fingerprint': fingerprint,
                'path': path,
                'versions_seen': sorted(_banner_label(g, v) for g, v in hits),
            })

    def _add_coverage(self, fingerprint: str, scan) -> None:
        coverage = scan.coverage
        if coverage == 0.0:
            self.coverage_zero += 1
        elif coverage < 0.5:
            self.coverage_lt_0_5 += 1
        elif coverage < 0.9:
            self.coverage_ge_0_5_lt_0_9 += 1
        else:
            self.coverage_ge_0_9 += 1

        if coverage >= 0.5:
            self.ge_0_5_list.append({
                'fingerprint': fingerprint,
                'coverage': coverage,
                'residue_changed': scan.residue_changed,
                'generated_changed': scan.generated_changed,
                'total_changed': scan.total_changed,
            })

    def _add_candidate_hit(self, fingerprint: str, path: str, snippet: str) -> None:
        self.candidate_total += 1
        basename = generated.strip_backup_suffixes(content._basename(path))
        self.candidate_basename_counts[basename] += 1
        if len(self.candidate_samples) < _CANDIDATE_SAMPLES:
            self.candidate_samples.append({
                'fingerprint': fingerprint,
                'path': path,
                'snippet': snippet,
            })


# ---------------------------------------------------------------------------
# Report assembly (dict keys sort via ``json.dump(sort_keys=True)``; every LIST is
# sorted explicitly here, since ``sort_keys`` does not touch list order).
# ---------------------------------------------------------------------------

def _name_match_counts(acc: _Accumulator) -> dict[str, dict]:
    counts = {}
    for basename in generated.GENERATED_NAME_FAMILY:
        total = acc.name_totals[basename]
        banner = acc.name_banner[basename]
        counts[basename] = {
            'total': total,
            'banner_count': banner,
            'banner_fraction': (banner / total) if total else 0.0,
        }
    return counts


def _gap_list(acc: _Accumulator) -> list[dict]:
    records = [
        {
            'basename': basename,
            'count': acc.gap_counts[basename],
            'examples': sorted(acc.gap_examples[basename]),
            'generators': sorted(acc.gap_generators[basename]),
        }
        for basename in acc.gap_counts
    ]
    return sorted(records, key=lambda r: (-r['count'], r['basename']))


def _coverage_distribution(acc: _Accumulator) -> dict:
    return {
        'buckets': {
            'zero': acc.coverage_zero,
            'lt_0_5': acc.coverage_lt_0_5,
            'ge_0_5_lt_0_9': acc.coverage_ge_0_5_lt_0_9,
            'ge_0_9': acc.coverage_ge_0_9,
        },
        'ge_0_5_list': sorted(acc.ge_0_5_list, key=lambda r: (-r['coverage'], r['fingerprint'])),
    }


def _low_frequency_names(acc: _Accumulator) -> dict[str, list[dict]]:
    result = {}
    for basename, total in acc.name_totals.items():
        if total > _LOW_FREQUENCY_MAX_TOTAL:
            continue
        seen: dict[str, str] = {}
        for path, fingerprint in acc.name_occurrences.get(basename, []):
            seen.setdefault(path, fingerprint)
        result[basename] = sorted(
            ({'path': path, 'fingerprint': fingerprint} for path, fingerprint in seen.items()),
            key=lambda r: r['path'])
    return result


def _acinclude(acc: _Accumulator) -> dict:
    return {
        'touched': acc.acinclude_touched,
        'banner_marked': acc.acinclude_banner,
        'generators': sorted(acc.acinclude_generators),
    }


def _multi_banner(acc: _Accumulator) -> dict:
    return {
        'count': acc.multi_banner_count,
        'examples': sorted(acc.multi_banner_examples, key=lambda r: (r['fingerprint'], r['path'])),
    }


def _candidate_family(acc: _Accumulator) -> dict:
    basename_counts = sorted(
        ({'basename': basename, 'count': count} for basename, count in acc.candidate_basename_counts.items()),
        key=lambda r: (-r['count'], r['basename']))[:_CANDIDATE_TOP_N]
    samples = sorted(acc.candidate_samples, key=lambda r: (r['fingerprint'], r['path']))
    return {
        'total_files_hit': acc.candidate_total,
        'basename_counts': basename_counts,
        'samples': samples,
    }


def build_report(corpus_dir: str, limit: int | None = None) -> dict:
    """Walk ``corpus_dir`` and return the full measurement report as a plain dict."""
    started = time.monotonic()
    acc = _Accumulator()

    for fingerprint, sha in representative_rows(corpus_dir, limit=limit):
        try:
            text = read_body(corpus_dir, sha)
        except OSError:
            acc.missing += 1
            continue
        acc.add_body(fingerprint, text)

    elapsed = time.monotonic() - started

    return {
        'name_match_counts': _name_match_counts(acc),
        'gap_list': _gap_list(acc),
        'coverage_distribution': _coverage_distribution(acc),
        'low_frequency_names': _low_frequency_names(acc),
        'acinclude': _acinclude(acc),
        'multi_banner': _multi_banner(acc),
        'candidate_family': _candidate_family(acc),
        'totals': {
            'fingerprints_scanned': acc.scanned,
            'bodies_missing': acc.missing,
            'wall_clock_seconds': elapsed,
        },
    }


# ---------------------------------------------------------------------------
# Human summary
# ---------------------------------------------------------------------------

def print_summary(report: dict) -> None:
    """A compact, top-entries-only human summary of ``report`` to stdout."""
    totals = report['totals']
    print('scanned %d fingerprints in %.1fs (%d bodies missing)' % (
        totals['fingerprints_scanned'], totals['wall_clock_seconds'], totals['bodies_missing']))

    print('\nper-name match counts (name set, top 10 by total):')
    ranked = sorted(report['name_match_counts'].items(), key=lambda kv: (-kv[1]['total'], kv[0]))
    for basename, counts in ranked[:10]:
        print('  %-20s total=%-6d banner=%-6d fraction=%.2f' % (
            basename, counts['total'], counts['banner_count'], counts['banner_fraction']))

    print('\ngap list (banner-only, outside the name set; top 10 by count):')
    for record in report['gap_list'][:10]:
        print('  %-20s count=%-6d examples=%s generators=%s' % (
            record['basename'], record['count'], record['examples'][:3], record['generators']))

    print('\ncoverage distribution:')
    for label, count in report['coverage_distribution']['buckets'].items():
        print('  %-14s %d' % (label, count))
    print('  >=0.5 population: %d fingerprints (top 5 by coverage):' % len(
        report['coverage_distribution']['ge_0_5_list']))
    for record in report['coverage_distribution']['ge_0_5_list'][:5]:
        print('    %s coverage=%.2f residue=%d generated=%d total=%d' % (
            record['fingerprint'][:12], record['coverage'], record['residue_changed'],
            record['generated_changed'], record['total_changed']))

    low_freq_names_with_hits = {k: v for k, v in report['low_frequency_names'].items() if v}
    print('\nlow-frequency names with at least one hit: %d names' % len(low_freq_names_with_hits))
    for basename, paths in sorted(low_freq_names_with_hits.items())[:10]:
        print('  %-20s %d distinct paths' % (basename, len(paths)))

    acinclude = report['acinclude']
    print('\nacinclude.m4: touched=%d banner_marked=%d generators=%s' % (
        acinclude['touched'], acinclude['banner_marked'], acinclude['generators']))

    multi_banner = report['multi_banner']
    print('\nmulti-banner files: %d' % multi_banner['count'])
    for record in multi_banner['examples'][:5]:
        print('  %s %-30s %s' % (record['fingerprint'][:12], record['path'], record['versions_seen']))

    candidate = report['candidate_family']
    print('\ncandidate do-not-edit family: %d files hit (top 10 basenames):' % candidate['total_files_hit'])
    for record in candidate['basename_counts'][:10]:
        print('  %-20s %d' % (record['basename'], record['count']))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('corpus_dir', help='corpus directory (bodies/ + fingerprints.sqlite)')
    parser.add_argument('--output', default=None, help='write the full JSON report to this path')
    parser.add_argument('--limit', type=int, default=None, help='scan only the first N fingerprints (smoke runs)')
    args = parser.parse_args(argv)

    report = build_report(args.corpus_dir, limit=args.limit)
    print_summary(report)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as handle:
            json.dump(report, handle, sort_keys=True, indent=2)
        print('\nwrote results to %s' % args.output)

    return 0


if __name__ == '__main__':
    sys.exit(main())
