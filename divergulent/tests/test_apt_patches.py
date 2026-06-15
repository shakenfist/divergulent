import io
import os
import subprocess
import tarfile
import tempfile

import testtools

from divergulent.dep3 import PatchClass
from divergulent.sources.apt_patches import AptSourcePatches, _download_source, _source_uris, deb_src_available
from divergulent.sources.debian_patches import DivergenceState


SAMPLE_URIS = (
    "'http://deb.debian.org/debian/pool/main/b/bash/bash_5.2.37.orig.tar.xz' bash_5.2.37.orig.tar.xz 9999 SHA256:a\n"
    "'http://deb.debian.org/debian/pool/main/b/bash/bash_5.2.37-2.debian.tar.xz' "
    "bash_5.2.37-2.debian.tar.xz 50 SHA256:b\n"
    "'http://deb.debian.org/debian/pool/main/b/bash/bash_5.2.37-2.dsc' bash_5.2.37-2.dsc 20 SHA256:c\n"
)


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


class DownloadTestCase(testtools.TestCase):

    @staticmethod
    def _run(stdout, returncode=0):
        def run(args, cwd=None):
            return subprocess.CompletedProcess(args, returncode, stdout, '')
        return run

    def test_source_uris_picks_dsc_and_debian_tar(self):
        dsc, debian = _source_uris('bash', '5.2.37-2', run=self._run(SAMPLE_URIS))
        self.assertTrue(dsc.endswith('.dsc'))
        self.assertIn('.debian.tar.xz', debian)

    def test_source_uris_failure(self):
        self.assertEqual((None, None), _source_uris('bash', '5.2.37-2', run=self._run('', returncode=100)))

    def test_download_fetches_dsc_and_debian_tar_only(self):
        fetched = []

        def fetch(url, dest_path):
            fetched.append(url)
            open(dest_path, 'w').close()

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ok = _download_source('bash', '5.2.37-2', tmp.name, run=self._run(SAMPLE_URIS), fetch=fetch)
        self.assertTrue(ok)
        # The .dsc and .debian.tar.xz are fetched; the large .orig tarball is skipped.
        self.assertEqual(2, len(fetched))
        self.assertFalse(any('.orig.' in url for url in fetched))


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
