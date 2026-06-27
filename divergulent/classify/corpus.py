"""Central, resumable corpus of raw carried-patch bodies (curation-side only).

This is the acquisition half of phase 1: it crawls a work-list of
``(source_package, version)`` pairs, fetches the FULL ``debian/patches`` series
for each via the apt-source lean fetch (``.dsc`` + ``.debian.tar.*`` only, never
the ``.orig``), and lays the raw bodies down in a content-addressed store with
two append-only manifests. It does NO fingerprinting or classification -- step
1c reads this corpus OFFLINE, so re-normalising never re-crawls the archive.

Layout under ``<corpus_dir>``::

    bodies/<sha[:2]>/<sha>   one file per DISTINCT raw body (sha256 of utf-8)
    patches.jsonl            one row per (package, version, patch)
    packages.jsonl           one row per processed (package, version)

The store is content-addressed by the RAW body sha256, so identical bodies
dedup for free (a blob is written once and never rewritten). ``patches.jsonl``
keeps full provenance -- every (package, version, patch) row, even when several
share a blob. ``packages.jsonl`` is the HONEST ACCOUNTING: non-quilt skips
(native / ``1.0``) and fetch failures are RECORDED with a state and an error
string, never silently dropped.

Resumability: on start ``build_corpus`` reads ``packages.jsonl`` and skips any
``(package, version)`` already recorded, so an interrupted crawl resumes without
duplicating work. Concurrency: a bounded ``ThreadPoolExecutor`` fans out the
fetches; manifest appends are serialised under a lock since the worker threads
share the two files.

This module is builder-only -- no client command imports it -- and keeps all
network/subprocess work out of import time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from divergulent.sources.apt_patches import deb_src_available, fetch_source_details

# Errors that are TRANSIENT (a network/DNS blip or an unresolved download), not
# a terminal outcome. A package recorded with one of these is retried on resume
# rather than treated as done, so a blip is never baked into the corpus.
_TRANSIENT_ERROR_PREFIXES = ('fetch-failed', 'fetch-error')


# A fetch returns ``(source_format, texts, changelog_date)`` like
# ``fetch_source_details``: texts is ``{name: raw_text}`` (patched), ``{}`` (clean
# quilt) or ``None`` (native / non-quilt / unresolved, disambiguated by
# source_format); changelog_date is the package's last-upload date (ISO) or None.
FetchResult = tuple["str | None", "dict[str, str] | None", "str | None"]
Fetch = Callable[[str, str], FetchResult]


@dataclass
class CorpusStats:
    """Counts summarising a ``build_corpus`` run (this run only, not the store)."""

    packages_processed: int = 0
    patched: int = 0
    clean: int = 0
    native: int = 0
    unknown: int = 0
    fetch_failures: int = 0
    patch_rows_written: int = 0
    distinct_bodies: int = 0


def _is_transient_failure(error: "str | None") -> bool:
    """True if a package-row error is a transient fetch failure (retryable)."""
    error = error or ''
    return any(error.startswith(prefix) for prefix in _TRANSIENT_ERROR_PREFIXES)


def _with_retries(call: Callable[[], FetchResult], *, attempts: int = 3,
                  backoff: float = 1.5, sleep: Callable[[float], None] = time.sleep) -> FetchResult:
    """Call ``call()``, retrying on exception with exponential backoff.

    Transient network/DNS failures during the crawl should not be recorded on
    the first miss. The final exception propagates if every attempt fails (it is
    then recorded as ``fetch-error`` and retried again on the next resume).
    """
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception:  # noqa: BLE001 -- retry any transient failure, re-raise on the last attempt
            if attempt == attempts:
                raise
            sleep(delay)
            delay *= backoff


def _default_fetch(source_package: str, version: str) -> FetchResult:
    """Real acquisition boundary: the apt-source lean fetch, with retries."""
    return _with_retries(lambda: fetch_source_details(source_package, version))


def body_sha256(raw_text: str) -> str:
    """The content address of a raw patch body: sha256 of its utf-8 bytes."""
    return hashlib.sha256(raw_text.encode('utf-8')).hexdigest()


def _body_path(corpus_dir: str, sha: str) -> str:
    return os.path.join(corpus_dir, 'bodies', sha[:2], sha)


def _store_body(corpus_dir: str, raw_text: str) -> tuple[str, bool]:
    """Write a body content-addressed; return ``(sha, newly_written)``.

    An existing blob is never rewritten (identical bodies dedup for free). The
    write is atomic via a temp file + rename so a crash cannot leave a partial
    blob at the addressed path.
    """
    sha = body_sha256(raw_text)
    path = _body_path(corpus_dir, sha)
    if os.path.exists(path):
        return sha, False
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    # A unique temp file in the destination directory keeps the rename atomic and
    # avoids two threads clobbering one another's temp name (mkstemp is unique).
    fd, tmp = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(raw_text)
        # ``link`` fails if the target already exists, so concurrent writers of
        # an identical body race cleanly: exactly one wins and reports newly=True,
        # which keeps ``distinct_bodies`` accurate under threads.
        try:
            os.link(tmp, path)
            newly = True
        except FileExistsError:
            newly = False
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return sha, newly


def _read_done(corpus_dir: str) -> set[tuple[str, str]]:
    """(package, version) pairs TERMINALLY recorded in packages.jsonl.

    A transient fetch failure is NOT terminal: it is retried on resume so a blip
    is never baked into the corpus. The manifest is append-only, so the LAST row
    per (package, version) wins -- a package that failed then succeeded on a
    later run counts as done; one that only ever failed is retried.
    """
    path = os.path.join(corpus_dir, 'packages.jsonl')
    last: dict[tuple[str, str], dict] = {}
    if not os.path.exists(path):
        return set()
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            last[(row['source_package'], row['version'])] = row
    return {key for key, row in last.items() if not _is_transient_failure(row.get('error'))}


def _classify_texts(source_format: str | None, texts: dict[str, str] | None) -> tuple[str, str | None]:
    """Map a fetch result to ``(state, error)`` for the package manifest.

    Mirrors ``AptSourcePatches.details()``: ``None`` texts is a fetch failure
    when there is no format, native when the format says so, else a non-quilt
    (``1.0``) skip recorded as unknown. ``{}`` is a clean quilt source.
    """
    if texts is None:
        if source_format is None:
            return 'unknown', 'fetch-failed'
        if 'native' in source_format.lower():
            return 'native', None
        return 'unknown', 'non-quilt-format'
    if not texts:
        return 'clean', None
    return 'patched', None


class _Writer:
    """Serialises the two append-only manifests across worker threads."""

    def __init__(self, corpus_dir: str) -> None:
        self._patches_path = os.path.join(corpus_dir, 'patches.jsonl')
        self._packages_path = os.path.join(corpus_dir, 'packages.jsonl')
        self._lock = threading.Lock()

    def _append(self, path: str, row: dict) -> None:
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(row, sort_keys=True) + '\n')

    def record(self, package_row: dict, patch_rows: list[dict]) -> None:
        """Append the patch rows then the package row, atomically as a unit.

        The package row is written LAST so that a crash mid-record leaves the
        package un-recorded -- on resume it is re-processed (the patch rows it
        re-emits are idempotent against the content-addressed store) rather than
        being skipped with patch rows missing.
        """
        with self._lock:
            for row in patch_rows:
                self._append(self._patches_path, row)
            self._append(self._packages_path, package_row)


def _process(source_package: str, version: str, *, corpus_dir: str,
             fetch: Fetch) -> tuple[dict, list[dict], int]:
    """Fetch one package, store its bodies, and build its manifest rows.

    Returns ``(package_row, patch_rows, distinct_new)`` where ``distinct_new``
    is how many bodies this package added to the store (for stats).
    """
    try:
        source_format, texts, changelog_date = fetch(source_package, version)
    except Exception as exc:  # noqa: BLE001 -- record any fetch failure, never crash the crawl
        package_row = {
            'source_package': source_package, 'version': version, 'state': 'unknown',
            'source_format': None, 'n_patches': 0, 'changelog_date': None,
            'error': 'fetch-error: %s' % exc}
        return package_row, [], 0

    state, error = _classify_texts(source_format, texts)
    patch_rows: list[dict] = []
    distinct_new = 0
    if state == 'patched':
        for name, raw_text in texts.items():
            sha, is_new = _store_body(corpus_dir, raw_text)
            if is_new:
                distinct_new += 1
            patch_rows.append({
                'source_package': source_package, 'version': version,
                'patch_name': name, 'raw_sha256': sha})

    package_row = {
        'source_package': source_package, 'version': version, 'state': state,
        'source_format': source_format, 'n_patches': len(patch_rows),
        'changelog_date': changelog_date, 'error': error}
    return package_row, patch_rows, distinct_new


def build_corpus(worklist: Iterable[tuple[str, str]], corpus_dir: str, *,
                 fetch: Fetch = _default_fetch, max_workers: int = 8,
                 progress=None) -> CorpusStats:
    """Build (or resume) the corpus over ``worklist``; return run counts.

    ``worklist`` yields ``(source_package, version)`` pairs. ``fetch`` is the
    injectable acquisition boundary (default: the real apt-source fetch) so
    tests run fully offline. Already-recorded pairs are skipped (resumable),
    fetches run under a bounded ``ThreadPoolExecutor(max_workers)``, and the two
    manifests are appended under a lock. ``progress``, if given, has its
    ``step(label)`` called once per processed package.
    """
    os.makedirs(corpus_dir, exist_ok=True)
    done = _read_done(corpus_dir)
    pending = [(pkg, ver) for pkg, ver in worklist if (pkg, ver) not in done]

    writer = _Writer(corpus_dir)
    stats = CorpusStats()
    state_counter = {
        'patched': 'patched', 'clean': 'clean', 'native': 'native', 'unknown': 'unknown'}

    def work(item: tuple[str, str]):
        return _process(item[0], item[1], corpus_dir=corpus_dir, fetch=fetch)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for (source_package, version), (package_row, patch_rows, distinct_new) in zip(
                pending, executor.map(work, pending)):
            writer.record(package_row, patch_rows)

            stats.packages_processed += 1
            setattr(stats, state_counter[package_row['state']],
                    getattr(stats, state_counter[package_row['state']]) + 1)
            # A fetch failure is a genuine acquisition error (unresolved download
            # or a raised exception), NOT a legitimate non-quilt skip -- the
            # latter is honest accounting recorded in the package row's error but
            # is an expected, classified outcome.
            error = package_row['error'] or ''
            if error.startswith('fetch-failed') or error.startswith('fetch-error'):
                stats.fetch_failures += 1
            stats.patch_rows_written += len(patch_rows)
            stats.distinct_bodies += distinct_new
            if progress is not None:
                progress.step('%s %s' % (source_package, version))

    if progress is not None:
        progress.finish()
    return stats


def _worklist_from_bundle(bundle) -> list[tuple[str, str]]:
    """The patched ``(source_package, version)`` pairs in a divergence bundle."""
    work: list[tuple[str, str]] = []
    for source_package, entry in bundle.divergence.items():
        if entry.get('state') == 'patched':
            work.append((source_package, entry['version']))
    return work


def main(argv: list[str] | None = None) -> int:
    """``python -m divergulent.classify.corpus``: crawl a bundle into a corpus."""
    parser = argparse.ArgumentParser(
        prog='python -m divergulent.classify.corpus',
        description='Build a content-addressed corpus of carried patch bodies from a '
                    'divergence bundle (curation-side; requires deb-src indices).')
    parser.add_argument('bundle', help='path to a divergence bundle (gzipped JSON)')
    parser.add_argument('corpus_dir', help='directory to build the corpus under')
    parser.add_argument('--max-workers', type=int, default=8,
                        help='bounded concurrency for the fetch (default: 8)')
    parser.add_argument('--quiet', action='store_true', help='suppress progress output')
    args = parser.parse_args(argv)

    # Imported lazily so import time stays free of network/subprocess concerns.
    from divergulent import bundle as bundle_module
    from divergulent.progress import Progress

    if not deb_src_available():
        print('error: apt source (deb-src) indices are not configured; cannot fetch '
              'source packages. Enable deb-src and run apt-get update first.', file=sys.stderr)
        return 2

    loaded = bundle_module.load(args.bundle)
    worklist = _worklist_from_bundle(loaded)
    progress = Progress(len(worklist), enabled=not args.quiet)
    stats = build_corpus(worklist, args.corpus_dir, max_workers=args.max_workers, progress=progress)

    print('packages processed: %d' % stats.packages_processed)
    print('  patched: %d  clean: %d  native: %d  unknown: %d' % (
        stats.patched, stats.clean, stats.native, stats.unknown))
    print('  fetch failures: %d' % stats.fetch_failures)
    print('patch rows written: %d' % stats.patch_rows_written)
    print('distinct bodies added: %d' % stats.distinct_bodies)
    return 0


if __name__ == '__main__':
    sys.exit(main())
