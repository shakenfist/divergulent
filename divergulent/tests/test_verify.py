import random

import testtools

from divergulent import bundle
from divergulent import verify
from divergulent.sources.debian_patches import DivergenceState, DivergenceSummary


def _bundle(divergence):
    return bundle.Bundle(
        schema=bundle.SCHEMA_VERSION,
        cache_schema=bundle.CACHE_SCHEMA_VERSION,
        generated_at='2026-06-18T00:00:00+00:00',
        release='trixie',
        repology_repo='debian_unstable',
        built_on={'arch': 'amd64', 'release': 'trixie'},
        staleness={},
        divergence=divergence)


# --- signature verification (sigstore objects injected) -----------------------


class FakeBundle:
    last = None

    @classmethod
    def from_json(cls, data):
        cls.last = data
        return ('parsed', data)


class FakeBadBundle:
    @classmethod
    def from_json(cls, data):
        raise ValueError('not a bundle')


class FakePolicyModule:
    class Identity:
        def __init__(self, *, identity, issuer):
            self.identity = identity
            self.issuer = issuer


class FakeVerificationError(Exception):
    pass


def _verifier(outcome):
    class FakeVerifier:
        captured = {}

        @classmethod
        def production(cls):
            return cls()

        def verify_artifact(self, artifact, signature_bundle, policy):
            FakeVerifier.captured = {
                'artifact': artifact, 'bundle': signature_bundle,
                'identity': policy.identity, 'issuer': policy.issuer}
            if outcome == 'raise':
                raise FakeVerificationError('identity mismatch')

    return FakeVerifier


class VerifySignatureCoreTestCase(testtools.TestCase):

    def test_verified_passes_identity_and_artifact(self):
        verifier = _verifier('ok')
        result = verify._verify_with_sigstore(
            FakeBundle, verifier, FakePolicyModule, FakeVerificationError,
            b'artifact', b'sig', ('expected-id',), 'expected-issuer')
        self.assertEqual(verify.SignatureStatus.VERIFIED, result.status)
        self.assertEqual(b'artifact', verifier.captured['artifact'])
        self.assertEqual('expected-id', verifier.captured['identity'])
        self.assertEqual('expected-issuer', verifier.captured['issuer'])

    def test_verification_error_is_failed(self):
        result = verify._verify_with_sigstore(
            FakeBundle, _verifier('raise'), FakePolicyModule, FakeVerificationError,
            b'artifact', b'sig', ('id',), 'iss')
        self.assertEqual(verify.SignatureStatus.FAILED, result.status)

    def test_second_identity_verifies_when_the_first_does_not(self):
        # The default branch rename means a published bundle may carry either
        # ref in its certificate, so a candidate that fails must not end the
        # search. Fails on the first identity, verifies on the second.
        class FirstFailsVerifier:
            captured = {}
            attempts = []

            @classmethod
            def production(cls):
                return cls()

            def verify_artifact(self, artifact, signature_bundle, policy):
                FirstFailsVerifier.attempts.append(policy.identity)
                if policy.identity == 'old-id':
                    raise FakeVerificationError('identity mismatch')
                FirstFailsVerifier.captured = {'identity': policy.identity}

        result = verify._verify_with_sigstore(
            FakeBundle, FirstFailsVerifier, FakePolicyModule, FakeVerificationError,
            b'artifact', b'sig', ('old-id', 'new-id'), 'iss')
        self.assertEqual(verify.SignatureStatus.VERIFIED, result.status)
        self.assertEqual(['old-id', 'new-id'], FirstFailsVerifier.attempts)
        self.assertEqual('new-id', FirstFailsVerifier.captured['identity'])

    def test_failed_when_no_identity_verifies(self):
        result = verify._verify_with_sigstore(
            FakeBundle, _verifier('raise'), FakePolicyModule, FakeVerificationError,
            b'artifact', b'sig', ('old-id', 'new-id'), 'iss')
        self.assertEqual(verify.SignatureStatus.FAILED, result.status)
        # Every candidate is named, not just the last. The last is the
        # vestigial pre-rename ref, so reporting it alone would explain a
        # refusal in terms of the identity the bundle never had.
        self.assertIn('old-id', result.detail)
        self.assertIn('new-id', result.detail)

    def test_verifier_is_built_once_not_once_per_identity(self):
        # The verifier loads Sigstore's TUF trust root and does not depend on
        # the identity, so a failed first candidate must not pay for it twice.
        class CountingVerifier:
            builds = 0

            @classmethod
            def production(cls):
                cls.builds += 1
                return cls()

            def verify_artifact(self, artifact, signature_bundle, policy):
                raise FakeVerificationError('identity mismatch')

        verify._verify_with_sigstore(
            FakeBundle, CountingVerifier, FakePolicyModule, FakeVerificationError,
            b'artifact', b'sig', ('a', 'b', 'c'), 'iss')
        self.assertEqual(1, CountingVerifier.builds)

    def test_empty_identities_is_failed(self):
        result = verify._verify_with_sigstore(
            FakeBundle, _verifier('ok'), FakePolicyModule, FakeVerificationError,
            b'artifact', b'sig', (), 'iss')
        self.assertEqual(verify.SignatureStatus.FAILED, result.status)
        self.assertIn('no expected signer identity was configured', result.detail)

    def test_malformed_signature_is_failed(self):
        result = verify._verify_with_sigstore(
            FakeBadBundle, _verifier('ok'), FakePolicyModule, FakeVerificationError,
            b'artifact', b'not-a-bundle', ('id',), 'iss')
        self.assertEqual(verify.SignatureStatus.FAILED, result.status)


class VerifySignatureArgumentTestCase(testtools.TestCase):

    def test_a_bare_string_is_refused(self):
        # The parameter was `identity` and took one string. A stale caller
        # passing one would otherwise iterate it character by character and
        # report a mismatch against the identity 'l'.
        self.assertRaises(
            TypeError, verify.verify_signature, b'artifact', b'sig',
            identities='https://github.com/shakenfist/divergulent/x.yml@refs/heads/main')


class VerifySignatureSkipTestCase(testtools.TestCase):

    def test_skipped_when_sigstore_absent(self):
        # The test environment does not install sigstore, so the lazy import
        # fails and verification is skipped (not failed).
        result = verify.verify_signature(b'artifact', b'sig')
        self.assertEqual(verify.SignatureStatus.SKIPPED, result.status)


# --- spot-check against the live origin --------------------------------------


class FakePatches:
    def __init__(self, by_source):
        self.by_source = by_source
        self.calls = []

    def summary(self, source, version):
        self.calls.append((source, version))
        return self.by_source.get(source)


def _summary(source, version, total, state):
    return DivergenceSummary(source, version, '3.0 (quilt)', total, state)


class SpotCheckTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.bundle = _bundle({
            'bash': {'version': '5.2-1', 'format': '3.0 (quilt)', 'total': 2, 'state': 'patched'},
            'hello': {'version': '2.10-3', 'format': '3.0 (native)', 'total': 0, 'state': 'native'},
        })

    def test_agreeing_sample_passes(self):
        patches = FakePatches({
            'bash': _summary('bash', '5.2-1', 2, DivergenceState.PATCHED),
            'hello': _summary('hello', '2.10-3', 0, DivergenceState.NATIVE),
        })
        result = verify.spot_check(self.bundle, patches, sample=8, rng=random.Random(0))
        self.assertEqual(verify.SpotCheckStatus.PASSED, result.status)
        self.assertEqual(2, result.checked)

    def test_definite_disagreement_is_mismatch(self):
        patches = FakePatches({
            'bash': _summary('bash', '5.2-1', 5, DivergenceState.PATCHED),  # total differs
            'hello': _summary('hello', '2.10-3', 0, DivergenceState.NATIVE),
        })
        result = verify.spot_check(self.bundle, patches, sample=8, rng=random.Random(0))
        self.assertEqual(verify.SpotCheckStatus.MISMATCH, result.status)
        self.assertEqual(1, len(result.mismatches))

    def test_bundle_unknown_is_inconclusive_not_mismatch(self):
        # A bundle entry that itself says UNKNOWN (e.g. a transient build-time
        # fetch failure) is the bundle declining to claim, not a contradiction,
        # even when the live source resolves it definitely.
        unknown_bundle = _bundle({
            'accessible-pygments': {
                'version': '0.0.5-2', 'format': '3.0 (quilt)', 'total': 0, 'state': 'unknown'},
        })
        patches = FakePatches({
            'accessible-pygments': _summary('accessible-pygments', '0.0.5-2', 0, DivergenceState.CLEAN),
        })
        result = verify.spot_check(unknown_bundle, patches, sample=8, rng=random.Random(0))
        self.assertEqual(verify.SpotCheckStatus.PASSED, result.status)
        self.assertEqual(0, result.checked)
        self.assertEqual(1, result.inconclusive)
        # The bundle's UNKNOWN entry is skipped before any live query is made.
        self.assertEqual([], patches.calls)

    def test_live_unknown_is_inconclusive_not_mismatch(self):
        patches = FakePatches({
            'bash': _summary('bash', '5.2-1', 0, DivergenceState.UNKNOWN),
            'hello': None,
        })
        result = verify.spot_check(self.bundle, patches, sample=8, rng=random.Random(0))
        self.assertEqual(verify.SpotCheckStatus.PASSED, result.status)
        self.assertEqual(0, result.checked)
        self.assertEqual(2, result.inconclusive)

    def test_sample_zero_disables(self):
        patches = FakePatches({})
        result = verify.spot_check(self.bundle, patches, sample=0)
        self.assertEqual(verify.SpotCheckStatus.PASSED, result.status)
        self.assertEqual([], patches.calls)

    def test_sample_is_bounded_by_population(self):
        patches = FakePatches({
            'bash': _summary('bash', '5.2-1', 2, DivergenceState.PATCHED),
            'hello': _summary('hello', '2.10-3', 0, DivergenceState.NATIVE),
        })
        result = verify.spot_check(self.bundle, patches, sample=50, rng=random.Random(1))
        # Only two entries exist, so at most two are checked.
        self.assertEqual(2, len(patches.calls))
        self.assertEqual(verify.SpotCheckStatus.PASSED, result.status)
