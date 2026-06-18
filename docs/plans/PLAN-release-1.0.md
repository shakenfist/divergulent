# Road to a 1.0 release

A living checklist of what divergulent must do before it earns a `1.0`
tag. This is a placeholder/tracking plan: each workstream graduates to its
own detailed `PLAN-…` (and per-phase files) when we pick it up. Created
from [PLAN-TEMPLATE.md](../../PLAN-TEMPLATE.md) in spirit, but kept as a
checklist rather than a single linear phase list.

## What 1.0 means for divergulent

divergulent's promise is **honest, private drift measurement**: it tells
you how far a Debian machine has drifted from pure upstream (staleness)
and how many Debian-only patches it carries (divergence), never crying
wolf, without the machine's inventory leaving the host. A 1.0 is the point
where that promise holds **by default, for ordinary Debian users, with no
hand-holding** — fast, private, trustworthy, and documented.

## Must-do workstreams

### 1. Finish the published cache (publish — phase 5)

The default experience must be: install, `cache pull`, and runs are fast
and private. Tracked in
[PLAN-published-cache-phase-05-publish.md](PLAN-published-cache-phase-05-publish.md).
Blocking, because phases 1–4 are inert until something is published at the
default URL.

- [ ] Scheduled daily (incremental) + weekly (full `--refresh`) builds.
- [ ] Signed bundle published to a stable URL; client constants reconciled.

### 2. Multi-release build matrix (Debian 11, 12, 13, testing, unstable)

Today the builder targets one release (trixie). A 1.0 should serve the
**releases people actually run**: bullseye (11), bookworm (12), trixie
(13), plus **testing** and **unstable** for the developers who track them.
The bundle data is release-partitioned and architecture-independent, so
one bundle per release serves every architecture of that release — the
client already keys storage, the URL, and validation on the release.

The interesting part: **we have no CI runner images for testing and
unstable** (and maintaining self-hosted runners per release is a poor use
of effort). The challenge we're choosing to take on:

- [ ] **Build inside official Debian containers** on a single Docker-capable
      runner: `debian:bullseye`, `debian:bookworm`, `debian:trixie`,
      `debian:testing`, `debian:sid`. Because divergence is
      arch-independent, one amd64 container per release suffices — no
      per-release runners, no per-arch builds. `tools/build-cache.sh`
      already enables deb-src and builds in a venv, so it should run
      largely as-is inside each container.
- [ ] **Per-release Repology repo** for staleness: confirm the mapping
      (`debian_11`, `debian_12`, `debian_13`, `debian_unstable`, and the
      right id for testing) — `builder.build_staleness_map(repo=…)` and the
      bundle's `repology_repo` field already parameterise this.
- [ ] **Release detection for testing/unstable.** `/etc/os-release`
      `VERSION_CODENAME` is reliable for stable releases but slippery for
      testing (often reports the *next* stable codename) and unstable
      (`sid`). Decide and document how the client names and matches these
      so `cache pull` selects the right bundle (and how a testing/unstable
      user is told when no exact bundle exists → live fallback).
- [ ] **A CI matrix** that builds, signs, and publishes a bundle per
      release, reusing the phase-5 publish mechanism (assets already keyed
      on `<release>`). Stagger and stay incremental — five full crawls is a
      lot of upstream traffic, so daily-incremental + periodic-full per
      release, spread out, matters even more here.
- [ ] **Politeness at matrix scale** — one polite central crawler is the
      whole point; confirm aggregate Repology/sources.debian.org load stays
      reasonable across five releases.

This graduates to its own `PLAN-cache-matrix.md` when picked up.

### 3. Trust hardening

- [ ] **A real end-to-end VERIFIED.** Signing/verification has only been
      exercised with mocks and the malformed→FAILED path. Do a genuine
      sign → publish → `cache pull` → VERIFIED cycle and pin the exact
      signer identity the real certificate carries (closes the phase-4
      risk that `EXPECTED_SIGNER_IDENTITY` is a guess).
- [ ] **Decide the default trust level.** Today, with no `verify` extra, a
      bundle that passes the spot-check is stored even unsigned. Confirm
      spot-check-as-the-floor is the intended 1.0 default, or nudge harder
      toward signatures (docs, prompts, or making the extra a default for
      some install paths).
- [ ] **Commit to schema stability.** State that `schema`/`cache_schema` 1
      is stable, and that the publisher owns migrations while the client
      drops what it cannot read.

### 4. "No cry wolf" validation on real machines

The product is trustworthiness, so validate it beyond the CI sample.

- [ ] Run `score`/`staleness`/`divergence` on several real, diverse Debian
      machines and confirm we are not producing **false BEHIND** (Repology
      name-matching, epoch/upstream-version handling) or **false clean/
      native** (source-format detection). Where we are unsure, we must say
      UNKNOWN, not guess.
- [ ] Sanity-check the provisional scoring weights against real data (the
      score only ranks and both axes are always shown, so this is tuning,
      not correctness — but worth a pass).

### 5. Privacy model, stated plainly

- [ ] Document the two privacy regimes crisply: the **bundle path** sends
      nothing about your machine anywhere (whole-bundle download, local
      match); the **live path** (no bundle, or per-miss lookups) sends
      package names to Repology / sources.debian.org. Tell users how to
      stay fully private (pull a bundle).

### 6. Robustness for a public release

- [ ] Behave gracefully off the beaten path: non-Debian systems, missing
      `dpkg`, unexpected versions, and network failures should produce a
      clear message, never a traceback.
- [ ] A clean `--help` and a short quickstart in the README.

### 7. Release mechanics

- [ ] Confirm the existing tag-driven release pipeline
      (`release.yml`: Sigstore-signed tags + PyPI trusted publishing)
      produces a clean `v1.0`, including the optional `verify` extra on
      PyPI.
- [ ] Release notes / changelog for 1.0.

## Administration

- Registered in [docs/plans/index.md](index.md).
- Each workstream above becomes its own detailed plan when scheduled; this
  file tracks the overall 1.0 gate and is updated as items close.
