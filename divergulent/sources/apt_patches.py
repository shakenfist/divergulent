'''Tier 2 divergence: classify carried patches via apt source packages.

Uses the local apt toolchain to download a source package from the configured
mirror, extracts ``debian/patches`` from its ``.debian.tar.*``, and classifies
each patch with ``dep3`` — giving the full Debian-only / forwarded / unknown
breakdown at whole-machine scale without making one web request per patch.

This relies on the Debian mirror network (built for bulk) rather than a single
web service, but it requires source (``deb-src``) indices; ``deb_src_available``
lets callers degrade clearly when they are absent.
'''
from __future__ import annotations

import email.utils
import glob
import http.client
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable

from debian import deb822  # type: ignore[import-untyped]

from divergulent.http import DEFAULT_USER_AGENT
from divergulent.sources.debian_patches import (
    DivergenceState, PackagePatches, PatchDetail, patch_detail)


_PATCHES_PREFIX = 'debian/patches/'

_FETCH_TIMEOUT = 30


def _run(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, cwd=cwd)


def deb_src_available(run: Callable[..., subprocess.CompletedProcess] = _run) -> bool:
    '''True if apt has source (deb-src) indices configured.'''
    result = run(['apt-get', 'indextargets', '--format', '$(CREATED_BY)'])
    return result.returncode == 0 and 'Sources' in result.stdout


def _source_uris(source_package: str, version: str,
                 run: Callable[..., subprocess.CompletedProcess] = _run) -> tuple[str | None, str | None]:
    '''Resolve the (.dsc, .debian.tar.*) mirror URLs for a source version.

    Uses ``apt-get source --print-uris`` (the user's configured mirror) without
    downloading, so we can fetch only the small packaging files and skip the
    potentially huge .orig tarball. Returns (None, None) if it cannot resolve.
    '''
    result = run(['apt-get', 'source', '--print-uris', '--only-source', '%s=%s' % (source_package, version)])
    if result.returncode != 0:
        return None, None
    dsc_url = debian_url = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("'"):
            continue
        url = line.split("'")[1]
        if url.endswith('.dsc'):
            dsc_url = url
        elif '.debian.tar.' in url:
            debian_url = url
    return dsc_url, debian_url


class _KeepAliveFetcher:
    '''Fetch files reusing one keep-alive HTTP connection per worker thread.

    The corpus crawl fetches ~37k files from a single mirror host. A fresh
    ``urllib.request.urlopen`` per file re-resolves DNS and reopens TCP every
    time, which hammered a home DNS server with ~37k identical lookups. This
    fetcher keeps a per-thread ``http.client.HTTPConnection`` keyed by the
    (host, port) actually connected to -- the proxy when proxied, else the
    target -- so each worker resolves DNS and opens a socket roughly once
    rather than once per file.

    It only handles the plain-HTTP target case (optionally through a plain-HTTP
    proxy); anything else (https targets, non-HTTP/unexpected proxy schemes)
    falls back to ``urllib.request.urlopen`` so correctness never depends on the
    reuse path. The proxy decision uses ``urllib`` semantics
    (``getproxies()``/``proxy_bypass``) so it matches what urllib would do today
    and honours ``HTTP_PROXY``/``http_proxy`` on the CI runner.
    '''

    def __init__(self) -> None:
        # Per-thread connection cache. http.client.HTTPConnection is not
        # thread-safe, but each worker thread gets its own connections, so we
        # never share a connection across threads.
        self._local = threading.local()

    def _connection(self, host: str, port: int) -> http.client.HTTPConnection:
        '''Return a cached connection for (host, port), creating one if needed.'''
        cache = getattr(self._local, 'connections', None)
        if cache is None:
            cache = self._local.connections = {}
        key = (host, port)
        conn = cache.get(key)
        if conn is None:
            conn = cache[key] = http.client.HTTPConnection(host, port, timeout=_FETCH_TIMEOUT)
        return conn

    def _drop(self, host: str, port: int) -> None:
        '''Discard a cached connection that errored, so the next call reconnects.'''
        cache = getattr(self._local, 'connections', None)
        if cache is None:
            return
        conn = cache.pop((host, port), None)
        if conn is not None:
            conn.close()

    def fetch(self, url: str, dest_path: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        # Only the plain-HTTP target case (optionally via a plain-HTTP proxy)
        # uses the keep-alive path; everything else falls back to urllib so
        # correctness never depends on the reuse path.
        if parsed.scheme != 'http' or not parsed.hostname:
            self._fallback(url, dest_path)
            return

        proxies = urllib.request.getproxies()
        proxy = proxies.get('http')
        if proxy and not urllib.request.proxy_bypass(parsed.hostname):
            proxy_parts = urllib.parse.urlsplit(proxy if '://' in proxy else 'http://' + proxy)
            if proxy_parts.scheme not in ('', 'http') or not proxy_parts.hostname:
                self._fallback(url, dest_path)
                return
            # Proxied: reuse the connection to the PROXY and send the absolute
            # URI in the request line (GET http://target/path HTTP/1.1).
            conn_host = proxy_parts.hostname
            conn_port = proxy_parts.port or 80
            request_target = url
        else:
            # Direct: reuse the connection to the TARGET and send origin-form.
            conn_host = parsed.hostname
            conn_port = parsed.port or 80
            request_target = parsed.path or '/'
            if parsed.query:
                request_target += '?' + parsed.query

        target_host = parsed.netloc
        headers = {
            'User-Agent': DEFAULT_USER_AGENT,
            'Host': target_host,
            'Connection': 'keep-alive',
        }
        self._request_with_retry(conn_host, conn_port, request_target, headers, dest_path)

    def _request_with_retry(self, host: str, port: int, request_target: str,
                            headers: dict[str, str], dest_path: str) -> None:
        '''Issue the request, recreating the connection and retrying once.

        A server may close an idle keep-alive connection between requests; that
        surfaces as a connection-level error on the next use. We retry once on a
        fresh connection before giving up so a dropped idle socket is invisible
        to the caller.
        '''
        for attempt in range(2):
            conn = self._connection(host, port)
            try:
                conn.request('GET', request_target, headers=headers)
                response = conn.getresponse()
                if not 200 <= response.status < 300:
                    # Drain the body so the connection stays reusable, then raise
                    # so the caller's existing error handling sees a failed fetch
                    # rather than an error body written to disk.
                    response.read()
                    raise http.client.HTTPException(
                        'GET %s returned HTTP %d' % (request_target, response.status))
                # copyfileobj reads to EOF, leaving the connection reusable.
                with open(dest_path, 'wb') as out:
                    shutil.copyfileobj(response, out)
                return
            except http.client.HTTPException:
                # A non-2xx is a real failure, not a stale socket: drop the
                # connection and propagate without a futile retry.
                self._drop(host, port)
                raise
            except (ConnectionError, OSError):
                # A connection-level error likely means the keep-alive socket
                # was closed; drop it and, on the first attempt, retry fresh.
                self._drop(host, port)
                if attempt == 1:
                    raise

    @staticmethod
    def _fallback(url: str, dest_path: str) -> None:
        request = urllib.request.Request(url, headers={'User-Agent': DEFAULT_USER_AGENT})
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT) as response, open(dest_path, 'wb') as out:
            shutil.copyfileobj(response, out)


_FETCHER = _KeepAliveFetcher()


def _fetch_file(url: str, dest_path: str) -> None:
    '''Fetch ``url`` to ``dest_path``, reusing a per-thread keep-alive connection.

    Preserves the original signature and behaviour (writes the body, raises on
    failure) so ``_download_source`` and the injectable seams are unchanged.
    '''
    _FETCHER.fetch(url, dest_path)


def _download_source(source_package: str, version: str, dest_dir: str,
                     run: Callable[..., subprocess.CompletedProcess] = _run,
                     fetch: Callable[[str, str], None] = _fetch_file) -> bool:
    '''Fetch only the .dsc and .debian.tar.* into dest_dir; False if unresolved.

    Deliberately skips the .orig tarball: we only need the packaging to read
    debian/patches, and downloading upstream source per package would be huge.
    '''
    dsc_url, debian_url = _source_uris(source_package, version, run=run)
    if dsc_url is None:
        return False
    fetch(dsc_url, os.path.join(dest_dir, os.path.basename(dsc_url)))
    if debian_url is not None:
        fetch(debian_url, os.path.join(dest_dir, os.path.basename(debian_url)))
    return True


def _read_format(dest_dir: str) -> str | None:
    dscs = glob.glob(os.path.join(dest_dir, '*.dsc'))
    if not dscs:
        return None
    with open(dscs[0]) as handle:
        return deb822.Dsc(handle).get('Format')


def _member(tar: tarfile.TarFile, path: str):
    for candidate in (path, './' + path):
        try:
            return tar.getmember(candidate)
        except KeyError:
            continue
    return None


def _read(tar: tarfile.TarFile, member) -> str:
    handle = tar.extractfile(member)
    return handle.read().decode('utf-8', 'replace') if handle is not None else ''


def _extract_patches(dest_dir: str) -> dict[str, str] | None:
    '''Return {patch_name: text} from the source's debian/patches, or None.

    None means there is no quilt patch series (e.g. a native or 1.0 source).
    '''
    debian_tars = glob.glob(os.path.join(dest_dir, '*.debian.tar.*'))
    if not debian_tars:
        return None
    texts: dict[str, str] = {}
    with tarfile.open(debian_tars[0], 'r:*') as tar:
        series = _member(tar, _PATCHES_PREFIX + 'series')
        if series is None:
            return {}
        for line in _read(tar, series).splitlines():
            entry = line.strip()
            if not entry or entry.startswith('#'):
                continue
            name = entry.split()[0]  # series entries may carry trailing options
            member = _member(tar, _PATCHES_PREFIX + name)
            if member is not None:
                texts[name] = _read(tar, member)
    return texts


_CHANGELOG_TRAILER = re.compile(r'^ -- .+?  +(.+?)\s*$', re.MULTILINE)


def _changelog_date(text: str) -> str | None:
    '''The ISO ``YYYY-MM-DD`` date of the TOP ``debian/changelog`` entry, or None.

    Reads only the first ``" -- maintainer  <RFC2822 date>"`` trailer (the most
    recent entry's upload date) and normalises it -- no full-history parse. A
    missing or unparseable date yields ``None``.
    '''
    match = _CHANGELOG_TRAILER.search(text)
    if match is None:
        return None
    try:
        when = email.utils.parsedate_to_datetime(match.group(1))
    except (TypeError, ValueError):
        return None
    return when.date().isoformat() if when is not None else None


def _extract_changelog_date(dest_dir: str) -> str | None:
    '''The package's last-upload date from ``debian/changelog`` in the debian tar.

    The same ``.debian.tar.*`` ``_extract_patches`` reads, so no extra download.
    ``None`` when there is no debian tar (native / non-quilt) or no changelog.
    '''
    debian_tars = glob.glob(os.path.join(dest_dir, '*.debian.tar.*'))
    if not debian_tars:
        return None
    with tarfile.open(debian_tars[0], 'r:*') as tar:
        member = _member(tar, 'debian/changelog')
        if member is None:
            return None
        return _changelog_date(_read(tar, member))


def _fetch_source(source_package: str, version: str, *, download: Callable[..., bool]
                  ) -> tuple[str | None, dict[str, str] | None, str | None]:
    '''Download once; return ``(source_format, texts, changelog_date)``.

    The shared core of :func:`fetch_patch_texts` (texts only) and
    :func:`fetch_source_details` (texts + the package's changelog date), so both
    cost a single ``.dsc`` + ``.debian.tar.*`` download.
    '''
    with tempfile.TemporaryDirectory() as dest:
        if not download(source_package, version, dest):
            return None, None, None
        source_format = _read_format(dest)
        changelog_date = _extract_changelog_date(dest)
        if 'native' in (source_format or '').lower():
            return source_format, None, changelog_date
        return source_format, _extract_patches(dest), changelog_date


def fetch_patch_texts(source_package: str, version: str, *,
                      download: Callable[..., bool] = _download_source) -> tuple[str | None, dict[str, str] | None]:
    '''Fetch a source version and return ``(source_format, {patch_name: raw_text})``.

    This is the reusable acquisition half of ``AptSourcePatches.details()``: it
    downloads only the ``.dsc`` + ``.debian.tar.*`` (never the ``.orig``), reads
    the source format, and extracts the FULL ``debian/patches/series`` as raw
    bodies -- no DEP-3 parsing or classification. It is shared by the divergence
    classifier and the curation-side corpus builder so both see the same texts.

    The returned ``texts`` is:
      * ``{patch_name: raw_text}`` -- a quilt source carrying patches,
      * ``{}`` -- a clean quilt source (series present but empty),
      * ``None`` -- no quilt series (native, ``1.0``, or an unresolved download).

    A ``None`` texts is disambiguated by ``source_format``: a ``native`` format
    means native, ``None`` format means the download could not be resolved, and
    any other format (e.g. ``1.0``) means a non-quilt source. This mirrors the
    distinctions ``details()`` draws today.
    '''
    source_format, texts, _date = _fetch_source(source_package, version, download=download)
    return source_format, texts


def fetch_source_details(source_package: str, version: str, *,
                         download: Callable[..., bool] = _download_source
                         ) -> tuple[str | None, dict[str, str] | None, str | None]:
    '''Like :func:`fetch_patch_texts` but also returns the package's changelog date.

    The curation-side corpus builder uses this to record the package's last-upload
    date alongside its patches, from the SAME download. The client divergence path
    stays on :func:`fetch_patch_texts` (it needs no date).
    '''
    return _fetch_source(source_package, version, download=download)


class AptSourcePatches:
    '''Classify carried patches by fetching source packages via apt.'''

    name = 'apt-source'

    def __init__(self, *, download: Callable[..., bool] = _download_source,
                 available: Callable[[], bool] = deb_src_available) -> None:
        self._download = download
        self._available = available

    def available(self) -> bool:
        return self._available()

    def details(self, source_package: str, version: str) -> PackagePatches:
        '''Return per-patch detail for an installed source package version.'''
        source_format, texts = fetch_patch_texts(source_package, version, download=self._download)

        if texts is None:
            if source_format is None:
                # The download could not be resolved at all.
                return PackagePatches(source_package, version, None, DivergenceState.UNKNOWN, [])
            if 'native' in source_format.lower():
                return PackagePatches(source_package, version, source_format, DivergenceState.NATIVE, [])
            # No quilt series and not native: cannot classify via patches.
            return PackagePatches(source_package, version, source_format, DivergenceState.UNKNOWN, [])
        if not texts:
            return PackagePatches(source_package, version, source_format, DivergenceState.CLEAN, [])

        patches: list[PatchDetail] = [patch_detail(name, text) for name, text in texts.items()]
        return PackagePatches(source_package, version, source_format, DivergenceState.PATCHED, patches)
