"""The reach axis -- a deterministic, install-base classification.

A security-impacting patch in ``glibc``/``openssl``/``systemd`` is on essentially
every machine; the same construct in a package installed on a few hundred hosts is
a far smaller aggregate exposure. divergulent's thesis is supply-chain exposure
*to users*, so "how many machines actually run this code" is a first-class signal
for WHERE a human reviewer should look first. This is a different KIND from
category and security-risk: those are semantic LLM judgments (each one multiplies
cost), whereas reach is purely structural -- a t-shirt size derived from Debian
popcon install counts -- so it is computed for free over the whole corpus with no
model. It rides ALONGSIDE the category and reviewability (a patch can be an ``XL``
``oversized`` ``bugfix``) as a supersedable ``reach`` observation.

Reach is defined RELATIVE to the ceiling, not as an absolute vote count, so it
stays meaningful as popcon's reporting population drifts and is robust to popcon's
opt-in undercounting (only the ordering and rough magnitude must be right, which
the bias preserves). With ``anchor`` the snapshot's ``max(inst)`` (the near-
universal base package), ``reach = inst(source) / anchor`` and the t-shirt cuts
are powers of ten. Anchoring to ``max(inst)`` rather than the literal name
``libc6`` is deliberate: the base libc package itself can be renamed by a
time64/soname transition.

The ONE hard rule (enforced where reach enters the priority order, not here):
reach multiplies WITHIN a security tier, never across it. Popularity is not risk;
a ubiquitous package carrying a benign patch is not a concern. Reach breaks ties
among comparably-risky patches; it never promotes a low-risk patch over a high-
risk one.

Provenance mirrors the other tiers: ``observed_by='popcon-rule'`` /
``rule_version=REACH_VERSION``, so a threshold change is a new identity and old
levels supersede cleanly.

Curation-side only: no client command imports ``classify/``.
"""
from __future__ import annotations

import json

# The rule identity recorded on the observation. Bump REACH_VERSION when the
# thresholds change so a re-record supersedes the old level cleanly.
REACH_VERSION = 1
REACH_KIND = 'reach'
REACH_OBSERVED_BY = 'popcon-rule'

# The coarse ordinal t-shirt scale (rank order matters; higher == more machines).
REACH_LEVELS = ('XS', 'S', 'M', 'L', 'XL')
REACH_RANK = {level: rank for rank, level in enumerate(REACH_LEVELS)}

# A source we have a binary list for but cannot reach-rank (no popcon snapshot
# yet). Distinct from XS, which is a real "too rare to report" verdict. ``unknown``
# is intentionally NOT in REACH_RANK, so it never promotes in the priority order.
REACH_UNKNOWN = 'unknown'

# Fraction-of-anchor lower bounds (inclusive) per level, agreed 2026-06-28 from a
# live by_inst snapshot (the powers-of-ten cuts land on intuitive groupings: XL =
# the base system, L = nginx/apache/vim, M = postgresql/docker, S/XS = the tail).
#   XL: reach >= 0.5     L: 0.1 <= reach < 0.5    M: 0.01 <= reach < 0.1
#   S:  0.001 <= reach < 0.01                     XS: reach < 0.001
REACH_XL_FRACTION = 0.5
REACH_L_FRACTION = 0.1
REACH_M_FRACTION = 0.01
REACH_S_FRACTION = 0.001


def fraction(inst: int, anchor: int) -> float:
    """The source's install count as a fraction of the snapshot anchor (max inst).

    A degenerate anchor (<= 0, e.g. an empty snapshot) yields 0.0, so a caller that
    forgot to guard buckets to ``XS`` rather than dividing by zero -- though the
    recorder decides ``unknown`` upstream when there is no usable snapshot.
    """
    if anchor <= 0:
        return 0.0
    return inst / anchor


def bucket_for(inst: int, anchor: int) -> str:
    """Map a source's max-over-binaries install count + anchor to a t-shirt level.

    ``inst`` is the MAX popcon ``inst`` over the source's binaries (max, not sum:
    summing double-counts a machine that has several binaries from one source; max
    answers "is any of this source's code on the box" and is resilient to t64/
    soname renames that split popcon across binary names). A source absent from the
    snapshot has ``inst`` 0 -> ``XS`` ("too rare to report"). ``unknown`` (no
    binary list at all) is decided by the recorder, not here.
    """
    f = fraction(inst, anchor)
    if f >= REACH_XL_FRACTION:
        return 'XL'
    if f >= REACH_L_FRACTION:
        return 'L'
    if f >= REACH_M_FRACTION:
        return 'M'
    if f >= REACH_S_FRACTION:
        return 'S'
    return 'XS'


def evidence_for(*, binary: str, inst: int, anchor: int, snapshot_date: str) -> str:
    """Canonical JSON evidence for a reach observation.

    Records the deciding binary (the source's most-installed), its ``inst``, the
    snapshot ``anchor`` and ``date``, and the derived ``fraction``/``bucket`` -- so
    the badge is explainable ("XL: openssl, 278070/278978 = 0.997, snapshot
    2026-06-28") and the snapshot is auditable. ``fraction`` is rounded so the
    evidence is stable across insignificant re-counts and the recorder can skip an
    unchanged re-record rather than churn a new row.
    """
    return json.dumps(
        {'binary': binary,
         'inst': inst,
         'anchor_inst': anchor,
         'fraction': round(fraction(inst, anchor), 4),
         'bucket': bucket_for(inst, anchor),
         'snapshot_date': snapshot_date},
        sort_keys=True)


def reach_levels_for_index(index_path: str, popcon_path: str, *,
                           column: str = 'inst') -> dict[str, tuple[str, str | None]]:
    """``{fingerprint: (level, evidence)}`` for every fingerprint in the index.

    The corpus-level join the recorder runs once: the index's ``patch`` table
    (fingerprint -> carrying sources) and ``package.binaries`` (source -> binary
    names, from R3) against the popcon snapshot (binary -> inst). A fingerprint's
    reach is the MAX install count over the binaries of EVERY source that carries
    it -- the patch's exposure is the most-installed package it lands in. A
    fingerprint whose carrying sources list no binaries (a corpus built before
    binary capture, or a missing ``binaries`` column) is ``unknown`` with no
    evidence; the recorder counts those rather than churning unrankable rows.
    """
    import sqlite3
    from divergulent.classify import popcon as popcon_mod  # lazy: keep this module import-light

    snapshot = popcon_mod.open_snapshot(popcon_path)
    try:
        inst_map = popcon_mod.installs_by_binary(snapshot, column=column)
        anchor = popcon_mod.anchor_inst(snapshot)
        snapshot_date = popcon_mod.snapshot_meta(snapshot).get('snapshot_date', '')
    finally:
        snapshot.close()

    conn = sqlite3.connect(index_path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            binaries_by_source = {
                row['source_package']: json.loads(row['binaries'] or '[]')
                for row in conn.execute('SELECT source_package, binaries FROM package')}
        except sqlite3.OperationalError:
            binaries_by_source = {}  # pre-R3 index: no binaries column -> all unknown
        sources_by_fp: dict[str, set[str]] = {}
        for row in conn.execute('SELECT DISTINCT fingerprint, source_package FROM patch'):
            sources_by_fp.setdefault(row['fingerprint'], set()).add(row['source_package'])
    finally:
        conn.close()

    levels: dict[str, tuple[str, str | None]] = {}
    for fingerprint, sources in sources_by_fp.items():
        binaries: list[str] = []
        for source in sources:
            binaries.extend(binaries_by_source.get(source, []))
        if not binaries:
            levels[fingerprint] = (REACH_UNKNOWN, None)
            continue
        # MAX over the binaries: the most-installed package the patch lands in.
        best_binary, best_inst = max(
            ((name, inst_map.get(name, 0)) for name in binaries), key=lambda pair: pair[1])
        levels[fingerprint] = (
            bucket_for(best_inst, anchor),
            evidence_for(binary=best_binary, inst=best_inst, anchor=anchor,
                         snapshot_date=snapshot_date))
    return levels


def reach_by_fingerprint(conn) -> dict[str, str]:
    """``{fingerprint: level}`` from the live ``reach`` observations.

    The current reach level per fingerprint -- the input the review UI badges/
    filters and the priority order uses. A fingerprint with no live reach
    observation is absent (treated as lowest rank by the priority code).
    ``unknown`` observations are excluded (not a rankable level).
    """
    from divergulent.classify import ledger as ledger_mod  # lazy: keep this module import-light
    levels: dict[str, str] = {}
    for obs in ledger_mod.live_observations(conn):
        if obs['kind'] == REACH_KIND and obs['detail'] in REACH_RANK:
            levels[obs['fingerprint']] = obs['detail']
    return levels


def reach_rank_by_fingerprint(conn) -> dict[str, int]:
    """``{fingerprint: rank}`` (XS=0 .. XL=4) from the live ``reach`` observations.

    The ordinal the priority order consumes. A fingerprint with no rankable reach
    observation is absent, which the priority code treats as rank 0 (== XS), so an
    un-reached or ``unknown`` patch never out-sorts a reached one within its tier.
    """
    return {fp: REACH_RANK[level] for fp, level in reach_by_fingerprint(conn).items()}
