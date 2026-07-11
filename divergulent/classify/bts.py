"""A pinned Debian BTS bug index -- the bug-existence input for phase 6.

The cross-reference tier also verifies the *bug* references a patch declares
(DEP-3 ``Bug-Debian: #NNNNNN``) against Debian's own records: does the bug exist,
is it filed against this source, is it open or done. As with the Security Tracker
this is BULK-FIRST -- a single pinned snapshot under the corpus, never a per-bug
``bugreport.cgi`` fetch across ~60k patches.

The snapshot is a flat ``bug -> (source, status)`` table. Debian's Ultimate Debian
Database (UDD) exports exactly this; the source URL is overridable (like popcon's)
so an operator points it at whatever UDD/BTS bulk export they mirror, and the
parser accepts a tolerant TSV::

    123456<TAB>openssl<TAB>done
    234567<TAB>coreutils<TAB>pending

Unlike a CVE, a bug reference maps to no category, so the BTS check only ever
produces a provenance corroboration/contradiction signal (``cross_reference``),
never a settled category. Curation-side only: no client command imports
``classify/``.
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import os
import shutil
import sqlite3
import tempfile
import urllib.request

from divergulent.http import DEFAULT_USER_AGENT

# The default source is the rolling ``bts`` GitHub prerelease: a gzipped
# ``bug<TAB>source<TAB>status`` TSV rebuilt weekly from UDD (bugs + archived_bugs)
# and served at a stable URL, exactly like the divergence cache and classification
# bundle. Release-independent (a bug is a bug regardless of Debian release). An
# operator can still override ``--url`` to point at their own export -- ``pull``
# accepts a plain TSV or a gzipped one (``file://`` URLs work), and the tests
# inject the download so no network is hit.
BTS_URL = 'https://github.com/shakenfist/divergulent/releases/download/bts/bts-index.tsv.gz'
BTS_FILENAME = 'bts.sqlite'
# BTS statuses that mean the bug is still live (vs closed/done).
OPEN_STATUSES = frozenset({'pending', 'forwarded', 'pending-fixed'})
_FETCH_TIMEOUT = 120


def default_bts_path(corpus_dir: str) -> str:
    """The pinned snapshot's home: ``<corpus_dir>/bts.sqlite``."""
    return os.path.join(corpus_dir, BTS_FILENAME)


def parse_bug_index(text: str) -> list[tuple[int, str, str]]:
    """Parse a ``bug<TAB>source<TAB>status`` export into ``[(bug, source, status)]``.

    Tolerant: skips blank lines, ``#`` comments, and any row whose bug column is
    not an integer, rather than aborting the whole parse. Accepts tabs or runs of
    whitespace as the separator so a hand-mirrored file still loads.
    """
    rows: list[tuple[int, str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        parts = stripped.split('\t') if '\t' in stripped else stripped.split()
        if len(parts) < 3:
            continue
        try:
            bug = int(parts[0])
        except ValueError:
            continue
        rows.append((bug, parts[1], parts[2].lower()))
    return rows


def _download(url: str, dest_path: str) -> None:
    """Fetch ``url`` to ``dest_path`` (injectable seam; default uses urllib)."""
    request = urllib.request.Request(url, headers={'User-Agent': DEFAULT_USER_AGENT})
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT) as response, open(dest_path, 'wb') as out:
        shutil.copyfileobj(response, out)


def _read_maybe_gzip(path: str) -> str:
    """Read ``path`` as text, transparently decompressing a gzipped file.

    The hosted artifact is ``bts-index.tsv.gz``; an operator's own export may be a
    plain TSV. Detect gzip by its ``\\x1f\\x8b`` magic so ``pull`` accepts either
    without the caller having to say which.
    """
    with open(path, 'rb') as handle:
        raw = handle.read()
    if raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    return raw.decode('utf-8', errors='replace')


def write_snapshot(path: str, rows: list[tuple[int, str, str]], *,
                   snapshot_date: str, source_url: str) -> int:
    """Write ``rows`` to a fresh ``bts.sqlite``; return the row count.

    The ``bug`` table is keyed by bug number; ``bts_meta`` records the snapshot
    date, source URL, and row count. Replaces any existing tables (a snapshot is
    wholly superseded, never merged).
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute('DROP TABLE IF EXISTS bug')
        connection.execute(
            'CREATE TABLE bug ('
            'bug INTEGER PRIMARY KEY, source TEXT NOT NULL, status TEXT NOT NULL)')
        connection.executemany(
            'INSERT OR REPLACE INTO bug (bug, source, status) VALUES (?, ?, ?)', rows)
        connection.execute(
            'CREATE TABLE IF NOT EXISTS bts_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        connection.execute('DELETE FROM bts_meta')
        connection.executemany(
            'INSERT INTO bts_meta (key, value) VALUES (?, ?)',
            [('snapshot_date', snapshot_date),
             ('source_url', source_url),
             ('row_count', str(len(rows)))])
        connection.commit()
    finally:
        connection.close()
    return len(rows)


def pull(corpus_dir: str, *, snapshot_date: str | None = None, url: str = BTS_URL,
         download=_download, dest_path: str | None = None) -> tuple[str, int]:
    """Download, parse and pin the BTS bug index; return ``(path, count)``.

    Refuses to write an empty snapshot (a transient bad download must not silently
    replace good data). ``snapshot_date`` defaults to today (UTC); callers pass it
    explicitly for determinism. ``download`` is the injectable fetch seam for tests.
    """
    if snapshot_date is None:
        snapshot_date = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    path = dest_path or default_bts_path(corpus_dir)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with tempfile.NamedTemporaryFile('wb', suffix='.tsv', delete=False) as handle:
        tmp = handle.name
    try:
        download(url, tmp)
        rows = parse_bug_index(_read_maybe_gzip(tmp))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if not rows:
        raise ValueError('BTS bug index from %s parsed to no usable rows' % url)
    write_snapshot(path, rows, snapshot_date=snapshot_date, source_url=url)
    return path, len(rows)


def open_snapshot(path: str) -> sqlite3.Connection:
    """Open a pinned snapshot with a row factory set for readers."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def bug_row(conn: sqlite3.Connection, bug: int) -> sqlite3.Row | None:
    """The ``(source, status)`` row for ``bug``, or ``None`` if no such bug."""
    return conn.execute('SELECT bug, source, status FROM bug WHERE bug = ?', (bug,)).fetchone()


def snapshot_meta(conn: sqlite3.Connection) -> dict[str, str]:
    """The ``bts_meta`` key/value map (snapshot_date, source_url, row_count)."""
    return {row['key']: row['value'] for row in conn.execute('SELECT key, value FROM bts_meta')}


def main(argv=None) -> int:
    """``python -m divergulent.classify.bts <corpus_dir> --url URL`` -- pull a snapshot."""
    parser = argparse.ArgumentParser(
        prog='divergulent.classify.bts',
        description='Download and pin a Debian BTS bug index for the phase-6 cross-reference.')
    parser.add_argument('corpus_dir', help='corpus directory; the snapshot is written to bts.sqlite there')
    parser.add_argument('--url', default=BTS_URL, help='bug-index export URL (default: %(default)s)')
    parser.add_argument('--date', default=None, help='snapshot date to pin (default: today, UTC)')
    args = parser.parse_args(argv)
    path, count = pull(args.corpus_dir, snapshot_date=args.date, url=args.url)
    conn = open_snapshot(path)
    try:
        meta = snapshot_meta(conn)
    finally:
        conn.close()
    print('BTS bug index pinned: %s' % path)
    print('  date %s   rows %s' % (meta.get('snapshot_date'), count))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
