"""The local, signed human-review tool (phase 4, step 4e).

The deterministic tiers and the LLM triage are batch, curation-side, and belong
in CI; **human review is interactive and identity-bound and does not**. This
module is the local CLI a reviewer runs on *their own machine*: it pulls the
next highest-priority un-reviewed item from the ledger's human-review queue,
shows the diff **in the context of the original upstream code**, takes the
human's verdict, and records it as a **signed ManualDecision**
(``kind='human'``) -- non-repudiation, reusing the project's existing Sigstore
posture.

Four design decisions from the plan are load-bearing here:

* **A human verdict is a *signed* ManualDecision.**  A ``kind='human'`` decision
  is the top of the precedence (``verdict.decision_rank``), so it is the most
  trusted verdict in the system; it is also the most *accountable*.  The
  reviewer signs a canonical decision record with their keyless Sigstore OIDC
  identity, and the ``decision`` row gains a ``signature`` (the bundle JSON) and
  a verified ``signed_by`` (the OIDC identity).  Phase 5 can then *prove* "this
  patch was human-reviewed by <identity>".

* **Human review runs locally and interactively -- never in CI.**  A GitHub
  Actions job cannot sit a person in front of a diff, and the reviewer's signing
  identity lives on *their* machine.  So this is a local CLI: the clock and
  interactive stdin live only in the CLI entry (``main``), threaded down.

* **The reviewer sees the diff in the context of the original code.**  A unified
  diff's two or three lines of context are not enough to judge what a change
  really does, so the tool fetches the original (pre-patch) upstream file from
  sources.debian.org -- on-demand, per reviewed item, so the bulk corpus's
  deliberate skip of ``.orig`` is untouched -- and renders the diff against it.

* **Everything external is injected.**  The source ``fetch``, the Sigstore
  ``signer``, the interactive ``ask``, and the clock ``now`` are all parameters,
  so the test suite runs fully offline: no network, no real OIDC/browser, no
  real stdin.  Only ``main`` wires in the real ones.

The Sigstore signing mirrors ``verify.py`` exactly: the ``sigstore`` import is
lazy, behind the ``verify`` extra, so the base install never loads it and a
missing dependency degrades to a clear, actionable error.

Curation-side only: no client command imports ``classify/``; nothing here runs
on a client.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
import urllib.parse
from dataclasses import dataclass

from divergulent.classify import measure
from divergulent.classify.claim import extract_claim

# ---------------------------------------------------------------------------
# Versioned constants.
# ---------------------------------------------------------------------------

# The human-review "rule version".  A human decision is not produced by a
# versioned rule the way a heuristic is, but the schema requires an integer
# rule_version; this pins the review-tool/record format so phase-5 re-verifies
# the canonical record under a known shape.  Bumping it is a tracked change to
# what the signature covers.
REVIEW_RULE_VERSION = 1

# The rule identity carried on a human decision's ``decided_by``.  Distinct from
# any LLM or heuristic rule id so a human verdict is unmistakable in the ledger.
DECIDED_BY = 'human-review'

# The valid verdict choices ``ask`` may return.  ``accept`` takes the LLM
# draft's category; an override names a ``TRIAGE_CATEGORIES`` value (or
# ``unknown``); ``defer`` leaves the item pending and records nothing.
CHOICE_ACCEPT = 'accept'
CHOICE_DEFER = 'defer'

# The sources.debian.org raw-content base.  We derive the per-file raw URL from
# the same ``data/<area>/<prefix>/<pkg>/<version>/<path>`` layout the patches
# adapter discovers (see ``sources.debian_patches``), reconstructed here for the
# original (pre-patch, unapplied) upstream file -- the context the patch applies
# against.  ``debian_patches`` discovers this base from a patch's ``raw_url``; a
# review fetch wants an *upstream* file (not under ``debian/patches/``), so we
# build the ``/data/...`` path directly.
SOURCES_BASE = 'https://sources.debian.org'

# The on-disk value cache namespace + TTL for fetched original files.  Original
# upstream source for a fixed (package, version) is immutable, so cache it long.
SOURCE_FILE_NAMESPACE = 'review-original-source'
SOURCE_FILE_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

# The production Sigstore OAuth issuer (sigstore 4.x has no Issuer.production()).
_SIGSTORE_OAUTH_ISSUER = 'https://oauth2.sigstore.dev/auth'

# How many lines of original context to show around each diff hunk when the file
# is too large to show whole.
DEFAULT_CONTEXT_WINDOW = 12

# Below this many lines, the original file is shown in full rather than windowed.
FULL_FILE_THRESHOLD = 400


# ---------------------------------------------------------------------------
# 1. Original-source context fetch.
# ---------------------------------------------------------------------------

def _area_prefix(source_package: str) -> str:
    """The pool directory prefix for a source package on sources.debian.org.

    The Debian pool keys packages by a one-letter (or ``libX``) prefix:
    ``bash`` -> ``b``, ``libfoo`` -> ``libf``.  sources.debian.org mirrors this
    under ``/data/<area>/<prefix>/<pkg>/...``.  We do not know the archive area
    (main / contrib / non-free) without an API round-trip, so callers pass it;
    this helper produces only the ``<prefix>/<pkg>`` tail.
    """
    if source_package.startswith('lib') and len(source_package) > 3:
        return source_package[:4]
    return source_package[:1]


def source_file_url(source_package: str, version: str, path: str, *, area: str = 'main') -> str:
    """Build the sources.debian.org raw URL for one ORIGINAL (pre-patch) file.

    sources.debian.org serves the *unpacked* source tree -- which for a quilt
    package is the upstream tree with the Debian patches NOT applied -- under
    ``/data/<area>/<prefix>/<pkg>/<version>/<path>``.  That is exactly the
    pre-patch context a quilt patch applies against, so fetching ``path`` at
    ``version`` gives the original file the reviewer needs.  The ``<prefix>`` is
    the Debian pool prefix (``b`` for ``bash``, ``libf`` for ``libfoo``); the
    path components are URL-quoted but their slashes are preserved.
    """
    prefix = _area_prefix(source_package)
    quoted_path = urllib.parse.quote(path)
    return '%s/data/%s/%s/%s/%s/%s' % (
        SOURCES_BASE, area, prefix,
        urllib.parse.quote(source_package, safe=''),
        urllib.parse.quote(version, safe=''),
        quoted_path)


def fetch_source_file(source_package: str, version: str, path: str, *, fetch,
                      area: str = 'main') -> str | None:
    """Fetch the ORIGINAL (pre-patch) upstream file for ``path`` at ``version``.

    Builds the sources.debian.org raw URL (:func:`source_file_url`) for the
    unpacked source tree -- the file state a quilt patch applies against -- and
    fetches it via the injected ``fetch(url) -> str | None``.  ``fetch`` is
    injected so the test suite runs fully offline; the CLI wires in a real
    ``HttpClient.get_text``-backed fetcher.  Returns the file text, or ``None``
    when the file cannot be fetched (a missing original is rendered as "no
    original available", never a hard failure of the review).
    """
    url = source_file_url(source_package, version, path, area=area)
    return fetch(url)


# ---------------------------------------------------------------------------
# 2. Render the diff in the context of the original file.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Hunk:
    """One unified-diff hunk: where it lands in the original and its lines."""

    old_start: int
    old_count: int
    lines: list[str]


_HUNK_HEADER_PREFIX = '@@'


def _parse_hunks(diff_body: str) -> list[_Hunk]:
    """Parse the ``@@ -a,b +c,d @@`` hunks out of a unified-diff body.

    Returns the hunks with their original-side start/count and their ``+``/``-``/
    context lines.  A diff with no recognisable hunk header yields an empty list,
    which :func:`render_in_context` renders as a raw diff (still readable).
    """
    hunks: list[_Hunk] = []
    current: list[str] | None = None
    old_start = old_count = 0
    for line in diff_body.splitlines():
        if line.startswith(_HUNK_HEADER_PREFIX):
            if current is not None:
                hunks.append(_Hunk(old_start, old_count, current))
            old_start, old_count = _parse_hunk_header(line)
            current = []
        elif current is not None:
            current.append(line)
    if current is not None:
        hunks.append(_Hunk(old_start, old_count, current))
    return hunks


def _parse_hunk_header(header: str) -> tuple[int, int]:
    """Parse ``@@ -old_start,old_count +new... @@`` -> ``(old_start, old_count)``.

    Degrades to ``(0, 0)`` for a malformed header rather than raising, so a
    surprising diff still renders (as raw lines) instead of crashing the review.
    """
    try:
        old_part = header.split('@@')[1].strip().split(' ')[0]  # e.g. '-12,7'
        old_part = old_part.lstrip('-')
        if ',' in old_part:
            start_str, count_str = old_part.split(',', 1)
            return int(start_str), int(count_str)
        return int(old_part), 1
    except (ValueError, IndexError):
        return (0, 0)


def render_in_context(original_file: str | None, diff_body: str, *,
                      window: int = DEFAULT_CONTEXT_WINDOW) -> str:
    """Render ``diff_body``'s changes against the ORIGINAL file for a human.

    Shows the touched region of the original file with the diff's ``+``/``-``
    changes marked in place: the original lines a hunk replaces are shown (each
    marked ``-``), the added lines are shown (each marked ``+``), and a generous
    ``window`` of surrounding original context frames the change.  A small file
    is shown in full; a large one is windowed around each hunk.  This is what the
    human reads to judge what the change really does.

    When no original is available (the fetch returned ``None``) the diff is shown
    on its own with a clear notice -- the review still proceeds, just with the
    diff's own context.  A diff with no parseable hunks falls back to showing the
    raw diff verbatim.
    """
    if original_file is None:
        return ('[no original upstream file available -- showing the raw diff only]\n\n'
                + diff_body.rstrip('\n'))

    hunks = _parse_hunks(diff_body)
    if not hunks:
        return ('[diff has no parseable hunks -- showing the raw diff only]\n\n'
                + diff_body.rstrip('\n'))

    original_lines = original_file.splitlines()
    show_full = len(original_lines) <= FULL_FILE_THRESHOLD

    out: list[str] = []
    for index, hunk in enumerate(hunks):
        if index:
            out.append('')
        out.extend(_render_hunk(original_lines, hunk, window=window, show_full=show_full))
    return '\n'.join(out)


def _render_hunk(original_lines: list[str], hunk: _Hunk, *, window: int,
                 show_full: bool) -> list[str]:
    """Render one hunk: a window of original context with ``+``/``-`` markers.

    The original-side region the hunk covers is shown with each line marked
    ``-`` (removed) or ' ' (unchanged context, drawn from the diff's own context
    lines), the added lines marked ``+``, and ``window`` lines of surrounding
    original code on each side (or the whole file when ``show_full``).
    """
    # The 0-based original index the hunk starts at (header is 1-based).
    start = max(hunk.old_start - 1, 0)
    end = start + hunk.old_count

    if show_full:
        lead_from, tail_to = 0, len(original_lines)
    else:
        lead_from = max(start - window, 0)
        tail_to = min(end + window, len(original_lines))

    out: list[str] = []
    out.append('@@ original lines %d-%d @@' % (start + 1, max(end, start + 1)))

    # Leading original context.
    for line in original_lines[lead_from:start]:
        out.append('  %s' % line)

    # The hunk body itself, as the diff expresses it ('+'/'-'/' ' lines), so the
    # reviewer sees exactly what is added and removed in place.  We render the
    # diff lines verbatim (they already carry the marker) but normalise a bare
    # context line (no marker) to a leading space.
    for diff_line in hunk.lines:
        if diff_line[:1] in ('+', '-', ' '):
            out.append(diff_line)
        else:
            out.append(' %s' % diff_line)

    # Trailing original context.
    for line in original_lines[end:tail_to]:
        out.append('  %s' % line)

    return out


# ---------------------------------------------------------------------------
# 3. The canonical signed record + Sigstore signing.
# ---------------------------------------------------------------------------

def canonical_record(fingerprint: str, category: str, decided_at: str, *,
                     note: str | None = None) -> bytes:
    """The canonical, deterministic bytes a human decision's signature covers.

    A stable JSON serialisation of exactly what the reviewer is attesting: the
    ``fingerprint`` reviewed, the ``category`` they chose, the ``decided_at``
    timestamp, the ``rule_version`` (so the record shape is versioned), and an
    optional free-text ``note``.  ``sort_keys`` + ``separators`` make the bytes
    deterministic so phase 5 can reconstruct exactly these bytes and re-verify
    the Sigstore signature over them.  No clock, no environment, nothing
    non-deterministic -- the signed artifact is a pure function of its inputs.
    """
    record = {
        'category': category,
        'decided_at': decided_at,
        'fingerprint': fingerprint,
        'rule_version': REVIEW_RULE_VERSION,
    }
    if note:
        record['note'] = note
    return json.dumps(record, sort_keys=True, separators=(',', ':')).encode('utf-8')


def sign_decision(record_bytes: bytes, *, signer) -> tuple[str, str]:
    """Sign the canonical ``record_bytes`` and return ``(signature, signed_by)``.

    ``signer(record_bytes) -> (signature, signed_by)`` is injected so the test
    suite never touches a browser/OIDC flow; the default real signer is
    :func:`sigstore_signer`, which does keyless Sigstore signing exactly as
    ``verify.py`` verifies (lazy import behind the ``verify`` extra).  Returns
    the bundle JSON as ``signature`` and the reviewer's OIDC identity as
    ``signed_by`` -- the non-repudiation binding stored on the decision row.
    """
    return signer(record_bytes)


def sigstore_signer(record_bytes: bytes) -> tuple[str, str]:
    """KEYLESS Sigstore signing of ``record_bytes`` -> ``(bundle_json, identity)``.

    The real signer: it runs the interactive keyless OIDC flow (a browser
    authentication), signs ``record_bytes`` against the operator's identity, and
    returns the Sigstore bundle as a JSON string (the ``signature``) plus the
    OIDC identity the certificate was bound to (the ``signed_by``).  The
    ``sigstore`` import is LAZY and behind the ``verify`` extra -- exactly as
    ``verify.py`` imports it -- so the base install never loads it; a missing
    dependency raises the same clear, actionable "pip install
    divergulent[verify]" error.

    This is the only function here that performs external I/O (the OIDC flow and
    the Fulcio/Rekor calls); the tests inject a fake signer and never reach it.
    """
    try:
        from sigstore.oidc import Issuer
        from sigstore.sign import ClientTrustConfig, SigningContext
    except ImportError as exc:
        raise RuntimeError(
            'sigstore not installed; run "pip install divergulent[verify]" to '
            'sign a human-review decision') from exc

    # sigstore 4.x API: build the OAuth issuer from its URL (there is no
    # Issuer.production()), get an interactive identity token, and build the
    # signing context from the production trust config (not SigningContext
    # .production()). sign_artifact takes raw bytes, not a file object.
    identity_token = Issuer(_SIGSTORE_OAUTH_ISSUER).identity_token()
    signing_context = SigningContext.from_trust_config(ClientTrustConfig.production())
    with signing_context.signer(identity_token) as signer:
        bundle = signer.sign_artifact(record_bytes)

    return bundle.to_json(), identity_token.identity


# ---------------------------------------------------------------------------
# 4. The review session for one item.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReviewContext:
    """Everything a human needs to judge one item -- the input to ``ask``.

    Assembled by :func:`review_one`: the representative diff body, the LLM draft
    (category + confidence + reasoning), the author's claim category, the routing
    flags/reason that sent the item to review, and the diff rendered in the
    context of the original upstream file.
    """

    fingerprint: str
    diff_body: str
    context_view: str
    draft_category: str | None
    draft_confidence: str | None
    draft_reasoning: str | None
    claim_category: str
    reason: str | None


@dataclass(frozen=True)
class ReviewOutcome:
    """What :func:`review_one` did for one item.

    ``recorded`` is whether a signed human decision was appended (False for a
    ``defer``); ``category`` is the chosen category (None on defer);
    ``decision_id`` is the new decision row id (None on defer); ``deferred`` is
    the inverse of ``recorded`` for readability.
    """

    fingerprint: str
    recorded: bool
    deferred: bool
    category: str | None
    decision_id: int | None


def _llm_draft(conn: sqlite3.Connection, fingerprint: str) -> sqlite3.Row | None:
    """The most recent LIVE ``kind='llm'`` decision for ``fingerprint``, or None.

    The review tool shows the LLM draft the reviewer accepts or overrides.  The
    triage recorder (step 4c) appended it; we read the latest live llm row from
    the audit trail so the reviewer sees the current draft, not a superseded one.
    """
    from divergulent.classify import ledger as ledger_mod
    llm_rows = [
        row for row in ledger_mod.decisions_for(conn, fingerprint)
        if row['kind'] == 'llm' and row['superseded_at'] is None]
    return llm_rows[-1] if llm_rows else None


def review_one(conn: sqlite3.Connection, corpus_dir: str, index_path: str,
               item: sqlite3.Row, *, fetch, signer, ask, now) -> ReviewOutcome:
    """Review ONE pending item: gather context, ask the human, record the verdict.

    Loads the representative body (``measure.read_body`` via the phase-1 index),
    the author's claim (``extract_claim``), and the live LLM draft
    (``decisions_for``); fetches the original upstream file and renders the diff
    in context; then calls ``ask(context) -> choice``.  ``choice`` is the LLM
    draft's category (accept), an override category, ``'unknown'``, or ``'defer'``.

    On a real verdict (anything but ``'defer'``): builds the canonical record,
    signs it (``sign_decision``), appends a ``kind='human'`` decision with
    ``verified=True``, the ``signature`` + ``signed_by``, and an ``evidence`` JSON
    recording what was reviewed, then marks the queue item reviewed.  On
    ``'defer'`` the item is left pending and NOTHING is recorded.

    ``fetch``/``signer``/``ask``/``now`` are all injected so this is pure given
    fakes; ``now`` is the caller-supplied ISO-8601 timestamp (this module never
    reads a clock).
    """
    from divergulent.classify import ledger as ledger_mod

    fingerprint = item['fingerprint']
    patch_info = _representative_patch(index_path, fingerprint)
    if patch_info is None:
        # No provenance row -> no body to review.  Leave it pending; a queued
        # fingerprint with no index row cannot be shown, and silently dropping it
        # would hide it, so we defer rather than record.
        return ReviewOutcome(fingerprint, False, True, None, None)

    source_package, version, patch_name, raw_sha = patch_info
    body = measure.read_body(corpus_dir, raw_sha)
    claim = extract_claim(patch_name, body)

    from divergulent.classify.triage import diff_body as extract_diff_body
    body_diff = extract_diff_body(body)

    original = fetch_source_file(source_package, version, patch_name, fetch=fetch)
    context_view = render_in_context(original, body_diff)

    draft = _llm_draft(conn, fingerprint)
    context = ReviewContext(
        fingerprint=fingerprint,
        diff_body=body_diff,
        context_view=context_view,
        draft_category=draft['category'] if draft is not None else None,
        draft_confidence=draft['confidence'] if draft is not None else None,
        draft_reasoning=_draft_reasoning(draft),
        claim_category=claim.claimed_category,
        reason=item['reason'])

    choice = ask(context)
    if choice == CHOICE_DEFER:
        return ReviewOutcome(fingerprint, False, True, None, None)

    category = (context.draft_category or 'unknown') if choice == CHOICE_ACCEPT else choice

    record_bytes = canonical_record(fingerprint, category, now)
    signature, signed_by = sign_decision(record_bytes, signer=signer)

    evidence = json.dumps({
        'reviewed': {
            'source_package': source_package,
            'version': version,
            'patch_name': patch_name,
            'fingerprint': fingerprint,
        },
        'draft_category': context.draft_category,
        'claim_category': context.claim_category,
        'choice': choice,
        'queue_reason': context.reason,
    }, sort_keys=True)

    decision_id = ledger_mod.append_decision(
        conn, fingerprint=fingerprint, category=category, confidence='high',
        decided_by=DECIDED_BY, rule_version=REVIEW_RULE_VERSION, kind='human',
        verified=True, signature=signature, signed_by=signed_by,
        evidence=evidence, decided_at=now, commit=False)
    ledger_mod.mark_reviewed(conn, item_id=item['id'], reviewed_at=now)

    return ReviewOutcome(fingerprint, True, False, category, decision_id)


def _draft_reasoning(draft: sqlite3.Row | None) -> str | None:
    """The LLM draft's reasoning, pulled from its evidence JSON, or None.

    The triage recorder stores the draft inside an ``evidence`` JSON blob; we
    surface its ``reasoning`` so the human sees WHY the LLM drafted what it did
    (without anchoring on it).  A missing/garbled blob degrades to ``None``.
    """
    if draft is None or draft['evidence'] is None:
        return None
    try:
        data = json.loads(draft['evidence'])
        return data.get('draft', {}).get('reasoning')
    except (ValueError, TypeError):
        return None


def _representative_patch(index_path: str, fingerprint: str):
    """The representative ``(source_package, version, patch_name, raw_sha256)``.

    Reads the first phase-1 ``patch`` index row for ``fingerprint`` (row order is
    stable for a given index build, matching how the driver picks a
    representative), so the review shows one concrete carried instance of the
    deduplicated fingerprint.  Returns ``None`` when the fingerprint has no index
    row (nothing to show).
    """
    connection = sqlite3.connect(index_path)
    try:
        row = connection.execute(
            'SELECT source_package, version, patch_name, raw_sha256 '
            'FROM patch WHERE fingerprint = ? ORDER BY rowid LIMIT 1',
            (fingerprint,)).fetchone()
    finally:
        connection.close()
    return row


# ---------------------------------------------------------------------------
# 5. The CLI (``python -m divergulent.classify.review``).
#
# This is the ONLY place that reads a wall clock, selects the REAL Sigstore
# signer, and reads interactive stdin: ``main`` captures one ``now``, wires the
# real ``fetch`` / ``signer`` / ``ask``, and threads them into ``review_one`` for
# each pulled item, up to ``--limit``.  The heavy/verify pieces are lazy-imported
# inside ``main`` so the module stays import-time clean.  The tests inject fakes
# and never reach a real backend.
# ---------------------------------------------------------------------------

DEFAULT_LIMIT = 10


def _cli_now() -> str:
    """The single clock read of the review stack: an ISO-8601 UTC timestamp.

    Only the CLI entry point reads the clock; the value is threaded down as the
    decision's ``decided_at`` (and into the canonical signed record) so every
    other path stays deterministic, exactly as the ledger/triage CLIs do.
    """
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _real_fetch():
    """Build the real source-file fetcher: a cached, polite ``HttpClient.get_text``.

    Lazy-built inside ``main`` so the module import stays clean.  Returns a
    ``fetch(url) -> str | None`` closure over an ``HttpClient`` (stdlib urllib +
    the on-disk cache), so the original-source fetch is throttled and cached like
    every other network access in the project.
    """
    from divergulent.cache import Cache, default_cache_dir
    from divergulent.http import HttpClient

    client = HttpClient(Cache(default_cache_dir()))

    def fetch(url: str) -> str | None:
        return client.get_text(
            url, cache_namespace=SOURCE_FILE_NAMESPACE, cache_key=url,
            ttl_seconds=SOURCE_FILE_TTL_SECONDS)

    return fetch


def _interactive_ask(context: ReviewContext) -> str:
    """The real interactive ``ask``: print the context + draft + claim, read stdin.

    Shows the diff IN CONTEXT, the LLM draft (category + confidence + reasoning),
    the author's claim, and the routing reason, then reads the human's choice
    from stdin: accept the LLM draft, override to a named category, ``unknown``,
    or defer.  The reading of stdin is confined here (the CLI entry); every other
    path takes ``choice`` as data.
    """
    from divergulent.classify.triage import TRIAGE_CATEGORIES

    print('=' * 78)
    print('fingerprint: %s' % context.fingerprint)
    if context.reason:
        print('routed to review because: %s' % context.reason)
    print('author claim category: %s' % context.claim_category)
    if context.draft_category is not None:
        print('LLM draft: %s (confidence %s)' % (
            context.draft_category, context.draft_confidence))
        if context.draft_reasoning:
            print('LLM reasoning: %s' % context.draft_reasoning)
    else:
        print('LLM draft: (none)')
    print('-' * 78)
    print(context.context_view)
    print('-' * 78)

    options = []
    if context.draft_category is not None:
        options.append('"accept" (take the LLM draft: %s)' % context.draft_category)
    options.append('a category to override: %s' % ', '.join(TRIAGE_CATEGORIES))
    options.append('"defer" (leave it for later)')
    print('Your verdict -- ' + '; '.join(options))

    valid = set(TRIAGE_CATEGORIES) | {CHOICE_ACCEPT, CHOICE_DEFER}
    while True:
        choice = input('verdict> ').strip()
        if choice in valid:
            return choice
        print('  please enter one of: %s' % ', '.join(sorted(valid)))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m divergulent.classify.review',
        description='Drain the human-review queue LOCALLY and INTERACTIVELY '
                    '(never CI, never a client feature): show each diff in the '
                    'context of the original upstream code alongside the LLM draft '
                    "and the author's claim, take the human's verdict, and record "
                    'it as a SIGNED ManualDecision (Sigstore identity -> '
                    'non-repudiation). No malice is ever pronounced by a machine.')
    parser.add_argument('ledger', help='path to a ledger sqlite built by classify.ledger')
    parser.add_argument('corpus_dir', help='directory of a corpus built by classify.corpus')
    parser.add_argument('--index', default=None,
                        help='path to the phase-1 sqlite fingerprint index (default: '
                             '<corpus_dir>/fingerprints.sqlite)')
    parser.add_argument('--limit', type=int, default=DEFAULT_LIMIT,
                        help='max items to review this session (default: %d)' % DEFAULT_LIMIT)
    return parser


def main(argv: list[str] | None = None) -> int:
    """``python -m divergulent.classify.review``: drain the review queue locally.

    Pulls the highest-priority pending items (``pending_review_items``), reviews
    each via :func:`review_one` -- wiring the REAL ``fetch`` (cached HttpClient),
    the REAL Sigstore ``signer``, the interactive stdin ``ask``, and the single
    clock read -- up to ``--limit``, then rebuilds the current-verdict cache so a
    fresh human verdict tops the precedence immediately.  The heavy/verify pieces
    are lazy-imported here so the module stays import-time clean.
    """
    import os

    from divergulent.classify import ledger as ledger_mod
    from divergulent.classify import verdict as verdict_mod

    parser = _build_parser()
    args = parser.parse_args(argv)

    index_path = args.index or os.path.join(args.corpus_dir, 'fingerprints.sqlite')

    fetch = _real_fetch()
    conn = sqlite3.connect(args.ledger)
    try:
        pending = ledger_mod.pending_review_items(conn)
        reviewed = deferred = 0
        for item in pending[:args.limit]:
            outcome = review_one(
                conn, args.corpus_dir, index_path, item,
                fetch=fetch, signer=sigstore_signer, ask=_interactive_ask,
                now=_cli_now())
            if outcome.recorded:
                reviewed += 1
                print('recorded human verdict %s for %s (signed by %s)' % (
                    outcome.category, outcome.fingerprint[:16],
                    _signed_by(conn, outcome.decision_id)))
            else:
                deferred += 1
                print('deferred %s' % outcome.fingerprint[:16])
        verdict_mod.rebuild_current_verdict(conn)
    finally:
        conn.close()

    print('reviewed %d, deferred %d, of %d pending' % (reviewed, deferred, len(pending)))
    return 0


def _signed_by(conn: sqlite3.Connection, decision_id: int | None) -> str:
    """The ``signed_by`` identity recorded on a decision row (for the CLI print)."""
    if decision_id is None:
        return '(unsigned)'
    row = conn.execute(
        'SELECT signed_by FROM decision WHERE id = ?', (decision_id,)).fetchone()
    return (row[0] if row and row[0] else '(unsigned)')


if __name__ == '__main__':
    sys.exit(main())
