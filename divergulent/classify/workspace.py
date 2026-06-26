"""The divergulent curation **data root** -- discovery and the path layout.

The curation commands all operate on the same ledger, corpus and cache. Rather
than re-typing those paths, they resolve them from a single **data root**: a
directory marked by a ``.divergulent`` file, holding ``corpus/`` (bodies +
``fingerprints.sqlite`` + ``ledger.sqlite``) and ``cache/``. This matches the
existing layout -- the ledger already lives inside the corpus dir -- so an
existing setup becomes a root just by dropping a marker beside ``corpus/``.

Discovery is ``git``-style: an explicit ``--data`` flag, then ``DIVERGULENT_DATA``,
then walking up from the cwd for the marker, then a lenient fallback (a directory
that directly contains ``corpus/ledger.sqlite``). Failing all of that it raises a
clear, actionable error rather than guessing -- so the forgetful operator never
silently runs against the wrong database.

Curation-side only: no client command imports ``classify/``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The data-root marker file. Its presence anchors discovery; its contents are
# free for future config.
MARKER = '.divergulent'


class WorkspaceNotFound(Exception):
    """No divergulent data root could be resolved (with an actionable message)."""


@dataclass(frozen=True)
class Workspace:
    """A resolved data root and the conventional paths beneath it."""

    root: Path

    @property
    def corpus_dir(self) -> Path:
        return self.root / 'corpus'

    @property
    def ledger(self) -> Path:
        return self.corpus_dir / 'ledger.sqlite'

    @property
    def index(self) -> Path:
        return self.corpus_dir / 'fingerprints.sqlite'

    @property
    def cache_dir(self) -> Path:
        return self.root / 'cache'

    @property
    def marker(self) -> Path:
        return self.root / MARKER

    def ledger_exists(self) -> bool:
        return self.ledger.is_file()


def _has_corpus_ledger(path: Path) -> bool:
    return (path / 'corpus' / 'ledger.sqlite').is_file()


def find(explicit: str | None = None, *, start: str | os.PathLike | None = None,
         environ: dict | None = None) -> Workspace:
    """Resolve the data root, ``git``-style; raise :class:`WorkspaceNotFound` if none.

    Order: ``explicit`` (the ``--data`` flag) → ``DIVERGULENT_DATA`` env → walking
    up from ``start`` (cwd) for the marker → a lenient fallback where ``start``
    directly contains ``corpus/ledger.sqlite``. ``explicit``/env are trusted as
    roots (the caller checks the ledger exists); the walk-up/lenient paths require
    real evidence so an ambiguous cwd does not silently bind to the wrong place.
    """
    if explicit:
        return Workspace(Path(explicit).expanduser().resolve())

    environ = environ if environ is not None else os.environ
    env_root = environ.get('DIVERGULENT_DATA')
    if env_root:
        return Workspace(Path(env_root).expanduser().resolve())

    begin = Path(start).resolve() if start is not None else Path.cwd().resolve()
    for directory in (begin, *begin.parents):
        if (directory / MARKER).is_file():
            return Workspace(directory)
    if _has_corpus_ledger(begin):
        return Workspace(begin)

    raise WorkspaceNotFound(
        'not inside a divergulent data root (no %s marker found walking up from %s).\n'
        '  run "divergulent-classify init" in your data directory, or pass '
        '--data <root> / set DIVERGULENT_DATA.' % (MARKER, begin))


def init(root: str | os.PathLike, *, environ: dict | None = None) -> Workspace:
    """Make ``root`` a data root: write the marker and create ``corpus/``/``cache/``.

    Idempotent -- re-running leaves an existing root untouched. Returns the
    :class:`Workspace`.
    """
    resolved = Path(root).expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    (resolved / 'corpus').mkdir(exist_ok=True)
    (resolved / 'cache').mkdir(exist_ok=True)
    marker = resolved / MARKER
    if not marker.exists():
        marker.write_text('# divergulent data root\n', encoding='utf-8')
    return Workspace(resolved)
