'''Debian version parsing and comparison.

A thin wrapper around python-debian's ``debian_support.Version`` so the rest of
divergulent depends on this interface rather than the library directly, the
library's missing type stubs are contained to this module, and the
implementation can be swapped later if needed.

Debian version ordering (epochs, the special ``~`` pre-release ordering, and
revisions) is subtle, so we deliberately do not reimplement it.
'''
from __future__ import annotations

import functools

from debian import debian_support  # type: ignore[import-untyped]


@functools.total_ordering
class DebianVersion:
    '''A parsed, comparable Debian package version.'''

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self._version = debian_support.Version(raw)

    @property
    def epoch(self) -> int | None:
        epoch = self._version.epoch
        return int(epoch) if epoch is not None else None

    @property
    def upstream_version(self) -> str:
        return self._version.upstream_version

    @property
    def debian_revision(self) -> str | None:
        return self._version.debian_revision

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DebianVersion):
            return NotImplemented
        return self._version == other._version

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, DebianVersion):
            return NotImplemented
        return self._version < other._version

    def __hash__(self) -> int:
        return hash(self._version.full_version)

    def __str__(self) -> str:
        return self.raw

    def __repr__(self) -> str:
        return f'DebianVersion({self.raw!r})'


def parse(version_str: str) -> DebianVersion:
    '''Parse a Debian version string into a DebianVersion.'''
    return DebianVersion(version_str)


def compare(a: str | DebianVersion, b: str | DebianVersion) -> int:
    '''Compare two versions, returning -1, 0, or 1.

    Either argument may be a raw string or a DebianVersion.
    '''
    va = a if isinstance(a, DebianVersion) else DebianVersion(a)
    vb = b if isinstance(b, DebianVersion) else DebianVersion(b)
    if va < vb:
        return -1
    if va > vb:
        return 1
    return 0


def is_older(a: str | DebianVersion, b: str | DebianVersion) -> bool:
    '''Return True if version ``a`` is strictly older than version ``b``.'''
    return compare(a, b) < 0
