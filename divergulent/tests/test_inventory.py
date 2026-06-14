import os

import testtools

from divergulent import inventory
from divergulent.debversion import DebianVersion


FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'dpkg-query-sample.txt')


def _load_fixture():
    with open(FIXTURE, 'r') as handle:
        return handle.read()


class InventoryTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.packages = inventory.list_installed(run=_load_fixture)
        self.by_key = {(p.binary_name, p.architecture): p for p in self.packages}

    def test_count(self):
        self.assertEqual(5, len(self.packages))

    def test_excludes_removed_config_status(self):
        # "rc" (removed, config files remain) is not installed on disk.
        self.assertNotIn('old-package', {p.binary_name for p in self.packages})

    def test_excludes_garbage_line(self):
        # The short "un phantom" line must not produce a package.
        self.assertNotIn('phantom', {p.binary_name for p in self.packages})

    def test_includes_held_package(self):
        # "hi" (held but installed) is still installed on disk.
        self.assertIn(('docker-ce', 'amd64'), self.by_key)

    def test_source_name_differs_from_binary(self):
        self.assertEqual('glibc', self.by_key[('libc6', 'amd64')].source_name)

    def test_source_version_differs_from_binary(self):
        bash = self.by_key[('bash', 'amd64')]
        self.assertEqual('5.2.15-2+b7', str(bash.binary_version))
        self.assertEqual('5.2.15-2', str(bash.source_version))

    def test_multiarch_kept_separately(self):
        self.assertIn(('libc6', 'amd64'), self.by_key)
        self.assertIn(('libc6', 'i386'), self.by_key)

    def test_source_falls_back_to_binary(self):
        weird = self.by_key[('weird', 'amd64')]
        self.assertEqual('weird', weird.source_name)
        self.assertEqual('9.9', str(weird.source_version))

    def test_versions_are_debianversion(self):
        bash = self.by_key[('bash', 'amd64')]
        self.assertIsInstance(bash.binary_version, DebianVersion)
        self.assertIsInstance(bash.source_version, DebianVersion)
