'''Enumerate installed packages and map them to their source packages.'''
from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from divergulent import debversion
from divergulent.debversion import DebianVersion


# dpkg-query format string. The \t and \n escapes are interpreted by
# dpkg-query itself, so they are passed through literally. The
# ${source:Package} / ${source:Version} virtual fields make dpkg resolve the
# Source field for us (it is empty when the source equals the binary, and is
# "name (version)" when the source version differs from the binary version).
DPKG_QUERY_FORMAT = (
    r'${db:Status-Abbrev}\t${Package}\t${Version}\t'
    r'${source:Package}\t${source:Version}\t${Architecture}\n'
)


@dataclass(frozen=True)
class InstalledPackage:
    binary_name: str
    binary_version: DebianVersion
    source_name: str
    source_version: DebianVersion
    architecture: str


def _default_runner() -> str:
    try:
        result = subprocess.run(
            ['dpkg-query', '-W', '-f', DPKG_QUERY_FORMAT],
            check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            'dpkg-query not found; divergulent requires a dpkg-based system') from exc
    return result.stdout


def _is_installed(status_abbrev: str) -> bool:
    # The status abbreviation is "<want><status><err>", e.g. "ii". A package is
    # present on disk when the current-status flag (the second character) is
    # "i", which covers both "ii" (installed) and "hi" (held but installed).
    abbrev = status_abbrev.strip()
    return len(abbrev) >= 2 and abbrev[1] == 'i'


def _parse(output: str) -> list[InstalledPackage]:
    packages: list[InstalledPackage] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split('\t')
        if len(fields) != 6:
            continue
        status, name, version, source_name, source_version, arch = fields
        if not _is_installed(status):
            continue
        # The source virtual fields are normally populated, but fall back to the
        # binary's name/version defensively.
        source_name = source_name or name
        source_version = source_version or version
        packages.append(InstalledPackage(
            binary_name=name,
            binary_version=debversion.parse(version),
            source_name=source_name,
            source_version=debversion.parse(source_version),
            architecture=arch))
    return packages


def list_installed(run: Callable[[], str] = _default_runner) -> list[InstalledPackage]:
    '''Return the installed packages, mapped to their source packages.

    ``run`` is an injectable callable returning raw ``dpkg-query`` output; the
    default invokes dpkg-query. Tests pass a callable returning fixture text.
    '''
    return _parse(run())
