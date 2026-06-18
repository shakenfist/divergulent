'''Trust checks for a downloaded cache bundle.

A bundle is optional and untrusted, so before it is stored two independent,
fail-closed checks run:

* **Signature (provenance).** A Sigstore keyless signature proves the bundle
  came from our CI workflow and was not altered in transit or at rest. This
  needs the optional ``sigstore`` dependency (``pip install
  divergulent[verify]``); without it the check is *skipped* (a notice), not
  failed, so the minimal default install keeps its stdlib-only footprint. The
  ``sigstore`` import is deliberately lazy so the base install never loads it.

* **Spot-check (content).** Independently of any signature, a random sample of
  the bundle's divergence entries is compared against the live origin. A fixed
  ``(source, version)`` patch set is immutable, so the bundle's
  ``(state, total)`` must equal a live ``summary()`` exactly; a definite
  disagreement is strong evidence of corruption or tampering. This uses only
  the standard library and the existing live source, so it is always available
  -- the divergulent-native "no cry wolf" guarantee. A live lookup that cannot
  resolve (UNKNOWN/None) is *inconclusive*, never counted as a mismatch.
'''
from __future__ import annotations

import enum
import random
from dataclasses import dataclass, field

from divergulent.sources.debian_patches import DivergenceState


# The Sigstore signature published beside the bundle is ``<bundle>.sigstore.json``
# (sigstore-python's default output name).
SIGNATURE_SUFFIX = '.sigstore.json'

# The identity the published bundle must be signed by. Sigstore binds the
# signing certificate to the CI workflow's OIDC identity; the client refuses a
# bundle signed by anything else. Provisional: if phase 5 moves signing into a
# dedicated publish workflow, this ref changes with it.
EXPECTED_SIGNER_ISSUER = 'https://token.actions.githubusercontent.com'
EXPECTED_SIGNER_IDENTITY = (
    'https://github.com/shakenfist/divergulent/.github/workflows/build-cache.yml@refs/heads/main')

# Default number of bundle entries to spot-check against the live origin. Small,
# to stay a polite client; the live half is the unthrottled sources.debian.org.
DEFAULT_SPOT_CHECK = 8


class SignatureStatus(enum.Enum):
    VERIFIED = 'verified'   # signed by the expected identity
    FAILED = 'failed'       # present but invalid, or not the expected identity
    SKIPPED = 'skipped'     # the optional sigstore dependency is not installed


class SpotCheckStatus(enum.Enum):
    PASSED = 'passed'       # no sampled entry disagreed with live data
    MISMATCH = 'mismatch'   # a sampled entry definitely disagreed


@dataclass(frozen=True)
class SignatureResult:
    status: SignatureStatus
    detail: str


@dataclass(frozen=True)
class SpotCheckResult:
    status: SpotCheckStatus
    checked: int
    inconclusive: int
    mismatches: list = field(default_factory=list)


def verify_signature(artifact_bytes: bytes, signature_bytes: bytes, *,
                     identity: str = EXPECTED_SIGNER_IDENTITY,
                     issuer: str = EXPECTED_SIGNER_ISSUER) -> SignatureResult:
    '''Verify a Sigstore signature over ``artifact_bytes`` against an identity.

    Returns SKIPPED (not FAILED) when the optional ``sigstore`` dependency is
    absent, so a minimal install is not penalised. The import is lazy for the
    same reason.
    '''
    try:
        from sigstore.errors import VerificationError
        from sigstore.models import Bundle
        from sigstore.verify import Verifier, policy
    except ImportError:
        return SignatureResult(
            SignatureStatus.SKIPPED,
            'sigstore not installed; run "pip install divergulent[verify]" to verify signatures')
    return _verify_with_sigstore(
        Bundle, Verifier, policy, VerificationError, artifact_bytes, signature_bytes, identity, issuer)


def _verify_with_sigstore(bundle_cls, verifier_cls, policy_mod, verification_error,
                          artifact_bytes, signature_bytes, identity, issuer) -> SignatureResult:
    '''The sigstore-backed core, with the library objects injected for testing.'''
    try:
        signature_bundle = bundle_cls.from_json(signature_bytes)
    except Exception as exc:  # noqa: BLE001 - any parse error is an invalid signature
        return SignatureResult(SignatureStatus.FAILED, 'signature is not a valid Sigstore bundle: %s' % exc)

    verification_policy = policy_mod.Identity(identity=identity, issuer=issuer)
    try:
        verifier_cls.production().verify_artifact(artifact_bytes, signature_bundle, verification_policy)
    except verification_error as exc:
        return SignatureResult(SignatureStatus.FAILED, str(exc))
    return SignatureResult(SignatureStatus.VERIFIED, 'signed by %s' % identity)


@dataclass(frozen=True)
class _Mismatch:
    source: str
    version: str
    bundle_state: str
    bundle_total: int
    live_state: str
    live_total: int

    def __str__(self) -> str:
        return '%s %s: bundle says %s/%d, live says %s/%d' % (
            self.source, self.version, self.bundle_state, self.bundle_total,
            self.live_state, self.live_total)


def spot_check(bundle, patches_source, *, sample: int = DEFAULT_SPOT_CHECK,
               rng: random.Random | None = None) -> SpotCheckResult:
    '''Compare a random sample of the bundle's divergence against the live origin.

    Divergence for a fixed ``(source, version)`` is immutable, so a sampled
    entry whose live ``summary()`` definitely differs is a mismatch (and the
    bundle should be refused). A live result that cannot resolve the package
    (UNKNOWN / None) is inconclusive -- never a false mismatch ("no cry wolf").
    '''
    rng = rng or random.Random()
    items = list(bundle.divergence.items())
    if not items or sample <= 0:
        return SpotCheckResult(SpotCheckStatus.PASSED, 0, 0, [])

    chosen = rng.sample(items, min(sample, len(items)))
    mismatches: list = []
    inconclusive = 0
    checked = 0
    for source, entry in chosen:
        version = entry.get('version')
        live = patches_source.summary(source, version)
        if live is None or live.state == DivergenceState.UNKNOWN:
            inconclusive += 1
            continue
        checked += 1
        if live.state.value != entry.get('state') or live.total != entry.get('total'):
            mismatches.append(_Mismatch(
                source, version, entry.get('state'), entry.get('total'), live.state.value, live.total))

    status = SpotCheckStatus.MISMATCH if mismatches else SpotCheckStatus.PASSED
    return SpotCheckResult(status, checked, inconclusive, mismatches)
