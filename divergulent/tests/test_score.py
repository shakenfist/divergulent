import testtools

from divergulent import debversion
from divergulent import score
from divergulent.sources.debian_patches import DivergenceState, DivergenceSummary
from divergulent.sources.repology import StalenessResult, StalenessState


def _stale(name, state):
    newest = '2.0' if state == StalenessState.BEHIND else None
    return StalenessResult(name, debversion.parse('1.0-1'), newest, state)


def _div(name, total=0, state=DivergenceState.PATCHED):
    return DivergenceSummary(name, '1.0-1', '3.0 (quilt)', total, state)


class CombineTestCase(testtools.TestCase):

    def test_clean_and_current_scores_zero(self):
        drift = score.combine(_stale('a', StalenessState.CURRENT), _div('a', state=DivergenceState.CLEAN))
        self.assertEqual(0, drift.score)

    def test_behind_only(self):
        drift = score.combine(_stale('a', StalenessState.BEHIND), _div('a', state=DivergenceState.CLEAN))
        self.assertEqual(score.W_BEHIND, drift.score)

    def test_patches_weighted(self):
        drift = score.combine(_stale('a', StalenessState.CURRENT), _div('a', total=4))
        self.assertEqual(4 * score.W_PATCH, drift.score)

    def test_both_axes_sum(self):
        drift = score.combine(_stale('a', StalenessState.BEHIND), _div('a', total=3))
        self.assertEqual(score.W_BEHIND + 3 * score.W_PATCH, drift.score)

    def test_both_unknown_scores_zero(self):
        drift = score.combine(
            _stale('a', StalenessState.UNKNOWN), _div('a', total=0, state=DivergenceState.UNKNOWN))
        self.assertEqual(0, drift.score)

    def test_carries_package_and_version(self):
        drift = score.combine(_stale('bash', StalenessState.BEHIND), _div('bash', total=1))
        self.assertEqual('bash', drift.source_package)
        self.assertEqual('1.0-1', drift.version)

    def test_ranking_order(self):
        drifts = [
            score.combine(_stale('low', StalenessState.CURRENT), _div('low', state=DivergenceState.CLEAN)),
            score.combine(_stale('high', StalenessState.BEHIND), _div('high', total=5)),
            score.combine(_stale('mid', StalenessState.CURRENT), _div('mid', total=2)),
        ]
        ordered = sorted(drifts, key=lambda d: -d.score)
        self.assertEqual(['high', 'mid', 'low'], [d.source_package for d in ordered])
