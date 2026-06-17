import testtools

from divergulent import debversion
from divergulent.sources.bundle_backed import (
    BundleDivergenceSource, FallbackDivergence, FallbackStaleness)
from divergulent.sources.debian_patches import DivergenceState, DivergenceSummary
from divergulent.sources.repology import RepologyBulkSource, StalenessResult, StalenessState


DIVERGENCE_MAP = {
    'bash': {'version': '5.2.15-3', 'format': '3.0 (quilt)', 'total': 4, 'state': 'patched'},
    'hello': {'version': '2.10-3', 'format': '3.0 (native)', 'total': 0, 'state': 'native'},
}


class BundleDivergenceSourceTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.source = BundleDivergenceSource(DIVERGENCE_MAP)

    def test_hit_on_matching_version(self):
        result = self.source.summary('bash', '5.2.15-3')
        self.assertEqual(
            DivergenceSummary('bash', '5.2.15-3', '3.0 (quilt)', 4, DivergenceState.PATCHED), result)

    def test_miss_on_version_mismatch(self):
        self.assertIsNone(self.source.summary('bash', '5.2.15-4'))

    def test_miss_on_absent_package(self):
        self.assertIsNone(self.source.summary('absent', '1.0-1'))

    def test_miss_on_unrecognised_state(self):
        source = BundleDivergenceSource({'x': {'version': '1', 'format': None, 'total': 0, 'state': 'bogus'}})
        self.assertIsNone(source.summary('x', '1'))


class _RecordingDivergence:
    '''A live divergence source that records calls and returns a fixed summary.'''

    def __init__(self):
        self.calls = []

    def summary(self, source_package, version):
        self.calls.append((source_package, version))
        return DivergenceSummary(source_package, version, '3.0 (quilt)', 1, DivergenceState.PATCHED)


class FallbackDivergenceTestCase(testtools.TestCase):

    def test_hit_does_not_call_live(self):
        live = _RecordingDivergence()
        source = FallbackDivergence(BundleDivergenceSource(DIVERGENCE_MAP), live)
        result = source.summary('bash', '5.2.15-3')
        self.assertEqual(4, result.total)
        self.assertEqual([], live.calls)

    def test_version_mismatch_falls_back_to_live(self):
        live = _RecordingDivergence()
        source = FallbackDivergence(BundleDivergenceSource(DIVERGENCE_MAP), live)
        result = source.summary('bash', '5.2.15-4')
        self.assertEqual(1, result.total)  # the live source's answer
        self.assertEqual([('bash', '5.2.15-4')], live.calls)

    def test_absent_falls_back_to_live(self):
        live = _RecordingDivergence()
        source = FallbackDivergence(BundleDivergenceSource(DIVERGENCE_MAP), live)
        source.summary('absent', '1.0-1')
        self.assertEqual([('absent', '1.0-1')], live.calls)


class _RecordingStaleness:
    '''A live staleness source that records calls and returns a fixed result.'''

    def __init__(self):
        self.calls = []

    def staleness(self, source_package, installed_version):
        self.calls.append(source_package)
        return StalenessResult(source_package, installed_version, '9.9', StalenessState.BEHIND)


class FallbackStalenessTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.bundle = RepologyBulkSource({'bash': '5.3'})

    def test_hit_does_not_call_live(self):
        live = _RecordingStaleness()
        source = FallbackStaleness(self.bundle, live)
        result = source.staleness('bash', debversion.parse('5.2-1'))
        self.assertEqual('5.3', result.newest_version)
        self.assertEqual(StalenessState.BEHIND, result.state)
        self.assertEqual([], live.calls)

    def test_absent_falls_back_to_live(self):
        live = _RecordingStaleness()
        source = FallbackStaleness(self.bundle, live)
        result = source.staleness('absent', debversion.parse('1.0-1'))
        # The live source resolved it, so it is not a false UNKNOWN.
        self.assertEqual('9.9', result.newest_version)
        self.assertEqual(['absent'], live.calls)
