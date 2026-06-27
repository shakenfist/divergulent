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

import argparse
import datetime
import os
import sqlite3
import sys
from dataclasses import dataclass

from divergulent.classify.claim import CLAIM_RULE_VERSION
from divergulent.classify.rules import RULES_VERSION, _CATEGORY_RULES

# ---------------------------------------------------------------------------
# Versioning — every schema/enum bump is a tracked migration, never a silent
# reinterpretation (see the plan's "Versioning is explicit").
# ---------------------------------------------------------------------------

LEDGER_SCHEMA_VERSION = 2
"""The on-disk schema version, recorded in the ``meta`` table.

Version 2 (phase 4, step 4c) adds three columns to ``decision`` -- ``verified``
(an LLM decision counts only once an adversarial pass or a human confirms it),
``signature`` / ``signed_by`` (reserved for signed human ManualDecisions in
step 4e) -- and a ``review_queue`` table backing the human-review worklist.
Every bump is a tracked migration, never a silent reinterpretation."""

CATEGORY_ENUM_VERSION = 2
"""The category-enum version that travels on every rule and decision.  The
category enum (packaging / documentation / test / unknown / ...) is still
provisional; this version pins which enumeration a decision was made under.

Version 2 adds the deterministic ``test`` category (a patch touching only test
files -- non-shipping, structurally determined; see ``rules._rule_test_only``).
Every bump is a tracked migration, applied when the ledger is next rebuilt."""

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
    'test-only': 'all touched files are tests (non-shipping) -> test',
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

# The tables a built ledger must carry.  :func:`open_ledger` checks for these so
# a mistyped path fails up front with an actionable message rather than deep in a
# query with a baffling "no such table".
REQUIRED_TABLES: frozenset[str] = frozenset({'meta', 'rule', 'decision', 'observation'})


class LedgerError(Exception):
    """A user-facing error: a path is not a built ledger (or does not exist).

    Raised by :func:`open_ledger` and caught by the ledger / review CLIs, which
    print its message to stderr and exit non-zero -- so an operator sees one
    clear line, not a traceback.
    """


def open_ledger(path: str) -> sqlite3.Connection:
    """Open an EXISTING built ledger at ``path``; fail clearly if it is not one.

    A bare ``sqlite3.connect`` silently CREATES an empty database for a missing
    path, so a mistyped or unbuilt ledger surfaces only later as a confusing
    ``no such table: decision`` deep inside a query.  This guards the read/update
    commands instead: it requires ``path`` to already exist and to carry the
    ledger schema (:data:`REQUIRED_TABLES`), raising :class:`LedgerError` with an
    actionable message otherwise.  Returns a connection with ``row_factory`` set
    to :class:`sqlite3.Row` (the caller closes it).  ``build`` does NOT use this
    -- it legitimately creates a new ledger via :func:`create_ledger`.
    """
    if not os.path.exists(path):
        raise LedgerError(
            '%r does not exist. Pass a ledger built by `ledger build`, '
            'e.g. <corpus_dir>/ledger.sqlite.' % path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    present = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    missing = REQUIRED_TABLES - present
    if missing:
        conn.close()
        raise LedgerError(
            '%r is not a divergulent ledger (missing table(s): %s). Did you mean '
            'a built ledger such as <corpus_dir>/ledger.sqlite, or run '
            '`ledger build` first?' % (path, ', '.join(sorted(missing))))
    return conn


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
    #
    # verified (schema v2): an LLM decision counts only once an adversarial pass
    # (or a human) confirms it; a heuristic decision is always unverified (0) and
    # the precedence treats an unverified LLM as below a heuristic.  signature /
    # signed_by (schema v2) are reserved for signed human ManualDecisions
    # (step 4e) — nullable and unused now.
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
        'input_fresh_until TEXT, '
        'verified INTEGER NOT NULL DEFAULT 0, '
        'signature TEXT, '
        'signed_by TEXT)')
    conn.execute('CREATE INDEX idx_decision_fingerprint ON decision (fingerprint)')
    conn.execute(
        'CREATE INDEX idx_decision_decided_by_version ON decision (decided_by, rule_version)')

    # The human-review queue (schema v2): a ledger-backed worklist of fingerprints
    # the LLM tier routed to a human (step 4b).  An item is "pending" while
    # reviewed_at IS NULL; the index on reviewed_at makes that filter fast.  The
    # queue is a worklist, not a verdict store — a human's verdict is a signed
    # kind='human' decision (step 4e), and recording it is what clears the item.
    conn.execute(
        'CREATE TABLE review_queue ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'fingerprint TEXT NOT NULL, '
        'reason TEXT, '
        'draft_category TEXT, '
        'draft_confidence TEXT, '
        'priority INTEGER NOT NULL DEFAULT 0, '
        'enqueued_at TEXT, '
        'reviewed_at TEXT)')
    conn.execute('CREATE INDEX idx_review_queue_fingerprint ON review_queue (fingerprint)')
    conn.execute('CREATE INDEX idx_review_queue_reviewed_at ON review_queue (reviewed_at)')

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
                    input_fresh_until: str | None = None,
                    verified: bool = False, signature: str | None = None,
                    signed_by: str | None = None, commit: bool = True) -> int:
    """Append one immutable ``decision`` row; returns its new id.

    INSERT only — this never edits or replaces an existing row.  ``decided_at``
    is supplied by the caller (this module never reads a clock).
    ``input_snapshot`` / ``input_fresh_until`` are reserved for external rules
    (phase 6) and default to ``None`` for the pure phase-2 rules.

    ``verified`` (schema v2) defaults to ``False`` so every existing caller is
    preserved: a heuristic decision is unverified, and only an LLM decision the
    adversarial pass confirmed (or a human) is recorded ``verified=True``.
    ``signature`` / ``signed_by`` (schema v2) default to ``None`` and are
    reserved for signed human ManualDecisions (step 4e).

    ``commit`` defaults to True (each append is durable on its own).  A bulk
    caller (the recorder appending ~60k rows) passes ``commit=False`` and
    commits once at the end, turning 60k fsyncs into one transaction;
    same-connection reads still see the uncommitted rows, so the recorder's
    idempotency check is unaffected.
    """
    cursor = conn.execute(
        'INSERT INTO decision '
        '(fingerprint, category, confidence, decided_by, rule_version, kind, evidence, '
        'decided_at, superseded_at, input_snapshot, input_fresh_until, '
        'verified, signature, signed_by) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        (fingerprint, category, confidence, decided_by, rule_version, kind, evidence,
         decided_at, input_snapshot, input_fresh_until,
         1 if verified else 0, signature, signed_by))
    if commit:
        conn.commit()
    return int(cursor.lastrowid)


def append_observation(conn: sqlite3.Connection, *, fingerprint: str, kind: str,
                       detail: str, evidence: str | None, observed_by: str,
                       rule_version: int, observed_at: str, commit: bool = True) -> int:
    """Append one immutable ``observation`` row; returns its new id.

    INSERT only.  An observation (e.g. a dangerous-construct flag) is never a
    category decision; it rides alongside one.  ``observed_at`` is caller-
    supplied.  ``commit`` defaults to True; a bulk caller passes ``commit=False``
    and commits once at the end (see :func:`append_decision`).
    """
    cursor = conn.execute(
        'INSERT INTO observation '
        '(fingerprint, kind, detail, evidence, observed_by, rule_version, '
        'observed_at, superseded_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, NULL)',
        (fingerprint, kind, detail, evidence, observed_by, rule_version, observed_at))
    if commit:
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


def supersede_decisions_for_fingerprint(conn: sqlite3.Connection, *, fingerprint: str,
                                        kind: str | None = None, superseded_at: str,
                                        commit: bool = True) -> int:
    """Supersede the LIVE decisions of ONE fingerprint; returns the count.

    The surgical, single-fingerprint counterpart to :func:`supersede_decisions`
    (which is rule-wide): sets ``superseded_at`` only on currently-live rows for
    ``fingerprint``, optionally narrowed to one ``kind`` (e.g. ``'human'`` to
    redo just a human verdict).  Rows are never deleted — they stay as the audit
    trail, marked superseded.  Used by the review tool's ``requeue`` so a single
    patch can be sent back for human review without disturbing any other
    fingerprint's verdict.  ``commit`` defaults to True; a multi-step caller
    passes ``commit=False`` and commits once.
    """
    if kind is None:
        cursor = conn.execute(
            'UPDATE decision SET superseded_at = ? '
            'WHERE fingerprint = ? AND superseded_at IS NULL',
            (superseded_at, fingerprint))
    else:
        cursor = conn.execute(
            'UPDATE decision SET superseded_at = ? '
            'WHERE fingerprint = ? AND kind = ? AND superseded_at IS NULL',
            (superseded_at, fingerprint, kind))
    if commit:
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


def supersede_observations_for_fingerprint(conn: sqlite3.Connection, *, fingerprint: str,
                                           kind: str, observed_by: str | None = None,
                                           superseded_at: str, commit: bool = True) -> int:
    """Supersede the LIVE ``kind`` observations of ONE fingerprint; returns the count.

    The surgical, single-fingerprint counterpart to :func:`supersede_observations`:
    sets ``superseded_at`` only on currently-live ``kind`` rows for ``fingerprint``,
    optionally narrowed to one ``observed_by`` source.  Used to re-score a single
    fingerprint (e.g. the risk gate) without disturbing any other.  Rows are never
    deleted -- they stay as the audit trail.  ``commit`` defaults to True.
    """
    if observed_by is None:
        cursor = conn.execute(
            'UPDATE observation SET superseded_at = ? '
            'WHERE fingerprint = ? AND kind = ? AND superseded_at IS NULL',
            (superseded_at, fingerprint, kind))
    else:
        cursor = conn.execute(
            'UPDATE observation SET superseded_at = ? '
            'WHERE fingerprint = ? AND kind = ? AND observed_by = ? AND superseded_at IS NULL',
            (superseded_at, fingerprint, kind, observed_by))
    if commit:
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


def recent_human_decisions(conn: sqlite3.Connection, *, limit: int) -> list[sqlite3.Row]:
    """The most recent ``limit`` human decisions, newest first (live OR superseded).

    Backs the review tool's ``history`` command: the reviewer's last N verdicts in
    reverse-chronological (insert) order, INCLUDING superseded ones, so a reviewer
    can spot and reconsider a call they later changed.  Ordered by ``id`` DESC
    (insert order) rather than ``decided_at`` so the ordering is total even when
    timestamps collide.
    """
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM decision WHERE kind = 'human' ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()


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


# ---------------------------------------------------------------------------
# The human-review queue (schema v2).
#
# A small worklist: the LLM tier (step 4b) routes a fingerprint here when it
# cannot self-certify (verifier refuted, low confidence, claim mismatch, or a
# dangerous construct).  An item is "pending" while ``reviewed_at IS NULL``.
# The queue is NOT a verdict store: a human's verdict is a signed kind='human'
# decision (step 4e), and :func:`mark_reviewed` is what closes the item.
# ---------------------------------------------------------------------------


def pending_review_item_exists(conn: sqlite3.Connection, *, fingerprint: str,
                               decided_by: str | None = None) -> bool:
    """Whether a PENDING (``reviewed_at IS NULL``) review item exists.

    The idempotency check for enqueuing: re-running triage over the same residue
    must not enqueue a second pending item for a fingerprint already awaiting
    review.  ``decided_by`` is accepted for call-site clarity but not matched
    here — pending-ness is per fingerprint, so one human review settles it.
    SELECT-only; never mutates.
    """
    del decided_by  # pending-ness is per fingerprint; one review settles it.
    row = conn.execute(
        'SELECT 1 FROM review_queue '
        'WHERE fingerprint = ? AND reviewed_at IS NULL LIMIT 1',
        (fingerprint,)).fetchone()
    return row is not None


def append_review_item(conn: sqlite3.Connection, *, fingerprint: str,
                       reason: str | None, draft_category: str | None,
                       draft_confidence: str | None, enqueued_at: str,
                       priority: int = 0, commit: bool = True) -> int:
    """Append one ``review_queue`` item; returns its new id.

    INSERT only.  ``enqueued_at`` is caller-supplied (this module never reads a
    clock).  The new item is pending (``reviewed_at`` is NULL).  ``commit``
    defaults to True; a bulk caller passes ``commit=False`` and commits once.
    """
    cursor = conn.execute(
        'INSERT INTO review_queue '
        '(fingerprint, reason, draft_category, draft_confidence, priority, '
        'enqueued_at, reviewed_at) '
        'VALUES (?, ?, ?, ?, ?, ?, NULL)',
        (fingerprint, reason, draft_category, draft_confidence, priority, enqueued_at))
    if commit:
        conn.commit()
    return int(cursor.lastrowid)


def pending_review_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every pending (``reviewed_at IS NULL``) review item, priority-then-id order.

    Highest ``priority`` first (the prioritised slice the local review tool pulls
    from), then insertion order for a stable, deterministic worklist.
    """
    conn.row_factory = sqlite3.Row
    return conn.execute(
        'SELECT * FROM review_queue WHERE reviewed_at IS NULL '
        'ORDER BY priority DESC, id').fetchall()


def pending_review_items_in_category(conn: sqlite3.Connection, category: str) -> list[sqlite3.Row]:
    """Pending review items whose LLM draft category is ``category``.

    The web review UI's category slice.  Same priority-then-id order as
    :func:`pending_review_items`, narrowed to one ``draft_category`` (denormalised
    onto the queue row at enqueue time, so this needs no join).  The filter scopes
    the worklist; it does not change the ordering, so "next most important" still
    holds within a category.
    """
    conn.row_factory = sqlite3.Row
    return conn.execute(
        'SELECT * FROM review_queue WHERE reviewed_at IS NULL AND draft_category = ? '
        'ORDER BY priority DESC, id', (category,)).fetchall()


def mark_reviewed(conn: sqlite3.Connection, *, item_id: int, reviewed_at: str) -> int:
    """Mark one PENDING review item reviewed; returns the count touched (0 or 1).

    Sets ``reviewed_at`` only on a currently-pending item (``reviewed_at IS
    NULL``) with the given id, so re-marking an already-reviewed item is a no-op
    and never overwrites its original timestamp.  ``reviewed_at`` is caller-
    supplied; this module never reads a clock.
    """
    cursor = conn.execute(
        'UPDATE review_queue SET reviewed_at = ? '
        'WHERE id = ? AND reviewed_at IS NULL',
        (reviewed_at, item_id))
    conn.commit()
    return cursor.rowcount


def reprioritise_review_item(conn: sqlite3.Connection, *, item_id: int, priority: int,
                             commit: bool = True) -> int:
    """Re-stamp one PENDING review item's ``priority``; returns the count touched.

    The stored priority is otherwise frozen at enqueue time; this lets a later
    signal (a risk score that landed after the patch was queued) reach the queue
    order. The review_queue is a derived work list, not part of the append-only
    decision/observation audit trail, so its ``priority`` is mutable (as
    ``reviewed_at`` already is). Touches only a currently-pending item.
    """
    cursor = conn.execute(
        'UPDATE review_queue SET priority = ? WHERE id = ? AND reviewed_at IS NULL',
        (priority, item_id))
    if commit:
        conn.commit()
    return cursor.rowcount


def resolve_settled_review_items(conn: sqlite3.Connection, *, now: str,
                                 commit: bool = True) -> int:
    """Dequeue pending review items whose fingerprint is now DETERMINISTICALLY settled.

    When a deterministic rule (re)classifies a fingerprint to a settled category
    -- e.g. ``ledger record`` applying ``test-only`` -> ``test`` -- a human no
    longer needs to review it, but its pending ``review_queue`` item (enqueued
    earlier when the LLM tier routed it to needs-human) would otherwise still be
    pulled, wasting review effort on an already-settled patch.  This marks such
    items reviewed (``reviewed_at = now``, the dequeue mechanism) when the
    fingerprint's current winning verdict is a ``heuristic`` decision with a
    non-``unknown`` category -- i.e. a real rule settled it, not the
    ``substantive`` residue rule.  It reads the ``current_verdict`` cache, so the
    caller must rebuild that first.  Returns the count cleared; ``now`` is
    caller-supplied.
    """
    cursor = conn.execute(
        'UPDATE review_queue SET reviewed_at = ? '
        'WHERE reviewed_at IS NULL AND fingerprint IN ('
        "  SELECT fingerprint FROM current_verdict "
        "  WHERE kind = 'heuristic' AND category != 'unknown')",
        (now,))
    if commit:
        conn.commit()
    return cursor.rowcount


def reopen_review_items(conn: sqlite3.Connection, *, fingerprint: str,
                        commit: bool = True) -> int:
    """Re-open every ALREADY-REVIEWED queue item for ``fingerprint``; returns the count.

    The inverse of :func:`mark_reviewed`: clears ``reviewed_at`` (back to NULL) on
    items previously marked reviewed, so the fingerprint becomes pending again and
    :func:`pending_review_items` pulls it for a fresh human pass.  Items already
    pending are untouched (the filter requires ``reviewed_at IS NOT NULL``).  Used
    by the review tool's ``requeue``.  ``commit`` defaults to True; a multi-step
    caller passes ``commit=False`` and commits once.
    """
    cursor = conn.execute(
        'UPDATE review_queue SET reviewed_at = NULL '
        'WHERE fingerprint = ? AND reviewed_at IS NOT NULL',
        (fingerprint,))
    if commit:
        conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Supersession / redo (step 3d).
#
# These are the surgical-redo operations.  They are built ENTIRELY on the
# append-only-safe primitives above: ``supersede_rule`` only ever sets a
# ``superseded_at`` timestamp (via :func:`supersede_decisions` /
# :func:`supersede_observations`) and flips a registry flag (``retire_rule``);
# no decision or observation content is ever edited or deleted.  The re-queue is
# NOT stored here: superseding a rule's live decisions simply leaves the affected
# fingerprints with no live decision, and ``verdict.queue`` (step 3c) derives the
# re-queue from that on the next recompute.  This keeps the queue a view, never a
# stored list that could drift from the ledger.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupersedeResult:
    """What a :func:`supersede_rule` call superseded.

    ``decisions_superseded`` / ``observations_superseded`` are the counts of
    previously-live rows marked superseded; ``retired`` records whether the
    rule's registry row was flipped to ``retired=1``.  The re-queue is derived,
    not returned — ``verdict.queue`` recomputes it from the now-missing live
    decisions.
    """

    rule_id: str
    version: int
    decisions_superseded: int
    observations_superseded: int
    retired: bool


def retire_rule(conn: sqlite3.Connection, rule_id: str, version: int) -> None:
    """Mark one ``(rule_id, version)`` registry row retired (``retired=1``).

    This mutates REGISTRY state, not a decision: it records that the rule should
    no longer be run, leaving every decision row untouched.  Idempotent — setting
    the flag on an already-retired row is a no-op.  Whether the row exists is not
    asserted here; an absent rule simply updates nothing.
    """
    conn.execute(
        'UPDATE rule SET retired = 1 WHERE rule_id = ? AND version = ?',
        (rule_id, version))
    conn.commit()


def supersede_rule(conn: sqlite3.Connection, *, rule_id: str, version: int,
                   superseded_at: str, retire: bool = True) -> SupersedeResult:
    """Supersede one rule version's live decisions + observations; surgical redo.

    Marks every LIVE decision AND every LIVE observation made under
    ``(rule_id, version)`` superseded (setting ``superseded_at`` only — nothing
    is edited or deleted), and, when ``retire`` is true, flips the rule's
    registry row to ``retired=1`` so it is not re-run.  The
    ``dangerous-construct-scan`` rule emits observations rather than decisions, so
    superseding it touches the observation table; the content-category rules
    touch the decision table.  Superseding both means a single call cleanly
    redoes whichever a rule produces.

    The re-queue is AUTOMATIC and DERIVED: this stores no queue.  Fingerprints
    left with no live decision are re-queued by ``verdict.queue`` on the next
    recompute, and a higher-version re-registration (a new live decision for the
    same fingerprint) is picked up by ``verdict.current_verdict``.  Returns a
    :class:`SupersedeResult`.  ``superseded_at`` is caller-supplied; this module
    never reads a clock.
    """
    decisions = supersede_decisions(
        conn, decided_by=rule_id, rule_version=version, superseded_at=superseded_at)
    observations = supersede_observations(
        conn, observed_by=rule_id, rule_version=version, superseded_at=superseded_at)
    if retire:
        retire_rule(conn, rule_id, version)
    return SupersedeResult(
        rule_id=rule_id, version=version, decisions_superseded=decisions,
        observations_superseded=observations, retired=retire)


# ---------------------------------------------------------------------------
# The ledger CLI (``python -m divergulent.classify.ledger``).
#
# This is the ONLY place in the ledger stack that reads a wall clock: ``main``
# captures one ``now`` ISO-8601 string and threads it down to the recorder and
# the supersede operation as their ``decided_at`` / ``superseded_at``.  Every
# module below this remains deterministic and re-runnable.
#
# ``record`` and ``verdict`` are LAZY-imported inside the handlers, not at module
# top: ``record`` imports this module, so importing it here would be a cycle.
# Keeping this module import-time clean (only ``ledger`` deps + stdlib) is what
# lets ``record``/``verdict`` import it freely.
# ---------------------------------------------------------------------------


def _cli_now() -> str:
    """The single clock read of the ledger stack: an ISO-8601 UTC timestamp.

    Only the CLI entry point reads the clock; the value is passed down as
    ``decided_at`` / ``superseded_at`` so every deterministic module path stays
    re-runnable.
    """
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _decisions_at_risk(path: str) -> dict[str, int] | None:
    """Decision counts by kind in an existing ledger, or ``None`` if it isn't one.

    Used by :func:`_guard_overwrite` to tell the operator what a destructive
    ``build`` would delete -- especially the irreplaceable ``llm`` / ``human``
    decisions, which are NOT reproducible from the corpus.
    """
    try:
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute('SELECT kind, COUNT(*) FROM decision GROUP BY kind').fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return {kind: n for kind, n in rows}


def _guard_overwrite(out_path: str, *, force: bool) -> bool:
    """Confirm before ``build`` WIPES an existing populated ledger; True to proceed.

    ``build`` recreates the ledger from scratch (``create_ledger`` unlinks it),
    which destroys any appended ``llm`` / ``human`` decisions and the review queue
    -- work that cost real budget and is NOT reproducible from the corpus.  This
    refuses to do that silently: if ``out_path`` already holds a populated ledger
    it requires the operator to type ``wipe`` (or pass ``--force``), and refuses
    outright when stdin is not a TTY and ``--force`` was not given.  An absent or
    empty/non-ledger path proceeds without prompting (nothing to lose).
    """
    counts = _decisions_at_risk(out_path) if os.path.exists(out_path) else None
    if not counts:
        return True

    total = sum(counts.values())
    irreplaceable = counts.get('llm', 0) + counts.get('human', 0)
    summary = '%d decisions (%d llm, %d human, %d heuristic)' % (
        total, counts.get('llm', 0), counts.get('human', 0), counts.get('heuristic', 0))

    if force:
        print('overwriting existing ledger %r with %s (--force).' % (out_path, summary),
              file=sys.stderr)
        return True

    detail = (' The %d llm/human decisions are NOT reproducible from the corpus.'
              % irreplaceable) if irreplaceable else ''
    print('WARNING: %r already holds %s.\n'
          '`build` will PERMANENTLY DELETE it and rebuild from scratch.%s\n'
          'To apply new/changed rules WITHOUT wiping it, use `ledger record` instead, '
          'or build to a new --out path.' % (out_path, summary, detail), file=sys.stderr)

    if not sys.stdin or not sys.stdin.isatty():
        print('refusing to overwrite a populated ledger non-interactively; '
              're-run with --force to wipe it.', file=sys.stderr)
        return False

    answer = input("type 'wipe' to confirm destroying it: ").strip()
    return answer == 'wipe'


def _cmd_build(args: argparse.Namespace) -> int:
    """``build``: create a ledger from a corpus and print the verdict report."""
    from divergulent.classify import record, verdict
    from divergulent.progress import Progress

    index_path = args.index or os.path.join(args.corpus_dir, 'fingerprints.sqlite')
    out_path = args.out or os.path.join(args.corpus_dir, 'ledger.sqlite')

    if not _guard_overwrite(out_path, force=args.force):
        print('aborted; existing ledger left untouched.', file=sys.stderr)
        return 1

    # Count the distinct fingerprints up front so the build shows live progress
    # over the ~3-4 minute deterministic pass instead of going silent.
    index_conn = sqlite3.connect(index_path)
    try:
        (total,) = index_conn.execute('SELECT COUNT(DISTINCT fingerprint) FROM patch').fetchone()
    finally:
        index_conn.close()
    print('recording deterministic decisions for %d fingerprints...' % total, file=sys.stderr)
    progress = Progress(total)

    conn = create_ledger(out_path)
    try:
        stats = record.record_to_ledger(
            conn, args.corpus_dir, index_path, now=_cli_now(), progress=progress)
        rows = verdict.rebuild_current_verdict(conn)
        print(verdict.render_report(verdict.summarise_ledger(conn)))
        print('built ledger: %s' % out_path)
        print('decisions appended=%d skipped=%d; observations appended=%d skipped=%d; '
              'reviewability appended=%d skipped=%d; fingerprints=%d; current verdicts=%d' % (
                  stats.decisions_appended, stats.decisions_skipped,
                  stats.observations_appended, stats.observations_skipped,
                  stats.reviewability_appended, stats.reviewability_skipped,
                  stats.fingerprints, rows))
    finally:
        conn.close()
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    """``record``: apply the current deterministic rules to an EXISTING ledger.

    The NON-DESTRUCTIVE counterpart to ``build``.  ``build`` recreates the ledger
    and so destroys appended llm/human decisions; ``record`` opens the existing
    ledger and re-runs every deterministic rule append-only, superseding any
    heuristic decision whose winning rule has changed (so adding/bumping a rule --
    e.g. the new ``test-only`` rule -- reclassifies the affected fingerprints in
    place), bumps the recorded category-enum version, rebuilds the verdict cache,
    and DEQUEUES any pending review item whose fingerprint a rule has now settled
    deterministically (so a test-only patch queued before the rule existed stops
    being pulled for review).  llm/human decisions are untouched.
    """
    from divergulent.classify import record, verdict
    from divergulent.progress import Progress

    index_path = args.index or os.path.join(args.corpus_dir, 'fingerprints.sqlite')
    if not os.path.exists(index_path):
        raise LedgerError(
            '%r does not exist; pass --index or build the corpus fingerprint index first.'
            % index_path)

    now = _cli_now()
    conn = open_ledger(args.ledger)
    try:
        index_conn = sqlite3.connect(index_path)
        try:
            (total,) = index_conn.execute(
                'SELECT COUNT(DISTINCT fingerprint) FROM patch').fetchone()
        finally:
            index_conn.close()
        print('reconciling deterministic decisions for %d fingerprints...' % total,
              file=sys.stderr)
        progress = Progress(total)

        stats = record.record_to_ledger(
            conn, args.corpus_dir, index_path, now=now, progress=progress,
            reconcile=True)
        # A newly-applied rule may add a category (e.g. ``test`` at enum v2); record
        # the current enum version so ``meta`` reflects what the ledger now holds.
        conn.execute("UPDATE meta SET value = ? WHERE key = 'category_enum_version'",
                     (str(CATEGORY_ENUM_VERSION),))
        conn.commit()
        rows = verdict.rebuild_current_verdict(conn)
        # Drop now-settled fingerprints from the human-review queue (the cache the
        # query reads was just rebuilt above).
        dequeued = resolve_settled_review_items(conn, now=now)
        print(verdict.render_report(verdict.summarise_ledger(conn)))
        print('recorded into ledger: %s' % args.ledger)
        print('dequeued %d now-settled review items' % dequeued)
        print('decisions appended=%d skipped=%d superseded=%d; observations appended=%d '
              'skipped=%d; reviewability appended=%d skipped=%d; fingerprints=%d; '
              'current verdicts=%d' % (
                  stats.decisions_appended, stats.decisions_skipped,
                  stats.decisions_superseded, stats.observations_appended,
                  stats.observations_skipped, stats.reviewability_appended,
                  stats.reviewability_skipped, stats.fingerprints, rows))
    finally:
        conn.close()
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    """``report``: open a built ledger and print the current-verdict report."""
    from divergulent.classify import verdict

    conn = open_ledger(args.ledger)
    try:
        print(verdict.render_report(verdict.summarise_ledger(conn)))
    finally:
        conn.close()
    return 0


def _cmd_supersede(args: argparse.Namespace) -> int:
    """``supersede``: supersede a rule version's decisions/observations + re-queue."""
    from divergulent.classify import verdict

    conn = open_ledger(args.ledger)
    try:
        result = supersede_rule(
            conn, rule_id=args.rule_id, version=args.version,
            superseded_at=_cli_now(), retire=not args.keep)
        if not args.no_rebuild:
            verdict.rebuild_current_verdict(conn)
        queue_size = len(verdict.queue(conn))
        print('superseded rule %s v%d: decisions=%d observations=%d retired=%s' % (
            result.rule_id, result.version, result.decisions_superseded,
            result.observations_superseded, result.retired))
        print('queue size (phase-4 residue): %d' % queue_size)
    finally:
        conn.close()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m divergulent.classify.ledger',
        description='Build, record into, report on, and surgically redo the append-only '
                    'decision ledger (curation-side; offline). `build` creates from '
                    'scratch; `record` applies current rules to an EXISTING ledger '
                    'without wiping appended llm/human work. The current verdict is always '
                    'derived, never stored; superseding a rule re-queues only its '
                    'fingerprints. No malice is ever pronounced.')
    subparsers = parser.add_subparsers(dest='command', required=True)

    build = subparsers.add_parser(
        'build', help='create a ledger from a corpus and print the verdict report')
    build.add_argument('corpus_dir', help='directory of a corpus built by classify.corpus')
    build.add_argument('--index', default=None,
                       help='path to the phase-1 sqlite fingerprint index (default: '
                            '<corpus_dir>/fingerprints.sqlite)')
    build.add_argument('--out', default=None,
                       help='path for the ledger sqlite (default: <corpus_dir>/ledger.sqlite)')
    build.add_argument('--force', action='store_true',
                       help='overwrite an existing ledger WITHOUT confirmation (build '
                            'recreates from scratch, destroying any llm/human decisions)')
    build.set_defaults(func=_cmd_build)

    record_cmd = subparsers.add_parser(
        'record',
        help='apply current deterministic rules to an EXISTING ledger (non-destructive; '
             'preserves llm/human decisions)')
    record_cmd.add_argument('ledger', help='path to a ledger sqlite built by `build`')
    record_cmd.add_argument('corpus_dir', help='directory of a corpus built by classify.corpus')
    record_cmd.add_argument('--index', default=None,
                            help='path to the phase-1 sqlite fingerprint index (default: '
                                 '<corpus_dir>/fingerprints.sqlite)')
    record_cmd.set_defaults(func=_cmd_record)

    report = subparsers.add_parser(
        'report', help='print the current-verdict/queue report for a built ledger')
    report.add_argument('ledger', help='path to a ledger sqlite built by `build`')
    report.set_defaults(func=_cmd_report)

    supersede = subparsers.add_parser(
        'supersede', help="supersede a rule version's decisions and re-queue its fingerprints")
    supersede.add_argument('ledger', help='path to a ledger sqlite built by `build`')
    supersede.add_argument('rule_id', help='the rule id to supersede (e.g. doc-only)')
    supersede.add_argument('version', type=int, help='the rule version to supersede')
    supersede.add_argument('--keep', action='store_true',
                           help='do not retire the rule (supersede its decisions only)')
    supersede.add_argument('--no-rebuild', action='store_true',
                           help='do not rebuild the current_verdict cache after superseding')
    supersede.set_defaults(func=_cmd_supersede)

    return parser


def main(argv: list[str] | None = None) -> int:
    """``python -m divergulent.classify.ledger``: build / record / report / supersede.

    The single clock read of the ledger stack lives here (in the subcommand
    handlers via :func:`_cli_now`); it is threaded down as ``decided_at`` /
    ``superseded_at`` so every other module stays deterministic.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except LedgerError as exc:
        print('error: %s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
