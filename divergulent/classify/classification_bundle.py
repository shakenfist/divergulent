"""The classification bundle: schema, builder, writer, and loader.

The shareable half of the patch classification -- a fingerprint→verdict map so a
client can say "*this package's 85 patches: 30 feature, 10 security, …*" and, for
any patch, *why* it was classified that way, without running a classifier or an
LLM itself.  Distinct from the divergence cache (:mod:`divergulent.bundle`) in
lifecycle: that one is recomputed nightly from the archive; this one *grows* as
review settles more of the residue, so republishing simply enriches it.

Same trust model and shape as the divergence cache: a single gzipped JSON
document, ``schema`` versioning the envelope and ``entry_schema`` the per-verdict
value, so a client refuses a bundle it does not understand rather than misreading
it.  Keyed by patch fingerprint (the content hash) because a verdict is a property
of the diff, not of any machine or version.

The bundle is deliberately LEAN: it carries the derived category, the review axes
(risk / reach / reviewability), a short provenance *reason* and the deciding rule
-- but NOT the bulky raw LLM/verification evidence, which stays in the
source-of-truth ledger (the committed JSONL export).  The builder takes
``generated_at`` and host facts as plain values rather than reading the clock, so
assembling and round-tripping stays offline and deterministic in tests.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from divergulent.classify import cross_reference
from divergulent.classify import ledger as ledger_mod
from divergulent.classify import reach as reach_mod
from divergulent.classify import reviewability as reviewability_mod
from divergulent.classify import risk as risk_mod
from divergulent.classify import verdict as verdict_mod

# Envelope schema: the top-level shape. Bump when that changes.
CLASSIFICATION_SCHEMA_VERSION = 1
# Per-verdict value schema: the shape of each entry. Bump when the entry layout
# changes without the envelope changing.
ENTRY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ClassificationBundle:
    """A published fingerprint→verdict dataset for one corpus snapshot.

    ``verdicts`` maps a patch fingerprint to ``{category, confidence, reason,
    decided_by, rule_version, kind}`` plus, when scored, the ``risk`` / ``reach`` /
    ``reviewability`` axis levels.  Every fingerprint with a current (live) verdict
    is included -- including the un-reviewed ``unknown`` residue, so the client can
    report honestly what is *not* yet classified rather than hiding it.
    """

    schema: int
    entry_schema: int
    category_enum_version: int
    generated_at: str
    source_release: str
    built_on: dict[str, str]
    verdicts: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            'schema': self.schema,
            'entry_schema': self.entry_schema,
            'category_enum_version': self.category_enum_version,
            'generated_at': self.generated_at,
            'source_release': self.source_release,
            'built_on': self.built_on,
            'verdicts': self.verdicts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'ClassificationBundle':
        return cls(
            schema=data['schema'],
            entry_schema=data['entry_schema'],
            category_enum_version=data['category_enum_version'],
            generated_at=data['generated_at'],
            source_release=data['source_release'],
            built_on=data['built_on'],
            verdicts=data['verdicts'])


def _reason(v: verdict_mod.Verdict) -> str:
    """A short, evidence-free provenance line: *why* this category, in one phrase.

    Derived purely from the winning decision's provenance (kind + who decided +
    rule version), NOT from the stored evidence -- so the published bundle never
    ships raw model responses.  The full evidence stays auditable in the ledger's
    JSONL export for anyone who wants it.
    """
    if v.decided_by == cross_reference.EXTERNAL_CVE_RULE_ID:
        # The phase-6 external CVE cross-reference: surface the confirmed-CVE phrase
        # (the CVE id + snapshot date it recorded as evidence), not a generic
        # "deterministic rule". Still evidence-free of any raw model response.
        return v.evidence or ('confirmed CVE via Debian Security Tracker (v%d)' % v.rule_version)
    if v.kind == 'heuristic':
        return 'deterministic rule %s (v%d)' % (v.decided_by, v.rule_version)
    if v.kind == 'llm':
        tier = 'verified LLM triage' if v.verified else 'unverified LLM draft'
        return '%s by %s (v%d)' % (tier, v.decided_by, v.rule_version)
    if v.kind == 'human':
        return 'human review by %s' % v.decided_by
    return '%s by %s (v%d)' % (v.kind, v.decided_by, v.rule_version)


def build_classification_bundle(conn, *, generated_at: str, source_release: str,
                                built_on: dict[str, str] | None = None) -> ClassificationBundle:
    """Assemble a :class:`ClassificationBundle` from an open ledger connection.

    Joins the derived current verdict (:func:`verdict.current_verdict`) with the
    three review axes read straight from the live observations, attaches a short
    provenance reason, and drops all raw evidence.  Pure over its inputs: reads no
    clock and no host facts (the caller supplies ``generated_at`` / ``built_on``).
    """
    verdicts = verdict_mod.current_verdict(conn)
    risk_levels = risk_mod.risk_level_by_fingerprint(conn)
    reach_levels = reach_mod.reach_by_fingerprint(conn)
    review_levels = reviewability_mod.reviewability_by_fingerprint(conn)
    category_enum_version = int(
        ledger_mod.meta(conn).get('category_enum_version', ledger_mod.CATEGORY_ENUM_VERSION))

    entries: dict[str, dict[str, Any]] = {}
    for fingerprint, v in verdicts.items():
        entry: dict[str, Any] = {
            'category': v.category,
            'confidence': v.confidence,
            'reason': _reason(v),
            'decided_by': v.decided_by,
            'rule_version': v.rule_version,
            'kind': v.kind,
        }
        # Axis levels are attached only when scored; a missing key means "not yet
        # measured on this fingerprint" (keeps the bundle lean at ~60k entries).
        if fingerprint in risk_levels:
            entry['risk'] = risk_levels[fingerprint]
        if fingerprint in reach_levels:
            entry['reach'] = reach_levels[fingerprint]
        if fingerprint in review_levels:
            entry['reviewability'] = review_levels[fingerprint]
        entries[fingerprint] = entry

    return ClassificationBundle(
        schema=CLASSIFICATION_SCHEMA_VERSION,
        entry_schema=ENTRY_SCHEMA_VERSION,
        category_enum_version=category_enum_version,
        generated_at=generated_at,
        source_release=source_release,
        built_on=built_on or {},
        verdicts=entries)


def write(bundle: ClassificationBundle, path: str | Path) -> None:
    """Write a classification bundle to ``path`` as gzipped, key-sorted JSON."""
    payload = json.dumps(
        bundle.to_dict(), separators=(',', ':'), sort_keys=True).encode('utf-8')
    with gzip.open(path, 'wb') as handle:
        handle.write(payload)


def loads(data: bytes) -> ClassificationBundle:
    """Parse a classification bundle from raw gzipped-JSON bytes."""
    payload = gzip.decompress(data)
    return ClassificationBundle.from_dict(json.loads(payload.decode('utf-8')))


def load(path: str | Path) -> ClassificationBundle:
    """Read a gzipped-JSON classification bundle from ``path``."""
    with open(path, 'rb') as handle:
        return loads(handle.read())


def stored_path(cache_dir: str | Path, release: str) -> Path:
    """The on-disk location of the stored classification bundle for a release."""
    return Path(cache_dir) / ('classification-%s.json.gz' % release)


def _default_output(ledger_path: str, release: str) -> str:
    directory = os.path.dirname(ledger_path) or '.'
    return str(stored_path(directory, release))


def main(argv: list[str] | None = None) -> int:
    """``bundle <ledger> [--release NAME] [--output PATH]`` -> a signed-able bundle."""
    import datetime

    parser = argparse.ArgumentParser(
        prog='python -m divergulent.classify.classification_bundle',
        description='Build the published classification bundle from a ledger.')
    parser.add_argument('ledger', help='path to the ledger sqlite (or an imported export)')
    parser.add_argument('--release', default='unknown',
                        help='the Debian release the corpus was built from (provenance)')
    parser.add_argument('--output', default=None,
                        help='bundle output (default: classification-<release>.json.gz beside the ledger)')
    parser.add_argument('--generated-at', default=None,
                        help='ISO-8601 build timestamp (default: now, UTC)')
    args = parser.parse_args(argv)

    try:
        conn = ledger_mod.open_ledger(args.ledger)
    except ledger_mod.LedgerError as exc:
        print('error: %s' % exc)
        return 2

    generated_at = args.generated_at or datetime.datetime.now(
        datetime.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    try:
        bundle = build_classification_bundle(
            conn, generated_at=generated_at, source_release=args.release)
    finally:
        conn.close()

    output = args.output or _default_output(args.ledger, args.release)
    write(bundle, output)
    print('built classification bundle: %d verdicts -> %s' % (len(bundle.verdicts), output))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
