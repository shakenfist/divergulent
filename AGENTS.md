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
tracker link Repology mandates), a request timeout, ≤1 request per
second, on-disk caching (default 24h TTL), and graceful degradation —
any failure returns `None` and is surfaced to the user as *unknown*,
never as a confirmed finding. HTTP uses the standard library (no
`requests`/`httpx`).

The divergence axis is request-heavy (one series request plus one per
patch, per source package), so it caches version-pinned patch content
with a long TTL (30 days) — that content is immutable — and the
`divergence` command takes `--limit`. Do not run an unbounded
full-machine divergence scan against sources.debian.org as a casual
test.

DEP-3 metadata is sparse in real Debian patches, so `dep3.classify`
supplements explicit DEP-3 fields with Debian-authored heuristics (the
old `# DP:` convention and deb-*/debian-* filenames). Explicit DEP-3
always wins; patches with neither are UNKNOWN, not assumed divergent.

## Scoring

`score.combine` ranks packages with a transparent weighted sum
(`debian_only*3 + unknown_patches*1 + behind*2`; forwarded patches score
0). The weights are provisional, to be tuned once we have real data.
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

## Planning workflow

Plans live in `docs/plans/` — a master plan plus one file per phase,
created from [PLAN-TEMPLATE.md](PLAN-TEMPLATE.md). Pre-push checks are in
[PUSH-TEMPLATE.md](PUSH-TEMPLATE.md).
