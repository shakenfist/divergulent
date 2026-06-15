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
overrides sources.debian.org to run a few requests/second (it has no
documented limit), while Repology stays at the mandated 1 req/s — see
`cli._http_client` / `SOURCES_DEBIAN_INTERVAL`.

The whole-machine divergence overview uses `summary()` — one request per
source (patch count + state), no patch-body fetches — so a full
`score`/`divergence` run stays polite. Per-patch classification
(`details()`, used by `show`) fetches each patch body and caches
version-pinned content with a long TTL (30 days), since that content is
immutable. The `divergence`/`score` commands still take `--limit`.

Whole-machine **staleness** uses `repology.build_staleness_map` — one
cached sweep of the entire Repology archive (`/api/v1/projects/`
paginated, `RepologyBulkSource`) instead of a request per source. The
map is cached ~24h and shared across commands, so the staleness cost is
per-archive, not per-machine. Per-package staleness (`show`) still uses
the per-package `project-by` resolver.

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
