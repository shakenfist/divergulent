"""Phase 6 -- verify a patch's *claimed* CVE references against Debian's records.

Every earlier tier treats a header's ``CVE-YYYY-NNNN`` as a CLAIM and never checks
it. This module is the first that cross-references the claim against the pinned
Security Tracker snapshot (``security_tracker.py``) and returns one of three
outcomes:

* **confirmed** -- the CVE is recorded against *this* source package. Genuine
  external evidence the patch is a security fix; the caller may settle a
  ``security`` category (under the code-touch + deference guards it applies).
* **contradicted** -- the claimed CVE is not recorded against this source: either
  it is unknown to the tracker entirely (an invented / malformed id) or it is
  recorded only against *other* packages (``wrong-source`` -- possibly a legitimate
  cross-package fix, so a weaker signal). Never settles a category; the caller
  raises it for human review.
* **unknown** -- no CVE reference, or no snapshot to check against. No signal.

This module is PURE with respect to the ledger: it reads the snapshot and returns
a verdict + a compact ``input_snapshot`` dict and an ``input_fresh_until`` horizon.
Writing the decision/observation rows is the recorder's job (E3). Because the
result depends on mutable external state, the verdict records exactly what it saw
(the CVE, source, status, snapshot date) and until when it should be trusted.

Curation-side only: no client command imports ``classify/``.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field

import re

from divergulent.classify import bts as bts_mod
from divergulent.classify import ledger as ledger_mod
from divergulent.classify import security_tracker

# The external rule's identity, recorded on the decision it settles. Bump the
# version when the verification logic changes so a re-record supersedes cleanly.
EXTERNAL_CVE_RULE_ID = 'security-tracker-cve'
EXTERNAL_CVE_VERSION = 1
EXTERNAL_CVE_KIND = 'heuristic'      # ranks in the heuristic tier (below verified-LLM/human)
EXTERNAL_CVE_PURITY = 'external'     # consults the world -> records an input snapshot + freshness
EXTERNAL_CVE_CATEGORY = 'security'   # the only category a confirmed CVE settles

# The provenance observation the pass records alongside (or instead of) the
# category decision: it annotates every fingerprint that carries a CVE reference,
# so the review UI can badge "confirmed" / "unconfirmed" and the priority order
# can nudge a contradiction up for a human look.
PROVENANCE_KIND = 'provenance'
PROVENANCE_OBSERVED_BY = EXTERNAL_CVE_RULE_ID
DETAIL_CVE_CONFIRMED = 'cve-confirmed'
DETAIL_BUG_CONFIRMED = 'bug-confirmed'
DETAIL_CLAIM_UNCONFIRMED = 'claim-unconfirmed'

# A Debian bug reference as declared in a DEP-3 ``Bug-Debian:`` field: a bare
# number, ``#number``, or a bugs.debian.org URL. Extract the numeric id.
_BUG_NUMBER_RE = re.compile(r'(\d{3,})')


def registered_rule() -> ledger_mod.RegisteredRule:
    """The ``rule`` registry row for the external CVE cross-reference."""
    return ledger_mod.RegisteredRule(
        rule_id=EXTERNAL_CVE_RULE_ID, version=EXTERNAL_CVE_VERSION,
        kind=EXTERNAL_CVE_KIND, purity=EXTERNAL_CVE_PURITY,
        description='claimed CVE confirmed against the Debian Security Tracker for this source -> security',
        category_enum_version=ledger_mod.CATEGORY_ENUM_VERSION)


def provenance_by_fingerprint(conn) -> dict:
    """``{fingerprint: detail}`` from the live phase-6 provenance observations.

    ``detail`` is ``cve-confirmed`` or ``claim-unconfirmed``. Backs the review UI's
    provenance badge; a fingerprint with no CVE reference is simply absent.
    """
    return {obs['fingerprint']: obs['detail']
            for obs in ledger_mod.live_observations(conn)
            if obs['kind'] == PROVENANCE_KIND}


def should_settle_security(content_category: str, touches_code: bool) -> bool:
    """Whether a confirmed CVE may settle ``security`` for this fingerprint.

    The two guards from the plan: DEFER to a settled pure-content category (only
    the ``unknown`` residue is eligible -- a manpage that merely cites a CVE
    classifies as high-confidence ``documentation`` and must stay there), and
    require the patch to TOUCH CODE (a confirmed CVE over non-code content is not
    a code security fix). Both must hold.
    """
    return content_category == 'unknown' and touches_code


# How long an external verdict is trusted before the recorder must re-verify it
# against a current snapshot. Generous: a *settled* CVE status rarely flips.
DEFAULT_TTL_DAYS = 30

# The three outcomes.
CONFIRMED = 'confirmed'
CONTRADICTED = 'contradicted'
UNKNOWN = 'unknown'

# Contradiction sub-kinds (carried in the verdict's detail/evidence).
RESULT_NOT_FOUND = 'not-found'        # no such CVE anywhere in the snapshot (invented/malformed)
RESULT_WRONG_SOURCE = 'wrong-source'  # the CVE exists, but only for other packages


@dataclass(frozen=True)
class CveVerdict:
    """The outcome of cross-referencing a patch's claimed CVEs against the tracker.

    ``input_snapshot`` is the self-describing evidence the recorder stores on the
    decision row; ``fresh_until`` is the ISO date the verdict should be re-checked
    after. For ``unknown`` both are empty/None -- there is nothing to record.
    """

    outcome: str
    cve: str | None = None
    status: str | None = None
    fixed_version: str | None = None
    result: str | None = None            # for contradictions: not-found | wrong-source
    confidence: str = 'low'
    reason: str = ''
    input_snapshot: dict = field(default_factory=dict)
    fresh_until: str | None = None


def fresh_until(snapshot_date: str, *, ttl_days: int = DEFAULT_TTL_DAYS) -> str:
    """``snapshot_date`` (``YYYY-MM-DD``) + ``ttl_days``, as an ISO date string.

    The freshness horizon written to ``decision.input_fresh_until``; once ``now``
    passes it the recorder re-verifies against a current snapshot.
    """
    day = datetime.date.fromisoformat(snapshot_date)
    return (day + datetime.timedelta(days=ttl_days)).isoformat()


def verify_cve(cves: list[str], source: str, conn, *, snapshot_date: str,
               ttl_days: int = DEFAULT_TTL_DAYS) -> CveVerdict:
    """Cross-reference a patch's claimed ``cves`` for ``source`` against the snapshot.

    Returns the FIRST confirmation found (one corroborated CVE is enough). With no
    confirmation but at least one claimed CVE, returns a contradiction -- stronger
    (``not-found``) when no claimed id exists anywhere, weaker (``wrong-source``)
    when some exist only for other packages. With no CVE reference at all, returns
    ``unknown`` (no signal, nothing recorded).
    """
    if not cves:
        return CveVerdict(outcome=UNKNOWN, reason='no CVE reference')

    any_exists_elsewhere = False
    for raw in cves:
        cve = raw.upper()
        row = security_tracker.cve_row(conn, source, cve)
        if row is not None:
            status = row['status']
            fixed = row['fixed_version']
            # High confidence only when the tracker records it resolved WITH a
            # fixed version; an open/undetermined entry still corroborates, but
            # more weakly.
            confidence = 'high' if (status == 'resolved' and fixed) else 'medium'
            reason = 'confirmed %s (security-tracker %s)' % (cve, snapshot_date)
            snapshot = {'cve': cve, 'source': source, 'status': status,
                        'fixed_version': fixed, 'snapshot_date': snapshot_date}
            return CveVerdict(outcome=CONFIRMED, cve=cve, status=status, fixed_version=fixed,
                              confidence=confidence, reason=reason, input_snapshot=snapshot,
                              fresh_until=fresh_until(snapshot_date, ttl_days=ttl_days))
        if security_tracker.cve_exists(conn, cve):
            any_exists_elsewhere = True

    result = RESULT_WRONG_SOURCE if any_exists_elsewhere else RESULT_NOT_FOUND
    reason = ('claimed %s not recorded for %s (%s, security-tracker %s)'
              % (','.join(c.upper() for c in cves), source, result, snapshot_date))
    snapshot = {'cves': [c.upper() for c in cves], 'source': source,
                'result': result, 'snapshot_date': snapshot_date}
    return CveVerdict(outcome=CONTRADICTED, result=result, reason=reason,
                      input_snapshot=snapshot,
                      fresh_until=fresh_until(snapshot_date, ttl_days=ttl_days))


@dataclass(frozen=True)
class BugVerdict:
    """The outcome of cross-referencing a patch's claimed Debian bugs (E5).

    Provenance only -- a bug reference maps to no category, so this never settles
    a verdict; ``detail`` is the observation detail (``bug-confirmed`` /
    ``claim-unconfirmed``) and ``reason`` its evidence line.
    """

    outcome: str
    detail: str | None = None
    reason: str = ''


def debian_bug_number(ref: str) -> int | None:
    """The numeric Debian bug id in a ``Bug-Debian`` ref, or ``None``.

    Accepts a bare number, ``#number``, or a bugs.debian.org URL; takes the first
    3+ digit run so a stray year in a URL path does not masquerade as a bug.
    """
    match = _BUG_NUMBER_RE.search(ref or '')
    return int(match.group(1)) if match else None


def verify_bugs(bugs, source: str, conn, *, snapshot_date: str) -> BugVerdict:
    """Cross-reference a patch's declared Debian bugs for ``source`` against the BTS.

    Only ``tracker == 'debian'`` refs are checked (the generic upstream ``Bug:``
    tracker is out of scope). Returns the FIRST confirmation (a bug filed against
    this source); otherwise a contradiction when a declared bug is unknown to the
    BTS or filed against another package; ``unknown`` when the patch declares no
    resolvable Debian bug. Provenance only -- never a category.
    """
    numbers = [debian_bug_number(b.ref) for b in (bugs or []) if getattr(b, 'tracker', None) == 'debian']
    numbers = [n for n in numbers if n is not None]
    if not numbers:
        return BugVerdict(outcome=UNKNOWN)

    any_exists_elsewhere = False
    for bug in numbers:
        row = bts_mod.bug_row(conn, bug)
        if row is not None and row['source'] == source:
            reason = 'confirmed Debian bug #%d (%s, %s, bts %s)' % (
                bug, source, row['status'], snapshot_date)
            return BugVerdict(outcome=CONFIRMED, detail=DETAIL_BUG_CONFIRMED, reason=reason)
        if row is not None:
            any_exists_elsewhere = True

    result = RESULT_WRONG_SOURCE if any_exists_elsewhere else RESULT_NOT_FOUND
    reason = 'declared Debian bug %s not filed for %s (%s, bts %s)' % (
        ','.join('#%d' % n for n in numbers), source, result, snapshot_date)
    return BugVerdict(outcome=CONTRADICTED, detail=DETAIL_CLAIM_UNCONFIRMED, reason=reason)
