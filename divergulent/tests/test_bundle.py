import os
import tempfile

import testtools

from divergulent import bundle


def _sample_bundle():
    return bundle.Bundle(
        schema=bundle.SCHEMA_VERSION,
        cache_schema=bundle.CACHE_SCHEMA_VERSION,
        generated_at='2026-06-17T00:00:00+00:00',
        release='trixie',
        repology_repo='debian_unstable',
        built_on={'arch': 'amd64', 'release': 'trixie'},
        staleness={'bash': '5.3', 'hello': '2.12'},
        divergence={
            'bash': {'version': '5.2.15-3', 'format': '3.0 (quilt)', 'total': 4, 'state': 'patched'},
            'hello': {'version': '2.10-3', 'format': '3.0 (native)', 'total': 0, 'state': 'native'},
        })


class BundleRoundTripTestCase(testtools.TestCase):

    def test_to_dict_carries_every_field(self):
        data = _sample_bundle().to_dict()
        self.assertEqual(bundle.SCHEMA_VERSION, data['schema'])
        self.assertEqual(bundle.CACHE_SCHEMA_VERSION, data['cache_schema'])
        self.assertEqual('trixie', data['release'])
        self.assertEqual('debian_unstable', data['repology_repo'])
        self.assertEqual({'arch': 'amd64', 'release': 'trixie'}, data['built_on'])
        self.assertEqual('5.3', data['staleness']['bash'])
        self.assertEqual('patched', data['divergence']['bash']['state'])

    def test_from_dict_inverts_to_dict(self):
        original = _sample_bundle()
        self.assertEqual(original, bundle.Bundle.from_dict(original.to_dict()))

    def test_write_then_load_round_trips(self):
        original = _sample_bundle()
        fd, path = tempfile.mkstemp(suffix='.json.gz')
        os.close(fd)
        self.addCleanup(os.unlink, path)
        bundle.write(original, path)
        self.assertEqual(original, bundle.load(path))

    def test_written_file_is_gzip(self):
        fd, path = tempfile.mkstemp(suffix='.json.gz')
        os.close(fd)
        self.addCleanup(os.unlink, path)
        bundle.write(_sample_bundle(), path)
        with open(path, 'rb') as handle:
            magic = handle.read(2)
        self.assertEqual(b'\x1f\x8b', magic)  # gzip magic bytes
