"""A pinned Debian popcon snapshot -- the install-base input for the reach axis.

The reach axis (``reach.py``) turns "how many machines run this" into a t-shirt
size. The raw signal is Debian popcon's ``by_inst`` table: one row per BINARY
package with install/vote counts. This module fetches that table once, parses it,
and pins it into a small ``popcon.sqlite`` under the corpus directory so the
deterministic ``record`` pass can resolve a source's binaries to install counts
offline, with the snapshot date recorded for auditability.

The snapshot is deliberately SEPARATE from the rebuilt-from-corpus index
(``fingerprints.sqlite``, which ``measure.write_index`` unlinks and recreates):
popcon is refreshed on its own slow cadence and must survive a corpus rebuild.

``by_inst`` format (comments start with ``#``; a trailing ``----`` divider and a
``Total`` summary row close the file)::

    #rank name                 inst  vote   old recent no-files (maintainer)
    1     debconf            279046 263215  1421 14383    27 (Debconf Developers)
    ...
    218269 Total          390445941 ...

The ``Total`` row's ``inst`` is the population SUM, so it is skipped explicitly --
it would otherwise poison the ``max(inst)`` anchor the reach fractions divide by.

Curation-side only: no client command imports ``classify/``. ``inst`` is the
default count (vote is parsed and stored too, for the reach ``vote`` flag).
"""
from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sqlite3
import tempfile
import urllib.request

from divergulent.http import DEFAULT_USER_AGENT

# popcon publishes the by_inst table at a stable URL. Overridable for a mirror or
# a hand-hosted snapshot, mirroring the cache-bundle URL override.
POPCON_URL = 'https://popcon.debian.org/by_inst'
POPCON_FILENAME = 'popcon.sqlite'
_FETCH_TIMEOUT = 60


def default_popcon_path(corpus_dir: str) -> str:
    """The pinned snapshot's home: ``<corpus_dir>/popcon.sqlite``."""
    return os.path.join(corpus_dir, POPCON_FILENAME)


def parse_by_inst(text: str) -> list[tuple[str, int, int]]:
    """Parse a ``by_inst`` table into ``[(binary, inst, vote), ...]``.

    Skips the comment header, the trailing ``----`` divider, and the ``Total``
    summary row (its ``inst`` is the population sum and would become a bogus
    anchor). A row whose count columns are not integers is skipped rather than
    aborting the whole parse.
    """
    rows: list[tuple[str, int, int]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or stripped.startswith('-'):
            continue
        parts = stripped.split()
        # rank name inst vote old recent no-files (maintainer...)
        if len(parts) < 7:
            continue
        name = parts[1]
        if name == 'Total':
            continue
        try:
            inst = int(parts[2])
            vote = int(parts[3])
        except ValueError:
            continue
        rows.append((name, inst, vote))
    return rows


def _download(url: str, dest_path: str) -> None:
    """Fetch ``url`` to ``dest_path`` (injectable seam; default uses urllib)."""
    request = urllib.request.Request(url, headers={'User-Agent': DEFAULT_USER_AGENT})
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT) as response, open(dest_path, 'wb') as out:
        shutil.copyfileobj(response, out)


def write_snapshot(path: str, rows: list[tuple[str, int, int]], *,
                   snapshot_date: str, source_url: str) -> int:
    """Write ``rows`` to a fresh ``popcon.sqlite`` at ``path``; return the anchor.

    The ``popcon`` table is keyed by binary name; ``popcon_meta`` records the
    snapshot date, source URL, the ``max(inst)`` anchor, and the row count, so a
    reader can date the data and divide reach fractions by the anchor without a
    rescan. Replaces any existing table (a snapshot is wholly superseded, never
    merged).
    """
    anchor = max((inst for _, inst, _ in rows), default=0)
    connection = sqlite3.connect(path)
    try:
        connection.execute('DROP TABLE IF EXISTS popcon')
        connection.execute(
            'CREATE TABLE popcon ('
            'binary TEXT PRIMARY KEY, inst INTEGER NOT NULL, vote INTEGER NOT NULL)')
        connection.executemany(
            'INSERT OR REPLACE INTO popcon (binary, inst, vote) VALUES (?, ?, ?)', rows)
        connection.execute(
            'CREATE TABLE IF NOT EXISTS popcon_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        connection.execute('DELETE FROM popcon_meta')
        connection.executemany(
            'INSERT INTO popcon_meta (key, value) VALUES (?, ?)',
            [('snapshot_date', snapshot_date),
             ('source_url', source_url),
             ('anchor_inst', str(anchor)),
             ('row_count', str(len(rows)))])
        connection.commit()
    finally:
        connection.close()
    return anchor


def pull(corpus_dir: str, *, snapshot_date: str | None = None, url: str = POPCON_URL,
         download=_download, dest_path: str | None = None) -> tuple[str, int]:
    """Download, parse and pin the popcon snapshot; return ``(path, anchor)``.

    Refuses to write an empty or anchorless snapshot (a transient bad download must
    not silently replace good data). ``snapshot_date`` defaults to today (UTC);
    callers pass it explicitly for determinism. ``download`` is the injectable
    fetch seam used by tests.
    """
    if snapshot_date is None:
        snapshot_date = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    path = dest_path or default_popcon_path(corpus_dir)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with tempfile.NamedTemporaryFile('w+', suffix='.by_inst', delete=False) as handle:
        tmp = handle.name
    try:
        download(url, tmp)
        with open(tmp, 'r', encoding='utf-8', errors='replace') as handle:
            rows = parse_by_inst(handle.read())
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if not rows or max(inst for _, inst, _ in rows) <= 0:
        raise ValueError('popcon snapshot from %s parsed to no usable rows' % url)
    anchor = write_snapshot(path, rows, snapshot_date=snapshot_date, source_url=url)
    return path, anchor


def open_snapshot(path: str) -> sqlite3.Connection:
    """Open a pinned snapshot read-only-ish (row factory set for readers)."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def installs_by_binary(conn: sqlite3.Connection, *, column: str = 'inst') -> dict[str, int]:
    """``{binary: count}`` from the snapshot. ``column`` is ``inst`` (default) or
    ``vote`` (the "recently used" signal, behind the reach ``vote`` flag)."""
    if column not in ('inst', 'vote'):
        raise ValueError('unknown popcon column: %r' % column)
    return {row['binary']: row[column]
            for row in conn.execute('SELECT binary, %s FROM popcon' % column)}


def anchor_inst(conn: sqlite3.Connection) -> int:
    """The snapshot's ``max(inst)`` anchor (the near-universal base package)."""
    row = conn.execute("SELECT value FROM popcon_meta WHERE key = 'anchor_inst'").fetchone()
    return int(row['value']) if row else 0


def snapshot_meta(conn: sqlite3.Connection) -> dict[str, str]:
    """The ``popcon_meta`` key/value map (snapshot_date, source_url, anchor, ...)."""
    return {row['key']: row['value'] for row in conn.execute('SELECT key, value FROM popcon_meta')}


def main(argv=None) -> int:
    """``python -m divergulent.classify.popcon <corpus_dir>`` -- pull a snapshot."""
    parser = argparse.ArgumentParser(
        prog='divergulent.classify.popcon',
        description='Download and pin a Debian popcon snapshot for the reach axis.')
    parser.add_argument('corpus_dir', help='corpus directory; the snapshot is written to popcon.sqlite there')
    parser.add_argument('--url', default=POPCON_URL, help='by_inst URL (default: %(default)s)')
    parser.add_argument('--date', default=None, help='snapshot date to pin (default: today, UTC)')
    args = parser.parse_args(argv)
    path, anchor = pull(args.corpus_dir, snapshot_date=args.date, url=args.url)
    conn = open_snapshot(path)
    try:
        meta = snapshot_meta(conn)
    finally:
        conn.close()
    print('popcon snapshot pinned: %s' % path)
    print('  date %s   rows %s   anchor inst %s' % (
        meta.get('snapshot_date'), meta.get('row_count'), anchor))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
