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

Pure: no I/O, no network.  ``scan(text)`` is the whole public surface, plus
``candidate_banner_hits(text)`` which reports the unmeasured generic do-not-edit family
for the phase-1 measurement tool WITHOUT ever marking on it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from divergulent.classify import content
from divergulent.classify import fingerprint as fp

# Folds the name set + banner patterns into a version the phase-2 observation records;
# bumping it supersedes prior observations and re-scans, like every deterministic rule.
GENERATED_RULES_VERSION = 1

# The signals a file can carry.  Reported separately (and sorted) so evidence says which
# of the two independent claims fired.
NAME_SIGNAL = 'name'
BANNER_SIGNAL = 'banner'

# Well-known generator output basenames, keyed by family.  Only ``autotools`` exists in
# v1; the shape is a registry so gettext / cmake families can be added later without
# reshaping the scanner.  Matched by EXACT basename equality (never substring) at any
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


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GeneratedFile:
    """One touched file that claims to be generator output."""

    path: str
    """Path as ``content.profile`` reports it (``a/``/``b/`` prefix and timestamp gone)."""

    family: str
    """Generator family: ``'autotools'`` in v1, or ``'marker'`` for a bare ``@generated``.

    A name match settles the family; a banner-only match uses the banner's family.
    """

    signals: tuple[str, ...]
    """Sorted subset of ``('banner', 'name')`` -- which independent claims fired."""

    generator: str | None
    """Generator named by the first banner (``'autoconf'``, ``'automake'`` ...), else None.

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

    A file is marked when its name is in the generated set, or a generator banner is
    visible anywhere in its hunks, or both -- reported per file as separate signals.  The
    result is an observation, never a verdict: see the module docstring.
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
            if banner is not None:
                generator, banner_family, version = banner
                signals.append(BANNER_SIGNAL)
                # A name match settles the family; a banner-only match uses the banner's.
                family = family or banner_family

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
