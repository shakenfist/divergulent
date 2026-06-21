# Architecture

divergulent measures how far a Debian machine has drifted from pure
upstream, along two axes — **staleness** (version lag) and
**divergence** (carried distro-only patches). This document describes
the code as it exists today; see `docs/plans/` for where it is going.

## Overview

For its first swing divergulent is a local-only Python CLI. It reads the
installed-package set from `dpkg` and, in later phases, compares it
against external data sources, caching their responses on disk. The
installed-package inventory never leaves the machine.

## Modules

- `divergulent/cli.py` — `argparse` entry point (`divergulent
  <command>`). Currently implements the `inventory` command (table and
  `--json` output).
- `divergulent/inventory.py` — enumerates installed packages via
  `dpkg-query` and maps each to its source package and version.
  `InstalledPackage` is a frozen dataclass; `list_installed(run=...)`
  takes an injectable runner so tests stay offline.
- `divergulent/debversion.py` — Debian version parsing and comparison,
  wrapping python-debian's `debian_support.Version`. This is the only
  module that touches that (untyped) dependency, and the only correct
  way to order versions in the codebase — never compare version strings
  directly.
- `divergulent/cache.py` — an on-disk TTL cache for data fetched from
  external sources. Keys are sha256-hashed to form the filename
  (path-traversal safe), writes are atomic, and the clock is injectable.
  `default_cache_dir()` honours `DIVERGULENT_CACHE_DIR` then
  `XDG_CACHE_HOME`.
- `divergulent/http.py` — `HttpClient`, the polite HTTP layer all
  network-backed sources fetch through (`get_json` and `get_text`):
  identifying User-Agent, request timeout, per-host rate limiting
  (≤1 request/second by default; sources.debian.org has no documented
  limit and is set to 0, bounded by `--workers` instead), a response
  size cap, on-disk caching, and graceful degradation (failures return
  `None`). `get_bytes` fetches a raw binary body (the cache bundle)
  throttled and size-capped but *not* through the value cache, since the
  caller stores it as a file. The throttle is a thread-safe per-host
  "ticket" reservation,
  so it stays correct when several worker threads fetch concurrently:
  same-host requests stay spaced, different hosts overlap. A `refresh`
  flag skips the cache read but still writes, so the cache builder can
  force a clean recompute that repopulates the cache. Stdlib `urllib`;
  the urlopen/clock/sleep are injectable for offline tests.
- `divergulent/dep3.py` — a pure parser/classifier for DEP-3 patch
  headers. Classifies a patch as FORWARDED, DEBIAN_ONLY, or UNKNOWN;
  when DEP-3 metadata is absent it falls back to Debian-authored
  heuristics (the old `# DP:` convention and deb-*/debian-* filenames).
  `bug_references()` extracts the `Bug`/`Bug-<vendor>` references a
  patch declares.
- `divergulent/sources/base.py` — the `Source` protocol that
  data-source adapters implement.
- `divergulent/sources/repology.py` — the Repology adapter (staleness
  axis). Picks the newest stable upstream version and compares it
  against the installed *upstream* version; yields CURRENT / BEHIND /
  UNKNOWN (unresolved is UNKNOWN, never BEHIND). `RepologySource`
  resolves one package at a time via the `project-by` resolver,
  caching each result for ~24h; the whole-machine commands and `show`
  both use it. (A whole-archive bulk sweep was tried and reverted as a
  cold-run regression — see
  `docs/plans/PLAN-faster-full-run-phase-04-revert-bulk.md`.) Also holds
  `RepologyBulkSource`, which answers staleness from a prebuilt
  `{srcname: newest}` map (a published bundle) with no network, reusing
  the same version classification.
- `divergulent/sources/debian_patches.py` — the sources.debian.org
  adapter (divergence axis). Reads a source package's quilt series from
  the patches API, fetches each patch under its pool `raw_url`, and
  classifies it with `dep3`. `summary()` is the cheap one-request
  overview (patch count + state) used by the whole-machine commands; the
  count comes from the API's top-level `count` field (the true
  `debian/patches/series` length), not the rendered `patches` array, which
  the API truncates at 60 — so heavily-patched packages are not
  undercounted. `details()` fetches and classifies every patch body
  (`PatchDetail`: classification, description, bug references) for `show`;
  note its view is still bounded by that 60-entry array. Both yield
  PATCHED / CLEAN / NATIVE / UNKNOWN. Version-pinned patch content is
  cached with a long TTL.
- `divergulent/sources/apt_patches.py` — the Tier 2 classification
  provider. Resolves a source package's mirror URLs via `apt-get source
  --print-uris`, fetches only the `.dsc` and `.debian.tar.*` (not the
  `.orig` tarball), extracts `debian/patches`, and classifies with
  `dep3` — the full breakdown across the machine via the mirror network.
  Requires `deb-src` (`deb_src_available()`). `fetch_patch_texts()` is the
  reusable, uncapped acquisition half (the patches API caps its rendered
  list at 60; reading the `.debian.tar.*` series does not).
- `divergulent/classify/` — **curation-side only** (the central builder runs
  it; no client command imports it). `corpus.py` crawls the archive's
  patched packages (reusing `apt_patches`' uncapped fetch) into a resumable
  content-addressed corpus of raw patch bodies; `fingerprint.py` is the pure,
  versioned `normalise()`/`fingerprint()` (canonical v1 = `strip_path`,
  `keep_context`); `measure.py` deduplicates, writes a sqlite fingerprint
  index, and reports the distinct-patch count. Phase 1 of the patch-
  classification plan; it measured ≈61.5k carried patches → 60,640 distinct
  (dedup 1.02x — carried patches are overwhelmingly bespoke). Phase 2 adds the
  deterministic extractors: `claim.py` reads the author's (untrusted) claim
  from the DEP-3 header, `content.py` profiles what the diff touches (typing
  files code-vs-prose), `rules.py` settles the easy categories
  (packaging/documentation) and runs the code-aware dangerous-construct scan
  that surfaces candidates without ever pronouncing malice, and `classify.py`
  drives them over the index, deriving claim/content consistency + a review
  flag and writing a `classification` table. It measured 29.2% of patches as
  deterministically settled, leaving ~43k substantive for the later tiers.
  Phase 3 adds the provenance ledger: `ledger.py` (the append-only sqlite
  schema — a `rule` registry, an immutable `decision` table only ever
  superseded, and an `observation` table for flags — plus the supersession ops
  and a `python -m divergulent.classify.ledger` CLI), `record.py` (drives the
  rules into the ledger as decisions/observations, idempotently), and
  `verdict.py` (the **derived** current verdict — the highest-precedence live
  decision per fingerprint — plus the phase-4 residue queue and a report). The
  current verdict is never stored, so it cannot drift, and retiring a rule
  re-queues exactly its fingerprints. Phase 4 fills the llm/human seats:
  `triage.py` runs the claim-blind LLM draft + adversarial verification, and
  step 4c bumps the ledger to **schema v2** (a `verified` flag on `decision`,
  reserved `signature`/`signed_by` columns for signed human ManualDecisions, and
  a `review_queue` table) and refines the precedence to `human > verified-llm >
  heuristic > unverified-llm` via `verdict.decision_rank` — an unverified LLM
  guess never outranks a deterministic heuristic. `triage_record.py` records a
  `TriageResult` into the ledger: an `llm` decision keyed
  `decided_by='llm-triage:<model>'` / `rule_version=<prompt_version>` (so a model
  swap is a new rule identity and a prompt bump a new version, both
  supersedable), `verified` set from the routing, and a pending `review_queue`
  item for every `needs_human` result — idempotently. `triage_driver.py` (the
  `python -m divergulent.classify.triage` CLI) triages a **bounded, prioritised**
  slice of the residue (dangerous-construct then high-occurrence first, never the
  whole queue by accident), surfaces **candidate deterministic rules** (clusters
  of identical verified verdicts — for human approval, never auto-applied), and
  reports the untriaged remainder. `review.py` (the
  `python -m divergulent.classify.review` CLI) is the local, interactive human
  tier, with three subcommands. `review` drains the queue: it shows each
  high-priority diff **in its original source context** — fetched on-demand from
  sources.debian.org **per touched file by the file's real `+++ b/<path>` path**
  (not the patch filename), with an **epoch-stripped version fallback** — beside
  the LLM draft, and records a **Sigstore-signed ManualDecision** (`kind='human'`,
  with `signature` + `signed_by`) that tops the precedence. It authenticates to
  Sigstore **once per session** (the identity token is reused, not re-prompted
  per item). `requeue <fingerprint>` sends one patch back for re-review
  (superseding its live human verdict — preserved as history — and re-opening its
  queue item, then rebuilding the verdict cache); `history` lists recent verdicts
  including superseded ones, so a reviewer can spot and reconsider a past call.
  The LLM backends (`claude -p` default, Anthropic API optional) and signing are
  curation-side only; clients never run either.
- `divergulent/bundle.py` — the precomputed cache **bundle** schema, a
  gzipped-JSON `write()` and `load()`. A bundle is the shareable half of
  a cold run: staleness and divergence for a whole Debian release,
  computed centrally so a client downloads it once instead of querying
  Repology and sources.debian.org per package. `schema`/`cache_schema`
  version the envelope and entry-value shapes; `built_on` is provenance
  only (the data is architecture-independent). `loads(bytes)` parses a
  fresh download before it is stored, and `stored_path(cache_dir,
  release)` is the on-disk location (`cache-<release>.json.gz`) the
  builder, `cache pull`, and the consumer agree on. See
  `docs/plans/PLAN-published-cache.md`.
- `divergulent/sources/bundle_backed.py` — the client-side **consumers**
  of a bundle: `BundleDivergenceSource` (returns a published divergence
  summary only when the installed version matches the bundle's, else a
  miss) and the `FallbackStaleness` / `FallbackDivergence` wrappers that
  try the bundle first and fall back to the live source on a miss.
  Staleness consumption is `RepologyBulkSource` (in `repology.py`). The
  fallback is per entry, so UNKNOWN still means neither the bundle nor the
  live source could resolve a package — never that the bundle merely
  lacked it. The CLI selects these when `--bundle` points at a recognised,
  release-matched bundle; otherwise the commands run fully live. The
  bundle is found either from an explicit `--bundle` or, after a `cache
  pull`, automatically from `stored_path` for the running release. A
  **freshness contract** governs use: bundle divergence is always served
  (immutable), bundle staleness only while within `BUNDLE_STALENESS_TTL`
  (else live) — gated by the injectable `cli._utc_now` clock.
- `divergulent/verify.py` — the **trust** checks for a downloaded bundle,
  both fail-closed. `verify_signature` checks the bundle's Sigstore
  signature against the expected CI workflow identity; it lazily imports
  `sigstore` and returns SKIPPED (not FAILED) when the optional `verify`
  extra is absent, so the base install stays stdlib + python-debian.
  `spot_check` samples the bundle's immutable divergence entries and
  compares them exactly against a live `summary()`, refusing on a definite
  disagreement while treating an unresolvable live result as inconclusive
  ("no cry wolf"). Both run at `cache pull` time (and `cache verify`).
- `divergulent/builder.py` — the central cache **builder** (runs in CI,
  not on a user's machine). Enumerates every `(source, version, format)`
  from the release's deb-src `Sources` indices with
  `debian.deb822.Sources` (no network), and sweeps Repology's whole-repo
  project set once into `{srcname: newest version}`
  (`build_staleness_map`). The bulk sweep is the *right* tool centrally —
  one polite crawl feeds every user's bundle — even though it was a
  regression per-user. The per-package client path in `repology.py` is
  left untouched; the builder imports only its version-selection helper.
- `divergulent/progress.py` — `Progress`, a terminal-aware progress
  reporter (stderr; animates on a TTY, periodic lines off-TTY, silent
  when disabled) used by the long whole-machine commands; `--quiet`
  disables it.
- `divergulent/score.py` — combines a package's staleness and
  divergence into a `PackageDrift` with a transparent weighted score
  (used only for ranking; both axes are retained for display).
  `classified_score()` weights Debian-only patches under `--classify`.
  Pure, no I/O.
- `divergulent/tests/` — testtools tests run via stestr/tox; every
  external effect is mocked or driven from a fixture so the suite runs
  offline.

## Data flow (today)

```
inventory:  dpkg-query  ->  inventory.list_installed()  ->  [InstalledPackage]  ->  cli (table / JSON)

staleness:  inventory  ->  dedup by source  ->  RepologySource.staleness()  ->  cli (ranked table / JSON)
                                                      |
                                          HttpClient (cache + politeness)  ->  repology.org
                                          (per-package project-by lookup, <=1 req/s, cached ~24h)

divergence: inventory  ->  dedup by source  ->  DebianPatchesSource.summary()  ->  cli (count table / JSON)
                                                      |
                              concurrent workers (--workers, default 8; thread pool)
                                                      |
                                          HttpClient (per-host throttle)  ->  sources.debian.org patches API
                                          (no rate limit; concurrency is the politeness bound)

score:      inventory  ->  dedup by source  ->  [concurrent workers] staleness (per-package)
                                            ->  + divergence summary  ->  score.combine()
                                            ->  cli (ranked report + whole-machine summary)
                              (Repology self-limits to <=1 req/s; sources.debian.org overlaps under that wait)

--classify: inventory  ->  dedup by source  ->  AptSourcePatches.details() (apt mirror, per source)
                                            ->  dep3.classify() per patch  ->  cli (per-class breakdown)

show:       resolve one installed package  ->  staleness + details (one shared HttpClient)
                                            ->  cli (per-patch detail + Debian bug links)

cache build: deb-src Sources indices  ->  builder.enumerate_archive() (no network)
                                      ->  builder.build_staleness_map()  ->  repology.org (bulk sweep, <=1 req/s)
                                      ->  [concurrent workers] DebianPatchesSource.summary() per source
                                      ->  bundle.Bundle  ->  gzipped JSON on disk (CI artifact)
                              (--refresh forces a clean recompute; runs centrally in CI, not per-user)

--bundle:   inventory  ->  dedup by source  ->  Fallback{Staleness,Divergence}(bundle, live)
                                            ->  bundle hit (in-memory dict, no network)  ->  cli
                                            ->  miss -> live RepologySource / DebianPatchesSource
                              (bundle read locally + validated: schema recognised, release matches; else fully live)
                              (divergence always; staleness only while fresh, else live)

publish:    schedule (daily incremental / weekly --refresh)  ->  build-cache.sh + sign-bundle.sh
                                                  ->  publish-cache.sh  ->  rolling 'cache' GitHub prerelease
                              (stable URL .../releases/download/cache/cache-<release>.json.gz[.sigstore.json])

cache pull: --cache-url (or default for release)  ->  HttpClient.get_bytes() bundle + .sigstore.json
                                                  ->  bundle.loads() validate (schema + release)
                                                  ->  verify.verify_signature() (if the verify extra is present)
                                                  ->  verify.spot_check() sample vs live summary()
                                                  ->  all pass  ->  atomic write bundle + signature, verbatim
                              (--insecure skips checks; --require-signature makes a missing/failed sig fatal)
```

## Planned

- See `docs/plans/` Future work for BTS cross-referencing (open Debian
  bugs a package's patches do not reference) and the candidate "patch
  hygiene & justification" master plan.
