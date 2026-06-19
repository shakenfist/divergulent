import io
import json
import os
import tarfile
import tempfile

import testtools

from divergulent.classify import corpus
from divergulent.classify.corpus import build_corpus, body_sha256


PATCH_A = '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n'
PATCH_B = '--- a/y\n+++ b/y\n@@ -2 +2 @@\n-c\n+d\n'


def _fake_fetch(table):
    """Build a fetch() over a dict ``{(pkg, ver): (format, texts)}``.

    A value of the string ``'raise'`` makes the fetch raise, exercising the
    fetch-error path; otherwise the value is returned verbatim.
    """
    def fetch(source_package, version):
        value = table[(source_package, version)]
        if value == 'raise':
            raise RuntimeError('boom')
        return value
    return fetch


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


class BuildCorpusTestCase(testtools.TestCase):

    def _corpus_dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def test_mixed_worklist_accounts_for_every_outcome(self):
        table = {
            ('patched-pkg', '1-1'): ('3.0 (quilt)', {'a.patch': PATCH_A, 'b.patch': PATCH_B}),
            ('clean-pkg', '1-1'): ('3.0 (quilt)', {}),
            ('native-pkg', '1'): ('3.0 (native)', None),
            ('nonquilt-pkg', '1-1'): ('1.0', None),
            ('broken-pkg', '1-1'): (None, None),
            ('error-pkg', '1-1'): 'raise',
        }
        corpus_dir = self._corpus_dir()
        stats = build_corpus(table.keys(), corpus_dir, fetch=_fake_fetch(table), max_workers=4)

        self.assertEqual(6, stats.packages_processed)
        self.assertEqual(1, stats.patched)
        self.assertEqual(1, stats.clean)
        self.assertEqual(1, stats.native)
        # Non-quilt, unresolved download, and the raised error all land as unknown.
        self.assertEqual(3, stats.unknown)
        self.assertEqual(2, stats.fetch_failures)  # unresolved + raised error
        self.assertEqual(2, stats.patch_rows_written)
        self.assertEqual(2, stats.distinct_bodies)

        # Content-addressed blobs exist for both bodies.
        for text in (PATCH_A, PATCH_B):
            sha = body_sha256(text)
            blob = os.path.join(corpus_dir, 'bodies', sha[:2], sha)
            self.assertTrue(os.path.exists(blob))
            with open(blob, encoding='utf-8') as handle:
                self.assertEqual(text, handle.read())

        # patches.jsonl: one row per (package, version, patch).
        patch_rows = _read_jsonl(os.path.join(corpus_dir, 'patches.jsonl'))
        self.assertEqual(2, len(patch_rows))
        names = {row['patch_name']: row['raw_sha256'] for row in patch_rows}
        self.assertEqual(body_sha256(PATCH_A), names['a.patch'])
        self.assertEqual(body_sha256(PATCH_B), names['b.patch'])
        self.assertTrue(all(row['source_package'] == 'patched-pkg' for row in patch_rows))

        # packages.jsonl: one honest row per processed package.
        package_rows = {row['source_package']: row for row in _read_jsonl(
            os.path.join(corpus_dir, 'packages.jsonl'))}
        self.assertEqual(6, len(package_rows))
        self.assertEqual('patched', package_rows['patched-pkg']['state'])
        self.assertEqual(2, package_rows['patched-pkg']['n_patches'])
        self.assertIsNone(package_rows['patched-pkg']['error'])
        self.assertEqual('clean', package_rows['clean-pkg']['state'])
        self.assertEqual('native', package_rows['native-pkg']['state'])
        self.assertEqual('unknown', package_rows['nonquilt-pkg']['state'])
        self.assertEqual('non-quilt-format', package_rows['nonquilt-pkg']['error'])
        self.assertEqual('fetch-failed', package_rows['broken-pkg']['error'])
        self.assertTrue(package_rows['error-pkg']['error'].startswith('fetch-error:'))

    def test_identical_bodies_dedup_in_store_but_keep_provenance(self):
        # Two different packages carry the SAME raw body under different names.
        table = {
            ('pkg-one', '1-1'): ('3.0 (quilt)', {'shared.patch': PATCH_A}),
            ('pkg-two', '2-1'): ('3.0 (quilt)', {'also-shared.patch': PATCH_A}),
        }
        corpus_dir = self._corpus_dir()
        stats = build_corpus(table.keys(), corpus_dir, fetch=_fake_fetch(table), max_workers=2)

        # One blob in the store, two manifest rows.
        self.assertEqual(1, stats.distinct_bodies)
        self.assertEqual(2, stats.patch_rows_written)
        sha = body_sha256(PATCH_A)
        bodies_root = os.path.join(corpus_dir, 'bodies')
        blob_count = sum(len(files) for _, _, files in os.walk(bodies_root))
        self.assertEqual(1, blob_count)

        patch_rows = _read_jsonl(os.path.join(corpus_dir, 'patches.jsonl'))
        self.assertEqual(2, len(patch_rows))
        self.assertTrue(all(row['raw_sha256'] == sha for row in patch_rows))
        self.assertEqual({'pkg-one', 'pkg-two'}, {row['source_package'] for row in patch_rows})

    def test_resumable_second_run_does_not_duplicate_or_reprocess(self):
        table = {
            ('patched-pkg', '1-1'): ('3.0 (quilt)', {'a.patch': PATCH_A}),
            ('clean-pkg', '1-1'): ('3.0 (quilt)', {}),
        }
        corpus_dir = self._corpus_dir()

        first = build_corpus(table.keys(), corpus_dir, fetch=_fake_fetch(table), max_workers=2)
        self.assertEqual(2, first.packages_processed)

        # A second fetch that would raise if called proves nothing is re-fetched.
        def exploding_fetch(source_package, version):
            raise AssertionError('should not re-fetch %s %s' % (source_package, version))

        second = build_corpus(table.keys(), corpus_dir, fetch=exploding_fetch, max_workers=2)
        self.assertEqual(0, second.packages_processed)

        # Manifests are unchanged: no duplicate rows.
        self.assertEqual(1, len(_read_jsonl(os.path.join(corpus_dir, 'patches.jsonl'))))
        self.assertEqual(2, len(_read_jsonl(os.path.join(corpus_dir, 'packages.jsonl'))))


class RetryAndResumeTestCase(testtools.TestCase):

    def _corpus_dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def test_with_retries_succeeds_after_transient_failures(self):
        calls = {'n': 0}
        sleeps = []

        def flaky():
            calls['n'] += 1
            if calls['n'] < 3:
                raise OSError('temporary failure in name resolution')
            return ('3.0 (quilt)', {'a.patch': PATCH_A})

        result = corpus._with_retries(flaky, attempts=3, sleep=sleeps.append)
        self.assertEqual(('3.0 (quilt)', {'a.patch': PATCH_A}), result)
        self.assertEqual(3, calls['n'])
        self.assertEqual(2, len(sleeps))  # backed off before each retry

    def test_with_retries_reraises_after_exhausting_attempts(self):
        def always_fails():
            raise OSError('still failing')

        self.assertRaises(
            OSError, corpus._with_retries, always_fails, attempts=3, sleep=lambda _d: None)

    def test_resume_retries_a_transient_failure(self):
        # First run: the package fetch raises, recorded as a transient failure.
        failing = {('flaky-pkg', '1-1'): 'raise'}
        corpus_dir = self._corpus_dir()
        first = build_corpus(failing.keys(), corpus_dir, fetch=_fake_fetch(failing), max_workers=1)
        self.assertEqual(1, first.fetch_failures)
        self.assertEqual(0, first.patched)
        # A transient failure is NOT terminal, so it is not "done".
        self.assertEqual(set(), corpus._read_done(corpus_dir))

        # Second run over the same worklist: now it succeeds and is processed.
        succeeding = {('flaky-pkg', '1-1'): ('3.0 (quilt)', {'a.patch': PATCH_A})}
        second = build_corpus(succeeding.keys(), corpus_dir, fetch=_fake_fetch(succeeding), max_workers=1)
        self.assertEqual(1, second.packages_processed)
        self.assertEqual(1, second.patched)
        self.assertEqual({('flaky-pkg', '1-1')}, corpus._read_done(corpus_dir))
        # The patch row now exists exactly once.
        self.assertEqual(1, len(_read_jsonl(os.path.join(corpus_dir, 'patches.jsonl'))))

    def test_terminal_nonquilt_outcome_is_not_retried(self):
        # A non-quilt source is a terminal classification, not a transient
        # failure, so it must not be re-fetched on resume.
        table = {('nonquilt-pkg', '1-1'): ('1.0', None)}
        corpus_dir = self._corpus_dir()
        build_corpus(table.keys(), corpus_dir, fetch=_fake_fetch(table), max_workers=1)
        self.assertEqual({('nonquilt-pkg', '1-1')}, corpus._read_done(corpus_dir))

        def exploding_fetch(source_package, version):
            raise AssertionError('should not re-fetch a terminal non-quilt result')

        second = build_corpus(table.keys(), corpus_dir, fetch=exploding_fetch, max_workers=1)
        self.assertEqual(0, second.packages_processed)


def _add(tar, name, content):
    data = content.encode()
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


class EndToEndExtractTestCase(testtools.TestCase):
    """Exercise the real fetch_patch_texts/_extract_patches path offline.

    Only the download step is injected (it writes a synthesised .dsc +
    .debian.tar.xz); the format read and series extraction run for real.
    """

    def test_synthesised_debian_tar_is_extracted_and_stored(self):
        from divergulent.classify.corpus import _process

        def download(source_package, version, dest):
            with open(os.path.join(dest, 'pkg.dsc'), 'w') as handle:
                handle.write('Format: 3.0 (quilt)\n')
            with tarfile.open(os.path.join(dest, 'pkg.debian.tar.xz'), 'w:xz') as tar:
                _add(tar, 'debian/patches/series', 'first.patch\nsecond.patch\n')
                _add(tar, 'debian/patches/first.patch', PATCH_A)
                _add(tar, 'debian/patches/second.patch', PATCH_B)
            return True

        def fetch(source_package, version):
            return corpus.fetch_patch_texts(source_package, version, download=download)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        package_row, patch_rows, distinct_new = _process(
            'real-pkg', '3-1', corpus_dir=tmp.name, fetch=fetch)

        self.assertEqual('patched', package_row['state'])
        self.assertEqual('3.0 (quilt)', package_row['source_format'])
        self.assertEqual(2, package_row['n_patches'])
        self.assertEqual(2, distinct_new)
        shas = {row['patch_name']: row['raw_sha256'] for row in patch_rows}
        self.assertEqual(body_sha256(PATCH_A), shas['first.patch'])
        self.assertEqual(body_sha256(PATCH_B), shas['second.patch'])
