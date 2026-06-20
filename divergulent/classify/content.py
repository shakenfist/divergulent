"""Content profile of a patch diff body — the ground-truth half of classification.

Where ``claim.py`` reads what the author SAYS about a patch, this module reads
what the diff DOES.  ``profile(text)`` is a pure function over a unified-diff /
quilt patch body: no I/O, no network.  It produces a ``ContentProfile`` — a
structured, factual description of what files the diff touches and how — which
step 2c's rules consume.

The most important thing this module gets right is **file typing** (code vs
prose).  The whole "no cry wolf" promise rides on it: a construct mentioned in
a manpage is not the same as one added to a ``.c`` file, so later rules that
look for executable behaviour must run only over *code* files.  Every touched
file is typed into exactly one of **test / build / doc / code / data** with a
documented precedence (see ``_classify_file``).

The trivial-only boolean flags (``is_empty``, ``ignore_file_only``,
``whitespace_only``, ``comment_only``) drive a "packaging / trivial" verdict
downstream, so they default **False** and are set True only when we are
confident.  Crying wolf the other way — calling a real change trivial — is the
dangerous failure here, so every flag is deliberately conservative.

Curation-side only: no client command imports ``classify/``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from divergulent.classify import fingerprint as fp

# ---------------------------------------------------------------------------
# Version tag — phase-3 ledger keys on this to detect stale verdicts.
# ---------------------------------------------------------------------------

CONTENT_RULE_VERSION = 1

# ---------------------------------------------------------------------------
# File-type vocabulary.  Every touched file is typed into exactly one of these.
# ---------------------------------------------------------------------------

FILE_TYPES = ('test', 'build', 'doc', 'code', 'data')

# ---------------------------------------------------------------------------
# File-typing tables.  Tuned against the phase-1 recurring-tail taxonomy.
# Precedence is documented in ``_classify_file``; the tables here are the raw
# membership sets each precedence step consults.
# ---------------------------------------------------------------------------

# A path component that marks a file as a test.  Matched as a whole directory
# component (so ``tests/`` matches but ``contests/`` does not).
_TEST_DIR_COMPONENTS = ('test', 'tests', 'spec', 't')

# Build-system basenames (exact, case-sensitive where the ecosystem is).
_BUILD_BASENAMES = (
    'configure',
    'configure.ac',
    'configure.in',
    'cmakelists.txt',
    'meson.build',
    'setup.py',
    'setup.cfg',
    'pyproject.toml',
    'cargo.toml',
    'autogen.sh',
)

# Build-system extensions.
_BUILD_EXTENSIONS = ('.mk', '.m4', '.cmake', '.gyp', '.pro')

# Documentation basenames (matched case-insensitively, prefix where noted).
_DOC_EXACT_BASENAMES = ('news', 'authors', 'copying', 'todo')
_DOC_PREFIX_BASENAMES = ('readme', 'changelog')

# Documentation extensions that are docs wherever they live.
_DOC_EXTENSIONS = ('.md', '.rst', '.pod', '.adoc', '.texi', '.texinfo')

# Documentation extensions that are docs only under a documentation path
# (``.txt``/``.html`` are too generic to call doc on their own).
_DOC_PATH_ONLY_EXTENSIONS = ('.txt', '.html', '.htm')

# Directory components that mark a documentation tree.
_DOC_DIR_COMPONENTS = ('doc', 'docs', 'man', 'documentation')

# Source-code extensions.
_CODE_EXTENSIONS = (
    '.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.hh', '.hxx',
    '.py', '.go', '.js', '.ts', '.rs', '.java', '.rb', '.pl', '.pm',
    '.php', '.sh', '.bash', '.lua', '.el', '.scala', '.swift', '.kt',
    '.cs', '.vala', '.ml',
)

# Manpage extensions: ``.1`` .. ``.9`` (optionally with a trailing locale
# suffix such as ``.1p`` or ``.3pm``).
_MANPAGE_RE = re.compile(r'\.[1-9][a-z]*$')

# Ignore-file basenames.  When a diff touches *only* these and the changed
# lines look like ignore patterns, ``ignore_file_only`` may be set.
_IGNORE_BASENAMES = (
    '.gitignore',
    '.bzrignore',
    '.hgignore',
    '.cvsignore',
    '.svnignore',
)

# /dev/null sentinel used by diff for added/deleted files.
_DEV_NULL = '/dev/null'

# ---------------------------------------------------------------------------
# Per-language comment syntax, keyed by file extension.  ``line`` markers begin
# a line comment to end of line; ``block`` is a single (open, close) pair.
# Only used by the conservative ``comment_only`` flag.
# ---------------------------------------------------------------------------

_HASH = ('#',)
_SLASH = ('//',)
_C_BLOCK = ('/*', '*/')

_LINE_COMMENT_MARKERS = {
    '.py': _HASH, '.sh': _HASH, '.bash': _HASH, '.pl': _HASH, '.pm': _HASH,
    '.rb': _HASH, '.mk': _HASH, '.m4': ('dnl', '#'), '.toml': _HASH,
    '.cfg': _HASH, '.yaml': _HASH, '.yml': _HASH,
    '.c': _SLASH, '.h': _SLASH, '.cc': _SLASH, '.cpp': _SLASH, '.cxx': _SLASH,
    '.hpp': _SLASH, '.hh': _SLASH, '.hxx': _SLASH, '.js': _SLASH, '.ts': _SLASH,
    '.go': _SLASH, '.rs': _SLASH, '.java': _SLASH, '.php': ('//', '#'),
    '.scala': _SLASH, '.swift': _SLASH, '.kt': _SLASH, '.cs': _SLASH,
    '.vala': _SLASH,
    '.lua': ('--',), '.el': (';',),
}

_BLOCK_COMMENT_MARKERS = {
    '.c': _C_BLOCK, '.h': _C_BLOCK, '.cc': _C_BLOCK, '.cpp': _C_BLOCK,
    '.cxx': _C_BLOCK, '.hpp': _C_BLOCK, '.hh': _C_BLOCK, '.hxx': _C_BLOCK,
    '.js': _C_BLOCK, '.ts': _C_BLOCK, '.go': _C_BLOCK, '.rs': _C_BLOCK,
    '.java': _C_BLOCK, '.php': _C_BLOCK, '.scala': _C_BLOCK, '.swift': _C_BLOCK,
    '.kt': _C_BLOCK, '.cs': _C_BLOCK, '.vala': _C_BLOCK, '.ml': ('(*', '*)'),
}

# Collapse runs of whitespace for the whitespace-only comparison.
_WS_RUN_RE = re.compile(r'\s+')

# Makefiles have no extension but use ``#`` comments and are build files.
_MAKEFILE_PREFIXES = ('makefile', 'gnumakefile')


# ---------------------------------------------------------------------------
# ContentProfile dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContentProfile:
    """Ground-truth description of what a diff body touches.

    Built from the diff alone — never from the author's claim.  Use alongside
    ``Claim`` (step 2a); their disagreement is the loudest signal.
    """

    files: list[tuple[str, str]]
    """``(path, file_type)`` for every touched file, in diff order.

    ``path`` is the target path from the ``+++`` header (``a/``/``b/`` prefix
    and timestamp stripped); for a deletion whose target is ``/dev/null`` the
    source path from ``---`` is used instead.  ``file_type`` is one of
    ``FILE_TYPES``.
    """

    file_types: dict[str, int]
    """Count of touched files per type (e.g. ``{'code': 2, 'doc': 1}``).

    Only types that occur are present.
    """

    added_lines: int
    """Total ``+`` lines across all files (excluding the ``+++`` headers)."""

    removed_lines: int
    """Total ``-`` lines across all files (excluding the ``---`` headers)."""

    hunks: int
    """Total ``@@`` hunk headers across all files."""

    added_by_type: dict[str, int]
    """Added line count attributed to each file type."""

    removed_by_type: dict[str, int]
    """Removed line count attributed to each file type."""

    touches_code: bool
    """True iff at least one touched file is typed ``code``.

    The gate later rules use before running any executable-behaviour scan.
    """

    is_empty: bool
    """True iff the body normalises (``fingerprint.normalise``) to empty.

    Captures mode-only / pure-decoration patches that carry no real change.
    """

    whitespace_only: bool
    """True iff the change differs only in whitespace (conservative).

    The multiset of removed lines, with whitespace runs collapsed and stripped,
    equals that of the added lines.  False whenever we are unsure.
    """

    comment_only: bool
    """True iff every changed content line is blank or a comment (conservative).

    Evaluated per file in that file's language; a file whose comment syntax we
    do not know forces this False.
    """

    ignore_file_only: bool
    """True iff the only files touched are ignore files and the changed lines
    are ignore patterns (conservative)."""

    rule_version: int = field(default=CONTENT_RULE_VERSION)
    """The ``CONTENT_RULE_VERSION`` that produced this profile."""


# ---------------------------------------------------------------------------
# File typing
# ---------------------------------------------------------------------------

def _basename(path: str) -> str:
    return path.rsplit('/', 1)[-1]


def _extension(basename: str) -> str:
    """Lower-cased extension including the dot, or '' if none."""
    dot = basename.rfind('.')
    if dot <= 0:  # no dot, or leading-dot dotfile (.gitignore) -> no extension
        return ''
    return basename[dot:].lower()


def _path_components(path: str) -> list[str]:
    return [c for c in path.lower().split('/') if c]


def _classify_file(path: str) -> str:
    """Type ``path`` into exactly one of ``FILE_TYPES``.

    Precedence (first match wins), most specific signal first:

      1. **test** — a ``test``/``tests``/``spec``/``t`` directory component, or
         a basename matching ``test_*`` / ``*_test.*`` / ``*.t``.  Checked first
         so a test source file (``tests/foo.c``) is counted as test, not code:
         a touched test does not mean production code changed.
      2. **build** — anything under ``debian/`` (Debian packaging is build, not
         the upstream source it patches), or a build-system basename/extension
         (``Makefile``, ``configure.ac``, ``*.m4``, ``CMakeLists.txt`` ...).
      3. **doc** — manpages (``*.[1-9]``), doc extensions (``*.md``/``*.rst`` ...),
         a documentation directory tree, or a doc basename (``README`` ...).
         Beats code so prose is never typed as code.
      4. **code** — a recognised source extension.  This is the gate for the
         executable-behaviour scan, so we only reach it for genuine source.
      5. **data** — everything else (``*.json``, ``*.po``, images, ...).

    The code-vs-prose split (steps 3 and 4) is the one that matters most
    downstream, so doc deliberately wins ties against code.
    """
    components = _path_components(path)
    basename = _basename(path)
    base_lower = basename.lower()
    ext = _extension(basename)

    # --- 1. test ---
    if any(c in _TEST_DIR_COMPONENTS for c in components[:-1]):
        return 'test'
    if base_lower.startswith('test_'):
        return 'test'
    if ext == '.t':
        return 'test'
    # ``*_test.<ext>`` (e.g. foo_test.go, bar_test.py).
    stem = base_lower[:-len(ext)] if ext else base_lower
    if stem.endswith('_test'):
        return 'test'

    # --- 2. build ---
    if 'debian' in components[:-1]:
        return 'build'
    if base_lower in _BUILD_BASENAMES:
        return 'build'
    if any(base_lower.startswith(p) for p in _MAKEFILE_PREFIXES):
        return 'build'
    if ext in _BUILD_EXTENSIONS:
        return 'build'

    # --- 3. doc ---
    if _MANPAGE_RE.search(base_lower):
        return 'doc'
    if ext in _DOC_EXTENSIONS:
        return 'doc'
    if any(c in _DOC_DIR_COMPONENTS for c in components[:-1]):
        return 'doc'
    if base_lower in _DOC_EXACT_BASENAMES:
        return 'doc'
    if any(base_lower.startswith(p) for p in _DOC_PREFIX_BASENAMES):
        return 'doc'
    if ext in _DOC_PATH_ONLY_EXTENSIONS and any(
            c in _DOC_DIR_COMPONENTS for c in components[:-1]):
        return 'doc'

    # --- 4. code ---
    if ext in _CODE_EXTENSIONS:
        return 'code'

    # --- 5. data ---
    return 'data'


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------

@dataclass
class _FileSection:
    """Mutable accumulator for one file's hunks while parsing."""

    path: str
    file_type: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    hunks: int = 0


def _strip_header_path(line: str) -> str:
    """Path from a ``--- ``/``+++ `` header (reuse fingerprint's semantics)."""
    return fp._header_path(line)


def _parse_sections(text: str) -> list[_FileSection]:
    """Parse the diff body into per-file sections.

    Starts at ``fingerprint._diff_start`` so any DEP-3 / free-text header is
    skipped exactly as the fingerprint does.  Each ``+++`` opens a section;
    ``@@`` increments its hunk count; ``+``/``-`` content lines accumulate.
    """
    lines = fp._split_lines(text)
    start = fp._diff_start(lines)
    sections: list[_FileSection] = []
    pending_source: str | None = None  # path from the most recent ``---``
    current: _FileSection | None = None

    index = start
    while index < len(lines):
        raw = lines[index].rstrip()
        index += 1

        if any(raw.startswith(p) for p in fp._DECORATION_PREFIXES):
            continue

        if raw.startswith('--- '):
            pending_source = _strip_header_path(raw)
            continue

        if raw.startswith('+++ '):
            target = _strip_header_path(raw)
            # A deletion has target ``/dev/null``; use the source path so the
            # file is still named and typed.
            path = target
            if target == _DEV_NULL and pending_source and pending_source != _DEV_NULL:
                path = pending_source
            current = _FileSection(path=path, file_type=_classify_file(path))
            sections.append(current)
            pending_source = None
            continue

        if raw.startswith('@@'):
            if current is not None:
                current.hunks += 1
            continue

        if current is None:
            continue

        if raw.startswith('+'):
            current.added.append(raw[1:])
        elif raw.startswith('-'):
            current.removed.append(raw[1:])
        # context / ``\ No newline`` / blank lines are not changes.

    return sections


# ---------------------------------------------------------------------------
# Trivial-only flags
# ---------------------------------------------------------------------------

def _normalise_ws(line: str) -> str:
    """Collapse whitespace runs to a single space and strip the ends."""
    return _WS_RUN_RE.sub(' ', line).strip()


def _whitespace_only(sections: list[_FileSection]) -> bool:
    """True iff added and removed lines are equal as whitespace-normalised multisets.

    Conservative: an empty change (no added and no removed) is not
    whitespace-only — that case is ``is_empty``.  A change that only adds or
    only removes lines cannot be whitespace-only.
    """
    added: list[str] = []
    removed: list[str] = []
    for section in sections:
        added.extend(section.added)
        removed.extend(section.removed)
    if not added and not removed:
        return False
    if len(added) != len(removed):
        return False
    added_norm = sorted(_normalise_ws(line) for line in added)
    removed_norm = sorted(_normalise_ws(line) for line in removed)
    if added_norm != removed_norm:
        return False
    # At least one line must actually differ in whitespace; otherwise the
    # normalised sets being equal means the raw sets were equal too (a no-op),
    # which is not a meaningful whitespace change.
    return sorted(added) != sorted(removed)


def _is_comment_or_blank(line: str, ext: str) -> bool:
    """True iff ``line`` is blank or a whole-line comment in language ``ext``.

    Conservative: a block-comment *open* without its matching close on the same
    line is NOT treated as a comment (we do not track multi-line block state),
    so a real ``/* ... */`` spanning lines forces ``comment_only`` False.
    """
    stripped = line.strip()
    if not stripped:
        return True

    for marker in _LINE_COMMENT_MARKERS.get(ext, ()):  # type: ignore[union-attr]
        if stripped.startswith(marker):
            return True

    block = _BLOCK_COMMENT_MARKERS.get(ext)
    if block is not None:
        open_tok, close_tok = block
        if stripped.startswith(open_tok) and stripped.endswith(close_tok) \
                and len(stripped) >= len(open_tok) + len(close_tok):
            return True

    return False


def _comment_only(sections: list[_FileSection]) -> bool:
    """True iff every changed content line is blank or a comment (conservative).

    A file whose language we have no comment syntax for forces False, as does
    any non-comment, non-blank changed line.  An empty change is not
    comment-only (that is ``is_empty``).
    """
    saw_change = False
    for section in sections:
        ext = _extension(_basename(section.path))
        if ext not in _LINE_COMMENT_MARKERS and ext not in _BLOCK_COMMENT_MARKERS:
            # Unknown comment syntax: if it has changed lines we cannot judge.
            if section.added or section.removed:
                return False
            continue
        for line in (*section.added, *section.removed):
            saw_change = True
            if not _is_comment_or_blank(line, ext):
                return False
    return saw_change


def _is_ignore_pattern(line: str) -> bool:
    """True iff ``line`` looks like an ignore-file pattern (or blank/comment).

    Ignore files are line-oriented globs.  We accept blank lines, ``#``
    comments, and any single-token glob/path with no whitespace.  A line with
    interior whitespace is not a typical ignore pattern, so it fails (keeping
    the flag conservative).
    """
    stripped = line.strip()
    if not stripped or stripped.startswith('#'):
        return True
    return not any(c.isspace() for c in stripped)


def _ignore_file_only(sections: list[_FileSection]) -> bool:
    """True iff every touched file is an ignore file and every changed line is
    an ignore pattern (conservative)."""
    if not sections:
        return False
    saw_change = False
    for section in sections:
        if _basename(section.path).lower() not in _IGNORE_BASENAMES:
            return False
        for line in (*section.added, *section.removed):
            saw_change = True
            if not _is_ignore_pattern(line):
                return False
    return saw_change


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def code_added_lines(text: str) -> list[str]:
    """Return the text of ``+`` lines in *code*-typed files only.

    The semantic level that step 2c's dangerous-construct scan runs at: added
    lines in genuine source files, never prose.  A construct that appears in a
    manpage or other doc/build/data file is deliberately excluded, so a manpage
    mentioning ``system("/bin/sh")`` does not surface — only the same string
    *added to a ``.c``/``.sh`` file* does.  The leading ``+`` is stripped; the
    ``+++`` file headers are not included (they are not change lines).

    Pure: no I/O, no network.  Reuses ``_parse_sections`` so header-skipping and
    file typing match ``profile`` exactly.
    """
    lines: list[str] = []
    for section in _parse_sections(text):
        if section.file_type == 'code':
            lines.extend(section.added)
    return lines


def profile(text: str) -> ContentProfile:
    """Build the ``ContentProfile`` for a unified-diff / quilt patch body.

    Pure: no I/O, no network.  ``text`` may include a DEP-3 / free-text header;
    it is skipped via ``fingerprint``'s diff-start detection, so passing the
    whole patch file or just the diff body gives the same result.
    """
    sections = _parse_sections(text)

    files: list[tuple[str, str]] = [(s.path, s.file_type) for s in sections]

    file_types: dict[str, int] = {}
    added_by_type: dict[str, int] = {}
    removed_by_type: dict[str, int] = {}
    added_lines = 0
    removed_lines = 0
    hunks = 0
    for section in sections:
        file_types[section.file_type] = file_types.get(section.file_type, 0) + 1
        added_by_type[section.file_type] = (
            added_by_type.get(section.file_type, 0) + len(section.added))
        removed_by_type[section.file_type] = (
            removed_by_type.get(section.file_type, 0) + len(section.removed))
        added_lines += len(section.added)
        removed_lines += len(section.removed)
        hunks += section.hunks

    is_empty = fp.normalise(text) == ''

    return ContentProfile(
        files=files,
        file_types=file_types,
        added_lines=added_lines,
        removed_lines=removed_lines,
        hunks=hunks,
        added_by_type=added_by_type,
        removed_by_type=removed_by_type,
        touches_code=file_types.get('code', 0) > 0,
        is_empty=is_empty,
        whitespace_only=_whitespace_only(sections),
        comment_only=_comment_only(sections),
        ignore_file_only=_ignore_file_only(sections),
        rule_version=CONTENT_RULE_VERSION,
    )
