"""Client-side classification display + pull (phase 5, P3).

The client HASHES a patch it already fetched and looks the verdict up in the
published bundle -- it runs no classifier and no LLM. These assert the join key
(the fingerprint on PatchDetail) matches the curation side's, that `show` renders
the per-package breakdown and per-patch "why", that an absent bundle changes
nothing, and that `cache pull-classification` stores a downloaded bundle.
"""
import contextlib
import io
import json
import os
import tempfile
from unittest import mock

import testtools

from divergulent import cli
from divergulent import debversion
from divergulent.classify import classification_bundle as cb
from divergulent.classify import fingerprint as fingerprint_mod
from divergulent.dep3 import PatchClass
from divergulent.inventory import InstalledPackage
from divergulent.sources.debian_patches import (
    DivergenceState, PackagePatches, PatchDetail, patch_detail)
from divergulent.sources.repology import StalenessResult, StalenessState


_PATCH_TEXT = (
    'Description: fix a thing\n'
    'Forwarded: no\n'
    '--- a/src/foo.c\n'
    '+++ b/src/foo.c\n'
    '@@ -1,3 +1,3 @@\n'
    ' int main(void) {\n'
    '-  return 0;\n'
    '+  return 1;\n'
    ' }\n')


def _pkg(binary, source, source_version):
    return InstalledPackage(
        binary_name=binary, binary_version=debversion.parse('1-1'),
        source_name=source, source_version=debversion.parse(source_version),
        architecture='amd64')


PACKAGES = [_pkg('bash', 'bash', '5.2-1')]


class FakeRepology:
    def __init__(self, result):
        self.result = result

    def staleness(self, name, version):
        return self.result


class FakePatches:
    def __init__(self, package):
        self.package = package

    def details(self, name, version):
        return self.package


class FingerprintJoinTestCase(testtools.TestCase):

    def test_patch_detail_fingerprint_matches_the_curation_key(self):
        # The join only works if the client's fingerprint equals the bare hex
        # digest the curation side keys the ledger on.
        detail = patch_detail('foo.diff', _PATCH_TEXT)
        _version, digest = fingerprint_mod.fingerprint(_PATCH_TEXT)
        self.assertEqual(digest, detail.fingerprint)

    def test_description_and_diff_do_not_both_enter_the_fingerprint(self):
        # Two patches with the same diff but different DEP-3 descriptions share a
        # fingerprint (the header is a claim, not content).
        other = _PATCH_TEXT.replace('fix a thing', 'totally different claim')
        self.assertEqual(
            patch_detail('a', _PATCH_TEXT).fingerprint,
            patch_detail('b', other).fingerprint)


class ShowDisplayTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.staleness = StalenessResult(
            'bash', debversion.parse('5.2-1'), '5.3', StalenessState.BEHIND)
        self.package = PackagePatches(
            'bash', '5.2-1', '3.0 (quilt)', DivergenceState.PATCHED,
            [
                PatchDetail('sec.diff', PatchClass.DEBIAN_ONLY, 'a fix', None, [], fingerprint='fp-sec'),
                PatchDetail('feat.diff', PatchClass.DEBIAN_ONLY, 'a feature', None, [], fingerprint='fp-feat'),
                PatchDetail('mystery.diff', PatchClass.UNKNOWN, None, None, [], fingerprint='fp-none'),
            ])
        self.bundle = cb.ClassificationBundle(
            schema=cb.CLASSIFICATION_SCHEMA_VERSION, entry_schema=cb.ENTRY_SCHEMA_VERSION,
            category_enum_version=1, generated_at='2026-06-30T00:00:00Z', source_release='trixie',
            built_on={}, verdicts={
                'fp-sec': {'category': 'security', 'confidence': 'high', 'risk': 'high',
                           'reach': 'XL', 'reason': 'verified LLM triage by llm-triage:x (v3)',
                           'decided_by': 'llm-triage:x', 'rule_version': 3, 'kind': 'llm'},
                'fp-feat': {'category': 'feature', 'confidence': 'high',
                            'reason': 'deterministic rule features-dir (v1)',
                            'decided_by': 'features-dir', 'rule_version': 1, 'kind': 'heuristic'},
            })

    def _run(self, argv, classification):
        out = io.StringIO()
        with mock.patch('divergulent.cli.inventory.list_installed', return_value=PACKAGES), \
                mock.patch('divergulent.cli.RepologySource', return_value=FakeRepology(self.staleness)), \
                mock.patch('divergulent.cli.DebianPatchesSource', return_value=FakePatches(self.package)), \
                mock.patch('divergulent.cli._usable_classification', return_value=classification), \
                contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_breakdown_and_per_patch_why(self):
        rc, output = self._run(['show', 'bash'], self.bundle)
        self.assertEqual(0, rc)
        self.assertIn('classification: 1 feature, 1 security', output)  # sorted, count desc then name
        self.assertIn('class: security (risk high, reach XL)', output)
        self.assertIn('verified LLM triage by llm-triage:x', output)
        self.assertIn('class: feature', output)

    def test_unclassified_patch_has_no_verdict_line(self):
        rc, output = self._run(['show', 'bash'], self.bundle)
        # mystery.diff (fp-none) is not in the bundle, so only the two classified
        # patches get a "class: " verdict line.
        self.assertIn('mystery.diff', output)
        self.assertEqual(2, sum(1 for line in output.splitlines() if 'class: ' in line))

    def test_absent_bundle_leaves_output_unchanged(self):
        rc, output = self._run(['show', 'bash'], None)
        self.assertEqual(0, rc)
        self.assertNotIn('classification:', output)
        self.assertNotIn('class: ', output)

    def test_json_carries_fingerprint_and_classification(self):
        rc, output = self._run(['show', 'bash', '--json'], self.bundle)
        data = json.loads(output)
        by_name = {p['name']: p for p in data['patches']}
        self.assertEqual('fp-sec', by_name['sec.diff']['fingerprint'])
        self.assertEqual('security', by_name['sec.diff']['classification']['category'])
        self.assertIsNone(by_name['mystery.diff']['classification'])


class UsableClassificationTestCase(testtools.TestCase):

    def test_schema_mismatch_is_rejected(self):
        err = io.StringIO()
        bad = cb.ClassificationBundle(
            schema=999, entry_schema=1, category_enum_version=1,
            generated_at='2026-06-30T00:00:00Z', source_release='trixie', built_on={}, verdicts={})
        with contextlib.redirect_stderr(err):
            self.assertIsNone(cli._validate_classification(_dump(bad)))
        self.assertIn('schema not recognised', err.getvalue())


class PullClassificationTestCase(testtools.TestCase):

    def _bundle_bytes(self):
        bundle = cb.ClassificationBundle(
            schema=cb.CLASSIFICATION_SCHEMA_VERSION, entry_schema=cb.ENTRY_SCHEMA_VERSION,
            category_enum_version=1, generated_at='2026-06-30T00:00:00Z', source_release='trixie',
            built_on={}, verdicts={'fp': {'category': 'security', 'confidence': 'high',
                                          'reason': 'r', 'decided_by': 'x', 'rule_version': 1,
                                          'kind': 'llm'}})
        return _dump(bundle)

    def test_pull_stores_the_bundle(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = self._bundle_bytes()

        class FakeHttp:
            def get_bytes(self, url):
                return data if url.endswith('.json.gz') else None

        err = io.StringIO()
        with mock.patch('divergulent.cli._detect_release', return_value='trixie'), \
                mock.patch('divergulent.cli._http_client', return_value=FakeHttp()), \
                mock.patch('divergulent.cli.default_cache_dir', return_value=tmp.name), \
                contextlib.redirect_stderr(err):
            rc = cli.main(['cache', 'pull-classification', '--insecure'])
        self.assertEqual(0, rc)
        stored = cb.stored_path(tmp.name, 'trixie')
        self.assertTrue(os.path.exists(stored))
        self.assertEqual(1, len(cb.load(stored).verdicts))


def _dump(bundle):
    """A ClassificationBundle as gzipped-JSON bytes (what the wire carries)."""
    import gzip
    import json as _json
    return gzip.compress(_json.dumps(bundle.to_dict()).encode('utf-8'))
