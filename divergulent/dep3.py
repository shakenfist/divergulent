'''Parse and classify DEP-3 patch headers.

DEP-3 (https://dep-team.pages.debian.net/deps/dep3/) tags a patch with
RFC-2822-style metadata at the top of the file. We use it to classify each
carried patch as forwarded upstream, Debian-only, or unknown.

The classification is deliberately honest: a patch with no DEP-3 evidence is
UNKNOWN, never assumed divergent. This departs from DEP-3's implicit "not
forwarded" default, because the project's job is to surface divergence it can
actually justify, not to cry wolf.
'''
from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class PatchClass(enum.Enum):
    FORWARDED = 'forwarded'
    DEBIAN_ONLY = 'debian-only'
    UNKNOWN = 'unknown'


# Lines that mark the start of patch content (and so the end of any header).
_DIFF_MARKERS = ('--- ', '+++ ', 'diff ', 'Index:', '@@ ', 'rename ', 'GIT binary patch')


def parse_header(text: str) -> dict[str, str]:
    '''Parse the RFC-2822-style DEP-3 header at the top of a patch.

    The header ends at the first blank line, a line that is exactly ``---``, or
    the start of the diff. Field names are lower-cased; continuation lines
    (leading whitespace) are folded into the preceding field.
    '''
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == '' or stripped == '---' or line.startswith(_DIFF_MARKERS):
            break
        if line[:1] in (' ', '\t') and current_key is not None:
            fields[current_key] = (fields[current_key] + ' ' + stripped).strip()
            continue
        if ':' in line:
            key, _, value = line.partition(':')
            current_key = key.strip().lower()
            fields[current_key] = value.strip()
        else:
            # A non-field line in the header area (e.g. git "From <hash>" or
            # free text); it does not continue a field.
            current_key = None
    return fields


# The old dpatch convention prefixes its description lines with "# DP:"
# ("Debian Patch"); its presence marks a Debian-authored patch.
_DP_MARKER = re.compile(r'(?mi)^\s*#+\s*DP:')
# Patch filenames that conventionally denote Debian-authored changes, including
# the auto-generated debian-changes patch from 3.0 (quilt).
_DEBIAN_NAME_PREFIXES = ('deb-', 'debian-')


def _classify_dep3(text: str) -> PatchClass:
    '''Classify a patch purely from explicit DEP-3 metadata.'''
    fields = parse_header(text)
    if not fields:
        return PatchClass.UNKNOWN

    # Definitive: the change is already in upstream.
    if 'applied-upstream' in fields:
        return PatchClass.FORWARDED

    forwarded = fields.get('forwarded')
    if forwarded is not None:
        # "no", "not-needed" (and non-standard "not yet"/"not needed") all mean
        # the patch is not headed upstream; anything else ("yes", a URL) means
        # it is.
        return PatchClass.DEBIAN_ONLY if forwarded.strip().lower().startswith('no') else PatchClass.FORWARDED

    origin_category = fields.get('origin', '').split(',', 1)[0].strip().lower()
    if origin_category in ('upstream', 'backport'):
        return PatchClass.FORWARDED
    if origin_category == 'vendor':
        return PatchClass.DEBIAN_ONLY

    # DEP-3: Forwarded absent but a Bug reference present implies it was sent.
    if any(key == 'bug' or key.startswith('bug-') for key in fields):
        return PatchClass.FORWARDED

    return PatchClass.UNKNOWN


def _header_block(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if line.strip() == '---' or line.startswith(_DIFF_MARKERS):
            break
        lines.append(line)
    return '\n'.join(lines)


def _looks_debian_authored(text: str, name: str | None) -> bool:
    '''Heuristic Debian-authored signals, used only when DEP-3 is silent.'''
    if _DP_MARKER.search(_header_block(text)):
        return True
    if name:
        base = name.rsplit('/', 1)[-1]
        if base.startswith(_DEBIAN_NAME_PREFIXES):
            return True
    return False


def classify(text: str, name: str | None = None) -> PatchClass:
    '''Classify a patch as FORWARDED, DEBIAN_ONLY, or UNKNOWN.

    Explicit DEP-3 metadata wins. When DEP-3 is silent (which is common, as many
    Debian patches predate or omit it), the old "# DP:" comment convention and
    deb-*/debian-* patch filenames mark a patch as Debian-only; anything else
    remains UNKNOWN rather than being assumed divergent.
    '''
    explicit = _classify_dep3(text)
    if explicit is not PatchClass.UNKNOWN:
        return explicit
    if _looks_debian_authored(text, name):
        return PatchClass.DEBIAN_ONLY
    return PatchClass.UNKNOWN


@dataclass(frozen=True)
class BugRef:
    tracker: str  # 'debian', 'ubuntu', 'upstream', ...
    ref: str      # a bug number, "#number", or a URL, as declared


def bug_references(text: str) -> list[BugRef]:
    '''Return the bug references a patch declares via DEP-3 Bug/Bug-<vendor> fields.

    ``Bug`` is the generic/upstream tracker; ``Bug-Debian`` etc. name a vendor.
    The raw value is returned as-is; linkifying (e.g. to bugs.debian.org) is the
    caller's job.
    '''
    refs = []
    for key, value in parse_header(text).items():
        if key == 'bug':
            refs.append(BugRef('upstream', value))
        elif key.startswith('bug-'):
            refs.append(BugRef(key[len('bug-'):], value))
    return refs
