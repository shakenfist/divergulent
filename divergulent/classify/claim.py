"""Author-controlled claim extraction from a patch's DEP-3 header.

Everything this module produces is a CLAIM — metadata written by the patch
author and never verified against the diff content.  The ``Claim`` dataclass
is deliberately named to make that provenance obvious at every call site.

Claim classification is independent of content classification (step 2b).
Disagreement between the two is the loudest signal in the pipeline; a patch
that claims to fix a typo but touches executable code deserves review.

The ``claimed_category`` enum is **provisional and versioned** (see
``CLAIM_RULE_VERSION``).  The keyword lists below are named module constants
so they are easy to tune as the real corpus teaches us more.

Curation-side only: no client command imports ``classify/``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from divergulent import dep3
from divergulent.dep3 import BugRef

# ---------------------------------------------------------------------------
# Version tag — phase-3 ledger keys on this to detect stale verdicts.
# ---------------------------------------------------------------------------

CLAIM_RULE_VERSION = 1

# ---------------------------------------------------------------------------
# CVE pattern (NIST canonical form).
# ---------------------------------------------------------------------------

_CVE_RE = re.compile(r'CVE-\d{4}-\d{4,}', re.IGNORECASE)

# ---------------------------------------------------------------------------
# Keyword lists — provisional, versioned with CLAIM_RULE_VERSION.
#
# Precedence (highest → lowest):
#   security > packaging > documentation > feature > bugfix > unknown
#
# Security wins because missing a claimed-security patch is worse than
# mis-bucketing a non-security one.  Packaging is next because a packaging
# keyword in any context (filename, subdir, description) is a strong signal
# that the patch is Debian-local.  Documentation beats feature/bugfix because
# doc-only patches are easy to confirm and low-risk to classify early.
# ---------------------------------------------------------------------------

_SECURITY_KEYWORDS = (
    'security',
    'vulnerab',
    'overflow',
    'exploit',
    'cve',
    'denial of service',
    'injection',
    'use-after-free',
    'heap corruption',
    'privilege escalation',
    'remote code execution',
    'arbitrary code',
    'out-of-bounds',
)

_PACKAGING_KEYWORDS = (
    'packaging',
    'quilt',
    '.pc',
    'debian/',
    'build system',
    'makefile',
    'autotools',
    'reproducib',
    'debhelper',
    'dpkg',
    'dh_',
)

_DOCUMENTATION_KEYWORDS = (
    'typo',
    'spelling',
    'man page',
    'manpage',
    'documentation',
    'docstring',
    'comment',
    'readme',
)

_FEATURE_KEYWORDS = (
    'add support',
    'feature',
    'implement',
    'new option',
    'enable',
    'add a',
)

_BUGFIX_KEYWORDS = (
    'fix',
    'crash',
    'segfault',
    'error',
    'bug',
    'incorrect',
    'regression',
    'workaround',
)

# Filename prefixes that indicate a Debian-authored packaging patch.
_PACKAGING_NAME_PREFIXES = ('deb-', 'debian-')

# Subdirectory path fragments that indicate packaging or documentation.
_PACKAGING_SUBDIR_FRAGMENTS = ('debian/',)
_DOCUMENTATION_SUBDIR_FRAGMENTS = ('doc/', 'docs/', 'documentation/')


# ---------------------------------------------------------------------------
# Claim dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Claim:
    """Author-controlled metadata extracted from a patch header.

    Every field is a CLAIM by the patch author; nothing here is verified
    against the diff body.  Use alongside ``ContentProfile`` (step 2b) and
    compare the two before acting on any field.
    """

    claimed_category: str
    """One of: security / packaging / documentation / feature / bugfix / unknown.

    Derived from the description text, the patch filename, and the
    ``debian/patches/`` subdirectory path.  Provisional; see
    ``CLAIM_RULE_VERSION``.
    """

    forwarded: str
    """The ``PatchClass`` value from ``dep3.classify``.

    Captures the author's claim about whether the change is headed upstream.
    One of ``'forwarded'``, ``'debian-only'``, or ``'unknown'``.
    """

    description: str | None
    """The ``Description`` header (falling back to ``Subject``) from the DEP-3
    header, or ``None`` when neither field is present.
    """

    bugs: list[BugRef]
    """Bug references declared in ``Bug``/``Bug-<vendor>`` DEP-3 fields."""

    cves: list[str]
    """CVE identifiers (``CVE-YYYY-NNNN+``) found anywhere in the header text.

    De-duplicated, upper-cased, in first-seen order.
    """

    rule_version: int
    """The ``CLAIM_RULE_VERSION`` that produced this ``Claim``.

    Phase-3 can compare this against the current version to decide whether to
    re-extract.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_header_text(text: str) -> str:
    """Return the header block (everything before the diff starts)."""
    lines = []
    diff_markers = ('--- ', '+++ ', 'diff ', 'Index:', '@@ ', 'rename ', 'GIT binary patch')
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == '---' or line.startswith(diff_markers):
            break
        lines.append(line)
    return '\n'.join(lines)


def _extract_cves(header_text: str) -> list[str]:
    """Return de-duplicated, upper-cased CVE ids in first-seen order."""
    seen: dict[str, None] = {}
    for m in _CVE_RE.finditer(header_text):
        cve = m.group(0).upper()
        seen[cve] = None
    return list(seen)


def _classify_category(
        description: str | None,
        name: str,
        cves: list[str],
) -> str:
    """Return the ``claimed_category`` from author-controlled signals.

    Precedence: security > packaging > documentation > feature > bugfix > unknown.

    All matching is case-insensitive and performed over the combined signal
    string (description + patch basename + subdir).
    """
    # Build a lower-cased composite of all author-controlled text signals.
    desc_lower = (description or '').lower()
    name_lower = name.lower()
    # Extract basename and any leading path components separately for
    # subdirectory matching.
    if '/' in name:
        subdir = name.rsplit('/', 1)[0].lower() + '/'
        basename = name.rsplit('/', 1)[1].lower()
    else:
        subdir = ''
        basename = name_lower

    combined = f'{desc_lower} {basename} {subdir}'

    # --- security ---
    # A CVE present in the header is the strongest security claim.
    if cves:
        return 'security'
    if any(kw in combined for kw in _SECURITY_KEYWORDS):
        return 'security'

    # --- packaging ---
    if basename.startswith(_PACKAGING_NAME_PREFIXES):
        return 'packaging'
    if any(frag in subdir for frag in _PACKAGING_SUBDIR_FRAGMENTS):
        return 'packaging'
    if any(kw in combined for kw in _PACKAGING_KEYWORDS):
        return 'packaging'

    # --- documentation ---
    if any(frag in subdir for frag in _DOCUMENTATION_SUBDIR_FRAGMENTS):
        return 'documentation'
    if any(kw in combined for kw in _DOCUMENTATION_KEYWORDS):
        return 'documentation'

    # --- feature ---
    if any(kw in combined for kw in _FEATURE_KEYWORDS):
        return 'feature'

    # --- bugfix ---
    if any(kw in combined for kw in _BUGFIX_KEYWORDS):
        return 'bugfix'

    return 'unknown'


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_claim(name: str, text: str) -> Claim:
    """Extract the author-controlled ``Claim`` from a patch.

    Args:
        name: The patch filename or path as it appears in ``debian/patches/``
              (e.g. ``security/fix-cve-2024-1234.patch`` or
              ``deb-configure.diff``).  Used for filename and subdir signals.
        text: The full patch text, including any DEP-3 header and the diff.

    Returns:
        A ``Claim`` dataclass.  Every field reflects what the author SAYS,
        not what the diff does.
    """
    header_text = _extract_header_text(text)
    fields = dep3.parse_header(text)

    description: str | None = fields.get('description') or fields.get('subject') or None

    bugs: list[BugRef] = dep3.bug_references(text)
    cves: list[str] = _extract_cves(header_text)

    forwarded: str = dep3.classify(text, name).value

    claimed_category: str = _classify_category(description, name, cves)

    return Claim(
        claimed_category=claimed_category,
        forwarded=forwarded,
        description=description,
        bugs=bugs,
        cves=cves,
        rule_version=CLAIM_RULE_VERSION,
    )
