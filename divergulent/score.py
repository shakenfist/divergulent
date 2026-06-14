'''Combine the staleness and divergence axes into a per-package drift signal.

The score exists only to *rank* packages; the two axes are always reported
separately so the output is never an opaque verdict. The weights are
deliberately simple starting points, to be tuned once we have real data.
'''
from __future__ import annotations

from dataclasses import dataclass

from divergulent.sources.debian_patches import DivergenceResult
from divergulent.sources.repology import StalenessResult, StalenessState


# Scoring weights (transparent and provisional).
W_DEBIAN_ONLY = 3    # an undocumented distro-only carried patch: strongest signal
W_UNKNOWN_PATCH = 1  # a carried patch we could not classify
W_BEHIND = 2         # behind pure upstream (mild: expected on a stable release)
# Forwarded patches are benign drift headed upstream and score 0.


@dataclass(frozen=True)
class PackageDrift:
    source_package: str
    version: str
    staleness: StalenessResult
    divergence: DivergenceResult
    score: int


def combine(staleness: StalenessResult, divergence: DivergenceResult) -> PackageDrift:
    '''Combine both axes for one source package into a ranked drift signal.'''
    score = 0
    if staleness.state == StalenessState.BEHIND:
        score += W_BEHIND
    score += divergence.debian_only * W_DEBIAN_ONLY
    score += divergence.unknown * W_UNKNOWN_PATCH

    return PackageDrift(
        source_package=staleness.source_package,
        version=divergence.version,
        staleness=staleness,
        divergence=divergence,
        score=score)
