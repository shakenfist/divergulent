"""Deterministic marking of files that CLAIM to be build-system generator output.

Some carried patches are dominated by regenerated autotools output -- ``configure``,
``Makefile.in``, ``ltmain.sh`` -- committed into an old-style ``1.0`` source diff.  The
hand-written residue is small and entirely reviewable, but buried.  This module scans a
diff body and marks the files that *say* they are generator output, so later phases can
show a reviewer the residue first and tell the LLM passes what they are not being shown.

Posture (see ``docs/plans/PLAN-generated-marking.md``):

* **A mark, never a verdict.**  The only claim made here is the checkable one: this file
  says it is generator output, by its name and -- where a hunk happens to touch the top
  of the file -- by a generator banner.  It is not a statement that the content is
  benign, machine-produced, or unreviewable.  The xz-utils backdoor shipped precisely in
  dist-only generated build machinery, exploiting the reviewer reflex that autoconf
  output is noise; a marked file stays in the record, badged and collapsible, never
  dropped.  Nothing downstream may map this mark to a category.
* **Name leads, banner corroborates -- with one measured exception.**  A banner is
  visible in only ~4% of bodies (it needs a hunk near the file top), so the name signal
  carries the population.  The banner signal nonetheless runs STANDALONE on every touched
  file, because a banner in a file the name set misses is exactly how the name set's gaps
  get found.  ``Makefile.in`` is the one name that does NOT stand alone: roughly two
  thirds of its corpus matches are hand-written ``AC_OUTPUT`` templates, not automake
  output, so it marks only when a banner also corroborates it (see
  ``_NAME_REQUIRES_BANNER`` and the findings doc's "The Makefile.in problem").
* **Claims, not verification.**  Upgrading "claims generated" to "verified generated"
  (rebuilding the era's autotools output and subtracting it) is deliberately out of
  scope; the vocabulary here says "claims" and means it.

Phase 5 promotes the **translations family**: a ``.ts``/``.po``/``.pot`` extension match
corroborated by catalogue structure in the hunks marks ``family='translations'``, with
the corroborator names as the file's signals.  The extension alone never marks -- ``.ts``
measured 45% Qt Linguist / 55% TypeScript -- and the family name says "translations",
not "generated", because ``.po`` content is human-authored even though tool-managed.

The scanner is pure: no I/O, no network.  ``scan(text)`` is its whole public surface,
plus ``candidate_banner_hits(text)`` which reports the unmeasured generic do-not-edit
family for the phase-1 measurement tool WITHOUT ever marking on it, and
``candidate_translation_hits(text)``, the measurement view of the translations family
(every extension match, corroborated or not -- the uncorroborated ``.ts`` population is
TypeScript, and the measurement tool sizes it).  Beside it sit the
observation helpers: a scan that marks anything rides alongside the category as ONE
supersedable ``generated-content`` observation per fingerprint (``observed_by=
'generated-scan'``, ``rule_version=GENERATED_RULES_VERSION``), recorded by the
deterministic record pass -- ``detail_for(scan)`` builds its compact ``'<family>/
<percent>'`` detail, ``evidence_for(scan)`` its canonical-JSON per-file breakdown, and
``generated_marks(conn)`` reads the live rows back for later phases.  A scan that marks
nothing records nothing: absence means "nothing claimed generation".  Recording the mark
does not promote it -- it is still a mark and never a verdict, and nothing downstream may
map it to a category.

Phase 3 adds the two ROUTING helpers that consume the recorded mark, kept here beside it
so both consumers (the triage driver and the risk gate) share one definition:
``project_residue_first(body, files)`` reorders a diff body residue-first, replacing each
marked file with a loud note about what is not being shown, and
``residue_unlocked_fingerprints(conn)`` composes ``reviewability``'s ``oversized`` set with
the mark's residue arithmetic to say which oversized patches are reviewable after all.
Neither is a verdict either: the first only changes what a model is shown (and says so in
evidence), the second only changes which fingerprints reach one.

Phase 4 adds the one DISPLAY helper the review UIs share, kept here for the same reason:
``construct_tally(body, marked_paths)`` re-runs the deterministic dangerous-construct scan
per file (``rules.scan_dangerous_constructs_by_file`` -- rules' own tables, never a second
copy) and splits the hits into marked files versus the hand-written residue, so a reviewer
sees whether a patch's construct hits are all generated shell or whether one landed in the
residue.  It is advisory display recomputed from the body at render time: the ledger's
``dangerous-construct`` observations are never read, rewritten or re-attributed, and the
tally never claims to be their count.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from divergulent.classify import content
from divergulent.classify import fingerprint as fp
from divergulent.classify import rules as rules_mod

# Folds the name set + banner patterns into a version the phase-2 observation records;
# bumping it supersedes prior observations and re-scans, like every deterministic rule.
# v2: the translations family (corroborated ``.ts``/``.po``/``.pot`` catalogues) joins
# the autotools name set and the banners.
GENERATED_RULES_VERSION = 2

# The observation kind a marked fingerprint records.  A single string shared by the
# recorder (``record.py``), and later the routing and the review UI, so the wire name is
# defined in exactly one place -- the shape ``injection.INJECTION_KIND`` uses.
GENERATED_KIND = 'generated-content'

# The observation's source id, mirroring ``size-rule`` / ``injection-scan``.
GENERATED_OBSERVED_BY = 'generated-scan'

# The signals a file can carry.  Reported separately (and sorted) so evidence says which
# of the two independent claims fired.
NAME_SIGNAL = 'name'
BANNER_SIGNAL = 'banner'

# Well-known generator output basenames, keyed by family.  Only ``autotools`` names
# exist; the shape is a registry so cmake / protobuf families can be added later without
# reshaping the scanner.  (v2's translations family matches by EXTENSION + content
# corroboration instead -- see ``TRANSLATION_EXTENSIONS`` -- so it has no entry here.)
# Matched by EXACT basename equality (never substring) at any
# path depth, and case-sensitively -- these names are case-sensitive on disk, and
# ``Configure`` is somebody else's file.
_NAME_SETS: dict[str, tuple[str, ...]] = {
    # DROPPED (measured, phase-1 findings, "Per-name table" / "Proposed v1 signal set"):
    # ``GNUmakefile.in`` (7 of 7 distinct corpus paths hand-written, 0 of 10 carry a
    # banner -- 100% false-positive rate), ``config.status`` and ``configure.lineno``
    # (0 corpus hits each -- build-time products that never appear in a carried patch).
    'autotools': (
        'configure',
        'Makefile.in',
        'aclocal.m4',
        'libtool.m4',
        'ltoptions.m4',
        'ltsugar.m4',
        'ltversion.m4',
        'lt~obsolete.m4',
        'ltmain.sh',
        'ltconfig',
        'config.guess',
        'config.sub',
        'config.h.in',
        'missing',
        'install-sh',
        'mkinstalldirs',
        'depcomp',
        'compile',
        'ylwrap',
        'ar-lib',
        'py-compile',
        'test-driver',
        'mdate-sh',
        'texinfo.tex',
    ),
    # DELIBERATELY ABSENT: ``acinclude.m4``.  By convention it is a maintainer-WRITTEN
    # input that aclocal reads, even though the historical practice of pasting
    # ``libtool.m4`` into it means its content is often machine-copied.  It stays out
    # pending the phase-1 measurement; the banner signal may still fire on its pasted
    # content, which is exactly the number the findings need.
}

# Basename -> family.  The lookup ``scan`` uses; one entry per name above.
GENERATED_NAME_FAMILY: dict[str, str] = {
    name: family for family, names in _NAME_SETS.items() for name in names}

# Editor / patch-tool backup suffixes.  These are STRIPPED before name matching, not
# excluded: a backup is classified as whatever it is a copy of, in both directions.
# ``Makefile.in.ori`` -> ``Makefile.in`` -> marked; ``Makefile.am.ori`` -> ``Makefile.am``
# -> not marked.  A trailing ``~`` is handled separately (it is a suffix of one char, not
# a dotted extension) and stripping repeats, so ``Makefile.in.orig~`` reduces correctly.
_BACKUP_SUFFIXES = ('.ori', '.orig', '.bak', '.rej')
_BACKUP_TILDE = '~'

# Corroboration tier (phase-1 findings, "The Makefile.in problem"): for a file whose
# backup-stripped basename is in this tuple, the NAME signal counts ONLY when a banner
# hit is also present in the file's region.  With no banner the file is not marked at
# all -- no signals, not even 'name' alone.  Measured: ~880 of 1,280 corpus
# ``Makefile.in`` matches are hand-written ``AC_OUTPUT`` templates in plain-autoconf
# projects, not automake output.  A genuine regeneration rewrites the file from line 1
# and therefore shows its banner, so corroboration costs the target population nothing;
# precision over recall -- a missed mark is honest, a false mark is not.
_NAME_REQUIRES_BANNER = ('Makefile.in',)

# A captured version: digits, dots, hyphens and the occasional letter (``1.5.22``,
# ``2.13a``, ``1.9.6``, automake pre-1.5's hyphenated ``1.4-p6``), constrained to END on
# an alphanumeric.  Autoconf's real banner is ``Generated by GNU Autoconf 2.59.`` -- with
# the sentence's full stop -- and a version class that ran to the end of the run would
# capture ``2.59.``, which then fails to compare equal to the same version read from
# anywhere else.  The hyphen is needed for the automake <=1.4 banner form (measured:
# ``1.4-p5``, ``1.4-p4``, ``1.4-p6`` -- findings doc, "Pattern tuning"); it never
# reaches a trailing hyphen because the mandatory final character is alphanumeric.
_VERSION = r'([0-9](?:[0-9A-Za-z.-]*[0-9A-Za-z])?)'

# Generator self-identification, as ``(generator, family, pattern)``.  Scanned against
# EVERY line of a file's diff region -- added, removed and context -- because a banner
# usually arrives as a context line, and against every touched file, not just
# name-matched ones.  Case-sensitive: the generators emit these strings verbatim, and
# case-folding turns prose about autoconf into a banner.  Where the format carries a
# version it is captured as group 1; it dates the regeneration (autoconf 2.59 puts gatos
# at ~2003-2006) and the future rebuild-and-subtract work needs it.
BANNERS: tuple[tuple[str, str, re.Pattern], ...] = (
    ('autoconf', 'autotools', re.compile(r'Generated by GNU Autoconf(?: %s)?' % _VERSION)),
    ('automake', 'autotools', re.compile(r'generated by automake %s' % _VERSION)),
    # The automake <=1.4 banner form, measured separately from the entry above (57
    # regions / 7 fingerprints, versions 1.4-p5 etc. -- findings doc, "Pattern tuning").
    # Its wording ("generated automatically by automake") never overlaps the aclocal
    # entry below ("generated automatically by aclocal"), so neither can shadow the
    # other.
    ('automake', 'autotools', re.compile(r'generated automatically by automake %s' % _VERSION)),
    ('aclocal', 'autotools', re.compile(r'generated automatically by aclocal %s' % _VERSION)),
    # autoheader names its input, not its version.
    ('autoheader', 'autotools', re.compile(r'Generated from [^ ]+ by autoheader')),
    # The ``ltmain.sh`` form carries a version; the bare product name does not, so it is
    # a separate entry and comes second (version capture must win when both could match).
    ('libtool', 'autotools', re.compile(r'ltmain\.sh \(GNU libtool\)(?: %s)?' % _VERSION)),
    # Narrowed from the bare product name (``\bGNU Libtool\b``), which fired on prose --
    # a fortune cookie and libtool.texi's own manual text (findings doc, gap list). The
    # boilerplate line keeps all 10 real hits and kills both false positives.
    ('libtool', 'autotools', re.compile(r'This file is part of GNU Libtool')),
    # The libtool-paste header: marks a file (often a maintainer-written ``acinclude.m4``
    # into which ``libtool.m4`` was historically pasted) on the strength of what its
    # CONTENT claims -- pasted libtool.m4 self-identifies with this header line. Measured
    # 11 regions corpus-wide, 3 new (gatos's two ``acinclude.m4`` regions, smpeg), zero
    # false positives (findings doc, "acinclude.m4 -- the deliberate-absence question").
    ('libtool', 'autotools',
     re.compile(r'libtool\.m4 - Configure libtool for the (?:target|host) system')),
    # The modern machine-readable convention, generator-agnostic: its own family.
    ('@generated', 'marker', re.compile(r'@generated\b')),
)

# ``config.guess``/``config.sub`` carry no generator banner, only a datestamp of the
# revision they were copied from.  On a NAME-MATCHED file of exactly those two names it
# is version evidence -- it never adds the ``banner`` signal and never marks anything.
_TIMESTAMP_NAMES = ('config.guess', 'config.sub')
_TIMESTAMP_RE = re.compile(r"timestamp='([0-9]{4}-[0-9]{2}-[0-9]{2})'")

# CANDIDATE ONLY -- measured, never marking.  The generic do-not-edit family is too
# broad to trust unmeasured (it appears in hand-written files warning against editing a
# *different* file), so phase 1 reports its hits separately via
# ``candidate_banner_hits`` and the findings decide whether it earns a family.
# ``scan`` must never consult these.
CANDIDATE_BANNERS: tuple[re.Pattern, ...] = (
    re.compile(r'DO NOT EDIT'),
    re.compile(r'[Dd]o not edit this file'),
    re.compile(r'[Aa]utomatically generated'),
)

# The translations family (phase 5, promoted after the corpus measurement).  Translation
# catalogues are tool-managed serialisations whose bulk drowns a patch's hand-written
# residue (the motivating case: acetoneiso's ``translate.patch``, fingerprint
# ``6b51f47b...`` -- 36 Qt catalogues, ~58k added lines, a ~70-line ``.pro``/``.qrc``
# residue).  The extension alone is NOT evidence and never marks: measured over the
# corpus, ``.ts`` is 45% Qt Linguist and 55% TypeScript (which
# ``content._CODE_EXTENSIONS`` already claims), so an extension match counts ONLY when a
# content corroborator also fires -- the ``_NAME_REQUIRES_BANNER`` posture generalised,
# with the corroborator names as the per-file signals.  The corroboration recall cost is
# 591 changed lines corpus-wide (0.03% of corroborated ``.po`` lines; phase-5 findings).
# ``family='translations'``, not "generated": ``.po`` content is human-AUTHORED
# (translators) even though tool-managed, and the family name must not overclaim.
TRANSLATION_FAMILY = 'translations'
TRANSLATION_EXTENSIONS = ('.po', '.pot', '.ts')

# The tool each extension's catalogue format belongs to -- the ``generator`` label a
# marked file carries (no version: catalogue formats do not stamp one).
TRANSLATION_GENERATORS = {'.po': 'gettext', '.pot': 'gettext', '.ts': 'qt-linguist'}

# Qt Linguist corroborators.  The DOCTYPE / root element is the strongest claim but only
# visible when a hunk touches the top of the file (the new-file and full-rewrite cases);
# the element corroborator requires two DISTINCT message-structure element kinds in the
# region, so an update hunk deep in the catalogue still corroborates while a TypeScript
# file mentioning one such token in a string does not.  The root-element pattern is
# anchored to (diff-prefixed) line start so a TypeScript generic like ``Promise<TS>``
# mid-line never fires it.
_TS_DOCTYPE_RE = re.compile(r'<!DOCTYPE TS>')
_TS_ROOT_RE = re.compile(r'^[+\- ]?\s*<TS(?:\s[^>]*)?>\s*$')
_TS_ELEMENT_RES = (
    re.compile(r'<message[ >]'),
    re.compile(r'<source>'),
    re.compile(r'<translation[ >]'),
)

# gettext corroborators.  ``msgid``/``msgstr`` sit at column 0 in a catalogue, so both
# patterns anchor to the one-character diff prefix (an obsolete ``#~ msgid`` line does
# not corroborate); the pair must BOTH appear.  The header corroborator fires on the
# catalogue's own ``Project-Id-Version`` header string.
_PO_MSGID_RE = re.compile(r'^[+\- ]?msgid "')
_PO_MSGSTR_RE = re.compile(r'^[+\- ]?msgstr(?:\[[0-9]+\])? "')
_PO_HEADER_RE = re.compile(r'"Project-Id-Version:')


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GeneratedFile:
    """One touched file that claims to be generator output."""

    path: str
    """Path as ``content.profile`` reports it (``a/``/``b/`` prefix and timestamp gone)."""

    family: str
    """Generator family: ``'autotools'``, ``'translations'`` (v2), or ``'marker'`` for a
    bare ``@generated``.

    A name or corroborated-extension match settles the family; a banner-only match uses
    the banner's family.
    """

    signals: tuple[str, ...]
    """Sorted claims that fired: ``'name'``/``'banner'``, or for the translations family
    the corroborator names (``'ts-doctype'``, ``'ts-elements'``, ``'po-msgid-msgstr'``,
    ``'po-header'``) -- the family-honest vocabulary, never a bare extension match."""

    generator: str | None
    """Generator named by the first banner (``'autoconf'``, ``'automake'`` ...), the
    catalogue tool for a translations-family file (``'qt-linguist'``, ``'gettext'``),
    else None.

    For a name-matched ``config.guess``/``config.sub`` with no banner, the basename, whose
    datestamp is the only version evidence those files carry.
    """

    version: str | None
    """Version captured from the banner (``'2.59'``), a datestamp for the two
    timestamped names, or None when the format carries none."""

    added: int
    """``+`` lines in this file."""

    removed: int
    """``-`` lines in this file."""


@dataclass(frozen=True)
class ProjectedDiff:
    """A diff body reordered residue-first, plus the omission facts evidence records."""

    text: str
    """The text to hand the model: preamble, residue segments verbatim, then the notes.

    Byte-identical to the input when ``projected`` is False.
    """

    omitted_files: int
    """Marked files replaced by a note.  0 when nothing was projected."""

    omitted_changed: int
    """Changed lines in those files -- what the model is being told it cannot see."""

    projected: bool
    """False means the input was returned untouched; the caller records nothing."""


@dataclass(frozen=True)
class ConstructTally:
    """Dangerous-construct hits in ONE body, split into marked files and the residue.

    Recomputed at display time from the body (see :func:`construct_tally`); it is NOT the
    ledger's count of ``dangerous-construct`` observations and must never be shown as one.
    """

    total: int
    """Hits across every code-typed file in the body -- marked and residue together."""

    in_marked: int
    """Hits in files the mark says claim to be generator output."""

    in_residue: int
    """Hits in the hand-written residue.  Non-zero is the attention-worthy case.

    Invariant: ``in_marked + in_residue == total``.
    """

    residue_hits: tuple[tuple[str, str], ...]
    """Distinct ``(path, detail)`` residue hits, in first-hit order -- WHERE the loud
    case is and WHICH pattern fired, for the display that calls it out.  Distinct
    rather than one entry per hit: forty ``shell-out`` lines in one file are one fact
    about that file, and ``in_residue`` already says how many there are."""


@dataclass(frozen=True)
class GeneratedScan:
    """The whole-patch result: marked files plus the arithmetic routing consumes."""

    files: tuple[GeneratedFile, ...]
    """Only the MARKED files, in diff order.  Empty when nothing claimed generation."""

    generated_changed: int
    """Changed (``+`` plus ``-``) lines in marked files."""

    total_changed: int
    """Changed lines across every touched file."""

    residue_changed: int
    """Changed lines in UNMARKED files -- the hand-written residue phase 3 routes on.

    Invariant: ``residue_changed + generated_changed == total_changed``.
    """

    coverage: float
    """``generated_changed / total_changed``, or 0.0 when nothing changed."""

    rule_version: int = GENERATED_RULES_VERSION
    """The ``GENERATED_RULES_VERSION`` that produced this scan."""


# ---------------------------------------------------------------------------
# The name signal
# ---------------------------------------------------------------------------

def strip_backup_suffixes(basename: str) -> str:
    """``basename`` with any run of trailing backup suffixes removed.

    Repeats until nothing more strips, so ``Makefile.in.orig~`` reduces to
    ``Makefile.in``.  A name that is ONLY a suffix (``~``, ``.bak``) is left alone --
    there is no underlying file to classify it as.
    """
    while True:
        if len(basename) > 1 and basename.endswith(_BACKUP_TILDE):
            basename = basename[:-1]
            continue
        for suffix in _BACKUP_SUFFIXES:
            if basename.endswith(suffix) and len(basename) > len(suffix):
                basename = basename[:-len(suffix)]
                break
        else:
            return basename


def name_family(path: str) -> str | None:
    """The generator family claimed by ``path``'s basename, or None.

    Exact basename equality at any path depth, after backup-suffix stripping:
    ``m4/libtool.m4`` and a subdirectory ``configure`` claim as loudly as top-level ones,
    while ``preconfigure`` and ``configure.ac`` claim nothing.
    """
    return GENERATED_NAME_FAMILY.get(strip_backup_suffixes(content._basename(path)))


# ---------------------------------------------------------------------------
# Diff region walk
# ---------------------------------------------------------------------------

def _region_lines(text: str) -> list[list[str]]:
    """Every line of each file's diff region, in ``content._parse_sections`` order.

    The section parser discards context lines, and a banner is usually a context line, so
    this walk mirrors its header handling exactly -- same diff start, same decoration
    skipping, one region opened per ``+++`` header -- and keeps everything else verbatim
    (change prefixes included; no pattern here is anchored).  Region N therefore belongs
    to section N.  The ``/dev/null`` deletion case needs no mirroring: it only chooses
    which path names a section, and paths come from ``_parse_sections``.
    """
    lines = fp._split_lines(text)
    regions: list[list[str]] = []
    current: list[str] | None = None

    index = fp._diff_start(lines)
    while index < len(lines):
        raw = lines[index].rstrip()
        index += 1

        if any(raw.startswith(prefix) for prefix in fp._DECORATION_PREFIXES):
            continue

        if raw.startswith('--- '):
            continue

        if raw.startswith('+++ '):
            current = []
            regions.append(current)
            continue

        if current is None:
            continue

        current.append(raw)

    return regions


def _is_segment_preamble(line: str) -> bool:
    """True for a line that belongs to the FOLLOWING file's segment, not the previous one.

    The ``--- `` source header and the git/quilt decoration lines (``diff --git``,
    ``index`` ...) all arrive before the ``+++`` header that opens a section, so a
    verbatim segment must start at the first of them, not at the ``+++``.
    """
    raw = line.rstrip()
    return raw.startswith('--- ') or any(raw.startswith(prefix) for prefix in fp._DECORATION_PREFIXES)


def _file_segments(text: str) -> list[str]:
    """The VERBATIM source text of each file's segment, in ``content._parse_sections`` order.

    The projection's segmentation must never disagree with ``scan``'s, so this walk is
    driven by exactly the unit the section parser is driven by -- one segment opened per
    ``+++`` header, from the same ``fp._diff_start`` -- and segment N therefore belongs to
    section N, the same correspondence ``_region_lines`` maintains.  What differs is
    fidelity: ``_region_lines`` yields ``rstrip``-ed content lines for pattern matching,
    whereas the projection hands its output to a model and must reproduce the residue byte
    for byte.  So the decisions are taken on the ``rstrip``-ed lines (identical decisions)
    while the text returned comes from ``splitlines(keepends=True)``, which is
    index-aligned with ``fp._split_lines`` and preserves the original line endings and any
    missing final newline.

    A segment runs from its own leading decoration/``--- `` preamble (see
    ``_is_segment_preamble``) to the start of the next segment's, so joining every segment
    reproduces the diff from its start.  Anything BEFORE the first segment -- a DEP-3 /
    free-text header, or a headerless ``@@`` fragment -- belongs to no file and is not
    returned; see ``project_residue_first`` for what that means for its callers.
    """
    lines = fp._split_lines(text)
    raw_lines = text.splitlines(keepends=True)
    start = fp._diff_start(lines)

    heads = [index for index in range(start, len(lines)) if lines[index].rstrip().startswith('+++ ')]

    begins: list[int] = []
    for position, head in enumerate(heads):
        # Never walk back past the previous segment's own ``+++`` header: whatever sits
        # between two headers that does not look like a preamble stays with the earlier
        # file, which is where the section parser counted it.
        floor = start if position == 0 else heads[position - 1] + 1
        begin = head
        while begin > floor and _is_segment_preamble(lines[begin - 1]):
            begin -= 1
        begins.append(begin)

    segments: list[str] = []
    for position, begin in enumerate(begins):
        end = begins[position + 1] if position + 1 < len(begins) else len(lines)
        segments.append(''.join(raw_lines[begin:end]))
    return segments


# ---------------------------------------------------------------------------
# The banner signal
# ---------------------------------------------------------------------------

def _banner_hit(lines: list[str]) -> tuple[str, str, str | None] | None:
    """First ``(generator, family, version)`` claimed in ``lines``, or None.

    One file gets one generator, never an accumulation of every banner present. Lines
    are grouped by their FIRST character -- ``'+'`` added, ``'-'`` removed, anything else
    (context, including ``''`` and ``'@@'`` hunk-header lines) context -- and the winner
    is the first hit among added lines; if none, the first hit among context lines; if
    none, the first hit among removed lines.  Within a group, line order and pattern
    order are as written (unchanged from before): only the group *preference* is new.

    This matters because the captured version is evidence about the file AS THE PATCH
    LEAVES IT, not as it was before the patch.  Measured over the corpus, 121 of 203
    banner regions previously reported a removed-line (pre-patch) version, and 59 of 61
    multi-banner regions did the same (findings doc, "Multi-banner report") -- a
    regeneration that bumps a generator's version was systematically dated to the OLD
    version under first-hit-in-line-order.  Preferring added lines, then context, then
    removed fixes that without losing anything: a file whose only banner sits on a
    removed line still reports it.  Patterns still match the raw line, prefix included --
    they are unanchored, as before; only this grouping consults the prefix.
    """
    added: list[str] = []
    context: list[str] = []
    removed: list[str] = []
    for line in lines:
        if line.startswith('+'):
            added.append(line)
        elif line.startswith('-'):
            removed.append(line)
        else:
            context.append(line)

    for group in (added, context, removed):
        for line in group:
            for generator, family, pattern in BANNERS:
                match = pattern.search(line)
                if match:
                    version = match.group(1) if pattern.groups else None
                    return (generator, family, version)
    return None


def _timestamp_version(lines: list[str]) -> str | None:
    """The first ``timestamp='YYYY-MM-DD'`` datestamp in ``lines``, or None."""
    for line in lines:
        match = _TIMESTAMP_RE.search(line)
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan(text: str) -> GeneratedScan:
    """Mark the files in a diff body that claim to be generator output.

    Pure: no I/O, no network.  ``text`` may include a DEP-3 / free-text header; it is
    skipped via ``fingerprint``'s diff-start detection, so file segmentation and the
    added/removed counts match ``content.profile`` exactly.

    A file is marked when its name is in the generated set, a generator banner is
    visible anywhere in its hunks, or (v2) a translation-catalogue extension is
    corroborated by catalogue structure in its hunks -- reported per file as separate
    signals.  The result is an observation, never a verdict: see the module docstring.
    """
    sections = content._parse_sections(text)
    regions = _region_lines(text)

    marked: list[GeneratedFile] = []
    generated_changed = 0
    total_changed = 0

    for index, section in enumerate(sections):
        changed = len(section.added) + len(section.removed)
        total_changed += changed
        lines = regions[index] if index < len(regions) else []

        basename = strip_backup_suffixes(content._basename(section.path))
        family = GENERATED_NAME_FAMILY.get(basename)
        requires_banner = family is not None and basename in _NAME_REQUIRES_BANNER

        generator: str | None = None
        version: str | None = None
        banner = _banner_hit(lines)

        if requires_banner:
            # Corroboration tier: the name signal counts ONLY alongside a banner hit.
            # With no banner the file carries no signals at all -- not even 'name' alone
            # -- so it is not marked (see ``_NAME_REQUIRES_BANNER``); ``family`` is moot
            # in that case, since the empty ``signals`` below skips this file entirely.
            if banner is None:
                signals: list[str] = []
            else:
                generator, banner_family, version = banner
                family = family or banner_family
                signals = [NAME_SIGNAL, BANNER_SIGNAL]
        else:
            signals = [NAME_SIGNAL] if family else []
            # The translations family: an extension match counts ONLY alongside content
            # corroboration (the ``.ts``/TypeScript collision), and the corroborator
            # names ARE the file's signals.  No version to capture; the generator label
            # is the catalogue format's tool.
            extension = content._extension(basename)
            if extension in TRANSLATION_EXTENSIONS:
                corroborations = _translation_corroborations(extension, lines)
                if corroborations:
                    signals.extend(corroborations)
                    family = family or TRANSLATION_FAMILY
                    generator = TRANSLATION_GENERATORS[extension]
            if banner is not None:
                banner_generator, banner_family, banner_version = banner
                signals.append(BANNER_SIGNAL)
                # A name or corroborated-extension match settles the family; a
                # banner-only match uses the banner's.  A corroborated catalogue keeps
                # its tool as ``generator`` -- the banner still shows in signals.
                family = family or banner_family
                if generator is None:
                    generator, version = banner_generator, banner_version

        # Version evidence for the two datestamped names, on a name match only, and
        # only where no banner already identified a generator.
        if NAME_SIGNAL in signals and generator is None and basename in _TIMESTAMP_NAMES:
            stamp = _timestamp_version(lines)
            if stamp is not None:
                generator, version = basename, stamp

        if not signals:
            continue

        generated_changed += changed
        marked.append(GeneratedFile(
            path=section.path,
            family=family,
            signals=tuple(sorted(signals)),
            generator=generator,
            version=version,
            added=len(section.added),
            removed=len(section.removed),
        ))

    return GeneratedScan(
        files=tuple(marked),
        generated_changed=generated_changed,
        total_changed=total_changed,
        residue_changed=total_changed - generated_changed,
        coverage=generated_changed / total_changed if total_changed else 0.0,
        rule_version=GENERATED_RULES_VERSION,
    )


def candidate_banner_hits(text: str) -> list[tuple[str, str]]:
    """``(path, snippet)`` for each file whose region matches a CANDIDATE banner.

    Measurement only: ``scan`` never consults ``CANDIDATE_BANNERS``, so nothing here can
    mark a file.  One entry per file (the first matching line, trimmed), which is what
    the phase-1 report needs -- how often the generic do-not-edit family fires, and on
    what basenames.
    """
    sections = content._parse_sections(text)
    regions = _region_lines(text)

    hits: list[tuple[str, str]] = []
    for index, section in enumerate(sections):
        for line in (regions[index] if index < len(regions) else []):
            if any(pattern.search(line) for pattern in CANDIDATE_BANNERS):
                hits.append((section.path, line.strip()[:120]))
                break
    return hits


@dataclass(frozen=True)
class TranslationCandidate:
    """One touched file whose extension makes it a translation-catalogue candidate.

    The measurement view of the translations family: unlike ``scan`` -- which marks only
    corroborated matches -- every extension match is reported.  ``corroborations`` is
    empty when the extension matched but no content corroborator fired; for ``.ts`` that
    population is TypeScript (measured 55% of corpus matches), and sizing it is the point
    of reporting it.
    """

    path: str
    """Path as ``content.profile`` reports it."""

    extension: str
    """The matching extension, backup suffixes stripped first (``'.ts'``, ``'.po'``...)."""

    corroborations: tuple[str, ...]
    """Sorted content corroborators that fired (``'ts-doctype'``, ``'ts-elements'``,
    ``'po-msgid-msgstr'``, ``'po-header'``); empty means extension-only, never trusted."""

    added: int
    """``+`` lines in this file."""

    removed: int
    """``-`` lines in this file."""


def _translation_corroborations(extension: str, lines: list[str]) -> list[str]:
    """The content corroborators that fire in one candidate file's region."""
    fired = []
    if extension == '.ts':
        if any(_TS_DOCTYPE_RE.search(line) or _TS_ROOT_RE.match(line) for line in lines):
            fired.append('ts-doctype')
        distinct = sum(
            1 for pattern in _TS_ELEMENT_RES if any(pattern.search(line) for line in lines))
        if distinct >= 2:
            fired.append('ts-elements')
    else:
        if (any(_PO_MSGID_RE.match(line) for line in lines)
                and any(_PO_MSGSTR_RE.match(line) for line in lines)):
            fired.append('po-msgid-msgstr')
        if any(_PO_HEADER_RE.search(line) for line in lines):
            fired.append('po-header')
    return fired


def candidate_translation_hits(text: str) -> list[TranslationCandidate]:
    """One record per touched file with a translation-catalogue extension.

    Measurement only, but over the SAME tables ``scan`` marks with: a record whose
    ``corroborations`` is non-empty is exactly a file ``scan`` marks ``translations``,
    and an empty one is exactly a file it skips.  Every extension match is returned --
    corroborated or not -- so the measurement tool can keep sizing the uncorroborated
    ``.ts`` (TypeScript) population and the corroboration recall cost.
    """
    sections = content._parse_sections(text)
    regions = _region_lines(text)

    hits: list[TranslationCandidate] = []
    for index, section in enumerate(sections):
        basename = strip_backup_suffixes(content._basename(section.path))
        extension = content._extension(basename)
        if extension not in TRANSLATION_EXTENSIONS:
            continue
        lines = regions[index] if index < len(regions) else []
        hits.append(TranslationCandidate(
            path=section.path,
            extension=extension,
            corroborations=tuple(sorted(_translation_corroborations(extension, lines))),
            added=len(section.added),
            removed=len(section.removed)))
    return hits


# ---------------------------------------------------------------------------
# Residue-first projection
# ---------------------------------------------------------------------------

def _entry_changed(entry: dict) -> int:
    """``added + removed`` for one evidence file entry, tolerating a malformed row.

    Same posture as ``generated_marks``: the ledger is append-only operator data, so a
    count that is missing or not a number reads as 0 rather than raising in the middle of
    building a model's input.
    """
    total = 0
    for key in ('added', 'removed'):
        try:
            total += int(entry.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _note_for(entry: dict, changed: int) -> str:
    """The one-line note standing in for a marked file: what the model is NOT being shown.

    Loud and specific -- path, size, which signals fired, and the generator/version where
    the banner captured one -- because the whole posture of the mark is that an omission
    is announced, never silent.  The generator clause is dropped when the file carries no
    version evidence (a name-only match), and the version when the banner format carries
    none.
    """
    note = '[generated: %s — %d changed lines, signals %s' % (
        entry.get('path'), changed, '+'.join(entry.get('signals') or ()))
    generator = entry.get('generator')
    if generator:
        note += ', %s' % generator
        if entry.get('version'):
            note += ' %s' % entry['version']
    return note + ']'


def project_residue_first(body: str, files: list[dict]) -> ProjectedDiff:
    """Reorder ``body`` so the hand-written residue comes first and the marked files become notes.

    ``body`` is the DIFF BODY -- the callers (triage, the risk gate) already strip any
    DEP-3 / free-text header via ``triage.diff_body``.  Projection starts at the first
    file header regardless, so a body that still carries a header simply loses it; nothing
    before the first ``+++`` unit is projected, because it belongs to no file.

    ``files`` is the mark's per-file evidence list -- the ``files`` value ``generated_marks``
    returns, i.e. what ``evidence_for`` wrote.  Marked and residue files are told apart by
    matching those ``path`` values against the section paths ``content._parse_sections``
    derives, and the segments come from ``_file_segments``, which walks the same ``+++``
    units, so a file can never be counted as generated by the mark and shown as residue
    here (or vice versa).

    The result is: a one-line preamble naming what is missing; every UNMARKED file's
    segment verbatim and in diff order (decoration lines included -- byte-identical text,
    so an unmarked file reads exactly as it did before this phase); then one note per
    marked file, in diff order.  The existing character cap applies AFTER this, so the cap
    now spends its budget on residue rather than on generator output.

    IDENTITY when there is nothing to project -- an empty ``files``, or a mark whose paths
    do not appear in this body at all: the input string is returned unchanged with
    ``projected=False`` and zero counts, so callers can project unconditionally without
    special-casing the unmarked fingerprints (which are the overwhelming majority).

    Pure: no I/O, no network.
    """
    sections = content._parse_sections(body)
    marked_paths = {entry.get('path') for entry in files}
    marked_indexes = {index for index, section in enumerate(sections) if section.path in marked_paths}
    if not files or not marked_indexes:
        return ProjectedDiff(text=body, omitted_files=0, omitted_changed=0, projected=False)

    # First evidence entry per path: the notes are keyed by the path the section carries.
    entry_by_path: dict[str, dict] = {}
    for entry in files:
        entry_by_path.setdefault(entry.get('path'), entry)

    segments = _file_segments(body)
    total_changed = sum(len(section.added) + len(section.removed) for section in sections)

    residue: list[str] = []
    notes: list[str] = []
    omitted_changed = 0
    for index, section in enumerate(sections):
        segment = segments[index] if index < len(segments) else ''
        if index not in marked_indexes:
            residue.append(segment)
            continue
        entry = entry_by_path[section.path]
        changed = _entry_changed(entry)
        omitted_changed += changed
        notes.append(_note_for(entry, changed))

    parts = ['%d generated-claiming files not shown (%d of %d changed lines); '
             'hand-written residue follows.\n' % (len(notes), omitted_changed, total_changed)]
    residue_text = ''.join(residue)
    if residue_text:
        parts.append(residue_text)
        # A diff whose last line has no terminator must not run into the first note.
        if not residue_text.endswith('\n'):
            parts.append('\n')
    parts.extend('%s\n' % note for note in notes)

    return ProjectedDiff(text=''.join(parts), omitted_files=len(notes),
                         omitted_changed=omitted_changed, projected=True)


# ---------------------------------------------------------------------------
# The ledger observation
# ---------------------------------------------------------------------------

def _dominant_family(scan: GeneratedScan) -> str:
    """The family accounting for the most generated changed lines in ``scan``.

    Ties are broken ALPHABETICALLY, not by diff order: the detail string is compared
    against the live observation to decide whether a re-record is a no-op, so it must be
    a pure function of the scan's content and not of an ordering that a cosmetic diff
    reshuffle could change.  ``ValueError`` on a scan that marked nothing -- there is no
    dominant family of no files, and the recorder never asks (an unmarked scan records no
    observation at all).
    """
    if not scan.files:
        raise ValueError('no marked files: an unmarked scan has no dominant family')

    totals: dict[str, int] = {}
    for entry in scan.files:
        totals[entry.family] = totals.get(entry.family, 0) + entry.added + entry.removed
    return min(totals, key=lambda family: (-totals[family], family))


def detail_for(scan: GeneratedScan) -> str:
    """The observation ``detail`` for ``scan``: ``'<family>/<percent>'``.

    The dominant family (most generated changed lines, ties broken alphabetically) plus
    the coverage as a rounded
    integer percent -- ``'autotools/99'`` for gatos.  Compact enough for a worklist badge
    and stable per fingerprint at a fixed rule version, which is what lets the recorder
    skip an unchanged re-record.  ``ValueError`` on an empty scan.
    """
    return '%s/%d' % (_dominant_family(scan), round(scan.coverage * 100))


def evidence_for(scan: GeneratedScan) -> str:
    """Canonical JSON evidence for a ``generated-content`` observation.

    The per-file breakdown in DIFF order (``path``, ``family``, ``signals`` as a list,
    ``generator``, ``version``, ``added``, ``removed``) plus the arithmetic later phases
    route on: ``generated_changed``, ``residue_changed``, ``total_changed``.

    ``generator`` and ``version`` are always present, explicitly ``null`` where the file
    carries no version evidence, rather than omitted: a uniform schema means a consumer
    reads ``entry['version']`` unconditionally and a human diffing two evidence blobs
    sees a value change, not a key appearing.

    Stable: two calls on the same scan produce byte-identical output (``sort_keys``, and
    the file list carries no set or dict iteration), which the recorder's idempotency
    skip depends on.
    """
    return json.dumps(
        {'files': mark_files_for(scan),
         'generated_changed': scan.generated_changed,
         'residue_changed': scan.residue_changed,
         'total_changed': scan.total_changed},
        sort_keys=True)


def mark_files_for(scan: GeneratedScan) -> list[dict]:
    """The mark's per-file evidence list, in diff order.

    Exactly what :func:`evidence_for` writes and what ``generated_marks`` hands back on
    the read side, built directly from a fresh scan -- so a caller holding a scan (the
    recorder) can drive :func:`project_residue_first` without a JSON round-trip through
    the ledger, and there is exactly one definition of the shape both sides use.
    """
    return [
        {'path': entry.path,
         'family': entry.family,
         'signals': list(entry.signals),
         'generator': entry.generator,
         'version': entry.version,
         'added': entry.added,
         'removed': entry.removed}
        for entry in scan.files]


def generated_marks(conn) -> dict[str, dict]:
    """``{fingerprint: record}`` from the live ``generated-content`` observations.

    The consumer side of the mark: each record carries the observation's ``detail`` plus
    the parsed evidence (``files``, ``generated_changed``, ``residue_changed``,
    ``total_changed``) -- the residue arithmetic the routing reads and the per-file claims
    the review UI badges.  A fingerprint with no live observation is absent, which is the
    common case: nothing claimed generation.

    A row whose evidence is missing or not parseable JSON of the expected shape is SKIPPED
    rather than raised on.  The ledger is append-only operator data that outlives any one
    version of this module (an old row, a hand-edited import, a future schema); one bad
    row must degrade to one missing mark, not brick every consumer of the axis.
    """
    from divergulent.classify import ledger as ledger_mod  # lazy: keep this module import-light
    marks: dict[str, dict] = {}
    for obs in ledger_mod.live_observations(conn):
        if obs['kind'] != GENERATED_KIND:
            continue
        try:
            payload = json.loads(obs['evidence'])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get('files'), list):
            continue
        try:
            marks[obs['fingerprint']] = {
                'detail': obs['detail'],
                'files': payload['files'],
                'generated_changed': int(payload['generated_changed']),
                'residue_changed': int(payload['residue_changed']),
                'total_changed': int(payload['total_changed'])}
        except (KeyError, TypeError, ValueError):
            continue
    return marks


def residue_unlocked_fingerprints(conn) -> set[str]:
    """Oversized fingerprints whose hand-written residue is small enough to review after all.

    The routing composition the triage driver and the risk gate BOTH subtract from their
    ``oversized`` skip set, defined once here so the two consumers can never disagree
    about who is unlocked.  It composes two live observations without changing either:
    reviewability keeps measuring the WHOLE diff (a 47k-line patch is structurally
    oversized and its observation says so), while the mark's ``residue_changed`` says how
    much of that a human or a model would actually be asked to read.  A fingerprint is
    unlocked when it is observed ``oversized`` AND carries a live mark whose
    ``residue_changed`` is at or under ``reviewability.REVIEWABILITY_OVERSIZED_LINES`` --
    the same cut, applied to the residue instead of the total.

    An oversized fingerprint with no mark stays locked, as does one whose evidence is
    malformed (``generated_marks`` already drops those, so they simply never appear).  A
    ``large`` or ``normal`` fingerprint is never in this set: it was never locked, and
    nothing here unlocks what was already open.
    """
    from divergulent.classify import reviewability as reviewability_mod  # lazy: import-light
    marks = generated_marks(conn)
    return {digest for digest in reviewability_mod.oversized_fingerprints(conn)
            if digest in marks
            and marks[digest]['residue_changed'] <= reviewability_mod.REVIEWABILITY_OVERSIZED_LINES}


# ---------------------------------------------------------------------------
# Construct-vs-residue tally (render-time display)
# ---------------------------------------------------------------------------

def construct_tally(body: str, marked_paths: frozenset[str]) -> ConstructTally:
    """Split ``body``'s dangerous-construct hits into marked files and the residue.

    The number a reviewer of a gatos-shaped patch actually needs: 128 ``shell-out`` hits
    are reassuring when every one of them is in a file that claims to be generated
    ``configure``/``ltmain.sh`` shell, and are the whole point of the review when one of
    them is in the hand-written residue.  ``marked_paths`` is the mark's path set (the
    review context's ``generated_paths``); a path outside it is residue, which is the
    honest default -- an empty set tallies everything as residue.

    The hits come from ``rules.scan_dangerous_constructs_by_file``, i.e. THE recorded
    scan's own pattern tables and its own code-vs-prose gate, reached through the live
    module so a pattern added there flows into this tally with no second table to keep in
    step.  A construct in a doc file (or a build file such as ``configure``) counts
    nowhere here, exactly as it flagged nowhere when the observations were recorded.

    ADVISORY DISPLAY ONLY.  Nothing here reads, rewrites or re-attributes the ledger's
    ``dangerous-construct`` observations; those are recorded evidence and stay as
    recorded.  ``total`` may therefore differ from a fingerprint's observation count --
    the rows were recorded from the representative body under whatever rule version was
    then current -- so the display says the tally is computed from THIS body and never
    claims to be the ledger's number.

    Pure: no I/O, no network.
    """
    total = in_marked = 0
    residue_hits: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path, flag in rules_mod.scan_dangerous_constructs_by_file(body):
        total += 1
        if path in marked_paths:
            in_marked += 1
            continue
        key = (path, flag.detail)
        if key not in seen:
            seen.add(key)
            residue_hits.append(key)
    return ConstructTally(total=total, in_marked=in_marked, in_residue=total - in_marked,
                          residue_hits=tuple(residue_hits))
