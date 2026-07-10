"""A pinned Debian Security Tracker snapshot -- the CVE input for phase 6.

The cross-reference tier (``cross_reference.py``) verifies the CVE references a
patch *claims* in its header against Debian's own records rather than trusting
them. The raw signal is the Security Tracker's machine-readable JSON: a map of
SOURCE package -> CVE id -> per-release status. This module fetches that document
once, flattens it to a small ``security_tracker.sqlite`` under the corpus
directory, and pins the snapshot date, so the deterministic ``record`` pass can
look a claimed ``(source, cve)`` up offline -- bulk-first, never per-patch network.

Like ``popcon.sqlite`` the snapshot is deliberately SEPARATE from the
rebuilt-from-corpus index: it is refreshed on its own slow cadence and must
survive a corpus rebuild. Curation-side only -- no client command imports
``classify/``.

The tracker JSON is shaped::

    {
      "<source>": {
        "CVE-YYYY-NNNN": {
          "description": "...",
          "debianbug": 123456,                       # optional
          "releases": {
            "trixie": {"status": "resolved",
                       "fixed_version": "1.2-3",
                       "urgency": "high", ...},
            "bookworm": {"status": "open", ...},
            ...
          }
        },
        ...
      },
      ...
    }

A CVE is collapsed to one ``(source, cve, status, fixed_version)`` row against a
target release (default ``trixie``): the target-release entry when present, else
an aggregate across releases (a resolved+fixed entry is preferred, so a CVE fixed
in *some* Debian release still reads as corroboration).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sqlite3
import tempfile
import urllib.request

from divergulent.http import DEFAULT_USER_AGENT

# The Security Tracker publishes the whole database as one JSON document at a
# stable URL. Overridable for a mirror or a hand-hosted snapshot, like popcon.
SECURITY_TRACKER_URL = 'https://security-tracker.debian.org/tracker/data/json'
SECURITY_TRACKER_FILENAME = 'security_tracker.sqlite'
DEFAULT_RELEASE = 'trixie'
_FETCH_TIMEOUT = 120


def default_security_tracker_path(corpus_dir: str) -> str:
    """The pinned snapshot's home: ``<corpus_dir>/security_tracker.sqlite``."""
    return os.path.join(corpus_dir, SECURITY_TRACKER_FILENAME)


def _collapse_releases(releases: object, release: str) -> tuple[str, str | None]:
    """Collapse a CVE's per-release map to one ``(status, fixed_version)``.

    Prefers the target ``release``; otherwise aggregates -- a ``resolved`` entry
    (ideally one carrying a ``fixed_version``) wins over ``open`` wins over the
    fallback ``undetermined``. Returns ``('undetermined', None)`` for a malformed
    or empty map rather than raising.
    """
    if not isinstance(releases, dict):
        return 'undetermined', None
    target = releases.get(release)
    if isinstance(target, dict):
        status = target.get('status') or 'undetermined'
        fixed = target.get('fixed_version') or None
        return str(status), (str(fixed) if fixed else None)
    # No entry for the target release: aggregate across whatever is present.
    best_status = 'undetermined'
    best_fixed: str | None = None
    rank = {'undetermined': 0, 'open': 1, 'resolved': 2}
    for entry in releases.values():
        if not isinstance(entry, dict):
            continue
        status = str(entry.get('status') or 'undetermined')
        fixed = entry.get('fixed_version') or None
        if rank.get(status, 0) > rank.get(best_status, 0) or (
                status == best_status and best_fixed is None and fixed):
            best_status = status
            best_fixed = str(fixed) if fixed else best_fixed
    return best_status, best_fixed


def parse_tracker_json(data: object, *, release: str = DEFAULT_RELEASE) -> list[tuple[str, str, str, str | None]]:
    """Flatten the tracker document into ``[(source, cve, status, fixed_version)]``.

    Silently skips malformed sources/entries (a bad island must not abort the whole
    parse). CVE ids are upper-cased so a claim lookup is case-insensitive.
    """
    rows: list[tuple[str, str, str, str | None]] = []
    if not isinstance(data, dict):
        return rows
    for source, cves in data.items():
        if not isinstance(source, str) or not isinstance(cves, dict):
            continue
        for cve, entry in cves.items():
            if not isinstance(cve, str) or not cve.upper().startswith('CVE-'):
                continue
            if not isinstance(entry, dict):
                continue
            status, fixed = _collapse_releases(entry.get('releases'), release)
            rows.append((source, cve.upper(), status, fixed))
    return rows


def _download(url: str, dest_path: str) -> None:
    """Fetch ``url`` to ``dest_path`` (injectable seam; default uses urllib)."""
    request = urllib.request.Request(url, headers={'User-Agent': DEFAULT_USER_AGENT})
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT) as response, open(dest_path, 'wb') as out:
        shutil.copyfileobj(response, out)


def write_snapshot(path: str, rows: list[tuple[str, str, str, str | None]], *,
                   snapshot_date: str, source_url: str, release: str) -> int:
    """Write ``rows`` to a fresh ``security_tracker.sqlite``; return the row count.

    The ``cve`` table is keyed by ``(source, cve)``; ``tracker_meta`` records the
    snapshot date, source URL, target release, and row count so a reader can date
    the data and know which release the collapsed status refers to. Replaces any
    existing tables (a snapshot is wholly superseded, never merged).
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute('DROP TABLE IF EXISTS cve')
        connection.execute(
            'CREATE TABLE cve ('
            'source TEXT NOT NULL, cve TEXT NOT NULL, status TEXT NOT NULL, '
            'fixed_version TEXT, PRIMARY KEY (source, cve))')
        connection.executemany(
            'INSERT OR REPLACE INTO cve (source, cve, status, fixed_version) VALUES (?, ?, ?, ?)', rows)
        connection.execute('CREATE INDEX cve_by_id ON cve (cve)')
        connection.execute(
            'CREATE TABLE IF NOT EXISTS tracker_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        connection.execute('DELETE FROM tracker_meta')
        connection.executemany(
            'INSERT INTO tracker_meta (key, value) VALUES (?, ?)',
            [('snapshot_date', snapshot_date),
             ('source_url', source_url),
             ('release', release),
             ('row_count', str(len(rows)))])
        connection.commit()
    finally:
        connection.close()
    return len(rows)


def pull(corpus_dir: str, *, snapshot_date: str | None = None, url: str = SECURITY_TRACKER_URL,
         release: str = DEFAULT_RELEASE, download=_download, dest_path: str | None = None) -> tuple[str, int]:
    """Download, parse and pin the Security Tracker snapshot; return ``(path, count)``.

    Refuses to write an empty snapshot (a transient bad download must not silently
    replace good data). ``snapshot_date`` defaults to today (UTC); callers pass it
    explicitly for determinism. ``download`` is the injectable fetch seam for tests.
    """
    if snapshot_date is None:
        snapshot_date = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    path = dest_path or default_security_tracker_path(corpus_dir)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with tempfile.NamedTemporaryFile('w+', suffix='.json', delete=False) as handle:
        tmp = handle.name
    try:
        download(url, tmp)
        with open(tmp, 'r', encoding='utf-8', errors='replace') as handle:
            data = json.load(handle)
        rows = parse_tracker_json(data, release=release)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if not rows:
        raise ValueError('security tracker snapshot from %s parsed to no usable rows' % url)
    write_snapshot(path, rows, snapshot_date=snapshot_date, source_url=url, release=release)
    return path, len(rows)


def open_snapshot(path: str) -> sqlite3.Connection:
    """Open a pinned snapshot with a row factory set for readers."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def cve_row(conn: sqlite3.Connection, source: str, cve: str) -> sqlite3.Row | None:
    """The ``(status, fixed_version)`` row for ``(source, cve)``, or ``None``.

    This is the corroboration test: a hit means the tracker records this CVE
    against *this* source package.
    """
    return conn.execute(
        'SELECT source, cve, status, fixed_version FROM cve WHERE source = ? AND cve = ?',
        (source, cve.upper())).fetchone()


def cve_exists(conn: sqlite3.Connection, cve: str) -> bool:
    """Whether ``cve`` is recorded against *any* source.

    Distinguishes "exists but for a different package" (a plausible cross-package
    fix) from "no such CVE" (a malformed or invented id) in the contradiction path.
    """
    return conn.execute('SELECT 1 FROM cve WHERE cve = ? LIMIT 1', (cve.upper(),)).fetchone() is not None


def snapshot_meta(conn: sqlite3.Connection) -> dict[str, str]:
    """The ``tracker_meta`` key/value map (snapshot_date, source_url, release, ...)."""
    return {row['key']: row['value'] for row in conn.execute('SELECT key, value FROM tracker_meta')}


def main(argv=None) -> int:
    """``python -m divergulent.classify.security_tracker <corpus_dir>`` -- pull a snapshot."""
    parser = argparse.ArgumentParser(
        prog='divergulent.classify.security_tracker',
        description='Download and pin a Debian Security Tracker snapshot for the phase-6 cross-reference.')
    parser.add_argument('corpus_dir',
                        help='corpus directory; the snapshot is written to security_tracker.sqlite there')
    parser.add_argument('--url', default=SECURITY_TRACKER_URL, help='tracker JSON URL (default: %(default)s)')
    parser.add_argument('--release', default=DEFAULT_RELEASE,
                        help='release whose status to pin (default: %(default)s)')
    parser.add_argument('--date', default=None, help='snapshot date to pin (default: today, UTC)')
    args = parser.parse_args(argv)
    path, count = pull(args.corpus_dir, snapshot_date=args.date, url=args.url, release=args.release)
    conn = open_snapshot(path)
    try:
        meta = snapshot_meta(conn)
    finally:
        conn.close()
    print('security tracker snapshot pinned: %s' % path)
    print('  date %s   release %s   rows %s' % (
        meta.get('snapshot_date'), meta.get('release'), count))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
