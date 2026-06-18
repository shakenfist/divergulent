# Phase 5 — Scheduled publish of the signed bundle

Part of [PLAN-published-cache.md](PLAN-published-cache.md).
Medium effort, but it makes the whole feature real: until a signed bundle
is published at a stable URL, `cache pull` (with no `--cache-url`) has
nothing to pull and every user falls back to the slow live path.

**Status: not started.**

## Prompt

Read, and reuse rather than reinvent:

- `.github/workflows/build-cache.yml` + `tools/build-cache.sh` +
  `tools/sign-bundle.sh` — the build and signing this phase schedules and
  publishes. The CI cache key (`divergulent-cache-trixie-<run_id>`,
  restore-key `divergulent-cache-trixie-`) already makes daily builds
  incremental (~80 s) vs a cold rebuild (~95 min) — see the phase-1
  measurements.
- `.github/workflows/release.yml` — the established publishing patterns
  (self-hosted runners, `softprops/action-gh-release`, `GITHUB_TOKEN`).
- `divergulent/cli.py` (`DEFAULT_CACHE_URL_TEMPLATE`) and
  `divergulent/verify.py` (`EXPECTED_SIGNER_IDENTITY`, `SIGNATURE_SUFFIX`)
  — the client constants that must match exactly what this phase
  publishes and how it is signed.

## Objective

Publish the signed bundle automatically so `cache pull` just works:
download → verify → store, fast and private, with no flags. Pair a cheap
daily incremental build with a periodic full `--refresh` rebuild (so a
once-bad value cannot live in the published bundle indefinitely), publish
the bundle and its `.sigstore.json` to a stable GitHub Releases URL, and
**reconcile the client constants** (download URL, asset name, expected
signer identity) with what is actually published — verified by a real
end-to-end `pull` that reports VERIFIED.

## Design decisions

- **Schedule: daily incremental + weekly full refresh.** Add `schedule:`
  cron triggers. The daily run restores the CI cache (cheap: only new
  versions are crawled); a weekly run passes `--refresh` for a clean
  rebuild. Keep `workflow_dispatch`. Stagger off the hour to be polite.
- **Stable publish location, not `/releases/latest`.** `/releases/latest`
  resolves to the newest *software* release (the tag-driven PyPI release),
  which must not be shadowed by cache uploads. Publish instead to a
  dedicated, in-place-updated release on a fixed tag (e.g. `cache`),
  marked **prerelease** so it never becomes the software "latest". The
  client URL becomes
  `https://github.com/shakenfist/divergulent/releases/download/cache/cache-<release>.json.gz`
  — update `DEFAULT_CACHE_URL_TEMPLATE` to match. Host stays configurable
  (`--cache-url`) for mirrors.
- **Reconcile the asset name.** Name the built/published assets
  `cache-<release>.json.gz` (+ `.sigstore.json`) — the client's
  expectation — rather than the phase-1 `cache-debian13.json.gz`. Detect
  the release in the workflow (or `build-cache.sh`) and name accordingly.
- **Finalise and verify the signer identity.** Signing stays in
  `build-cache.yml` on `main`, so the Sigstore cert identity remains
  `…/build-cache.yml@refs/heads/main` — matching the client's
  `EXPECTED_SIGNER_IDENTITY`. Confirm this against a *real* published
  signature (the first end-to-end VERIFIED); if it differs, update the
  constant. This closes the phase-4 "never verified end-to-end" risk.
- **Publish step.** After build + sign, upload both assets to the `cache`
  release (`gh release upload --clobber` or `action-gh-release` with the
  fixed tag), needing `contents: write`. Replace-in-place so the URL is
  rolling.
- **Single release for now; matrix is 1.0.** Phase 5 publishes the
  current (trixie) bundle and proves the pipeline end-to-end. Growing the
  build to a Debian 11/12/13/unstable/testing matrix is tracked in
  [PLAN-release-1.0.md](PLAN-release-1.0.md); the publish mechanism here
  should be matrix-ready (assets already keyed on `<release>`).

## Steps

| Step | Effort | Model | Brief for sub-agent |
|------|--------|-------|---------------------|
| 5a | medium | sonnet | Add daily + weekly(`--refresh`) `schedule:` triggers to `build-cache.yml` (keep `workflow_dispatch`); name the bundle `cache-<release>.json.gz` by detecting the release. Ensure the weekly run forces a clean rebuild and the daily restores the cache. actionlint clean. |
| 5b | medium | opus | Add a publish step that uploads `cache-<release>.json.gz` and its `.sigstore.json` to a fixed, prerelease `cache` GitHub Release in place (rolling URL), with `contents: write`. Any multi-line shell in `tools/`. |
| 5c | medium | opus | Reconcile the client: point `DEFAULT_CACHE_URL_TEMPLATE` at the published `cache` release URL; confirm `EXPECTED_SIGNER_IDENTITY` against a real published signature and update if needed. Update/add tests for the new URL. Do a real `cache pull` end-to-end and record the VERIFIED result. |
| 5d | low | sonnet | Update `README.md` (cache pull now works with no flags; the publish cadence and privacy story), `ARCHITECTURE.md`/`AGENTS.md` (the publish flow), and the phase/master/index plan statuses. Note the build-cache workflow is no longer "manual only". |

## Success criteria

- A scheduled run builds (incremental daily, full weekly), signs, and
  publishes `cache-<release>.json.gz` + `.sigstore.json` to a stable URL.
- A fresh machine runs `divergulent cache pull` with no arguments,
  downloads, **verifies (VERIFIED with the extra; spot-check always)**,
  and stores the bundle; a subsequent `score` is fast and offline for
  covered packages.
- The client's default URL, asset name, and expected signer identity all
  match what is published (proven by a real end-to-end VERIFIED).
- The publisher is polite: incremental by default, Repology ≤1 req/s,
  sources.debian.org crawled incrementally, identifying User-Agent.
- Docs describe the publish cadence and the privacy model; plan statuses
  updated.

## Out of scope

- The Debian 11/12/13/unstable/testing build **matrix** (PLAN-release-1.0).
- Multiple-mirror failover and reproducible-build cross-verification
  (Future work).

## Back brief

Before executing, back brief the operator on your understanding of this
phase.
