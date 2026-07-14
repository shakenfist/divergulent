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
- `divergulent/classify/` — **curation-side only** (the central builder
  runs it; no client command imports it except its two shareable
  leaves: `fingerprint.py`, to hash a fetched patch for bundle lookup,
  and `classification_bundle.py`, to load the published classification
  bundle). The pipeline's narrative lives in
  [docs/workflow.md](docs/workflow.md) and every deterministic rule is
  documented in
  [docs/deterministic-rules.md](docs/deterministic-rules.md); what
  follows is the module map.
  - `corpus.py` — crawls the archive's patched packages (reusing
    `apt_patches`' uncapped fetch) into a resumable, content-addressed
    corpus of raw patch bodies, capturing each package's
    `debian/changelog` last-upload date for review-time package-age
    display.
  - `fingerprint.py` — the pure, versioned `normalise()`/`fingerprint()`
    (canonical v1 = `strip_path`, `keep_context`); the identity every
    later phase keys on.
  - `measure.py` — deduplicates into a sqlite fingerprint index (a
    `patch` table plus a `package` table carrying the changelog date and
    `.dsc` binary names). Measured ≈61.5k carried patches → 60,640
    distinct (dedup 1.02x — carried patches are overwhelmingly bespoke).
  - `claim.py` / `content.py` / `rules.py` / `classify.py` — the
    deterministic extractors: the author's (untrusted) DEP-3 claim; the
    content profile (code-vs-prose file typing, conservative
    trivial-change flags); the precedence-ordered category rules plus
    the code-aware dangerous-construct scan (flags, never verdicts); and
    the driver deriving claim/content consistency + a review flag.
    Measured 29.2% of patches deterministically settled before
    `test-only` was added.
  - `ledger.py` — the append-only provenance schema: a `rule` registry,
    an immutable `decision` table (only ever superseded), an
    `observation` table, and schema v2's `verified` flag,
    `review_queue`, and reserved signature columns. CLI: `build`
    (guarded — refuses to wipe a populated ledger's llm/human work
    without `--force`), `record` (apply current rules to an existing
    ledger, superseding a fingerprint's stale decision when its winning
    rule changed), `report`, `supersede`.
  - `record.py` — drives the deterministic tiers into the ledger
    idempotently (category decisions, dangerous-construct observations,
    reviewability, reach, and the opt-in phase-6 cross-reference), with
    an opt-in `reconcile` mode for in-place re-records.
  - `verdict.py` — the **derived** current verdict: precedence
    `human > verified-llm > heuristic > unverified-llm`
    (`decision_rank`), then recency, confidence, and id; plus the
    phase-4 residue queue. Never stored, so it cannot drift, and
    retiring a rule re-queues exactly its fingerprints.
  - `triage.py` / `triage_record.py` / `triage_driver.py` — the
    claim-blind LLM draft + adversarial verification over a
    `call(system, user, *, model) -> CallResult` boundary. The default
    `claude -p` backend runs `--system-prompt` + `--tools ""` +
    `--strict-mcp-config` + `--setting-sources ""` +
    `--output-format json` (no new dependency; stripping unused context
    shrank each request from ~66k to ~640 tokens); the anthropic backend
    caches the rubric with `cache_control`; per-call usage feeds a
    **Cost & cache** report. Ledger decisions are keyed
    `decided_by='llm-triage:<model>'` / `rule_version=<prompt_version>`
    (a model swap is a new rule identity, a prompt bump a new version),
    `verified` set from the routing, with a pending `review_queue` item
    for every `needs_human` result — idempotently. The driver triages a
    **bounded, prioritised** slice (risk, then dangerous-construct, then
    high-occurrence — never the whole queue by accident), surfaces
    **candidate deterministic rules** (clusters of identical verified
    verdicts, for human approval, never auto-applied), and reports the
    untriaged remainder.
  - `risk.py` — the **security-risk gate**: a deterministic
    provably-benign cull (no LLM call), else a cheap claim-blind LLM
    score (`none/low/elevated/high`) over the **whole corpus** — a
    patch settled as `packaging` can still be security-relevant.
    Advisory only: a supersedable `security-risk` observation feeding
    work-list/`review_queue` priority (a risk run re-stamps pending
    items via `reprioritise_review_queue`; the web worklist also sorts
    by the live level), never the verdict precedence. Failures degrade
    to `elevated` (recall-safe); `large` diffs are head-capped
    (`RISK_MAX_DIFF_CHARS`, truncation recorded). Default model Opus
    (bake-off: 100% recall / 0% false-alarm at the ≥elevated cut vs
    Sonnet 73%/3%).
  - `reviewability.py` — the deterministic size axis
    (`normal`/`large`/`oversized` at 500/5,000 changed lines,
    `observed_by='size-rule'`), recorded during the deterministic
    record pass; an `oversized` diff skips the LLM passes entirely and
    gets its own review-UI bucket.
  - `reach.py` / `popcon.py` — the deterministic install-base axis
    (`XS`–`XL` from a pinned popcon snapshot in
    `corpus/popcon.sqlite`, max-over-binaries, bucketed relative to the
    snapshot's `max(inst)` anchor, `observed_by='popcon-rule'`). A
    secondary priority key **within** a security tier
    (`risk_rank * 1e9 + reach_rank * 1e6 + occurrence`, non-overlapping
    bands), never across one; re-records only when a bucket changes.
  - `cross_reference.py` / `security_tracker.py` / `bts.py` — the
    phase-6 **external** tier (`purity='external'`): claimed CVEs
    verified against a pinned Security Tracker snapshot
    (`corpus/security_tracker.sqlite`) and claimed Debian bugs against
    a BTS index snapshot (`corpus/bts.sqlite`, a gzipped
    `bug→source,status` TSV built weekly from UDD
    (`bugs ∪ archived_bugs`) and hosted on the rolling `bts`
    prerelease, so `bts` works with no operator URL — `pull`
    transparently gunzips it). A code-touching
    **confirmed** CVE over the `unknown` residue settles `security`,
    carrying an `input_snapshot` + `input_fresh_until` horizon (the
    recorder re-verifies past it and retracts a corroboration the
    tracker no longer supports); a **contradicted** claim records only
    a `claim-unconfirmed` provenance observation — a review nudge,
    never a malice verdict. Only ~10% of patches claim any reference
    (1.44% a CVE), so the tier is a scalpel.
  - `review.py` — the local, interactive human tier (`review` /
    `requeue <fingerprint>` / `history`): each queued diff shown **in
    its original source context** (fetched on-demand per touched file
    by its real `+++ b/<path>` path, with an epoch-stripped version
    fallback) beside the LLM draft and the carrying source package(s),
    with a **files-changed summary** (per-file added/removed counts,
    largest change first) before the diff so the bulk of a huge
    multi-file patch and the small hand-edits buried in it are visible
    before scrolling begins;
    records a **Sigstore-signed ManualDecision** (`kind='human'`) that
    tops the precedence, authenticating once per session. The LLM
    backends and signing are curation-side only; clients never run
    either.
- `divergulent/classify/review_web.py` (the
  `python -m divergulent.classify.review_web` tool) is a **local web UI over the
  same review machinery** — a presentation swap, not a second implementation. It
  reuses `review.py`'s fingerprint-keyed `build_review_context` and the signed
  `record_review_verdict` verbatim, so a web verdict and a CLI verdict are
  **byte-identical** (same canonical record, same `kind='human'` signature, same
  dequeue) and a reviewer can switch front-ends mid-grind against one ledger. It
  adds the slices the linear CLI queue cannot: **review by category**,
  **cherry-pick by fingerprint or package**, and an **audit/spot-check view** over settled
  patches *not* in the queue (the derived `current_verdict`, filtered by category
  and provenance) to confirm a deterministic rule is right — re-queuing a misfire
  via the existing `requeue_one` (which records no decision; only the eventual
  human verdict is signed). The queue worklist filters on the **LLM draft**
  category; the audit view filters on the **derived verdict** — which for a
  rule-classified fingerprint is the rule's category, so "the rule defines the
  category when a patch never reached the LLM" needs no special case. The web UI
  also carries **signed reviewer notes** — append-only, free-text human
  annotations on a fingerprint (a third ledger entry type beside decisions and rule
  observations, in an OPTIONAL `note` table existing ledgers gain via
  `ensure_note_table`), signed with the same session signer as verdicts
  (`record_note`/`canonical_note`), shown with their identity + signature, badged
  on the worklist, and never published. The review page's files-changed list
  anchor-links each row to its per-file block in the rendered diff. Flask +
  Jinja2 (autoescaping HTML) live behind the optional **`review` extra**
  (`pip install divergulent[review]`, or `[review,verify]` to sign), off the
  default scan/report path; the server binds **loopback only**, has no auth, and
  is a single-user local tool — never CI, never a client feature. Signing is the
  same lazy Sigstore flow, built on the first verdict so browsing needs no extra.
- `divergulent/classify/cli.py` + `workspace.py` — `divergulent-classify`, the
  **one curation front**. `workspace.py` resolves a **data root** (a
  `.divergulent` marker beside `corpus/`+`cache/`, discovered git-style); `cli.py`
  forwards each verb (`status`/`triage`/`risk`/`review`/`web`/`report`/…) to the
  existing module main with the resolved paths spliced in, so the operator types
  no paths. It guards a forgetful operator — clear errors for a missing ledger or
  not-a-root cwd, and a loud nag when the published **cache looks stale** — and
  `status` is the one-screen orientation. The old `python -m
  divergulent.classify.<x>` forms still work.
- `divergulent/classify/export.py` (the `export`/`import` verbs) — the ledger's
  **committed source of truth**. The classification ledger embeds irreproducible
  human + verified-LLM verdicts, so it reaches CI as a **JSONL export directory**,
  never the sqlite (binary: unreviewable, unmergeable, bloats git). `write_export`
  serialises every table as compact JSONL (null columns omitted), the two big
  append-only tables (`decision`, `observation`) **sharded by calendar month** — so
  no file crosses GitHub's 100 MB limit as the append-only ledger grows — plus a
  `manifest.json`; everything stably ordered so two exports are byte-identical.
  `load_export` rebuilds a faithful sqlite (ids preserved, so the derived verdict —
  which tie-breaks on `decision.id` — is identical) via `ledger.create_schema`. The
  round-trip is the trust anchor; the operator's `export → commit → push` is the
  human-in-the-loop publish gate (a reviewable diff).
- `divergulent/classify/classification_bundle.py` (the `bundle` verb) — the
  publishable half, mirroring `bundle.py`: a single gzipped, key-sorted JSON
  document, `schema`/`entry_schema`-versioned, keyed by patch fingerprint.
  `build_classification_bundle` projects the ledger down to a **lean**
  fingerprint→verdict map (category + risk/reach/reviewability + a short provenance
  reason + the deciding rule) with **no raw LLM evidence** (that stays in the
  export). CI builds it from the export (`tools/build-classification.sh`, pure
  Python), signs keyless (`tools/sign-bundle.sh`) and publishes to the rolling
  `classification` release (`build-classification.yml`); the client pulls it (`cache
  pull-classification`) and `show` joins by hashing the patch body it already
  fetched. It *grows* as review settles the residue.
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
- Prompt-injection screening of LLM-bound patch text is under evaluation
  (`docs/plans/PLAN-prompt-injection-screening.md`, prototypes in
  `tools/injection-screening/`): if a technique graduates it lands as an
  `llm-injection-suspect` observation mirroring the dangerous-construct
  scan — skip the LLM passes, raise review priority, badge the UI; never
  a category, never a malice verdict.
