# A shared, signed, precomputed cache for cold-run speed

## Prompt

Before responding to questions or discussion points in this
document, explore the divergulent codebase and ground answers in
what it does today: `divergulent/http.py` (the polite client,
per-host throttle, size cap, on-disk cache), `divergulent/cache.py`
(the keyed TTL cache), `divergulent/sources/repology.py`
(per-package staleness via `project-by`; note the
`RepologyBulkSource`/`build_staleness_map` that
`PLAN-faster-full-run` phase 4 removed and git history retains),
`divergulent/sources/debian_patches.py` (divergence `summary()` per
`(source, version)`), and `divergulent/cli.py` (how the
whole-machine commands build sources and gather).

Where a question touches external concepts, research rather than
guess. Key references:

- **Repology bulk API** — `/api/v1/projects/` (≤200/page, paged by
  name, `?inrepo=debian_unstable`) yields `srcname` + `version` +
  `status` per repo entry. ≤1 req/s, identifying User-Agent.
- **sources.debian.org** — `/patches/api/<pkg>/<ver>/` gives
  format + patch names per source version. No documented rate
  limit. Divergence for a fixed `(source, version)` is
  **immutable**.
- **Archive enumeration** — the set of `(source, version)` to crawl
  comes from a Debian `Sources` index (deb-src) or
  sources.debian.org's package list; resolve which in phase 3.
- **Sigstore** — the project already signs release tags
  (`.github/workflows/release.yml`); keyless OIDC signing of an
  artifact in a workflow is the model for signing the bundle.

Flag uncertainty explicitly (Repology name-matching is heuristic;
unresolved staleness is UNKNOWN, never a false BEHIND).

Planning documents live in `docs/plans/`. Per-phase plans are
`PLAN-published-cache-phase-NN-descriptive.md`, tracked in the
Execution table. One commit per logical change, ≥1 per phase.

## Situation

`PLAN-faster-full-run` took a cold full `score` from ~34 min to
~7 min by reverting the per-user whole-archive Repology sweep
(phases 4) and adding bounded concurrency on sources.debian.org
(phase 5). What remains is the **Repology ≤1 req/s floor**: ~570
per-package lookups ≈ ~9.5 min that no client-side change can beat
without being a bad API citizen.

The deeper observation (recorded as Future work there): staleness
and divergence are functions of `(source_package, version)` plus
the upstream world, **not of the user's machine**. So the
expensive half is fully *shareable*. Computing it once, centrally,
and publishing it turns every user's cold run into a small download
— and makes the project a far better API citizen (one scheduled
crawler instead of N users hammering Repology and
sources.debian.org).

## Mission and problem statement

Publish a small, signed, versioned **precomputed cache bundle** of
staleness and divergence for the whole Debian archive, refreshed
daily, and consume it in the client so a cold whole-machine run
becomes a few-MB download plus local matching (seconds), with live
fallback for anything the bundle does not cover. Preserve the
"inventory stays local" privacy property and the "no cry wolf"
honesty property throughout.

Non-goals: replacing the live path (the bundle is an optimisation,
never a dependency); per-package remote lookups (privacy); sharded
chunks (premature at single-digit MB).

## Open questions

Resolved up front (operator decisions, 2026-06-17):

- **Hosting.** GitHub Releases rolling `latest` asset (GitHub CDN,
  no infra, stable URL), with the cache URL **configurable**
  (`--cache-url` / config) so `images.shakenfist.com` or other
  mirrors can be used. Because the bundle is signed and
  spot-verified, the host is untrusted and mirrors are free.
- **Coverage.** Whole archive (~34k sources) from day one. The first
  cold build measured **~95 min** (phase 1) — over the ~45–57 min
  estimate, the divergence half being the underestimate — but a
  CI-cache-restored incremental rebuild measured **~80 s** (~70× faster),
  confirming the cheap daily-delta model (divergence is immutable, so
  only new versions are crawled). 95 min is the cold/periodic-rebuild
  cost; the daily cost is seconds.
- **Bundle shape.** A single whole bundle (one gzipped file,
  optionally split staleness/divergence), downloaded whole and
  filtered locally — maximally private, trivial client. The measured
  bundle is **~0.73 MB gzipped** (phase 1), well under the "few MB"
  sharding threshold, so the single-bundle decision is confirmed and
  sharding stays Future work.

Still to resolve in phases:

- **Archive enumeration source** — deb-src `Sources` index vs
  sources.debian.org list vs UDD (phase 1).
- **Divergence depth** — publish the cheap `summary()` (count +
  state + format) for everything; per-patch DEP-3 classification
  (`--classify`, heavier) is deferred unless cheap to include.
- **Freshness contract** — *resolved in phase 3.* Bundle divergence is
  always used (immutable); bundle staleness only while `generated_at` is
  within a generous 7-day window (a stale "newest" only under-reports
  BEHIND, never cries wolf), past which staleness is queried live.

## Design overview

**Bundle (versioned, signed):**

```
{
  "schema": 1,                       # envelope schema
  "cache_schema": 1,                 # entry-value shape
  "generated_at": "<iso8601>",
  "release": "trixie",               # Debian release the divergence set describes
  "repology_repo": "debian_unstable",# repo swept for staleness
  "built_on": {"arch": "amd64", "release": "trixie"},  # provenance only; data is arch-independent
  "staleness": { "<srcname>": "<newest_version>", ... },
  "divergence": { "<pkg>": {"version": "<v>", "format": "...",
                            "total": N, "state": "patched|clean|native|unknown"}, ... }
}
```

Compact (computed values, not raw API responses): ~1–2 MB gzipped.

**Client consumption (prefer-bundle, fall back live):**

- Staleness from the bundle reuses the **`RepologyBulkSource`**
  abstraction `PLAN-faster-full-run` phase 4 removed from the
  per-user path — resurrected here as the *consumer* of published
  data (the removal was about not *building* it per-user; consuming
  a published map is its right use).
- Divergence from the bundle uses a new thin `BundleDivergenceSource`
  that returns the published `summary` **only when the installed
  version matches** the bundle's (divergence is version-specific);
  otherwise a miss.
- A small `Fallback(bundle_source, live_source)` wrapper tries the
  bundle, then the live source. Misses, stale bundles, and absent
  bundles degrade to today's behaviour exactly.

**Trust:** the bundle is optional and untrusted. The client
ignores a bundle whose envelope/`cache_schema` it does not
recognise (runs live), verifies the Sigstore signature against the
expected workflow identity, and **spot-verifies a random sample**
against live origins ("no cry wolf"). Migration is the publisher's
job; the client never migrates, it just drops what it cannot read.
On the publisher side, the build is incremental by default but
supports a forced clean recompute (`cache build --refresh`); the
schedule (phase 5) pairs daily incremental builds with a periodic
full rebuild so a once-bad cached value cannot live in the bundle
indefinitely.

**Privacy:** whole-bundle download means the host learns nothing
about the user's inventory. No package list ever leaves the box.

**Scope — per Debian release, not per architecture.** The builder's
data is tied to the runner's **Debian release** (the divergence
`(source, version)` set is enumerated from that release's deb-src
`Sources` index), but it is **architecture-independent**: divergence
lives in the arch-independent source package's `debian/patches`, and
divergulent dedups installed binaries to their *source version*
before querying (even an arch-specific binNMU like `1.2-3+b1` keeps
source version `1.2-3`). Staleness (`srcname → newest upstream`) is
arch- and largely release-independent regardless. So a bundle built
on amd64 Debian 13 is equally correct for every architecture of
Debian 13; a different release (e.g. Debian 12) has different
versions and is a separate bundle (cross-release users fall back to
live). Consequences for later phases:

- The **correctness partition is the release.** Key the CI build
  cache and name the artifact / Releases asset on the release (e.g.
  `divergulent-cache-trixie`, `cache-debian13.json.gz`), not on
  arch.
- Record the **build-host architecture (and release) as provenance**
  inside the bundle metadata, with a note that the data is
  arch-independent — so we do not needlessly build per-arch bundles.
- The bundle metadata should carry the release it describes so the
  client can refuse a bundle whose release does not match the
  running system.

## Execution

Five phases, **builder first to de-risk the assumptions**. The
biggest unknowns are the whole-archive *build time* and the *bundle
size*; phase 1 settles both by running the builder in CI and
uploading the bundle as a workflow artifact, before any effort goes
into client consumption or GitHub Releases delivery. If the real
numbers diverge from the estimates (e.g. far larger than a few MB,
or far slower than ~1 hour), we adjust scope here rather than after
building the whole pipeline. The GitHub portions (signing, Releases
hosting) are the last phases.

The consume phases (2–3) can develop against the real bundle phase
1 produces (or a trimmed fixture of it), independent of the
delivery phases (4–5).

| Phase | Plan | Status |
|-------|------|--------|
| 1. Central builder + CI run: whole-archive sweep, emit bundle, **measure size/timing** | PLAN-published-cache-phase-01-builder.md | Measured (~0.73 MB; ~95 min cold, ~80 s incremental); one spot-check remaining |
| 2. Bundle schema + bundle-backed sources + live fallback | PLAN-published-cache-phase-02-consume.md | Implemented (`--bundle`, bundle-backed sources + per-entry live fallback) |
| 3. `cache pull`: download, validate, store, configurable URL | PLAN-published-cache-phase-03-pull.md | Implemented (`cache pull`, auto-discovery, freshness contract) |
| 4. Signing + client verification + spot-verify | PLAN-published-cache-phase-04-signing.md | Not started |
| 5. Scheduled daily publish to GitHub Releases `latest` | PLAN-published-cache-phase-05-publish.md | Not started |

**Phase 5 politeness note (from the phase-1 measurement).** The cold
whole-archive crawl is ~95 min and, at `--workers 8`, sustains up to 8
concurrent connections to sources.debian.org for most of it. The
scheduled job must therefore lean on **incremental** builds (the daily
delta is cheap because divergence is immutable) and run a full
`--refresh` rebuild only periodically (e.g. weekly), and should keep the
worker count **moderate** — a central daily crawler should not be the
heaviest client sources.debian.org sees. Revisit the default worker
count and whether to add a small per-request interval for the scheduled
crawl when designing phase 5.

## Agent guidance

Follows the execution model, effort/model rubric, and review
checklist in [PLAN-TEMPLATE.md](../../PLAN-TEMPLATE.md). Phases 1
and 4 are high-effort (archive enumeration, incremental crawl
politeness, and trust-critical signing/verification); phases 2, 3,
5 are medium. Tests stay offline: mocked HTTP for the builder, a
recorded fixture bundle for the client. Skew to the more capable
model for phases 1 and 4.

## Administration and logistics

### Success criteria

* A cold whole-machine `score` with a fresh bundle present
  completes in **seconds**, with results identical to the live
  path on a verified sample.
* With no bundle (or a stale/unrecognised one), behaviour is
  exactly today's live path — the bundle never breaks a run.
* The published bundle is signed; the client verifies it and
  spot-verifies a random sample against live origins, surfacing any
  divergence rather than trusting blindly.
* The user's installed-package inventory never leaves the machine
  (whole-bundle download, local matching).
* The daily publisher is a polite single crawler: Repology ≤1
  req/s, sources.debian.org crawled incrementally (immutable
  versions fetched once), with an identifying User-Agent.
* `pre-commit run --all-files` passes; tests are offline.
* `README.md` / `ARCHITECTURE.md` / `AGENTS.md` document the
  bundle, `cache pull`, `--cache-url`, the builder, and the trust
  model.

### Future work

- **Sharded bundles** if the dataset outgrows a few MB.
- **Per-patch (`--classify`) data in the bundle** if it can be made
  cheap to compute and publish.
- **Multiple mirrors with client-side failover** (GitHub +
  images.shakenfist.com) once a single host is proven.
- **Reproducible-build verification** of the bundle by a second
  independent builder, raising the trust bar further.

### Bugs fixed during this work

None yet; record any encountered here.

### Documentation index maintenance

Registered in [docs/plans/index.md](index.md). Update phase
statuses there and in the Execution table as phases complete.

### Back brief

Before executing any step, back brief the operator on your
understanding of the plan and how the intended work aligns with it.
