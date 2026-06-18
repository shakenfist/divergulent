# Phase 4 — Signing, client verification, and spot-verify

Part of [PLAN-published-cache.md](PLAN-published-cache.md).
High effort: a trust-critical phase. Sigstore signing in CI, an opt-in
in-process verifier on the client, and an always-on "no cry wolf"
spot-check of the bundle against live origins.

**Status: implemented (CI signing pending a real run).** `divergulent/
verify.py` adds `verify_signature` (lazy `sigstore` import, SKIPPED when
the `verify` extra is absent) and `spot_check` (immutable-divergence
exact match vs live, inconclusive on UNKNOWN); the `verify` extra is
declared (`sigstore>=4.3,<5`). `cache pull` downloads the `.sigstore.json`,
runs both checks and stores both files verbatim only on success, with
`--spot-check N` / `--require-signature` / `--insecure`; `cache verify`
re-checks a stored or given bundle. `build-cache.yml` signs the bundle
(`tools/sign-bundle.sh`, `id-token: write`). Tests are offline (sigstore
objects injected; SKIPPED path real; spot-check with fake live sources;
the real-`sigstore` FAILED path was confirmed manually). Suite green;
`pre-commit` clean. The signed-CI-run end-to-end (a real VERIFIED) needs
a `workflow_dispatch`, like the phase-1 measurement.

The sigstore API was verified empirically against **sigstore 4.3.0**:
`Verifier.production().verify_artifact(input_, bundle, policy)`,
`policy.Identity(identity=, issuer=)`, `Bundle.from_json`,
`sigstore.errors.VerificationError`; the signing CLI emits
`<input>.sigstore.json`.

Scope note: the spot-check verifies **divergence only** (exact, immutable)
as the integrity gate; the softer advisory staleness "not ahead" sample is
deferred (it is noisy and Repology-rate-limited for little gain). The
build-cache artifact is still named `cache-debian13.json.gz`; reconciling
it with the client's `cache-<release>.json.gz` expectation (and the signed
asset name) is part of phase 5's publishing.

## Prompt

Read, and reuse rather than reinvent:

- `.github/workflows/release.yml` — the project already signs with
  Sigstore keyless OIDC **on a self-hosted runner**
  (`[self-hosted, debian-12, s]`, `id-token: write`): it signs the git tag
  with `gitsign` and attests artifacts with `attest-build-provenance`.
  This proves keyless OIDC works on these runners; reuse that permission
  pattern. (We sign a *blob* here, not a tag, so the tool differs — see
  the signing decision.)
- `.github/workflows/build-cache.yml` + `tools/build-cache.sh` — where the
  bundle is produced; the signing step is added here (and reused by the
  phase-5 scheduled publish).
- `divergulent/cli.py` — `cache pull` (`_cache_pull_command`,
  `_atomic_write_bytes`), `_http_client().get_bytes`, `bundle.stored_path`,
  and the validate-before-store flow verification slots into.
- `divergulent/bundle.py` — `loads`/`stored_path`; the stored bytes are
  kept **verbatim** (phase 3), which is what a detached signature verifies
  against.
- `divergulent/sources/repology.py` and `sources/debian_patches.py` — the
  live sources the spot-check queries; divergence for a fixed
  `(source, version)` is immutable, which is what makes an exact spot-check
  possible.
- Packaging (`pyproject.toml`/`setup.cfg`) — where the optional `verify`
  extra is declared.

Where the design touches Sigstore, research the current `sigstore-python`
API (it is version-sensitive) rather than guess; pin the version.

## Objective

The publisher signs each bundle with Sigstore keyless OIDC in CI,
producing a detached signature published beside it. The client, when the
optional `sigstore` extra is installed, verifies that signature against
the **expected workflow identity** before trusting a downloaded bundle;
and **always** spot-verifies a random sample of the bundle's entries
against the live origins, refusing a bundle whose data demonstrably
disagrees. Verification happens at `cache pull` time (a one-off gate), so
day-to-day use stays fast. Dependency minimalism is preserved: the base
install is unchanged (stdlib + python-debian); `sigstore` is opt-in.

## Design decisions

- **Trust model: two independent checks, both fail-closed.**
  1. **Signature (provenance).** Proves the bundle came from our CI
     workflow and was not tampered with in transit or at rest. Requires
     the opt-in `sigstore` extra; absent it, this check is *skipped with a
     printed notice*, not failed.
  2. **Spot-check (content).** Proves the bundle's *data* matches reality
     by sampling it against the live origins. Always runs (stdlib + the
     existing live sources). This is the divergulent-native "no cry wolf"
     guarantee and does not depend on any external tooling.
  A bundle is stored only if neither check fails. A *skipped* signature
  check (no extra) is not a failure; a *failed* one is.

- **Signing (CI) with `sigstore-python`, for symmetry.** Add a signing
  step to `build-cache.yml` with `id-token: write` (the pattern
  `release.yml` proves works on the self-hosted runner). Sign the bundle
  *blob* with `sigstore-python` (`python -m sigstore sign
  cache-<release>.json.gz`) rather than `gitsign`/`cosign`, so the emitted
  `cache-<release>.json.gz.sigstore` is exactly the format the client's
  `verify` extra (also `sigstore-python`) consumes — one tool, one bundle
  format, both ends. Upload it beside the bundle. No `release` environment
  gate is needed (this signs an artifact, it does not publish to PyPI);
  phase 5 decides any gating for the scheduled publish.

- **Optional `verify` extra + lazy import.** Declare
  `verify = ["sigstore==<pinned>"]` as a packaging extra. A new
  `divergulent/verify.py` imports `sigstore` **inside the function**, so
  the base install never imports it. `verify_signature(bundle_bytes,
  signature_bytes)` returns one of VERIFIED / FAILED / SKIPPED (skipped
  when the import fails), checking the certificate identity equals the
  expected workflow ref and the issuer equals GitHub Actions' OIDC issuer.

- **Expected identity is a constant (provisional).**
  `EXPECTED_SIGNER_ISSUER = 'https://token.actions.githubusercontent.com'`
  and `EXPECTED_SIGNER_IDENTITY =
  'https://github.com/shakenfist/divergulent/.github/workflows/build-cache.yml@refs/heads/main'`.
  This must match exactly what the signing workflow produces; if phase 5
  moves signing into a dedicated publish workflow, the identity changes —
  finalise it there. Keep it overridable for testing.

- **Spot-check against live origins (always on, stdlib).**
  `spot_check(bundle, repology, patches, sample, rng)`:
  - Sample up to `sample` random entries from `bundle.divergence`
    (default 8; `--spot-check N`, 0 disables). Divergence for a fixed
    `(source, version)` is **immutable**, so the bundle's
    `(state, total, format)` must equal a live `DebianPatchesSource.summary`
    exactly. A definite disagreement is strong evidence of corruption or
    tampering → the bundle is refused.
  - "No cry wolf" applies to the checker itself: a live lookup that
    returns UNKNOWN/None (a transient failure, an unresolvable package) is
    **inconclusive**, never counted as a mismatch. Only a definite,
    differing live result fails the check.
  - Staleness is a softer, advisory sample (the bundle's newest must not
    be *ahead* of live Repology — newest only increases — but exact match
    is not required since the map ages); it warns, it does not refuse.
  - Sample size stays small for politeness; the live divergence half is
    the unthrottled, concurrent sources.debian.org.

- **Where verification runs.** At `cache pull`: download the bundle **and**
  its `.sigstore` (derived as `<bundle-url>.sigstore`), verify the
  signature (if the extra is present), spot-check, and only then store
  both files verbatim. Flags: `--spot-check N` (sample size; 0 disables),
  `--require-signature` (treat a skipped/failed signature as fatal — for
  users who installed the extra and want to enforce it), and `--insecure`
  (skip all verification; loudly noted). A standalone `cache verify
  [--bundle PATH]` re-runs both checks on a stored or given bundle without
  re-downloading.

## Steps

| Step | Effort | Model | Brief for sub-agent |
|------|--------|-------|---------------------|
| 4a | medium | opus | Add a Sigstore signing step to `build-cache.yml` (`id-token: write`, reusing the keyless pattern `release.yml` proves on the self-hosted runner) that signs the bundle blob with `sigstore-python` and uploads `cache-<release>.json.gz.sigstore` beside it. Put any multi-line shell in `tools/`. actionlint clean. |
| 4b | high | opus | Declare the `verify` packaging extra (`sigstore`, pinned). Add `divergulent/verify.py` with a lazy-import `verify_signature(bundle_bytes, sig_bytes) -> result` (VERIFIED/FAILED/SKIPPED), the expected identity/issuer constants (overridable), against the real `sigstore-python` API. Offline tests: SKIPPED when the import is unavailable; the identity policy is built correctly; FAILED on a bad signature and VERIFIED on a good one are exercised with the verifier mocked (and, if cheap, a recorded fixture bundle/sig). |
| 4c | medium | opus | Add `spot_check(bundle, repology, patches, sample, rng) -> result` to `verify.py`: sample divergence entries, compare exactly against live `summary()`, refuse on a definite mismatch, treat live UNKNOWN/None as inconclusive, and add the advisory staleness "not ahead" check. Offline tests with fake live sources: agreeing sample passes; one definite divergence mismatch fails; all-inconclusive passes (no cry wolf); staleness-ahead warns but does not fail; sampling is bounded and deterministic under an injected rng. |
| 4d | high | opus | Wire verification into `cache pull`: download the `.sigstore` too, run signature verify + spot-check, store both verbatim only on success; add `--spot-check N`, `--require-signature`, `--insecure`. Add a `cache verify [--bundle PATH]` command reusing the same functions. Offline tests: a tampered bundle (divergence altered) is refused and nothing stored; a good bundle stores both files; `--insecure` skips; `--require-signature` fails without the extra; `cache verify` reports pass/fail on a stored bundle. |
| 4e | low | sonnet | Update `README.md` (the `verify` extra, `cache verify`, the `--spot-check`/`--require-signature`/`--insecure` flags, the trust model), `ARCHITECTURE.md` (`verify.py`, the two-check flow), `AGENTS.md` (the trust model, lazy import, no-cry-wolf spot-check, identity constant), and the phase/master/index plan statuses. |

## Testing requirements

- Offline. Mock the `sigstore` verifier (and exercise the absent-extra
  SKIPPED path by simulating the import failure); fake live sources for
  the spot-check; an injected rng for deterministic sampling; a temp
  cache dir. If a tiny real bundle+signature fixture can be produced
  cheaply, use it to exercise a genuine VERIFIED/FAILED once.
- No live network in unit tests.
- `pre-commit run --all-files` green (actionlint covers the new signing
  step).

## Success criteria

- The build-cache CI run produces a `.sigstore` signature beside the
  bundle.
- With the `verify` extra installed, `cache pull` verifies the signature
  against the expected identity and refuses a bundle that fails; without
  the extra it prints a notice and proceeds to the spot-check (unless
  `--require-signature`).
- `cache pull` always spot-checks a random sample against live origins and
  refuses a bundle whose divergence demonstrably disagrees; a transient
  live failure never causes a false refusal.
- A bundle is stored (both `.json.gz` and `.sigstore`, verbatim) only when
  verification passes; `--insecure` bypasses with a loud notice.
- `cache verify` re-checks a stored or given bundle on demand.
- The base install gains **no** new runtime dependency; `sigstore` is
  opt-in via `divergulent[verify]`.

## Out of scope (later phases)

- Scheduled daily publishing of the signed bundle to GitHub Releases
  `latest` (phase 5) — phase 4 signs and verifies; phase 5 publishes.
- Reproducible-build cross-verification by a second builder (Future work).
- Multiple-mirror failover (Future work).

## Open questions

- **Self-hosted runner OIDC** — *largely answered*: `release.yml` already
  does keyless Sigstore signing (`gitsign`, `attest-build-provenance`) on
  self-hosted runners with `id-token: write`, so the `debian-13` runner
  should mint a token for `sigstore sign`. Confirm on first run; if the
  specific runner differs, fall back to a GitHub-hosted signing job.
- **`sigstore-python` API + version** — pin a version and code against its
  actual verify API (it changes between releases).
- **Expected identity finalisation** — the workflow ref baked into the
  client must match the signing workflow; revisit when phase 5 decides
  whether signing lives in `build-cache.yml` or a publish workflow.
- **Spot-check sample size / refusal policy** — confirm 8 is polite enough
  and that a single definite divergence mismatch should hard-refuse
  (recommended, since divergence is immutable).

## Back brief

Before executing, back brief the operator on your understanding of this
phase.
