'''Combine the staleness and divergence axes into a per-package drift signal.

The score exists only to *rank* packages; the two axes are always reported
separately so the output is never an opaque verdict. The weights are
deliberately simple starting points, to be tuned once we have real data.
'''
from __future__ import annotations

from dataclasses import dataclass

from divergulent.dep3 import PatchClass
from divergulent.sources.debian_patches import DivergenceSummary, PackagePatches
from divergulent.sources.repology import StalenessResult, StalenessState


# Scoring weights (transparent and provisional).
#
# The default whole-machine view cannot tell Debian-only patches from
# forwarded ones (that needs patch bodies; see `show` / `--classify`), so it
# weights total carried patches. Being behind upstream is weighted low: it is
# expected on a stable Debian release.
W_PATCH = 1          # a carried patch (default view: class unknown)
W_BEHIND = 2         # behind pure upstream (mild: expected on a stable release)
# Under --classify we know each patch's class, so we weight the signal:
W_DEBIAN_ONLY = 3    # an undocumented distro-only carried patch: strongest signal
W_UNKNOWN_PATCH = 1  # a carried patch we could not classify
# Forwarded patches are benign drift headed upstream and score 0.


@dataclass(frozen=True)
class PackageDrift:
    source_package: str
    version: str
    staleness: StalenessResult
    divergence: DivergenceSummary
    score: int


def combine(staleness: StalenessResult, divergence: DivergenceSummary) -> PackageDrift:
    '''Combine both axes for one source package into a ranked drift signal.'''
    score = 0
    if staleness.state == StalenessState.BEHIND:
        score += W_BEHIND
    score += divergence.total * W_PATCH

    return PackageDrift(
        source_package=staleness.source_package,
        version=divergence.version,
        staleness=staleness,
        divergence=divergence,
        score=score)


def classified_score(staleness: StalenessResult, package: PackagePatches) -> int:
    '''Score using the per-patch classification available under --classify.

    Debian-only patches dominate (the supply-chain signal); forwarded patches
    score 0; unclassified patches count mildly; being behind is mild.
    '''
    debian_only = sum(1 for p in package.patches if p.patch_class == PatchClass.DEBIAN_ONLY)
    unknown = sum(1 for p in package.patches if p.patch_class == PatchClass.UNKNOWN)
    score = W_BEHIND if staleness.state == StalenessState.BEHIND else 0
    return score + debian_only * W_DEBIAN_ONLY + unknown * W_UNKNOWN_PATCH
