# Phase 1 — Central builder + CI run (de-risk size & timing)

Part of [PLAN-published-cache.md](PLAN-published-cache.md).
**High effort:** archive enumeration, a long polite crawl, and
incremental correctness. This is the de-risking phase — its real
deliverable is *measured numbers* (bundle size, build time), not a
finished product.

**Status: measured and validated; one spot-check remaining.** The
builder is complete and tested offline — `divergulent/bundle.py` (schema
+ gzipped write/load), `divergulent/builder.py` (deb-src enumeration via
`debian.deb822.Sources` + the recovered bulk Repology sweep),
`HttpClient(refresh=True)`, `cli.build_bundle` + the `divergulent cache
build [--output --release --workers --refresh]` command, and
`.github/workflows/build-cache.yml` (+ `tools/build-cache.sh`). Suite
green.

### Measured results (first cold `workflow_dispatch` on the debian-13 runner)

| Metric | Estimate | Measured (cold) | Measured (incremental, cache restored) |
|--------|----------|-----------------|----------------------------------------|
| Gzipped bundle size | ~1–2 MB | **750,350 bytes (~0.73 MB)** | **750,121 bytes (~0.73 MB)** |
| Build step time | ~45–57 min (~22 staleness + ~20–35 divergence) | **~95 min** | **~80 s** |

The second `workflow_dispatch` restored the first run's CI cache and the
build step took **~80 s** — roughly **70× faster** than the cold ~95 min,
producing a near-identical bundle (within ~230 bytes). This validates the
**daily-delta model decisively**: divergence is immutable (30-day cache)
and staleness is within its 24 h TTL, so an incremental build is almost
pure local recompute from cache with negligible network. The bundle's
near-identical size across runs is also a good determinism signal.

Conclusions:

- **Size is settled in our favour.** At ~0.73 MB the bundle is well
  under the "few MB → consider sharding" threshold, so the **single
  whole bundle** decision holds and sharding stays Future work. It also
  makes the privacy model cheap: a client downloads the whole ~730 KB
  and matches locally, so no package list leaves the box.
- **Time is over estimate but acceptable**, because 95 min is the
  *cold* cost. Divergence for a fixed `(source, version)` is immutable
  and cached 30 days, so a daily incremental build re-fetches only new
  versions plus the 24 h staleness pages — minutes, not hours. 95 min is
  paid only on the first build and the periodic `--refresh` full rebuild,
  which is fine for a scheduled CI job. The divergence half was the
  underestimate (≈34k sources, many needing a second request for the
  epoch-stripped version, and sources.debian.org slower per request under
  concurrency than the assumed ~0.6 s).

The **markedly-faster incremental re-run** success criterion is now met
(~80 s vs ~95 min). The only criterion still open is a **hand-checked
sample** (e.g. `bash`) confirmed against a live `divergulent show`,
plus a glance at the bundle's `release`/`built_on` provenance and a
staleness/divergence entry count near the archive size. Once that
spot-check passes, phase 1 is complete and phase 2 (client consumption)
can start.

Two scoping decisions taken during implementation:

- **`RepologyBulkSource` (the client-side consumer of the map) is
  deferred to phase 2**, where the master plan actually consumes it.
  Phase 1 only needs `build_staleness_map`; recovering the consumer now
  would be dead code.
- **The original map-level 24h staleness cache was dropped**; the
  per-page HTTP cache already gives incrementality and makes `--refresh`
  uniform (the HTTP client controls every read), whereas a separate
  map-level cache bought nothing at a daily cadence and would have
  sidestepped `--refresh`.

## Prompt

Read, and reuse rather than reinvent:

- `divergulent/sources/debian_patches.py` — `DebianPatchesSource.summary()`
  is exactly the per-`(source, version)` divergence we need; it already
  caches the patches-API result (`SERIES_NAMESPACE`, 30-day TTL).
- `divergulent/sources/repology.py` — the per-package path that exists
  today. The bulk sweep (`build_staleness_map` / `RepologyBulkSource`)
  was removed in `PLAN-faster-full-run` phase 4; recover it from git
  history (commit before `c6f91c7`) as the builder's staleness engine.
- `divergulent/http.py` / `divergulent/cache.py` — the polite client
  (per-host throttle, concurrency-safe) and the persisted cache the
  builder relies on for incremental daily runs.
- `divergulent/cli.py` — `_concurrent_map`, `_dedup_sources`, the
  `Progress` wiring, and `AptSourcePatches.available()` /
  `deb_src_available()` (the deb-src gating already used by `--classify`).
- `divergulent/progress.py` — reuse for the long crawl.

`python-debian` is already a runtime dependency; use
`debian.deb822.Sources` to parse apt's `Sources` indices rather than
shelling out.

## Objective

A builder that, on a Debian 13 host with deb-src enabled, enumerates
every `(source, version)` in the release, computes the staleness map
(bulk Repology) and the divergence summary per source version, and
writes a single gzipped bundle (the schema in the master plan,
including `release` and `built_on` provenance). Run it in CI on the
self-hosted `debian-13` runner, upload the bundle as a **workflow
artifact**, and read off the **actual bundle size and build time** to
confirm (or correct) the ~1–2 MB / ~under-an-hour estimates.

No GitHub Releases, no signing, no client consumption — those are later
phases. The bundle here is proof-of-feasibility, downloaded by a human
from the CI run.

## Design decisions

- **Enumeration from apt, no network.** Parse the deb-src `Sources`
  indices with `debian.deb822.Sources` to get every source package,
  its archive version, and `Format`. The workflow ensures deb-src is
  enabled (add `deb-src` entries + `apt-get update`); reuse the
  existing deb-src detection so the builder fails loudly if absent.
- **Staleness via the bulk sweep, once.** Resurrect `build_staleness_map`
  as a builder component (it is the *right* tool centrally — ~170
  pages, ~22 min, Repology ≤1 req/s). Choose the release-aligned
  Repology repo (`debian_13` if it exists, else `debian_unstable` as a
  superset); "newest" is upstream-global so the choice mainly affects
  srcname coverage — note the choice in the bundle (`repology_repo`).
- **Divergence via `summary()`, concurrent and incremental.** For each
  enumerated `(source, version)`, call `summary()` through the shared
  concurrent gather. Because a fixed version's patch set is immutable
  and `summary()` already caches it for 30 days, a CI run that restores
  the prior cache only fetches *new* versions — the daily delta. Use a
  moderate worker count (polite for a ~34k crawl) and show progress.
- **`divergulent cache build --output <file> [--release <r>] [--workers N] [--refresh]`.**
  A new `cache` subcommand group (`build` now, `pull` in phase 2). It
  assembles the bundle dataclass and writes gzipped JSON. Keep the
  bundle module (`divergulent/bundle.py`) small: the schema, a writer,
  and a loader (the loader is exercised properly in phase 2).
- **`--refresh` forces a clean recompute.** A `refresh` mode on
  `HttpClient` that **skips the cache read but still writes**, plumbed
  to `cache build --refresh`. Without it the build is incremental
  (reuses the 30-day divergence cache and 24h staleness — the cheap
  daily delta); with it the build ignores existing entries, re-fetches
  from the origins, and repopulates the cache. This is not just
  convenience: a purely incremental builder would republish a
  once-bad cached value forever, so a periodic forced clean rebuild
  (e.g. weekly full + daily incremental) bounds how long any error can
  live in the published bundle, and it produces the first authoritative
  build. The client gets analogous cache-control flags in the consume
  phases.
- **Measurement is the gate.** The workflow logs start/end of the build
  step and the gzipped artifact size; success means we have real
  numbers to compare against the estimate and decide whether to
  continue as planned, shard, or trim divergence depth.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 1a | medium | sonnet | none | Add `divergulent/bundle.py`: a `Bundle` dataclass matching the master-plan schema (`schema`, `cache_schema`, `generated_at`, `release`, `repology_repo`, `built_on`, `staleness`, `divergence`), a `write(path)` that emits gzipped JSON, and a `load(path)` that reads it back. Offline round-trip tests. `generated_at`/host facts are passed in (no `Date.now()`/uname in the dataclass core) so it stays testable. |
| 1b | medium | sonnet | none | Add archive enumeration: a function returning `[(source, version, format)]` by parsing apt's deb-src `Sources` indices with `debian.deb822.Sources`. Mirror the deb-src detection in `apt_patches.py`; fail clearly if deb-src is absent. Test against a small recorded `Sources` fixture. |
| 1c | medium | opus | none | Recover `build_staleness_map` + `RepologyBulkSource` from git history (state before `c6f91c7`) into a builder-side module (not the client path), keeping the hardening (dict guard, page-size/max-pages bounds). Wire it to produce the `staleness` map for the chosen `repology_repo`. Reuse existing repology tests; add coverage for the resurrected code. |
| 1d | high | opus | none | Add `divergulent cache build` (new `cache` subcommand group) that: enumerates sources (1b), builds the staleness map (1c), gathers `summary()` over all `(source, version)` via the shared concurrent path with progress and a moderate default worker count, assembles a `Bundle` (1a) with `release`/`built_on` provenance, and writes the gzipped output. Incremental by construction via the persisted patches cache. Add a `refresh` mode to `HttpClient` (skip the cache read, still write) and a `cache build --refresh` flag that forces a clean recompute. Offline tests: mocked Repology pages + patches API over a small fixture archive assembles the expected bundle; `--refresh` re-fetches despite warm cache and still updates it. |
| 1e | medium | sonnet | none | Add `.github/workflows/build-cache.yml`: `workflow_dispatch` only (scheduling is phase 5), self-hosted `[debian-13, s]`, enable deb-src + `apt-get update`, restore/save a CI cache keyed `divergulent-cache-trixie-<run_id>` (restore-keys `divergulent-cache-trixie-`) over the divergulent cache dir, run `divergulent cache build`, `echo` the gzipped size, and upload the bundle as an artifact. Any multi-line shell goes in `tools/`, per house style. |

## Testing requirements

- Offline: mock Repology bulk pages and the patches API; enumeration
  tested against a recorded `Sources` fixture; bundle write/load
  round-trips. No live network in unit tests.
- `pre-commit run --all-files` green (actionlint covers the new
  workflow).

## Success criteria

- A manual `workflow_dispatch` of `build-cache.yml` on the debian-13
  runner produces a bundle artifact, and the run gives us **measured
  bundle size (gzipped) and total build time**.
- Re-running with the CI cache restored is markedly faster on the
  divergence half (incremental), demonstrating the daily-delta model.
- The bundle validates: correct `release`/provenance, a staleness entry
  count near the archive size, divergence entries for the enumerated
  versions, and a hand-checked sample (e.g. `bash`) matching a live
  `divergulent show`.

## Out of scope (later phases)

- Client consumption / `RepologyBulkSource` on the user path (phase 2).
- `cache pull`, `--cache-url` (phase 3).
- Signing and spot-verification (phase 4).
- Scheduling and GitHub Releases publishing (phase 5).

## Open questions

- **Release-aligned Repology repo** — confirm `debian_13` exists in
  Repology; otherwise use `debian_unstable` and note the superset
  caveat in the bundle.
- **Divergence depth** — `summary()` (count + state) only for now;
  decide later whether per-patch (`--classify`) data is cheap enough to
  include.
- **Worker count for a 34k crawl** — pick a politeness-respecting
  default (sources.debian.org has no published limit, but a central
  daily crawler should stay moderate).

## Back brief

Before executing, back brief the operator on your understanding of this
phase.
