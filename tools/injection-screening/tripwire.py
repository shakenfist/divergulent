#!/usr/bin/env python3
"""Prototype: a deterministic prompt-injection tripwire over the patch corpus.

Part of docs/plans/PLAN-prompt-injection-screening.md (phase 1). This is an
EVALUATION prototype, not shipped classifier code: it scans every deduplicated
patch body in a corpus for injection-shaped text and reports hit rates per
pattern family, so the plan's go/no-go decision rests on measured numbers.

The scan mirrors what the LLM triage tier actually sees: the claim-stripped
diff body (``triage.diff_body``). Hits in the DEP-3/free-text header (which
humans and the review web UI see, but the LLM does not) are recorded
separately with region='header'.

Usage (from a venv with divergulent importable, e.g. .tox/py3):

    python tools/injection-screening/tripwire.py <corpus_dir> [--json OUT] [--limit N]

Output: per-family patch-hit counts and a per-hit listing (fingerprint,
package, family, region, snippet) for hand adjudication; --json writes the
full hit list for downstream tooling (e.g. feeding the model-scorer sample).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time

from divergulent.classify import measure
from divergulent.classify.triage import diff_body as extract_diff_body

# Each family is separately versioned and separately reportable so the findings
# can retire a noisy family without losing the quiet ones. Patterns are matched
# against the WHOLE region text (everything in a patch is attacker-authored;
# restricting to '+' lines would just move the payload to context lines).
FAMILIES: dict[str, list[re.Pattern]] = {
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
    'chat-template-marker': [
        re.compile(r'<\|im_start\|>|<\|im_end\|>|<\|endoftext\|>|<\|system\|>'),
        re.compile(r'\[/?INST\]|<<SYS>>|<</SYS>>'),
        re.compile(r'"role"\s*:\s*"(system|assistant)"'),
        re.compile(r'^\s*(Human|Assistant):\s', re.MULTILINE),
    ],
    'invisible-unicode': [
        re.compile('[\\u200b\\u200c\\u200d\\u2060]'),                       # zero-width space/joiners
        re.compile('[\\U000e0000-\\U000e007f]'),                       # Unicode tag block
    ],
    'bidi-control': [
        re.compile('[\\u202a-\\u202e\\u2066-\\u2069]'),                     # Trojan Source vector
    ],
    'large-base64-blob': [
        re.compile(r'[A-Za-z0-9+/]{400,}={0,2}'),
    ],
}


def scan_text(text: str) -> list[tuple[str, str]]:
    """All (family, snippet) matches in ``text``; one entry per matching pattern."""
    hits = []
    for family, patterns in FAMILIES.items():
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                start = max(match.start() - 40, 0)
                snippet = text[start:match.end() + 40].replace('\n', '\\n')
                hits.append((family, snippet[:160]))
    return hits


def scan_patch(body: str) -> list[dict]:
    """Scan one raw patch body; returns hit dicts with the region annotated.

    The diff body (what the LLM sees) and the header (what humans see) are
    scanned separately so the findings can weigh them differently.
    """
    diff = extract_diff_body(body)
    header = body[:len(body) - len(diff)] if diff else body
    out = []
    for region, text in (('diff', diff), ('header', header)):
        for family, snippet in scan_text(text):
            out.append({'region': region, 'family': family, 'snippet': snippet})
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('corpus_dir', help='corpus directory (bodies/ + fingerprints.sqlite)')
    parser.add_argument('--json', default=None, help='write the full hit list to this path')
    parser.add_argument('--limit', type=int, default=None, help='scan only the first N fingerprints')
    args = parser.parse_args(argv)

    index = sqlite3.connect(args.corpus_dir + '/fingerprints.sqlite')
    rows = index.execute(
        'SELECT fingerprint, MIN(raw_sha256), MIN(source_package) FROM patch GROUP BY fingerprint').fetchall()
    if args.limit:
        rows = rows[:args.limit]

    started = time.monotonic()
    hits, scanned, unreadable = [], 0, 0
    family_patches: dict[str, set] = {}
    for fingerprint, sha, package in rows:
        try:
            body = measure.read_body(args.corpus_dir, sha)
        except (OSError, UnicodeDecodeError):
            unreadable += 1
            continue
        scanned += 1
        for hit in scan_patch(body):
            hit.update({'fingerprint': fingerprint, 'package': package})
            hits.append(hit)
            family_patches.setdefault('%s/%s' % (hit['family'], hit['region']), set()).add(fingerprint)

    elapsed = time.monotonic() - started
    print('scanned %d patches in %.1fs (%d unreadable); %d hits in %d patches'
          % (scanned, elapsed, unreadable, len(hits), len({h['fingerprint'] for h in hits})))
    for key in sorted(family_patches):
        print('  %-38s %5d patches' % (key, len(family_patches[key])))
    print()
    for hit in hits:
        print('%s  %-20s %-24s %-6s %s' % (
            hit['fingerprint'][:12], hit['package'][:20], hit['family'], hit['region'], hit['snippet']))

    if args.json:
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(hits, handle, indent=1)
        print('\nwrote %d hits to %s' % (len(hits), args.json))
    return 0


if __name__ == '__main__':
    sys.exit(main())
