'''The interface implemented by external data-source adapters.

The staleness axis (Repology) and the divergence axis (sources.debian.org)
implement this protocol. The shared HTTP client and the politeness layer it
needs (descriptive User-Agent, request timeouts, rate limiting and graceful
degradation) arrive with the first network-backed source; this module only
fixes the contract.
'''
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Source(Protocol):
    '''A data source that can be queried about a Debian source package.'''

    name: str

    def lookup(self, source_package: str) -> Any:
        '''Return this source's data for ``source_package``.

        Implementations return source-specific structured data (for example the
        upstream version from Repology, or the patch series from
        sources.debian.org), or None when the source has nothing for the
        package. They must route external access through the shared cache and
        degrade gracefully when the source is unavailable.
        '''
        ...
