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
import re
import sqlite3
import sys
import urllib.parse
from dataclasses import dataclass

from divergulent.classify import fingerprint as fp
from divergulent.classify import ledger as ledger_mod
from divergulent.classify import measure
from divergulent.classify.claim import extract_claim

_DEV_NULL = '/dev/null'

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

# How many carrying package names to list before truncating with "(+N more)".
# A fingerprint is deduplicated across every package that carries the identical
# patch; some span dozens, so the review UI caps the list rather than flood the
# screen.
MAX_PACKAGES_SHOWN = 8

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


def _candidate_versions(version: str):
    """The version forms to try on sources.debian.org, as installed then epoch-stripped.

    sources.debian.org omits the Debian epoch (the ``N:`` prefix) from its
    ``/data/...`` path, so a version like ``1:0.13-4`` is served under
    ``0.13-4``.  We try the version as recorded first, then the epoch-stripped
    form -- mirroring the patches adapter's ``_candidate_versions`` so the review
    fetch resolves the same versions the corpus build did.
    """
    yield version
    if ':' in version:
        yield version.split(':', 1)[1]


def fetch_source_file(source_package: str, version: str, path: str, *, fetch,
                      area: str = 'main') -> str | None:
    """Fetch the ORIGINAL (pre-patch) upstream file for ``path`` at ``version``.

    Builds the sources.debian.org raw URL (:func:`source_file_url`) for the
    unpacked source tree -- the file state a quilt patch applies against -- and
    fetches it via the injected ``fetch(url) -> str | None``.  When ``version``
    carries a Debian epoch, the first (with-epoch) URL 404s because
    sources.debian.org strips the epoch from its path, so we fall back to the
    epoch-stripped form (:func:`_candidate_versions`).  ``fetch`` is injected so
    the test suite runs fully offline; the CLI wires in a real
    ``HttpClient.get_text``-backed fetcher.  Returns the file text, or ``None``
    when no candidate resolves (a missing original is rendered as "no original
    available", never a hard failure of the review).
    """
    for candidate in _candidate_versions(version):
        url = source_file_url(source_package, candidate, path, area=area)
        text = fetch(url)
        if text is not None:
            return text
    return None


@dataclass(frozen=True)
class _FileDiff:
    """One touched file's segment of a multi-file diff body.

    ``path`` is the source-tree path the patch modifies (the ``+++ b/<path>``
    target, prefix- and timestamp-stripped -- exactly what sources.debian.org
    serves), and ``body`` is the ``--- ``/``+++ `` headers plus the hunks for
    that one file.  A patch that touches several files yields one of these per
    file so each is fetched and rendered against its OWN original.
    """

    path: str
    body: str


def split_diff_by_file(diff_body: str) -> list[_FileDiff]:
    """Split a unified-diff body into one :class:`_FileDiff` per touched file.

    A new file segment opens at each ``--- `` header that is immediately
    followed by a ``+++ `` header; its ``path`` is taken from the ``+++ ``
    target (falling back to the ``--- `` source when the target is
    ``/dev/null``, i.e. a deletion).  Reuses ``fingerprint``'s diff-start and
    header-path semantics so the path matches the patch tier exactly.  A body
    with no recognisable file header yields an empty list -- the caller then
    renders the raw diff with no original context.
    """
    lines = fp._split_lines(diff_body)
    start = fp._diff_start(lines)
    segments: list[_FileDiff] = []
    current_path: str | None = None
    current_lines: list[str] | None = None

    index = start
    total = len(lines)
    while index < total:
        line = lines[index]
        nxt = lines[index + 1] if index + 1 < total else ''
        if line.startswith('--- ') and nxt.startswith('+++ '):
            if current_lines is not None:
                segments.append(_FileDiff(current_path, '\n'.join(current_lines)))
            source = fp._header_path(line)
            target = fp._header_path(nxt)
            path = target
            if target == _DEV_NULL and source and source != _DEV_NULL:
                path = source
            current_path = path
            current_lines = [line]
        elif current_lines is not None:
            current_lines.append(line)
        index += 1

    if current_lines is not None:
        segments.append(_FileDiff(current_path, '\n'.join(current_lines)))
    return segments


def build_context_view(source_package: str, version: str, diff_body: str, *,
                       fetch, area: str = 'main') -> str:
    """Render a whole patch in the context of EACH original file it touches.

    Splits ``diff_body`` per file (:func:`split_diff_by_file`), fetches each
    touched file's ORIGINAL (pre-patch) upstream content from sources.debian.org
    by its real source-tree path -- NOT the quilt patch filename -- and renders
    that file's hunks against it (:func:`render_in_context`).  The per-file
    blocks are joined under a header naming each file (and noting when its
    original could not be fetched).  A diff with no parseable file headers falls
    back to showing the raw diff with no original context.
    """
    segments = split_diff_by_file(diff_body)
    if not segments:
        return render_in_context(None, diff_body)

    blocks: list[str] = []
    for segment in segments:
        # Most quilt patches are a/ b/ prefixed, so segment.path is already
        # source-root-relative; a raw two-tree diff keeps the upstream tarball
        # root directory, which sources.debian.org does not have in its path.
        fetch_path = _source_tree_path(segment)
        original = fetch_source_file(source_package, version, fetch_path, fetch=fetch, area=area)
        if original is None and fetch_path != segment.path:
            original = fetch_source_file(source_package, version, segment.path, fetch=fetch, area=area)
        header = '### %s' % segment.path
        if original is None:
            header += '  [original not fetched: %s]' % source_file_url(
                source_package, version, fetch_path, area=area)
        blocks.append('%s\n%s' % (header, render_in_context(original, segment.body)))
    return '\n\n'.join(blocks)


# Suffixes the OLD side of a raw two-tree diff puts on the tarball-root directory
# (``diff -ruN <root>.orig/... <root>/...``); a trailing one is stripped to match
# the NEW side when detecting the shared root component.
_TWO_TREE_OLD_SUFFIXES = ('.orig', '.old', '~')

# A tarball-root directory looks versioned (``botan-2.12.0``, ``foo_1.0``,
# ``llvm-toolchain-snapshot_17~++...``); a plain source subdir (``src``, ``lib``,
# ``tests``) does not.  Used to avoid stripping a real subdir off a bare-path diff.
_VERSIONED_DIR_RE = re.compile(r'[-_]\d')


def _raw_header_path(line: str) -> str:
    """The ``--- ``/``+++ `` header path, timestamp-stripped but NOT ``a/``/``b/`` stripped.

    Unlike ``fingerprint._header_path`` this keeps any leading ``a/``/``b/`` or
    tarball-root component, so :func:`_source_tree_path` can tell a quilt-prefixed
    diff (root-relative already) from a raw two-tree diff (carries the root dir).
    """
    rest = line[4:]
    tab = rest.find('\t')
    if tab != -1:
        rest = rest[:tab]
    else:
        double = rest.find('  ')
        if double != -1:
            rest = rest[:double]
    return rest.strip()


def _source_tree_path(segment: _FileDiff) -> str:
    """The path within the unpacked source tree to fetch from sources.debian.org.

    sources.debian.org serves files relative to the unpacked source ROOT.  A
    quilt patch's ``a/``/``b/`` path is already root-relative, but a raw two-tree
    diff (``--- <root>.orig/<path>`` / ``+++ <root>/<path>``) keeps the upstream
    tarball-root directory, so the leading component must be dropped.

    Works from the RAW headers (``a/``/``b/`` intact): a quilt prefix means the
    path is already root-relative (leave it); otherwise, when both sides share a
    leading component -- the old side modulo a trailing ``.orig``/``.old``/``~`` --
    that component is the tarball root and is dropped, but only when it actually
    looks like a versioned tarball dir (or carried an ``.orig``-family suffix), so
    a bare-path diff against a real subdir like ``src/`` is left untouched.
    """
    lines = segment.body.splitlines()
    raw_old = raw_new = None
    for index, line in enumerate(lines):
        if line.startswith('--- ') and index + 1 < len(lines) and lines[index + 1].startswith('+++ '):
            raw_old = _raw_header_path(line)
            raw_new = _raw_header_path(lines[index + 1])
            break
    if not raw_old or not raw_new or '/' not in raw_new:
        return segment.path

    old_first = raw_old.split('/', 1)[0]
    new_first = raw_new.split('/', 1)[0]
    if old_first in ('a', 'b') or new_first in ('a', 'b'):
        return segment.path  # quilt-prefixed: segment.path is already root-relative

    had_suffix = False
    old_root = old_first
    for suffix in _TWO_TREE_OLD_SUFFIXES:
        if old_root.endswith(suffix):
            old_root, had_suffix = old_root[:-len(suffix)], True
            break
    if old_root != new_first:
        return segment.path
    # Shared leading dir: strip it only when it is clearly a tarball root (had an
    # .orig-family suffix, or looks versioned), never a plain source subdir.
    if had_suffix or _VERSIONED_DIR_RE.search(new_first):
        return raw_new.split('/', 1)[1]
    return segment.path


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


def _sign_with_refresh(attempt, refresh, expired_errors):
    """Run ``attempt()``; on an expired-identity error, ``refresh()`` and retry ONCE.

    The keyless OIDC identity token is short-lived (minutes), so a reviewer who
    reads a diff longer than its lifetime would otherwise crash at sign time with
    the verdict already given.  This retries exactly once after re-authenticating,
    so a slow read costs at most one extra browser prompt -- never a lost verdict.
    A second consecutive expiry (e.g. the reviewer abandons the re-auth) is
    allowed to propagate.  ``expired_errors`` is the tuple of exception types that
    mean "token expired"; kept as a parameter so this is testable without
    ``sigstore`` installed.
    """
    try:
        return attempt()
    except expired_errors:
        refresh()
        return attempt()


def build_sigstore_signer():
    """Return a keyless Sigstore ``signer`` that authenticates lazily and refreshes.

    Returns a ``signer(record_bytes) -> (bundle_json, identity)`` closure.  The
    interactive keyless OIDC flow (the browser authentication) runs on the FIRST
    signature and the resulting identity token is reused for every later one, so
    draining a multi-item queue prompts the browser once, not once per item (and
    a session where every item is deferred never prompts at all).  Because that
    token is short-lived, if it has EXPIRED by the time a signature is made -- the
    reviewer spent longer reading the diff than the token's lifetime -- the signer
    re-authenticates and retries once (:func:`_sign_with_refresh`) rather than
    crashing with the verdict already given.

    The ``sigstore`` import is LAZY and behind the ``verify`` extra -- exactly as
    ``verify.py`` imports it -- so the base install never loads it; a missing
    dependency raises the same clear, actionable "pip install
    divergulent[verify]" error.  This is the only place that performs external
    I/O (the OIDC flow and the Fulcio/Rekor calls); the tests inject a fake
    signer and never reach it.
    """
    try:
        from sigstore.oidc import ExpiredIdentity, Issuer
        from sigstore.sign import ClientTrustConfig, SigningContext
    except ImportError as exc:
        raise RuntimeError(
            'sigstore not installed; run "pip install divergulent[verify]" to '
            'sign a human-review decision') from exc

    # sigstore 4.x API: build the OAuth issuer from its URL (there is no
    # Issuer.production()) and the signing context from the production trust
    # config (not SigningContext.production()). The identity token is acquired
    # lazily on first use and refreshed on expiry.
    issuer = Issuer(_SIGSTORE_OAUTH_ISSUER)
    signing_context = SigningContext.from_trust_config(ClientTrustConfig.production())
    holder: dict = {'token': None}

    def refresh() -> None:
        # (Re)run the browser OIDC flow; called on first sign and on expiry.
        holder['token'] = issuer.identity_token()

    def signer(record_bytes: bytes) -> tuple[str, str]:
        if holder['token'] is None:
            refresh()

        def attempt() -> tuple[str, str]:
            token = holder['token']
            # A fresh signer context per artifact (cheap, no re-auth) over the
            # SAME token; sign_artifact takes raw bytes, not a file object.
            with signing_context.signer(token) as one_shot:
                bundle = one_shot.sign_artifact(record_bytes)
            return bundle.to_json(), token.identity

        return _sign_with_refresh(attempt, refresh, (ExpiredIdentity,))

    return signer


def sigstore_signer(record_bytes: bytes) -> tuple[str, str]:
    """Single-shot keyless Sigstore signing (authenticates, signs once).

    A convenience wrapper over :func:`build_sigstore_signer` for callers signing
    exactly one record; it authenticates and signs in one call.  Multi-item
    callers (the review CLI) should call :func:`build_sigstore_signer` ONCE and
    reuse the returned signer so the browser flow runs a single time per session
    rather than once per item.
    """
    return build_sigstore_signer()(record_bytes)


# ---------------------------------------------------------------------------
# 4. The review session for one item.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReviewContext:
    """Everything a human needs to judge one item -- the input to ``ask``.

    Assembled by :func:`build_review_context`: the representative diff body, the
    LLM draft (category + confidence + reasoning), the author's claim category, the
    routing flags/reason that sent the item to review, the diff rendered in the
    context of the original upstream file, and the package(s) that carry this
    fingerprint (``source_package``/``version``/``patch_name`` are the
    representative instance; ``patch_name`` travels here because the evidence blob
    records it; ``packages`` is every source package carrying the identical patch --
    a fingerprint is deduplicated, so "which packages does this affect?" is real
    review context).  ``reason`` is the queue routing reason when this context was
    built from a queue item, and ``None`` when built straight from a fingerprint
    (the audit/spot-check path).
    """

    fingerprint: str
    diff_body: str
    context_view: str
    draft_category: str | None
    draft_confidence: str | None
    draft_reasoning: str | None
    claim_category: str
    reason: str | None
    source_package: str
    version: str
    patch_name: str
    packages: tuple[str, ...]


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
    llm_rows = [
        row for row in ledger_mod.decisions_for(conn, fingerprint)
        if row['kind'] == 'llm' and row['superseded_at'] is None]
    return llm_rows[-1] if llm_rows else None


def build_review_context(conn: sqlite3.Connection, corpus_dir: str, index_path: str,
                         *, fingerprint: str, fetch, item: sqlite3.Row | None = None
                         ) -> ReviewContext | None:
    """Assemble the human-review context for ONE fingerprint, or ``None``.

    Keyed by ``fingerprint`` (not by a queue item) so the same render path serves
    both the queue review page and the audit/spot-check view: loads the
    representative body (``measure.read_body`` via the phase-1 index), the author's
    claim (``extract_claim``), and the live LLM draft (``decisions_for``); fetches
    the original upstream file and renders the diff in context.  ``item`` is the
    optional queue row -- supplied when reviewing from the queue (its ``reason``
    rides along), and ``None`` when auditing a settled fingerprint.

    Returns ``None`` when the fingerprint has no representative index row (nothing
    to show); the caller treats that as a defer/skip.  ``fetch`` is injected so
    this is pure given a fake; this module never reads a clock or a socket.
    """
    patch_info = _representative_patch(index_path, fingerprint)
    if patch_info is None:
        # No provenance row -> no body to review.  A queued fingerprint with no
        # index row cannot be shown, and silently dropping it would hide it.
        return None

    source_package, version, patch_name, raw_sha = patch_info
    body = measure.read_body(corpus_dir, raw_sha)
    claim = extract_claim(patch_name, body)

    from divergulent.classify.triage import diff_body as extract_diff_body
    body_diff = extract_diff_body(body)

    # Fetch the ORIGINAL upstream content for each file the patch touches, keyed
    # by the file's real source-tree path (the ``+++ b/<path>`` target), NOT the
    # quilt patch filename -- so sources.debian.org serves the right file.
    context_view = build_context_view(source_package, version, body_diff, fetch=fetch)

    draft = _llm_draft(conn, fingerprint)
    return ReviewContext(
        fingerprint=fingerprint,
        diff_body=body_diff,
        context_view=context_view,
        draft_category=draft['category'] if draft is not None else None,
        draft_confidence=draft['confidence'] if draft is not None else None,
        draft_reasoning=_draft_reasoning(draft),
        claim_category=claim.claimed_category,
        reason=item['reason'] if item is not None else None,
        source_package=source_package,
        version=version,
        patch_name=patch_name,
        packages=_carrying_packages(index_path, fingerprint))


def record_review_verdict(conn: sqlite3.Connection, item: sqlite3.Row,
                          context: ReviewContext, choice: str, *, signer, now
                          ) -> ReviewOutcome:
    """Record the human ``choice`` for a queued item against its ``context``.

    ``choice`` is the LLM draft's category (accept), an override category,
    ``'unknown'``, or ``'defer'``.  On a real verdict (anything but ``'defer'``):
    builds the canonical record, signs it (``sign_decision``), appends a
    ``kind='human'`` decision with ``verified=True``, the ``signature`` +
    ``signed_by``, and an ``evidence`` JSON recording what was reviewed, then marks
    the queue ``item`` reviewed.  On ``'defer'`` the item is left pending and
    NOTHING is recorded.

    ``signer``/``now`` are injected so this is pure given a fake signer; ``now`` is
    the caller-supplied ISO-8601 timestamp (this module never reads a clock).
    """
    fingerprint = context.fingerprint
    if choice == CHOICE_DEFER:
        return ReviewOutcome(fingerprint, False, True, None, None)

    category = (context.draft_category or 'unknown') if choice == CHOICE_ACCEPT else choice

    record_bytes = canonical_record(fingerprint, category, now)
    signature, signed_by = sign_decision(record_bytes, signer=signer)

    evidence = json.dumps({
        'reviewed': {
            'source_package': context.source_package,
            'version': context.version,
            'patch_name': context.patch_name,
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


def review_one(conn: sqlite3.Connection, corpus_dir: str, index_path: str,
               item: sqlite3.Row, *, fetch, signer, ask, now) -> ReviewOutcome:
    """Review ONE pending item: gather context, ask the human, record the verdict.

    A thin composition of :func:`build_review_context` (gather), ``ask`` (decide),
    and :func:`record_review_verdict` (record).  A missing representative row
    leaves the item pending without recording (the audit-able defer case).

    ``fetch``/``signer``/``ask``/``now`` are all injected so this is pure given
    fakes; ``now`` is the caller-supplied ISO-8601 timestamp.
    """
    context = build_review_context(
        conn, corpus_dir, index_path, fingerprint=item['fingerprint'], item=item, fetch=fetch)
    if context is None:
        return ReviewOutcome(item['fingerprint'], False, True, None, None)
    choice = ask(context)
    return record_review_verdict(conn, item, context, choice, signer=signer, now=now)


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


def _carrying_packages(index_path: str, fingerprint: str) -> tuple[str, ...]:
    """Every distinct source package that carries ``fingerprint``, sorted.

    A fingerprint is deduplicated across all carried instances, so the same
    patch can ride in many packages (488 fingerprints in the corpus span more
    than one; some dozens). The review UI shows these so a reviewer sees the real
    blast radius -- a patch in 53 packages is a different risk than one in a
    single obscure package. Returns a sorted tuple; empty if the fingerprint has
    no index row.
    """
    connection = sqlite3.connect(index_path)
    try:
        rows = connection.execute(
            'SELECT DISTINCT source_package FROM patch WHERE fingerprint = ? '
            'ORDER BY source_package',
            (fingerprint,)).fetchall()
    finally:
        connection.close()
    return tuple(row[0] for row in rows)


# ---------------------------------------------------------------------------
# 5. Re-queue and history (ledger operations -- no diff, no signing).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RequeueOutcome:
    """What :func:`requeue_one` did for one fingerprint.

    ``superseded`` is how many live human decisions were marked superseded (so the
    prior verdict no longer stands while the patch is back under review);
    ``reopened`` is how many already-reviewed queue items were re-opened; and
    ``created`` is whether a fresh pending item had to be appended (the
    fingerprint had no queue item at all).
    """

    fingerprint: str
    superseded: int
    reopened: int
    created: bool


def resolve_fingerprint(conn: sqlite3.Connection, query: str) -> tuple[str | None, list[str]]:
    """Resolve a full-or-PREFIX fingerprint to the one it names; ``(resolved, matches)``.

    A reviewer pastes a long hash or just its leading hex; this matches ``query``
    as a prefix against every fingerprint known to the ledger (decisions OR the
    review queue) and returns ``(fingerprint, [fingerprint])`` for a unique hit,
    ``(None, [])`` for no match, and ``(None, matches)`` when the prefix is
    ambiguous (so the caller can list the candidates rather than guess).
    """
    rows = conn.execute(
        'SELECT fingerprint FROM ('
        '  SELECT DISTINCT fingerprint FROM decision WHERE fingerprint LIKE ? '
        '  UNION '
        '  SELECT DISTINCT fingerprint FROM review_queue WHERE fingerprint LIKE ?'
        ') ORDER BY fingerprint LIMIT 11',
        (query + '%', query + '%')).fetchall()
    matches = [row[0] for row in rows]
    if len(matches) == 1:
        return matches[0], matches
    return None, matches


def requeue_one(conn: sqlite3.Connection, fingerprint: str, *, now,
                reason: str | None = None) -> RequeueOutcome:
    """Put ``fingerprint`` back in the human-review queue; pure given ``now``.

    Supersedes any live ``kind='human'`` decision for the fingerprint (so its
    settled verdict no longer stands while it is re-reviewed), then makes it
    pending again: re-opening an already-reviewed queue item if one exists, else
    -- when there is neither a reviewed nor a pending item -- appending a fresh
    pending item carrying ``reason``.  Does NOT commit or rebuild the verdict
    cache; the CLI does both once.  ``now`` is caller-supplied (this module never
    reads a clock).
    """
    superseded = ledger_mod.supersede_decisions_for_fingerprint(
        conn, fingerprint=fingerprint, kind='human', superseded_at=now, commit=False)
    reopened = ledger_mod.reopen_review_items(conn, fingerprint=fingerprint, commit=False)

    created = False
    if reopened == 0 and not ledger_mod.pending_review_item_exists(conn, fingerprint=fingerprint):
        ledger_mod.append_review_item(
            conn, fingerprint=fingerprint,
            reason=reason or 'manually re-queued for human review',
            draft_category=None, draft_confidence=None,
            enqueued_at=now, priority=0, commit=False)
        created = True

    return RequeueOutcome(fingerprint, superseded, reopened, created)


# ---------------------------------------------------------------------------
# 6. The CLI (``python -m divergulent.classify.review <command>``).
#
# Three subcommands: ``review`` (the default-feeling workhorse) drains the queue
# interactively -- the ONLY place that reads a wall clock, selects the REAL
# Sigstore signer, and reads interactive stdin; ``requeue`` sends one fingerprint
# back for re-review; ``history`` lists recent human verdicts.  The heavy/verify
# pieces are lazy-imported so the module stays import-time clean; the tests
# inject fakes and never reach a real backend.
# ---------------------------------------------------------------------------

DEFAULT_LIMIT = 10
DEFAULT_HISTORY = 20


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


def _page(text: str) -> None:
    """Show ``text`` through a pager so a big diff does not scroll off-screen.

    Honours ``$PAGER`` (defaulting to ``less -FRX``: ``-F`` quits immediately if
    the content fits one screen so short diffs are not trapped in the pager,
    ``-R`` passes raw control chars, ``-X`` does not clear the screen). When
    stdout is not a TTY, or no pager is available, or the pager errors, it falls
    back to a plain ``print`` -- so scripted/non-interactive use is unaffected.
    """
    import os
    import shutil
    import subprocess
    import sys

    if not sys.stdout.isatty():
        print(text)
        return
    pager = os.environ.get('PAGER') or ('less -FRX' if shutil.which('less') else '')
    if not pager:
        print(text)
        return
    try:
        subprocess.run(pager, shell=True, input=text, text=True, check=False)
    except OSError:
        print(text)


def _format_package_lines(context: ReviewContext, *, limit: int = MAX_PACKAGES_SHOWN) -> list[str]:
    """The package line(s) for the review view: representative + full blast radius.

    Always shows the representative ``package: <name> (<version>)``.  When the
    fingerprint is carried by more than one source package, adds a second line
    naming up to ``limit`` of them (truncating the rest as ``(+N more)``), so the
    reviewer sees how widely the identical patch is carried without flooding the
    screen for a fingerprint that spans dozens.
    """
    lines = ['package: %s (%s)' % (context.source_package, context.version)]
    others = context.packages
    if len(others) > 1:
        shown = ', '.join(others[:limit])
        extra = len(others) - limit
        suffix = ' (+%d more)' % extra if extra > 0 else ''
        lines.append('carried by %d packages: %s%s' % (len(others), shown, suffix))
    return lines


def _assignable_categories() -> tuple[str, ...]:
    """The categories a human reviewer may assign -- the FULL category enum.

    The LLM drafts only the semantic categories (``triage.TRIAGE_CATEGORIES``); a
    human can additionally assign the structural ``test`` category
    (``rules._rule_test_only``).  The LLM never drafts ``test`` (it judges intent,
    not structure), but a test-only item that reached review -- e.g. queued
    before the deterministic test-only rule was applied -- must still be
    classifiable as ``test`` rather than mis-filed under a semantic category.
    """
    from divergulent.classify.triage import TRIAGE_CATEGORIES
    return tuple(TRIAGE_CATEGORIES) + ('test',)


def _interactive_ask(context: ReviewContext) -> str:
    """The real interactive ``ask``: page the context + draft + claim, read stdin.

    Pages the diff IN CONTEXT, the LLM draft (category + confidence + reasoning),
    the author's claim, and the routing reason through ``$PAGER`` so a large diff
    is navigable, then reads the human's choice from stdin: accept the LLM draft,
    override to a named category (the full enum, including ``test``), or defer.
    The reading of stdin is confined here (the CLI entry); every other path takes
    ``choice`` as data.
    """
    categories = _assignable_categories()

    view = ['=' * 78, 'fingerprint: %s' % context.fingerprint]
    view.extend(_format_package_lines(context, limit=MAX_PACKAGES_SHOWN))
    if context.reason:
        view.append('routed to review because: %s' % context.reason)
    view.append('author claim category: %s' % context.claim_category)
    if context.draft_category is not None:
        view.append('LLM draft: %s (confidence %s)' % (
            context.draft_category, context.draft_confidence))
        if context.draft_reasoning:
            view.append('LLM reasoning: %s' % context.draft_reasoning)
    else:
        view.append('LLM draft: (none)')
    view.append('-' * 78)
    view.append(context.context_view)
    view.append('-' * 78)
    _page('\n'.join(view))

    options = []
    if context.draft_category is not None:
        options.append('"accept" (take the LLM draft: %s)' % context.draft_category)
    options.append('a category to override: %s' % ', '.join(categories))
    options.append('"defer" (leave it for later)')
    print('Your verdict -- ' + '; '.join(options))

    valid = set(categories) | {CHOICE_ACCEPT, CHOICE_DEFER}
    while True:
        choice = input('verdict> ').strip()
        if choice in valid:
            return choice
        print('  please enter one of: %s' % ', '.join(sorted(valid)))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m divergulent.classify.review',
        description='The LOCAL, INTERACTIVE human-review tool (never CI, never a '
                    'client feature). Review the queue, send a patch back for '
                    're-review, or list recent verdicts. No malice is ever '
                    'pronounced by a machine.')
    sub = parser.add_subparsers(dest='command', required=True)

    p_review = sub.add_parser(
        'review', help='drain the human-review queue interactively',
        description='Drain the queue: show each diff in the context of the original '
                    'upstream code alongside the LLM draft and the author claim, take '
                    "the human's verdict, and record it as a SIGNED ManualDecision "
                    '(Sigstore identity -> non-repudiation).')
    p_review.add_argument('ledger', help='path to a ledger sqlite built by classify.ledger')
    p_review.add_argument('corpus_dir', help='directory of a corpus built by classify.corpus')
    p_review.add_argument('--index', default=None,
                          help='path to the phase-1 sqlite fingerprint index (default: '
                               '<corpus_dir>/fingerprints.sqlite)')
    p_review.add_argument('--limit', type=int, default=DEFAULT_LIMIT,
                          help='max items to review this session (default: %d)' % DEFAULT_LIMIT)
    p_review.set_defaults(func=_cmd_review)

    p_requeue = sub.add_parser(
        'requeue', help='send a fingerprint back to the human-review queue',
        description='Re-open one fingerprint for human review: supersede its settled '
                    'human verdict (kept in history) and make it pending again, so a '
                    'reviewer can reconsider it. Accepts a full fingerprint or an '
                    'unambiguous leading prefix.')
    p_requeue.add_argument('ledger', help='path to a ledger sqlite built by classify.ledger')
    p_requeue.add_argument('fingerprint', help='the fingerprint (full hex or unique prefix)')
    p_requeue.add_argument('--reason', default=None,
                           help='reason recorded on a freshly created queue item '
                                '(default: "manually re-queued for human review")')
    p_requeue.set_defaults(func=_cmd_requeue)

    p_history = sub.add_parser(
        'history', help='list recent human verdicts (newest first)',
        description='Show the last N human review verdicts, newest first, including '
                    'ones later superseded -- so a reviewer can spot and reconsider a '
                    'call they have since changed (re-open it with "requeue").')
    p_history.add_argument('ledger', help='path to a ledger sqlite built by classify.ledger')
    p_history.add_argument('--limit', type=int, default=DEFAULT_HISTORY,
                           help='how many recent verdicts to show (default: %d)' % DEFAULT_HISTORY)
    p_history.set_defaults(func=_cmd_history)

    return parser


def _cmd_review(args) -> int:
    """``review``: drain the queue locally, signing each human verdict.

    Pulls the highest-priority pending items (``pending_review_items``), reviews
    each via :func:`review_one` -- wiring the REAL ``fetch`` (cached HttpClient),
    the REAL Sigstore ``signer``, the interactive stdin ``ask``, and the single
    clock read -- up to ``--limit``, then rebuilds the current-verdict cache so a
    fresh human verdict tops the precedence immediately.
    """
    import os

    from divergulent.classify import verdict as verdict_mod

    index_path = args.index or os.path.join(args.corpus_dir, 'fingerprints.sqlite')

    fetch = _real_fetch()
    conn = ledger_mod.open_ledger(args.ledger)
    try:
        pending = ledger_mod.pending_review_items(conn)
        # One signer for the whole session: it authenticates on the FIRST
        # signature (so an all-defer session never prompts) and reuses the token
        # for the rest, refreshing it if it expires while a diff is being read.
        signer = build_sigstore_signer() if pending[:args.limit] else None
        reviewed = deferred = 0
        for item in pending[:args.limit]:
            outcome = review_one(
                conn, args.corpus_dir, index_path, item,
                fetch=fetch, signer=signer, ask=_interactive_ask,
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


def _cmd_requeue(args) -> int:
    """``requeue``: send one fingerprint back to the human-review queue.

    Resolves the fingerprint (full hex or unambiguous prefix), supersedes its
    live human verdict, re-opens (or creates) its queue item, and rebuilds the
    verdict cache so the fingerprint drops back to pending immediately.  Refuses
    an unknown or ambiguous fingerprint with a clear, non-zero-exit message
    rather than guessing.
    """
    from divergulent.classify import verdict as verdict_mod

    conn = ledger_mod.open_ledger(args.ledger)
    try:
        resolved, matches = resolve_fingerprint(conn, args.fingerprint)
        if resolved is None:
            if not matches:
                print('no fingerprint matches %r' % args.fingerprint, file=sys.stderr)
            else:
                print('ambiguous prefix %r matches %d fingerprints:' % (
                    args.fingerprint, len(matches)), file=sys.stderr)
                for fingerprint in matches[:10]:
                    print('  %s' % fingerprint, file=sys.stderr)
                if len(matches) > 10:
                    print('  ... (more)', file=sys.stderr)
            return 1

        outcome = requeue_one(conn, resolved, now=_cli_now(), reason=args.reason)
        conn.commit()
        verdict_mod.rebuild_current_verdict(conn)
    finally:
        conn.close()

    where = ('re-opened its existing queue item' if outcome.reopened
             else 'created a new pending queue item' if outcome.created
             else 'already pending -- left as is')
    print('re-queued %s: superseded %d human verdict(s), %s' % (
        resolved[:16], outcome.superseded, where))
    return 0


def _cmd_history(args) -> int:
    """``history``: print the last N human verdicts, newest first.

    Read-only.  Lists each recent ``kind='human'`` decision -- category, when,
    who signed it, the source package it concerned, and whether it has since been
    superseded -- so a reviewer can scan their recent calls and re-open any they
    want to reconsider.
    """
    conn = ledger_mod.open_ledger(args.ledger)
    try:
        rows = ledger_mod.recent_human_decisions(conn, limit=args.limit)
    finally:
        conn.close()

    if not rows:
        print('no human reviews recorded yet')
        return 0

    print('last %d human verdict(s), newest first:' % len(rows))
    for row in rows:
        print(_format_history_row(row))
    return 0


def _format_history_row(row: sqlite3.Row) -> str:
    """One compact ``history`` line for a human decision row."""
    package = _history_package(row['evidence'])
    when = (row['decided_at'] or '')[:19]
    status = ' [SUPERSEDED]' if row['superseded_at'] else ''
    signer = row['signed_by'] or '(unsigned)'
    return '  %s  %-13s  %s  %-28s  %s%s' % (
        row['fingerprint'][:16], row['category'], when, package, signer, status)


def _history_package(evidence: str | None) -> str:
    """The source package recorded in a human decision's evidence JSON, or ``'?'``."""
    if not evidence:
        return '?'
    try:
        return json.loads(evidence).get('reviewed', {}).get('source_package') or '?'
    except (ValueError, TypeError):
        return '?'


def _signed_by(conn: sqlite3.Connection, decision_id: int | None) -> str:
    """The ``signed_by`` identity recorded on a decision row (for the CLI print)."""
    if decision_id is None:
        return '(unsigned)'
    row = conn.execute(
        'SELECT signed_by FROM decision WHERE id = ?', (decision_id,)).fetchone()
    return (row[0] if row and row[0] else '(unsigned)')


def main(argv: list[str] | None = None) -> int:
    """``python -m divergulent.classify.review <command>``: dispatch a subcommand.

    Parses the subcommand (``review`` / ``requeue`` / ``history``) and calls its
    handler.  Each handler owns its own ledger connection and the single clock
    read; this entry point only routes.  A bad ledger path raises
    :class:`ledger.LedgerError`, which we render as one clear stderr line.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ledger_mod.LedgerError as exc:
        print('error: %s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
