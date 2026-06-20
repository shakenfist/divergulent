"""The append-only decision ledger — schema, rule registry, and primitives.

This module is the *provenance backbone* of classification (phase 3, step 3a).
It defines the curation-side sqlite data model in which every classification
verdict lives, and the small set of primitives that maintain its one
load-bearing invariant: **the ledger is append-only**.

Two ideas govern the design:

1. **The ledger is the source of truth; the verdict is a derived view.**  A
   ``decision`` row is written exactly once and is thereafter *immutable*.  It
   is never edited and never deleted — it can only be *superseded* (a
   ``superseded_at`` timestamp set on it).  The current verdict for a
   fingerprint is computed (in step 3c) from the live, non-superseded decisions
   at ``human > llm > heuristic`` precedence; it is never stored.  Because the
   audit trail is complete, retiring or bumping a rule is a surgical redo:
   supersede exactly its decisions, recompute the view, and re-queue only the
   fingerprints left with no live decision.

2. **Dangerous-construct flags are observations, never category decisions.**  A
   flagged patch is still ``unknown``/substantive; the flag rides alongside the
   category decision in a separate ``observation`` table so it feeds the review
   queue without ever becoming a category.  This keeps "never pronounce malice"
   structural — there is no code path here that turns a flag into a verdict.

This step is the **data model only**: the schema, the registry that mirrors the
phase-2 deciders in ``rules.py``, and the append-only/supersede primitives.  It
runs no rules and derives no verdicts — that is steps 3b and 3c.

Timestamps are **passed in by the caller**, never generated here.  The recorder
(step 3b) supplies a ``decided_at`` / ``observed_at`` / ``superseded_at``
string; this module never reads a wall clock, keeping its paths deterministic
and re-runnable.

Pure vs external is modelled now: ``input_snapshot`` / ``input_fresh_until`` are
reserved for ``purity='external'`` rules (phase 6) and are nullable and unused
by every phase-2 (``purity='pure'``) rule.

Curation-side only: no client command imports ``classify/``.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

from divergulent.classify.claim import CLAIM_RULE_VERSION
from divergulent.classify.rules import RULES_VERSION, _CATEGORY_RULES

# ---------------------------------------------------------------------------
# Versioning — every schema/enum bump is a tracked migration, never a silent
# reinterpretation (see the plan's "Versioning is explicit").
# ---------------------------------------------------------------------------

LEDGER_SCHEMA_VERSION = 1
"""The on-disk schema version, recorded in the ``meta`` table."""

CATEGORY_ENUM_VERSION = 1
"""The category-enum version that travels on every rule and decision.  The
category enum (packaging / documentation / unknown / ...) is still provisional;
this version pins which enumeration a decision was made under."""

# ---------------------------------------------------------------------------
# The decision kinds and rule purities, and the precedence that ranks them.
#
# ``kind`` ranks WHO decided: a human overrides a (verified) LLM, which
# overrides a heuristic.  The derived current-verdict view (step 3c) uses
# ``KIND_PRECEDENCE`` to pick, per fingerprint, the highest-precedence live
# decision; it is defined here so the registry and the view share one source of
# truth.  Phase-2 decisions are all ``heuristic``; the ``llm`` and ``human``
# seats are reserved now so phases 4+ slot in without a schema change.
# ---------------------------------------------------------------------------

KINDS: frozenset[str] = frozenset({'heuristic', 'llm', 'human'})
"""Valid ``kind`` values for a rule and a decision."""

PURITIES: frozenset[str] = frozenset({'pure', 'external'})
"""Valid ``purity`` values.  ``pure`` is a function of the diff alone (no clock,
no network); ``external`` consults the world and so records an input snapshot +
freshness (phase 6)."""

# Precedence order, lowest-to-highest, for the derived view: human > llm >
# heuristic.  Higher index == higher precedence.  Used by step 3c.
KIND_PRECEDENCE: tuple[str, ...] = ('heuristic', 'llm', 'human')


def kind_rank(kind: str) -> int:
    """Precedence rank of a decision ``kind`` (higher wins).

    ``human`` outranks ``llm`` outranks ``heuristic``.  Step 3c uses this to
    pick the winning live decision per fingerprint.  Raises ``ValueError`` for
    an unknown kind so a typo cannot silently sort to the bottom.
    """
    try:
        return KIND_PRECEDENCE.index(kind)
    except ValueError:
        raise ValueError('unknown decision kind: %r' % kind)


# ---------------------------------------------------------------------------
# The rule registry.
#
# A ``RegisteredRule`` is one row of the ``rule`` table: a versioned, described
# decider.  ``default_registry()`` enumerates every phase-2 decider by READING
# ``rules.py`` (it iterates ``_CATEGORY_RULES`` rather than re-listing the ids),
# so adding a phase-2 rule without registering it here is caught by the
# cross-check test rather than drifting silently.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegisteredRule:
    """One row of the ``rule`` registry: a versioned, described decider.

    ``kind`` and ``purity`` are members of :data:`KINDS` / :data:`PURITIES`.
    ``category_enum_version`` pins the enumeration the rule decides into.
    """

    rule_id: str
    version: int
    kind: str
    purity: str
    description: str
    category_enum_version: int

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError('unknown rule kind: %r' % self.kind)
        if self.purity not in PURITIES:
            raise ValueError('unknown rule purity: %r' % self.purity)


# Short human descriptions for the seven content-category deciders, keyed by the
# id used in ``rules._CATEGORY_RULES``.  Keeping the descriptions keyed (rather
# than positional) means the registry stays correct even if the precedence order
# in ``rules.py`` is reordered; an id present in ``_CATEGORY_RULES`` but missing
# here fails loudly in ``default_registry`` rather than registering blank text.
_CATEGORY_RULE_DESCRIPTIONS: dict[str, str] = {
    'empty': 'change normalises to empty (mode-only / pure-decoration) -> packaging',
    'ignore-file-only': 'touches only ignore files with ignore-pattern lines -> packaging',
    'whitespace-only': 'change differs only in whitespace (no semantic change) -> packaging',
    'comment-only': 'every changed line is blank or a comment (prose in code) -> documentation',
    'doc-only': 'all touched files are documentation -> documentation',
    'build-only': 'all touched files are build-system / packaging -> packaging',
    'substantive': 'not settled by deterministic content rules -> unknown (phase-4 residue)',
}


def default_registry() -> list[RegisteredRule]:
    """Every phase-2 decider as a list of :class:`RegisteredRule` rows.

    Built by reading ``rules.py`` so the registry cannot drift from the deciders
    it describes:

    * the seven content-category rules from ``rules._CATEGORY_RULES`` (id +
      version taken straight from that tuple);
    * the dangerous-construct scan (``rule_id='dangerous-construct-scan'``,
      version ``RULES_VERSION``) — it produces *observations*, not category
      decisions, so it is registered for provenance but the recorder writes its
      output to the ``observation`` table;
    * the claim-category classifier (``rule_id='claim-category'``, version
      ``CLAIM_RULE_VERSION``).

    All phase-2 rules are ``kind='heuristic'``, ``purity='pure'``.  Raises
    ``KeyError`` if ``_CATEGORY_RULES`` grows a rule with no description here —
    the drift guard.
    """
    rules: list[RegisteredRule] = []
    for rule_id, version, _fn in _CATEGORY_RULES:
        rules.append(RegisteredRule(
            rule_id=rule_id,
            version=version,
            kind='heuristic',
            purity='pure',
            description=_CATEGORY_RULE_DESCRIPTIONS[rule_id],
            category_enum_version=CATEGORY_ENUM_VERSION))

    rules.append(RegisteredRule(
        rule_id='dangerous-construct-scan',
        version=RULES_VERSION,
        kind='heuristic',
        purity='pure',
        description='scans added code lines for dangerous constructs -> observations, never a category',
        category_enum_version=CATEGORY_ENUM_VERSION))

    rules.append(RegisteredRule(
        rule_id='claim-category',
        version=CLAIM_RULE_VERSION,
        kind='heuristic',
        purity='pure',
        description="classifies the author's DEP-3 claim into a category (provenance only)",
        category_enum_version=CATEGORY_ENUM_VERSION))

    return rules


# ---------------------------------------------------------------------------
# Schema.
# ---------------------------------------------------------------------------


def create_ledger(path: str) -> sqlite3.Connection:
    """Create a fresh ledger at ``path``, overwriting any existing file.

    Lays down the ``meta``, ``rule``, ``decision``, and ``observation`` tables
    and their indexes, seeds ``meta`` with the schema/enum versions, and returns
    an open connection (the caller closes it).  A re-run is deterministic: any
    stale ledger is removed first.
    """
    if os.path.exists(path):
        os.unlink(path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.execute(
        'CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    conn.executemany(
        'INSERT INTO meta (key, value) VALUES (?, ?)',
        [('schema_version', str(LEDGER_SCHEMA_VERSION)),
         ('category_enum_version', str(CATEGORY_ENUM_VERSION))])

    # The rule registry.  (rule_id, version) is the PK so a bumped version is a
    # new row alongside the old, never a replacement.
    conn.execute(
        'CREATE TABLE rule ('
        'rule_id TEXT NOT NULL, '
        'version INTEGER NOT NULL, '
        'kind TEXT NOT NULL, '
        'purity TEXT NOT NULL, '
        'category_enum_version INTEGER NOT NULL, '
        'description TEXT, '
        'retired INTEGER NOT NULL DEFAULT 0, '
        'PRIMARY KEY (rule_id, version))')

    # The append-only decision ledger.  A row is written once and only ever
    # superseded (superseded_at set); never edited, never deleted.
    # input_snapshot / input_fresh_until are reserved for purity='external'
    # rules (phase 6) — nullable and unused now.
    conn.execute(
        'CREATE TABLE decision ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'fingerprint TEXT NOT NULL, '
        'category TEXT NOT NULL, '
        'confidence TEXT NOT NULL, '
        'decided_by TEXT NOT NULL, '
        'rule_version INTEGER NOT NULL, '
        'kind TEXT NOT NULL, '
        'evidence TEXT, '
        'decided_at TEXT, '
        'superseded_at TEXT, '
        'input_snapshot TEXT, '
        'input_fresh_until TEXT)')
    conn.execute('CREATE INDEX idx_decision_fingerprint ON decision (fingerprint)')
    conn.execute(
        'CREATE INDEX idx_decision_decided_by_version ON decision (decided_by, rule_version)')

    # Observations (e.g. dangerous-construct flags) — same append-only/supersede
    # discipline as decisions, but never a category.
    conn.execute(
        'CREATE TABLE observation ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'fingerprint TEXT NOT NULL, '
        'kind TEXT NOT NULL, '
        'detail TEXT NOT NULL, '
        'evidence TEXT, '
        'observed_by TEXT NOT NULL, '
        'rule_version INTEGER NOT NULL, '
        'observed_at TEXT, '
        'superseded_at TEXT)')
    conn.execute('CREATE INDEX idx_observation_fingerprint ON observation (fingerprint)')

    conn.commit()
    return conn


def register_rules(conn: sqlite3.Connection, rules: list[RegisteredRule]) -> int:
    """Write ``rules`` into the ``rule`` table; idempotent.

    Uses ``INSERT OR IGNORE`` on the ``(rule_id, version)`` primary key, so
    re-registering the same registry is a no-op and never overwrites a row
    (e.g. its ``retired`` flag).  Returns the number of rules supplied.
    """
    conn.executemany(
        'INSERT OR IGNORE INTO rule '
        '(rule_id, version, kind, purity, category_enum_version, description, retired) '
        'VALUES (?, ?, ?, ?, ?, ?, 0)',
        [(r.rule_id, r.version, r.kind, r.purity, r.category_enum_version, r.description)
         for r in rules])
    conn.commit()
    return len(rules)


# ---------------------------------------------------------------------------
# Append-only primitives.
#
# These are the ONLY mutations the module exposes.  There is deliberately NO
# update-content and NO delete API for a decision or observation: a row is
# written once (``append_*``) and can only ever have its ``superseded_at`` set
# (``supersede_*``).  This enforces the append-only invariant in code, not just
# by convention — the append-only test asserts no other mutating public name
# exists.
# ---------------------------------------------------------------------------


def append_decision(conn: sqlite3.Connection, *, fingerprint: str, category: str,
                    confidence: str, decided_by: str, rule_version: int, kind: str,
                    evidence: str | None, decided_at: str,
                    input_snapshot: str | None = None,
                    input_fresh_until: str | None = None) -> int:
    """Append one immutable ``decision`` row; returns its new id.

    INSERT only — this never edits or replaces an existing row.  ``decided_at``
    is supplied by the caller (this module never reads a clock).
    ``input_snapshot`` / ``input_fresh_until`` are reserved for external rules
    (phase 6) and default to ``None`` for the pure phase-2 rules.
    """
    cursor = conn.execute(
        'INSERT INTO decision '
        '(fingerprint, category, confidence, decided_by, rule_version, kind, evidence, '
        'decided_at, superseded_at, input_snapshot, input_fresh_until) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)',
        (fingerprint, category, confidence, decided_by, rule_version, kind, evidence,
         decided_at, input_snapshot, input_fresh_until))
    conn.commit()
    return int(cursor.lastrowid)


def append_observation(conn: sqlite3.Connection, *, fingerprint: str, kind: str,
                       detail: str, evidence: str | None, observed_by: str,
                       rule_version: int, observed_at: str) -> int:
    """Append one immutable ``observation`` row; returns its new id.

    INSERT only.  An observation (e.g. a dangerous-construct flag) is never a
    category decision; it rides alongside one.  ``observed_at`` is caller-
    supplied.
    """
    cursor = conn.execute(
        'INSERT INTO observation '
        '(fingerprint, kind, detail, evidence, observed_by, rule_version, '
        'observed_at, superseded_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, NULL)',
        (fingerprint, kind, detail, evidence, observed_by, rule_version, observed_at))
    conn.commit()
    return int(cursor.lastrowid)


def supersede_decisions(conn: sqlite3.Connection, *, decided_by: str, rule_version: int,
                        superseded_at: str) -> int:
    """Supersede the LIVE decisions of one rule version; returns the count.

    Sets ``superseded_at`` only on currently-live (``superseded_at IS NULL``)
    decisions whose ``(decided_by, rule_version)`` matches.  The rows are NOT
    deleted — they remain as the audit trail, just marked superseded.  Already-
    superseded rows are left untouched (their original timestamp is preserved).
    """
    cursor = conn.execute(
        'UPDATE decision SET superseded_at = ? '
        'WHERE decided_by = ? AND rule_version = ? AND superseded_at IS NULL',
        (superseded_at, decided_by, rule_version))
    conn.commit()
    return cursor.rowcount


def supersede_observations(conn: sqlite3.Connection, *, observed_by: str, rule_version: int,
                           superseded_at: str) -> int:
    """Supersede the LIVE observations of one rule version; returns the count.

    The observation analogue of :func:`supersede_decisions`: marks live
    observations superseded without deleting them.
    """
    cursor = conn.execute(
        'UPDATE observation SET superseded_at = ? '
        'WHERE observed_by = ? AND rule_version = ? AND superseded_at IS NULL',
        (superseded_at, observed_by, rule_version))
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Read helpers.  Pure SELECTs; the derived current-verdict view is step 3c and
# is deliberately NOT built here.
# ---------------------------------------------------------------------------


def registered_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every row of the ``rule`` registry (rows, with column access by name)."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        'SELECT rule_id, version, kind, purity, category_enum_version, description, retired '
        'FROM rule ORDER BY rule_id, version').fetchall()


def live_decisions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All non-superseded decisions (``superseded_at IS NULL``)."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        'SELECT * FROM decision WHERE superseded_at IS NULL ORDER BY id').fetchall()


def live_decision_exists(conn: sqlite3.Connection, *, fingerprint: str, decided_by: str,
                         rule_version: int) -> bool:
    """Whether a LIVE decision already exists for ``(fingerprint, decided_by,
    rule_version)``.

    The idempotency check for the recorder (step 3b): a pure decision is
    reproducible from this triple, so one that already exists live must not be
    appended again.  SELECT-only; never mutates.
    """
    row = conn.execute(
        'SELECT 1 FROM decision '
        'WHERE fingerprint = ? AND decided_by = ? AND rule_version = ? '
        'AND superseded_at IS NULL LIMIT 1',
        (fingerprint, decided_by, rule_version)).fetchone()
    return row is not None


def decisions_for(conn: sqlite3.Connection, fingerprint: str) -> list[sqlite3.Row]:
    """Every decision for ``fingerprint``, live or superseded, in id order.

    Includes superseded rows so callers can read the full audit trail for a
    fingerprint.
    """
    conn.row_factory = sqlite3.Row
    return conn.execute(
        'SELECT * FROM decision WHERE fingerprint = ? ORDER BY id', (fingerprint,)).fetchall()


def live_observations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All non-superseded observations (``superseded_at IS NULL``)."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        'SELECT * FROM observation WHERE superseded_at IS NULL ORDER BY id').fetchall()


def live_observation_exists(conn: sqlite3.Connection, *, fingerprint: str, observed_by: str,
                            rule_version: int, detail: str, evidence: str | None) -> bool:
    """Whether a LIVE observation already exists for ``(fingerprint, observed_by,
    rule_version, detail, evidence)``.

    The idempotency check for recording dangerous-construct observations
    (step 3b): an identical live observation must not be appended twice.
    ``evidence`` is matched with ``IS`` so a ``NULL`` evidence compares equal to
    ``NULL``.  SELECT-only; never mutates.
    """
    row = conn.execute(
        'SELECT 1 FROM observation '
        'WHERE fingerprint = ? AND observed_by = ? AND rule_version = ? '
        'AND detail = ? AND evidence IS ? AND superseded_at IS NULL LIMIT 1',
        (fingerprint, observed_by, rule_version, detail, evidence)).fetchone()
    return row is not None


def observations_for(conn: sqlite3.Connection, fingerprint: str) -> list[sqlite3.Row]:
    """Every observation for ``fingerprint``, live or superseded, in id order."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        'SELECT * FROM observation WHERE fingerprint = ? ORDER BY id', (fingerprint,)).fetchall()


def meta(conn: sqlite3.Connection) -> dict[str, str]:
    """The ``meta`` table as a ``{key: value}`` dict (schema/enum versions)."""
    return dict(conn.execute('SELECT key, value FROM meta').fetchall())
