import testtools

from divergulent import debversion


class DebVersionTestCase(testtools.TestCase):

    def test_components(self):
        v = debversion.parse('1:2.3.4+dfsg-2')
        self.assertEqual(1, v.epoch)
        self.assertEqual('2.3.4+dfsg', v.upstream_version)
        self.assertEqual('2', v.debian_revision)

    def test_no_epoch_no_revision(self):
        v = debversion.parse('1.0')
        self.assertIsNone(v.epoch)
        self.assertIsNone(v.debian_revision)

    def test_ordering(self):
        # Each pair is (older, newer) per deb-version(7) ordering rules.
        pairs = [
            ('1.0', '1.1'),
            ('1.0', '2.0'),
            ('2.0', '1:1.0'),       # an epoch dominates the upstream version
            ('1.0~rc1', '1.0'),     # ~ sorts before the release it precedes
            ('1.0~beta', '1.0~rc1'),
            ('1.0-1', '1.0-2'),     # Debian revision breaks the tie
            ('1.0+dfsg-1', '1.1'),
        ]
        for older, newer in pairs:
            self.assertEqual(-1, debversion.compare(older, newer), '%s < %s' % (older, newer))
            self.assertEqual(1, debversion.compare(newer, older), '%s > %s' % (newer, older))
            self.assertTrue(debversion.is_older(older, newer))
            self.assertFalse(debversion.is_older(newer, older))

    def test_equality(self):
        self.assertEqual(debversion.parse('1.0-1'), debversion.parse('1.0-1'))
        self.assertEqual(0, debversion.compare('1.0-1', '1.0-1'))
        self.assertFalse(debversion.is_older('1.0-1', '1.0-1'))

    def test_sortable(self):
        versions = [debversion.parse(s) for s in ['1.1', '1.0~rc1', '1:0.1', '1.0']]
        ordered = sorted(versions)
        self.assertEqual(['1.0~rc1', '1.0', '1.1', '1:0.1'], [str(v) for v in ordered])

    def test_hash_consistent_with_equality(self):
        self.assertEqual(hash(debversion.parse('1.0-1')), hash(debversion.parse('1.0-1')))

    def test_try_parse_valid(self):
        self.assertIsNotNone(debversion.try_parse('1.2-3'))

    def test_try_parse_invalid_returns_none(self):
        # Gentoo-style version: '_' is not valid in a Debian version.
        self.assertIsNone(debversion.try_parse('5.3_p15'))
