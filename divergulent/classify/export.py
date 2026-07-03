"""Serialise the classification ledger to a sharded, compact JSONL export, and back.

The ledger (``corpus/ledger.sqlite``) is the append-only SOURCE OF TRUTH for the
patch classification: it holds irreproducible human verdicts and verified LLM
triage that CI can never regenerate.  To publish a bundle, CI needs that ledger --
but a sqlite file is the wrong thing to commit to git: it is binary (unreviewable
diffs, unmergeable) and bloats history (its pages do not delta-compress).

So the durable, CI-visible form of the ledger is a TEXT export the operator
commits; its diff is a *reviewable* "here are the verdicts I just added" -- the
human-in-the-loop publish gate.

The export is a **directory** (default ``<data-root>/ledger/``), not one file,
because the ledger is append-only and grows without bound (every re-triage or
re-review supersedes rows but keeps them), so any single file would eventually
cross GitHub's 100 MB per-file limit.  The layout:

  ledger/manifest.json              # {export_schema, shards, rows}
  ledger/decision-<YYYY-MM>.jsonl   # the big append-only logs, sharded by the
  ledger/observation-<YYYY-MM>.jsonl #   calendar month of the row's timestamp
  ledger/review_queue.jsonl         # the small, bounded tables, whole
  ledger/rule.jsonl / note.jsonl / meta.jsonl

Sharding the two big tables by month bounds every file: new work appends to the
current month (clean diffs), and a supersession is a small edit to one old shard.
Rows are **compact** -- null columns are omitted (import restores them from the
schema defaults, all nullable → NULL), so a row carries only what it asserts.
Raw LLM/verification evidence stays inline (it is ~25% of the bytes and a
re-triage burst still fits within one month's file); it can be split to a
content-addressed store later if a single month ever gets fat.

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
from typing import Iterator

from divergulent.classify import ledger as ledger_mod

# Bump when the export layout (not the ledger schema) changes shape.  v2 is the
# sharded-directory format; v1 was a single ledger.jsonl.
EXPORT_SCHEMA_VERSION = 2

MANIFEST_NAME = 'manifest.json'

# Every table, in a FIXED order.  The two big append-only logs are sharded by
# month; the rest (small, bounded) are written whole.  ``note`` is optional in
# old ledgers; a missing table is simply skipped on export.
_ALL_TABLES: tuple[str, ...] = (
    'meta', 'rule', 'decision', 'observation', 'review_queue', 'note')
_SHARDED_TABLES: frozenset[str] = frozenset({'decision', 'observation'})

# The timestamp column whose calendar month buckets a sharded row.
_SHARD_COL: dict[str, str] = {'decision': 'decided_at', 'observation': 'observed_at'}
_UNDATED = 'undated'

# The ORDER BY that makes each table's row order deterministic.
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
    """Compact, key-sorted JSON -- the canonical form every line uses."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _compact(row: dict) -> dict:
    """Drop null-valued columns; import restores them from the schema defaults.

    Every nullable column defaults to NULL, so omitting a NULL value is lossless:
    a re-INSERT that names only the non-null columns yields the same row back.
    """
    return {key: value for key, value in row.items() if value is not None}


def _month(timestamp) -> str:
    """``'2026-07-03T…'`` -> ``'2026-07'``; a null/odd timestamp -> ``'undated'``."""
    if isinstance(timestamp, str) and len(timestamp) >= 7 and timestamp[4] == '-':
        return timestamp[:7]
    return _UNDATED


def _shard_name(table: str, suffix: str | None) -> str:
    return '%s-%s.jsonl' % (table, suffix) if suffix else '%s.jsonl' % table


def _table_of(shard_filename: str) -> str:
    """The table a shard file belongs to, from its name (the inverse of naming)."""
    stem = shard_filename[:-len('.jsonl')] if shard_filename.endswith('.jsonl') else shard_filename
    for table in _ALL_TABLES:
        if stem == table or stem.startswith(table + '-'):
            return table
    raise ValueError('shard %r does not map to a known table' % shard_filename)


def export_shards(conn: sqlite3.Connection) -> Iterator[tuple[str, list[str]]]:
    """Yield ``(shard_filename, [json_line, ...])`` for the whole ledger.

    Sharded tables split by the calendar month of their timestamp; whole tables
    get one file.  Deterministic: fixed table order, id/key ordering within a
    table, shard names sorted, and compact key-sorted JSON per row.
    """
    shards: dict[str, list[str]] = {}
    for table in _ALL_TABLES:
        if not _table_exists(conn, table):
            continue
        columns = _columns(conn, table)
        query = 'SELECT * FROM %s ORDER BY %s' % (table, _ORDER_BY[table])
        for row in conn.execute(query):
            record = {col: row[idx] for idx, col in enumerate(columns)}
            suffix = _month(record.get(_SHARD_COL[table])) if table in _SHARDED_TABLES else None
            shards.setdefault(_shard_name(table, suffix), []).append(_dumps(_compact(record)))
    for name in sorted(shards):
        yield name, shards[name]


def _write_lines(path: str, lines: list[str]) -> None:
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        for line in lines:
            handle.write(line)
            handle.write('\n')
    os.replace(tmp, path)


def _write_text(path: str, text: str) -> None:
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        handle.write(text)
    os.replace(tmp, path)


def write_export(conn: sqlite3.Connection, dest_dir: str) -> dict:
    """Write the sharded export into ``dest_dir``; return the manifest.

    Clears any prior shard files + manifest first (the export is authoritative, so
    a month that lost all its rows never lingers), writes each shard atomically,
    then a ``manifest.json`` listing them.
    """
    os.makedirs(dest_dir, exist_ok=True)
    for existing in os.listdir(dest_dir):
        if existing.endswith('.jsonl') or existing == MANIFEST_NAME:
            os.remove(os.path.join(dest_dir, existing))

    names: list[str] = []
    total = 0
    for name, lines in export_shards(conn):
        names.append(name)
        total += len(lines)
        _write_lines(os.path.join(dest_dir, name), lines)

    manifest = {'export_schema': EXPORT_SCHEMA_VERSION, 'shards': names, 'rows': total}
    _write_text(os.path.join(dest_dir, MANIFEST_NAME), _dumps(manifest) + '\n')
    return manifest


def load_export(export_dir: str, dest_path: str) -> sqlite3.Connection:
    """Rebuild a ledger sqlite at ``dest_path`` from a sharded export directory.

    Overwrites any file at ``dest_path`` (the reconstruction is authoritative),
    recreates the schema with no seed rows (:func:`ledger.create_schema`), then
    inserts every exported row verbatim -- so ids, verified flags and evidence are
    preserved and the derived verdict is identical to the source ledger's.
    Returns an open connection (caller closes).
    """
    manifest_path = os.path.join(export_dir, MANIFEST_NAME)
    if not os.path.exists(manifest_path):
        raise ValueError(
            'no %s in %r (not a divergulent ledger export)' % (MANIFEST_NAME, export_dir))
    with open(manifest_path, encoding='utf-8') as handle:
        manifest = json.load(handle)
    version = manifest.get('export_schema')
    if version != EXPORT_SCHEMA_VERSION:
        raise ValueError(
            'unsupported export schema %r (this build reads %d)'
            % (version, EXPORT_SCHEMA_VERSION))

    if os.path.exists(dest_path):
        os.unlink(dest_path)
    directory = os.path.dirname(dest_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(dest_path)
    ledger_mod.create_schema(conn)

    for name in manifest['shards']:
        table = _table_of(name)
        with open(os.path.join(export_dir, name), encoding='utf-8') as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                record = json.loads(line)
                columns = list(record.keys())
                placeholders = ', '.join('?' for _ in columns)
                conn.execute(
                    'INSERT INTO %s (%s) VALUES (%s)'
                    % (table, ', '.join(columns), placeholders),
                    [record[col] for col in columns])

    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def _default_export_dir(ledger_path: str) -> str:
    """``…/corpus/ledger.sqlite`` -> ``…/corpus/ledger`` (the export directory)."""
    directory = os.path.dirname(ledger_path) or '.'
    return os.path.join(directory, 'ledger')


def main(argv: list[str] | None = None) -> int:
    """``export <ledger> [--output DIR]`` / ``import <dir> --ledger PATH``."""
    parser = argparse.ArgumentParser(
        prog='python -m divergulent.classify.export',
        description='Serialise the classification ledger to a sharded JSONL export and back.')
    sub = parser.add_subparsers(dest='mode', required=True)

    export_parser = sub.add_parser('export', help='ledger.sqlite -> ledger/ (sharded JSONL)')
    export_parser.add_argument('ledger', help='path to the ledger sqlite')
    export_parser.add_argument('--output', default=None,
                               help='export directory (default: a "ledger" dir beside the sqlite)')

    import_parser = sub.add_parser('import', help='ledger/ (sharded JSONL) -> ledger.sqlite')
    import_parser.add_argument('input', help='path to the exported directory')
    import_parser.add_argument('--ledger', required=True, help='ledger sqlite to (re)build')

    args = parser.parse_args(argv)

    if args.mode == 'export':
        try:
            conn = ledger_mod.open_ledger(args.ledger)
        except ledger_mod.LedgerError as exc:
            print('error: %s' % exc)
            return 2
        try:
            output = args.output or _default_export_dir(args.ledger)
            manifest = write_export(conn, output)
        finally:
            conn.close()
        print('exported %d rows across %d shards -> %s'
              % (manifest['rows'], len(manifest['shards']), output))
        return 0

    # import
    conn = load_export(args.input, args.ledger)
    conn.close()
    print('imported %s -> %s' % (args.input, args.ledger))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
