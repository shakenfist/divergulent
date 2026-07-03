"""Serialise the classification ledger to canonical JSONL, and back.

The ledger (``corpus/ledger.sqlite``) is the append-only SOURCE OF TRUTH for the
patch classification: it holds irreproducible human verdicts and verified LLM
triage that CI can never regenerate.  To publish a bundle, CI needs that ledger --
but a sqlite file is the wrong thing to commit to git: it is binary (unreviewable
diffs, unmergeable) and bloats history (its pages do not delta-compress).

So the durable, CI-visible form of the ledger is a TEXT export: one JSON object
per line (JSONL), full fidelity, stably ordered so the same ledger always yields
byte-identical output.  The operator commits that file; its diff is a *reviewable*
"here are the verdicts I just added" -- the human-in-the-loop publish gate.  CI
imports it back into a throwaway sqlite and builds the signed bundle from there,
reusing the existing verdict-derivation code unchanged.

Round-trip is the trust anchor: ``import(export(L))`` reproduces ``L`` row for row
(ids preserved, so verdict precedence -- which tie-breaks on ``decision.id`` -- is
identical), and re-export is byte-identical.  See
docs/plans/PLAN-patch-classification-phase-05-bundle.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from typing import Iterable, Iterator

from divergulent.classify import ledger as ledger_mod

# Bump when the JSONL envelope (not the ledger schema) changes shape.
EXPORT_SCHEMA_VERSION = 1

# Every table dumped, in a FIXED order, so two exports of one ledger match byte
# for byte.  ``note`` is optional in old ledgers; a missing table is skipped on
# export and simply recreated empty on import (no notes == an empty note table).
_EXPORT_TABLES: tuple[str, ...] = (
    'meta', 'rule', 'decision', 'observation', 'review_queue', 'note')

# The ORDER BY that makes each table's row order deterministic.  Every table has
# a stable key: ``meta`` by its PK, the append-only tables by their monotonic id,
# ``rule`` by its (rule_id, version) PK.
_ORDER_BY: dict[str, str] = {
    'meta': 'key',
    'rule': 'rule_id, version',
    'decision': 'id',
    'observation': 'id',
    'review_queue': 'id',
    'note': 'id',
}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,)).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute('PRAGMA table_info(%s)' % table)]


def _dumps(obj: dict) -> str:
    """Compact, key-sorted JSON -- the canonical form both lines use."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def export_ledger(conn: sqlite3.Connection) -> Iterator[str]:
    """Yield the ledger as canonical JSONL lines (no trailing newlines).

    The first line is a header ``{"export_schema": N}``; every subsequent line is
    ``{"table": <name>, "row": {<column>: <value>, ...}}``.  Rows carry every
    column verbatim (ids included), keyed by column name so the on-disk column
    order is irrelevant.  Deterministic: fixed table order, per-table ORDER BY,
    and key-sorted JSON.
    """
    yield _dumps({'export_schema': EXPORT_SCHEMA_VERSION})
    for table in _EXPORT_TABLES:
        if not _table_exists(conn, table):
            continue
        columns = _columns(conn, table)
        query = 'SELECT * FROM %s ORDER BY %s' % (table, _ORDER_BY[table])
        for row in conn.execute(query):
            record = {col: row[idx] for idx, col in enumerate(columns)}
            yield _dumps({'table': table, 'row': record})


def write_export(conn: sqlite3.Connection, path: str) -> int:
    """Write :func:`export_ledger` to ``path``; return the line count.

    Newline-terminated lines, written atomically-ish via a temp sibling then
    rename, so a committed export is never a half-written file.
    """
    tmp = path + '.tmp'
    count = 0
    with open(tmp, 'w', encoding='utf-8') as handle:
        for line in export_ledger(conn):
            handle.write(line)
            handle.write('\n')
            count += 1
    os.replace(tmp, path)
    return count


def import_ledger(lines: Iterable[str], dest_path: str) -> sqlite3.Connection:
    """Rebuild a ledger sqlite at ``dest_path`` from exported JSONL ``lines``.

    Overwrites any file at ``dest_path`` (the reconstruction is authoritative),
    recreates the schema with no seed rows (:func:`ledger.create_schema`), then
    inserts every exported row verbatim -- so ids, verified flags and evidence are
    preserved and the derived verdict is identical to the source ledger's.
    Returns an open connection (caller closes).
    """
    if os.path.exists(dest_path):
        os.unlink(dest_path)
    directory = os.path.dirname(dest_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(dest_path)
    ledger_mod.create_schema(conn)

    seen_header = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        obj = json.loads(line)
        if 'export_schema' in obj:
            version = obj['export_schema']
            if version != EXPORT_SCHEMA_VERSION:
                conn.close()
                raise ValueError(
                    'unsupported export schema %r (this build reads %d)'
                    % (version, EXPORT_SCHEMA_VERSION))
            seen_header = True
            continue
        table, row = obj['table'], obj['row']
        columns = list(row.keys())
        placeholders = ', '.join('?' for _ in columns)
        conn.execute(
            'INSERT INTO %s (%s) VALUES (%s)'
            % (table, ', '.join(columns), placeholders),
            [row[col] for col in columns])

    if not seen_header:
        conn.close()
        raise ValueError('not a divergulent ledger export (no header line)')

    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def load_export(export_path: str, dest_path: str) -> sqlite3.Connection:
    """Import the JSONL at ``export_path`` into a ledger at ``dest_path``."""
    with open(export_path, 'r', encoding='utf-8') as handle:
        return import_ledger(handle, dest_path)


def _default_export_path(ledger_path: str) -> str:
    """``…/ledger.sqlite`` -> ``…/ledger.jsonl`` (the committed sibling)."""
    base, _ext = os.path.splitext(ledger_path)
    return base + '.jsonl'


def main(argv: list[str] | None = None) -> int:
    """``export <ledger> [--output PATH]`` / ``import <input> --ledger PATH``."""
    parser = argparse.ArgumentParser(
        prog='python -m divergulent.classify.export',
        description='Serialise the classification ledger to canonical JSONL and back.')
    sub = parser.add_subparsers(dest='mode', required=True)

    export_parser = sub.add_parser('export', help='ledger.sqlite -> ledger.jsonl')
    export_parser.add_argument('ledger', help='path to the ledger sqlite')
    export_parser.add_argument('--output', default=None,
                               help='JSONL output (default: the ledger with a .jsonl suffix)')

    import_parser = sub.add_parser('import', help='ledger.jsonl -> ledger.sqlite')
    import_parser.add_argument('input', help='path to the exported JSONL')
    import_parser.add_argument('--ledger', required=True, help='ledger sqlite to (re)build')

    args = parser.parse_args(argv)

    if args.mode == 'export':
        try:
            conn = ledger_mod.open_ledger(args.ledger)
        except ledger_mod.LedgerError as exc:
            print('error: %s' % exc)
            return 2
        try:
            output = args.output or _default_export_path(args.ledger)
            count = write_export(conn, output)
        finally:
            conn.close()
        print('exported %d rows -> %s' % (count - 1, output))  # -1: the header line
        return 0

    # import
    conn = load_export(args.input, args.ledger)
    conn.close()
    print('imported %s -> %s' % (args.input, args.ledger))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
