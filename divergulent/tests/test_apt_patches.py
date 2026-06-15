import io
import os
import subprocess
import tarfile

import testtools

from divergulent.dep3 import PatchClass
from divergulent.sources.apt_patches import AptSourcePatches, deb_src_available
from divergulent.sources.debian_patches import DivergenceState


DEBIAN_ONLY = 'Description: distro tweak\nForwarded: not-needed\nOrigin: vendor\n\n--- a/x\n'
FORWARDED = 'Description: upstreamable\nForwarded: https://lists.example/1\n\n--- a/x\n'
BARE = '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n'


def _add(tar, name, content):
    data = content.encode()
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _writer(patches, fmt='3.0 (quilt)', with_debian_tar=True):
    '''Return a fake download() that writes a fixture source package into dest.'''
    def download(source_package, version, dest):
        with open(os.path.join(dest, 'pkg.dsc'), 'w') as handle:
            handle.write('Format: %s\n' % fmt)
        if with_debian_tar:
            with tarfile.open(os.path.join(dest, 'pkg.debian.tar.xz'), 'w:xz') as tar:
                _add(tar, 'debian/patches/series', ''.join(name + '\n' for name in (patches or {})))
                for name, text in (patches or {}).items():
                    _add(tar, 'debian/patches/' + name, text)
        return True
    return download


def _source(download):
    return AptSourcePatches(download=download, available=lambda: True)


class AptSourcePatchesTestCase(testtools.TestCase):

    def test_patched_classifies_each_patch(self):
        patches = {'a.patch': DEBIAN_ONLY, 'b.patch': FORWARDED, 'c.patch': BARE}
        package = _source(_writer(patches)).details('foo', '1.2-1')
        self.assertEqual(DivergenceState.PATCHED, package.state)
        self.assertEqual(3, len(package.patches))
        classes = {p.name: p.patch_class for p in package.patches}
        self.assertEqual(PatchClass.DEBIAN_ONLY, classes['a.patch'])
        self.assertEqual(PatchClass.FORWARDED, classes['b.patch'])
        self.assertEqual(PatchClass.UNKNOWN, classes['c.patch'])

    def test_empty_series_is_clean(self):
        package = _source(_writer({})).details('foo', '1.2-1')
        self.assertEqual(DivergenceState.CLEAN, package.state)

    def test_native_is_native(self):
        package = _source(_writer(None, fmt='3.0 (native)', with_debian_tar=False)).details('foo', '1.2')
        self.assertEqual(DivergenceState.NATIVE, package.state)

    def test_non_quilt_is_unknown(self):
        package = _source(_writer(None, fmt='1.0', with_debian_tar=False)).details('foo', '1.2-1')
        self.assertEqual(DivergenceState.UNKNOWN, package.state)

    def test_download_failure_is_unknown(self):
        package = _source(lambda p, v, d: False).details('foo', '1.2-1')
        self.assertEqual(DivergenceState.UNKNOWN, package.state)

    def test_name(self):
        self.assertEqual('apt-source', AptSourcePatches.name)


class DebSrcAvailableTestCase(testtools.TestCase):

    @staticmethod
    def _run(returncode, stdout):
        def run(args, cwd=None):
            return subprocess.CompletedProcess(args, returncode, stdout, '')
        return run

    def test_available_when_sources_present(self):
        self.assertTrue(deb_src_available(run=self._run(0, 'Packages\nSources\n')))

    def test_unavailable_when_no_sources(self):
        self.assertFalse(deb_src_available(run=self._run(0, 'Packages\n')))

    def test_unavailable_on_error(self):
        self.assertFalse(deb_src_available(run=self._run(100, '')))
