# Agent and contributor notes

## Build, test, lint

- Run the unit tests: `tox -epy3` (stestr + testtools).
- Run lint: `tox -eflake8` (flake8, max line length 120; checks the
  current change).
- Tests must pass **offline** — no calls to live external services.
  Mock external effects or use recorded fixtures
  (`divergulent/tests/fixtures/`). The `dpkg-query` call in
  `inventory.py` is injectable for exactly this reason.

## Conventions

- Python ≥ 3.11. Single quotes for strings, double quotes for
  docstrings, lines wrapped at 120 characters, no trailing whitespace.
- Compare versions only via `divergulent.debversion` (Debian ordering:
  epochs, `~`, revisions) — never with naive string comparison.
- The installed-package inventory is sensitive; do not transmit it
  off-box without an explicit, opt-in, documented path.

## Network access

All outbound HTTP goes through `divergulent.http.HttpClient`, never raw
`urllib`/sockets in a source. It enforces the politeness external
services require: an identifying User-Agent (with the repo + issue
tracker link Repology mandates), a request timeout, rate limiting **per
host**, on-disk caching (default 24h TTL), and graceful degradation —
any failure returns `None` and is surfaced to the user as *unknown*,
never as a confirmed finding. HTTP uses the standard library (no
`requests`/`httpx`). The default interval is ≤1 request/second; the CLI
sets sources.debian.org's interval to 0 (it has no documented limit) and
instead bounds it with concurrency, while Repology stays at the mandated
1 req/s — see `cli._http_client` / `SOURCES_DEBIAN_INTERVAL` /
`DEFAULT_WORKERS`. The per-host throttle is a thread-safe "ticket"
reservation, so it stays correct under concurrent workers: each host's
requests stay spaced (Repology ≤1 req/s in aggregate) while different
hosts overlap. The `divergence`/`score` commands gather over deduped
sources through `cli._concurrent_map` on a thread pool sized by
`--workers` (default 8); `--workers 1` is serial. `Cache.set` writes via
a uniquely named temp file so concurrent writers cannot clobber.

The whole-machine divergence overview uses `summary()` — one request per
source (patch count + state), no patch-body fetches — so a full
`score`/`divergence` run stays polite. The count is read from the API's
top-level `count` field, not the rendered `patches` array (which the API
caps at 60), so heavily-patched packages report their true total. Per-patch classification
(`details()`, used by `show`) fetches each patch body and caches
version-pinned content with a long TTL (30 days), since that content is
immutable. The `divergence`/`score` commands still take `--limit`.

Whole-machine **staleness** uses the per-package `project-by` resolver
(`RepologySource`), caching each result ~24h. A whole-archive bulk sweep
(`/api/v1/projects/`) was tried and reverted: for one machine it
downloaded ~600 MB to answer ~570 lookups and made a cold run *slower*
(~22 min vs ~9.5 min) — see
`docs/plans/PLAN-faster-full-run-phase-04-revert-bulk.md`. The real
cold-run win is the published precomputed cache
(`docs/plans/PLAN-published-cache.md`), with concurrency covering the
live fallback.

The **cache builder** (`divergulent.builder` + `cli.build_bundle`,
exposed as `divergulent cache build`, run centrally in CI — see
`.github/workflows/build-cache.yml`) is where the bulk Repology sweep
*does* belong: one polite whole-archive crawl, computed once, feeds every
user's bundle, instead of N users each hammering the APIs. It enumerates
the archive from deb-src `Sources` indices with `debian.deb822.Sources`
(no network) and gathers a divergence `summary()` per source through the
same `_concurrent_map`. `HttpClient(refresh=True)` (the `--refresh` flag)
skips cache reads but still writes, so a periodic full rebuild can bound
how long a once-bad cached value lives in the bundle. Bundles are written
by `divergulent.bundle` as gzipped JSON; the client-side consumer is a
later phase, so the builder must not import or alter the per-package
client path in `repology.py` (it imports only `_select_newest`).

**Bundle-backed consumption (`--bundle`).** When `--bundle PATH` points
at a recognised, release-matched bundle (`cli._usable_bundle` checks
`schema`/`cache_schema` and that the bundle's `release` matches
`_detect_release()`, else warns and runs fully live), the whole-machine
commands resolve staleness via `RepologyBulkSource` (a `{srcname:
newest}` dict lookup) and divergence via `BundleDivergenceSource` (used
only on an exact installed-version match, since divergence is
version-specific). Both are wrapped in `Fallback{Staleness,Divergence}`
(`sources/bundle_backed.py`) that fall back to the live source **per
entry** on a miss — so a bundle-backed run is fast (a release-matched
bundle covers nearly every installed source) yet never less complete
than the live run, and UNKNOWN still means neither source could resolve.
The bundle is read locally, so the inventory never leaves the host. The
sources are selected in `cli._resolve_sources`; `show` and `--classify`
stay fully live (the bundle holds summaries, not patch bodies).

`divergulent cache pull [--cache-url URL]` downloads the release bundle
with `HttpClient.get_bytes` (raw bytes, throttled and size-capped but not
value-cached), validates it with `bundle.loads` + schema/release checks,
and stores the **downloaded bytes verbatim** at `bundle.stored_path`
(`cache-<release>.json.gz` under the cache dir) via an atomic
temp-file+rename — verbatim so a phase-4 signature verifies against
exactly what was published; a download that fails to parse, is
unrecognised, or is for another release is refused and nothing is stored.
`cli._select_bundle` then auto-discovers that stored file when `--bundle`
is absent (silently, since no store is the normal pre-pull state). A
**freshness contract** governs use: bundle divergence is always served
(immutable), but bundle staleness only while `generated_at` is within
`BUNDLE_STALENESS_TTL_SECONDS` (7 days) — past that staleness is queried
live, since a stale "newest" only under-reports BEHIND (newest versions
only increase) and we would rather go live than silently miss. The
freshness clock is the injectable `cli._utc_now` (so tests pin it).

**Trust (`divergulent/verify.py`).** A downloaded bundle is untrusted, so
`cache pull` runs two independent, fail-closed checks before storing it
(and `cache verify` re-runs them). (1) **Signature**: the bundle is
Sigstore-signed in CI (`tools/sign-bundle.sh` in `build-cache.yml`, keyless
OIDC, emitting `<bundle>.sigstore.json`); `verify.verify_signature`
verifies it against `EXPECTED_SIGNER_IDENTITY`/`ISSUER`. It **lazily
imports `sigstore`** and returns SKIPPED — not FAILED — when the optional
`verify` extra (`divergulent[verify]`, `sigstore>=4.3,<5`) is absent, so
the base install keeps its stdlib + python-debian footprint;
`--require-signature` makes a skipped/failed signature fatal. (2)
**Spot-check** (always on, stdlib): `verify.spot_check` samples the
bundle's *immutable* divergence entries and compares `(state, total)`
exactly against a live `summary()`, refusing only on a definite-vs-definite
disagreement. "No cry wolf" applies to both sides: a bundle entry that is
itself UNKNOWN (the bundle declining to claim, e.g. a transient build-time
fetch failure) and an unresolvable live result (UNKNOWN/None) are both
inconclusive — neither refuses a signed bundle. `--insecure` skips
both; `--spot-check N` tunes the sample (0 disables). The stored bytes are
kept **verbatim** so the signature verifies against exactly what was
published. Signature verification's trust root is
fetched once via Sigstore's TUF and cached.

**Publishing.** `build-cache.yml` runs on a schedule (daily incremental,
weekly `--refresh` full rebuild) as well as `workflow_dispatch`: it
builds `cache-<release>.json.gz` (release detected from `/etc/os-release`),
signs it (`tools/sign-bundle.sh`), and publishes the bundle and its
signature with `tools/publish-cache.sh` to a **rolling, in-place `cache`
prerelease** (`contents: write`). That tag is deliberately a prerelease so
it never shadows the software "latest" release; the client's
`DEFAULT_CACHE_URL_TEMPLATE` points at
`.../releases/download/cache/cache-<release>.json.gz`. Because signing
stays in `build-cache.yml` on `main`, the Sigstore identity remains
`EXPECTED_SIGNER_IDENTITY` (`build-cache.yml@refs/heads/main`) for
scheduled and dispatched runs alike — but this must be confirmed against a
real published signature (no end-to-end VERIFIED has run yet).

`--classify` (Tier 2) classifies the whole machine via
`divergulent.sources.apt_patches.AptSourcePatches`: it resolves each
source package's URLs with `apt-get source --print-uris` and fetches only
the `.dsc` and `.debian.tar.*` from the configured mirror (never the
`.orig` tarball), extracting `debian/patches` and classifying with the
same `dep3` logic. It needs `deb-src` indices; `deb_src_available()`
gates it and the CLI falls back to patch counts with a clear message when
they are absent.

DEP-3 metadata is sparse in real Debian patches, so `dep3.classify`
supplements explicit DEP-3 fields with Debian-authored heuristics (the
old `# DP:` convention and deb-*/debian-* filenames). Explicit DEP-3
always wins; patches with neither are UNKNOWN, not assumed divergent.

`divergulent/classify/` is **curation-side only** — it runs centrally in
the builder, never on a client — and is phase 1 of the patch-classification
plan (`docs/plans/PLAN-patch-classification.md`). `corpus.py` crawls the
archive's patched packages into a resumable, content-addressed corpus of raw
patch bodies (reusing `apt_patches`' uncapped fetch with per-worker keep-alive
connection reuse so a bulk crawl resolves DNS ~once per worker, not per file);
`fingerprint.py`/`measure.py` deduplicate and count. The first crawl measured
≈61.5k carried patches → 60,640 distinct (dedup 1.02x): carried patches are
overwhelmingly bespoke, so classification leverage must come from category
rules, not deduplication. See
`docs/plans/PLAN-patch-classification-phase-01-findings.md`.

Phase 2 (`claim.py`/`content.py`/`rules.py`/`classify.py`) classifies each
fingerprint deterministically, keeping the author's **claim** (DEP-3 metadata,
untrusted) strictly separate from the **content** (the diff, ground truth) so
their disagreement is the signal. Content is typed code-vs-prose, and the
dangerous-construct scan runs only over added lines in code files — never
pronouncing malice, only surfacing candidate flags. It measured 29.2% of
patches as deterministically settled (packaging/documentation), ~43k
substantive residue for phase 4, and confirmed two things to act on: the
backtick scan needs to be language-aware (it cries wolf on JS/Lisp backticks),
and 58% of patches carry no usable claim. See
`docs/plans/PLAN-patch-classification-phase-02-findings.md`.

## Scoring

`score.combine` ranks packages with a transparent weighted sum
(`total_patches*W_PATCH + behind*W_BEHIND`). The whole-machine view uses
the cheap one-request-per-source patch *count* (it cannot tell
Debian-only from forwarded — that needs patch bodies; see `show`), so it
weights total carried patches. The weights are provisional, to be tuned
once we have real data.
Being *behind upstream* is weighted low on purpose: it is normal and
expected on a stable Debian release, so it must not dominate the report
or read as alarming on its own. The score only orders packages; the two
axes are always shown so the output is never an opaque verdict, and a
package that could not be assessed is reported as such, never as clean.

## Per-package detail (`show`)

`divergulent show <package>` is the per-package counterpart to `score`:
it lists each carried patch with its classification, description, and
the Debian/upstream bug references the patch *declares* (Debian refs are
linkified to bugs.debian.org). A patch with no declared bug shows "none
declared" — that means the patch does not reference a bug, not that no
bug exists. Querying the Debian BTS for bugs a patch does not reference
is deliberately out of scope (a Future work item).

## Dependencies

divergulent audits dependency/patch divergence, so it keeps its own
footprint small. The sole runtime dependency is `python-debian` (Debian
version comparison), wrapped in `debversion.py`. Add new runtime
dependencies only with clear, case-by-case justification, and prefer the
standard library (e.g. `argparse` over `click`).

## Releases

Releases are tag-driven (`v*`): Sigstore-signed tags plus PyPI trusted
publishing, running on self-hosted runners. The package version is
derived from the git tag by `setuptools_scm`. One-time configuration is
documented in [RELEASE-SETUP.md](RELEASE-SETUP.md).

The `sample-output.yml` workflow runs a full `divergulent score` on a
Debian 13 runner and uploads the rendered report as an artifact (its
logic lives in `tools/generate-sample-output.sh`, per the scripts-in-
tools rule). It runs Tier 1 in full (no `--limit`) — the polite full run
is the point — plus a small `--limit`ed `--classify` sample, and
persists `DIVERGULENT_CACHE_DIR` via `actions/cache`.

## Planning workflow

Plans live in `docs/plans/` — a master plan plus one file per phase,
created from [PLAN-TEMPLATE.md](PLAN-TEMPLATE.md). Pre-push checks are in
[PUSH-TEMPLATE.md](PUSH-TEMPLATE.md).
