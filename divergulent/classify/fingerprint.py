"""Pure, versioned normalisation and fingerprinting of unified-diff patch bodies.

Primarily curation-side: this runs centrally in the builder to deduplicate
carried patches. The CLIENT also imports it, for one narrow purpose -- to hash a
patch it has already fetched and join it to the published classification bundle
(hashing a diff is not classifying it; no rule and no LLM run on the client). The
functions here are pure -- no I/O, no network -- so a corpus can be re-normalised
offline and the client's join key always matches the curation side's exactly.

A patch's classification is a property of its *content*, so the fingerprint key
is ``sha256(normalise(diff_body))``. We normalise the diff to a canonical form
that strips what varies between trivially-different copies of the same change
(hunk line-number offsets, file-header timestamps, ``a/``/``b/`` prefixes, git
decoration, trailing whitespace, line endings) without altering the change
itself, then hash the result.

Only the *diff body* is fingerprinted. A quilt patch file often opens with a
DEP-3 / free-text header (``Description:``, ``Origin:``, ``Forwarded:`` ...)
before the diff begins. That header is an author-controlled *claim*, handled
separately downstream; it must not be part of the fingerprint, so two patches
with identical diffs but different descriptions share a fingerprint.

The normalisation is *versioned*: a fingerprint is ``(version, digest)``
because every later classification phase keys off it. Only ``version == 1`` is
defined; any other value raises ``ValueError``. The structure keeps adding a
future version easy.

The v1 canonical is ``strip_path=True, drop_context=False``. The phase-1 crawl
measured the sensitivity matrix (path in/out x context in/out) over the real
~61.5k-patch corpus and found the distinct count varies <2.5% across all four
variants, so the choice is settled and the knobs remain first-class only for
re-measuring. ``keep_context`` is the conservative choice (two patches with
identical changes but different surrounding code are different changes);
``strip_path`` merges the same change applied to differently-named files. See
docs/plans/PLAN-patch-classification-phase-01-findings.md.
"""
from __future__ import annotations

import hashlib

SUPPORTED_VERSIONS = (1,)

# Git/quilt decoration lines that carry no change content and are dropped
# entirely. Matched by prefix against the line with trailing whitespace already
# stripped.
_DECORATION_PREFIXES = (
    'diff --git ',
    'index ',
    'new file mode',
    'deleted file mode',
    'old mode',
    'new mode',
    'similarity index',
    'dissimilarity index',
    'rename from',
    'rename to',
    'copy from',
    'copy to',
)


def _split_lines(diff_text: str) -> list[str]:
    """Split into lines on any line ending, dropping the terminators.

    ``str.splitlines`` already treats ``\\r\\n``, ``\\r`` and ``\\n`` as
    breaks, which is how we normalise line endings to ``\\n`` on rejoin.
    """
    return diff_text.splitlines()


def _is_file_header(line: str) -> bool:
    return line.startswith('--- ') or line.startswith('+++ ')


def _diff_start(lines: list[str]) -> int:
    """Index of the first line that is part of the diff body.

    The diff starts at the first ``diff --git `` line, OR the first ``--- ``
    file header that is immediately followed by a ``+++ `` line, OR a bare
    ``@@ `` hunk header. Everything before that is an author-controlled header
    (DEP-3 / free text) and is not fingerprinted.
    """
    for index, line in enumerate(lines):
        if line.startswith('diff --git '):
            return index
        if line.startswith('@@ '):
            return index
        if line.startswith('--- '):
            nxt = lines[index + 1] if index + 1 < len(lines) else ''
            if nxt.startswith('+++ '):
                return index
    # No recognisable diff body: nothing to fingerprint.
    return len(lines)


def _header_path(line: str) -> str:
    """Path from a ``--- ``/``+++ `` header, timestamp- and prefix-stripped.

    The marker is the first three characters (``---`` or ``+++``); everything
    after the following space is ``<path>[<sep><timestamp>]``. quilt/diff
    separate the path from the trailing date with a tab or a run of spaces, so
    we cut at the first tab or double-space. The leading ``a/`` or ``b/`` quilt
    prefix is then removed.
    """
    rest = line[4:]
    # Drop the trailing timestamp: split on the first tab, else first run of
    # two or more spaces.
    tab = rest.find('\t')
    if tab != -1:
        rest = rest[:tab]
    else:
        double = rest.find('  ')
        if double != -1:
            rest = rest[:double]
    path = rest.strip()
    if path.startswith('a/') or path.startswith('b/'):
        path = path[2:]
    return path


def _normalise_v1(diff_text: str, *, strip_path: bool, drop_context: bool) -> str:
    lines = _split_lines(diff_text)
    out: list[str] = []
    index = _diff_start(lines)
    while index < len(lines):
        raw = lines[index].rstrip()
        index += 1

        if any(raw.startswith(prefix) for prefix in _DECORATION_PREFIXES):
            continue

        if _is_file_header(raw):
            marker = raw[:3]
            if strip_path:
                out.append(marker)
            else:
                path = _header_path(raw)
                out.append(f'{marker} {path}' if path else marker)
            continue

        if raw.startswith('@@'):
            # Drop the line-number ranges and the volatile function-context
            # tail after the second ``@@``.
            out.append('@@')
            continue

        if raw.startswith(' '):
            # Context line.
            if drop_context:
                continue
            out.append(raw)
            continue

        # Change lines (``+``/``-``), the ``\\ No newline at end of file``
        # marker, and any blank line within the body are retained verbatim
        # (already right-stripped).
        out.append(raw)

    # Deterministic, trailing-newline-terminated output.
    return '\n'.join(out) + '\n' if out else ''


def normalise(
        diff_text: str, *, version: int = 1, strip_path: bool = True,
        drop_context: bool = False) -> str:
    """Canonicalise a unified-diff/quilt patch body for fingerprinting.

    See the module docstring for the full v1 strip-set. ``strip_path`` and
    ``drop_context`` are real knobs (the 1c sensitivity matrix sweeps them);
    the v1 defaults are provisional. ``version != 1`` raises ``ValueError``.
    """
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(f'unsupported normalisation version: {version!r}')
    return _normalise_v1(diff_text, strip_path=strip_path, drop_context=drop_context)


def fingerprint(
        diff_text: str, *, version: int = 1, strip_path: bool = True,
        drop_context: bool = False) -> tuple[int, str]:
    """Return ``(version, sha256_hexdigest)`` of the normalised diff body.

    The digest is taken over ``normalise(...)`` encoded as UTF-8. The version
    travels with the digest because every later phase keys off it.
    """
    normalised = normalise(
        diff_text, version=version, strip_path=strip_path, drop_context=drop_context)
    digest = hashlib.sha256(normalised.encode('utf-8')).hexdigest()
    return (version, digest)
