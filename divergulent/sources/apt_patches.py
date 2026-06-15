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
import subprocess
import tarfile
import tempfile
from collections.abc import Callable

from debian import deb822  # type: ignore[import-untyped]

from divergulent.sources.debian_patches import (
    DivergenceState, PackagePatches, PatchDetail, patch_detail)


_PATCHES_PREFIX = 'debian/patches/'


def _run(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, cwd=cwd)


def deb_src_available(run: Callable[..., subprocess.CompletedProcess] = _run) -> bool:
    '''True if apt has source (deb-src) indices configured.'''
    result = run(['apt-get', 'indextargets', '--format', '$(CREATED_BY)'])
    return result.returncode == 0 and 'Sources' in result.stdout


def _download_source(source_package: str, version: str, dest_dir: str,
                     run: Callable[..., subprocess.CompletedProcess] = _run) -> bool:
    '''Download a source package into dest_dir; False if it could not be fetched.'''
    result = run(
        ['apt-get', 'source', '--download-only', '--only-source', '%s=%s' % (source_package, version)],
        cwd=dest_dir)
    return result.returncode == 0


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
