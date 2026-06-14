import testtools

from divergulent import debversion
from divergulent import score
from divergulent.sources.debian_patches import DivergenceResult, DivergenceState
from divergulent.sources.repology import StalenessResult, StalenessState


def _stale(name, state):
    newest = '2.0' if state == StalenessState.BEHIND else None
    return StalenessResult(name, debversion.parse('1.0-1'), newest, state)


def _div(name, debian_only=0, forwarded=0, unknown=0, state=DivergenceState.PATCHED):
    total = debian_only + forwarded + unknown
    return DivergenceResult(name, '1.0-1', '3.0 (quilt)', total, debian_only, forwarded, unknown, state)


class CombineTestCase(testtools.TestCase):

    def test_clean_and_current_scores_zero(self):
        drift = score.combine(_stale('a', StalenessState.CURRENT), _div('a', state=DivergenceState.CLEAN))
        self.assertEqual(0, drift.score)

    def test_behind_only(self):
        drift = score.combine(_stale('a', StalenessState.BEHIND), _div('a', state=DivergenceState.CLEAN))
        self.assertEqual(score.W_BEHIND, drift.score)

    def test_debian_only_patches(self):
        drift = score.combine(_stale('a', StalenessState.CURRENT), _div('a', debian_only=4))
        self.assertEqual(4 * score.W_DEBIAN_ONLY, drift.score)

    def test_unknown_patches(self):
        drift = score.combine(_stale('a', StalenessState.CURRENT), _div('a', unknown=3))
        self.assertEqual(3 * score.W_UNKNOWN_PATCH, drift.score)

    def test_forwarded_patches_add_nothing(self):
        drift = score.combine(_stale('a', StalenessState.CURRENT), _div('a', forwarded=5))
        self.assertEqual(0, drift.score)

    def test_both_axes_sum(self):
        drift = score.combine(_stale('a', StalenessState.BEHIND), _div('a', debian_only=2, unknown=1))
        self.assertEqual(score.W_BEHIND + 2 * score.W_DEBIAN_ONLY + 1 * score.W_UNKNOWN_PATCH, drift.score)

    def test_both_unknown_scores_zero(self):
        drift = score.combine(_stale('a', StalenessState.UNKNOWN), _div('a', state=DivergenceState.UNKNOWN))
        self.assertEqual(0, drift.score)

    def test_carries_package_and_version(self):
        drift = score.combine(_stale('bash', StalenessState.BEHIND), _div('bash', debian_only=1))
        self.assertEqual('bash', drift.source_package)
        self.assertEqual('1.0-1', drift.version)

    def test_ranking_order(self):
        drifts = [
            score.combine(_stale('low', StalenessState.BEHIND), _div('low', state=DivergenceState.CLEAN)),
            score.combine(_stale('high', StalenessState.BEHIND), _div('high', debian_only=5)),
            score.combine(_stale('mid', StalenessState.CURRENT), _div('mid', debian_only=2)),
        ]
        ordered = sorted(drifts, key=lambda d: -d.score)
        self.assertEqual(['high', 'mid', 'low'], [d.source_package for d in ordered])
