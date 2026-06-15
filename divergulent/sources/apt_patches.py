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

import glob
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable

from debian import deb822  # type: ignore[import-untyped]

from divergulent.http import DEFAULT_USER_AGENT
from divergulent.sources.debian_patches import (
    DivergenceState, PackagePatches, PatchDetail, patch_detail)


_PATCHES_PREFIX = 'debian/patches/'


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


def _fetch_file(url: str, dest_path: str) -> None:
    request = urllib.request.Request(url, headers={'User-Agent': DEFAULT_USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response, open(dest_path, 'wb') as out:
        shutil.copyfileobj(response, out)


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
        with tempfile.TemporaryDirectory() as dest:
            if not self._download(source_package, version, dest):
                return PackagePatches(source_package, version, None, DivergenceState.UNKNOWN, [])

            source_format = _read_format(dest)
            if 'native' in (source_format or '').lower():
                return PackagePatches(source_package, version, source_format, DivergenceState.NATIVE, [])

            texts = _extract_patches(dest)
            if texts is None:
                # No quilt series and not native: cannot classify via patches.
                return PackagePatches(source_package, version, source_format, DivergenceState.UNKNOWN, [])
            if not texts:
                return PackagePatches(source_package, version, source_format, DivergenceState.CLEAN, [])

            patches: list[PatchDetail] = [patch_detail(name, text) for name, text in texts.items()]
            return PackagePatches(source_package, version, source_format, DivergenceState.PATCHED, patches)
