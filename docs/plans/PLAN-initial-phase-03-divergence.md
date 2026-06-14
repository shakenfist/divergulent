# Phase 3 — Divergence axis (debian/patches + DEP-3)

Part of [PLAN-initial.md](PLAN-initial.md). Plan this phase at
**high effort**: the DEP-3 parsing/classification and the
honest handling of unknowns are the correctness core, and this
is the axis closest to the project's motivating concern —
surfacing distro-only carried patches.

**Status: complete.** All steps (3a get_text, 3b dep3, 3c
adapter, 3d divergence CLI, 3e docs) are implemented and
committed; `divergulent divergence` works and the suite passes
via `tox -epy3`. Two issues found by live smoke testing were
fixed: the raw patch URL needed the Debian pool path (and the
file-info API a trailing slash), and — per the operator's
decision — DEP-3 classification was supplemented with
Debian-authored heuristics (`# DP:` convention, deb-*/debian-*
filenames) because DEP-3 adoption is sparse.

## Prompt

Phases 1–2 are merged; explore the existing code before adding
to it: `divergulent/http.py` (the polite HTTP client to reuse
and extend), `divergulent/cache.py`, `divergulent/sources/base.py`
(the `Source` protocol), `divergulent/sources/repology.py` (the
adapter pattern to mirror), `divergulent/inventory.py`, and
`divergulent/cli.py` (the dedup + command pattern). Ground the
sources.debian.org and DEP-3 details in their docs rather than
guessing:

- **Patch series + format (one request):**
  `GET https://sources.debian.org/patches/api/<package>/<version>/`
  returns JSON `{package, version, format, count, patches}`,
  where `format` is e.g. `"3.0 (quilt)"` and `patches` is an
  array of patch filenames (the quilt series). `version` may be
  the literal `latest`.
- **Raw patch content (one request per patch):**
  `GET https://sources.debian.org/data/<package>/<version>/debian/patches/<name>`
  returns the raw patch file. (Alternatively the file API
  `/api/src/.../<name>` returns JSON with a `raw_url`.)
- **DEP-3 header:** an RFC-2822-style block at the top of a
  patch. It ends at the first empty line; a line of `---` stops
  metadata parsing. Key fields: `Description`/`Subject`,
  `Origin` (category `upstream`|`backport`|`vendor`|`other`,
  plus URL), `Author`/`From`, `Forwarded`
  (`no`|`not-needed`|`yes`|URL), `Bug`/`Bug-<vendor>`,
  `Applied-Upstream`, `Last-Update`. Implicit rule: if
  `Forwarded` is absent and a `Bug` field is present, treat as
  forwarded; otherwise the spec assumes not-forwarded.

Surface uncertainty honestly: a patch with no DEP-3 metadata is
*unknown*, not assumed divergent. A package we cannot resolve is
*unknown*, not zero-divergence.

## Objective

Add the divergence axis: for each installed source package,
fetch its quilt patch series, classify each patch via DEP-3
into forwarded-upstream vs Debian-only vs unknown, and report
per-package divergence ranked by Debian-only patch weight.
Deliver a `divergulent divergence` command, and extend the HTTP
client with raw-text fetching.

End state: `divergulent divergence` produces a ranked report on
a real Debian box; the suite passes offline with all HTTP
mocked; sources.debian.org is queried politely and patch
content (immutable per version) is cached with a long TTL.

## Design decisions

- **Source-format handling.** Use the `format` field from the
  patches API. Only `3.0 (quilt)` (and `3.0 (native)`) are
  expected. A **native** package has no upstream/Debian split,
  so divergence is reported as a distinct `NATIVE` state, not
  "zero patches". A non-quilt, non-native format with no series
  is reported `UNKNOWN` for divergence rather than guessed.
- **Patch series source.** One request to
  `/patches/api/<pkg>/<version>/` yields format + count +
  filenames — cheaper and more reliable than fetching
  `debian/source/format` and `debian/patches/series`
  separately.
- **Version to query.** Use the installed *source* version
  (immutable). If the API 404s for it, report the package as
  `UNKNOWN` (do not silently substitute `latest`, which would
  describe a different version than what is installed).
  *Implementation note:* verify how sources.debian.org encodes
  versions in the path (epoch handling, `+`/`~`/`:` encoding)
  against a real epoch package during 3c.
- **DEP-3 classification (the correctness core).** Parse the
  header block (stop at first blank line or a `---`/diff
  marker) and classify each patch into one of three buckets:

  | Class | Signals |
  |-------|---------|
  | `FORWARDED` | `Applied-Upstream` present; or `Forwarded: yes`/URL; or `Origin` category `upstream`/`backport`; or `Forwarded` absent **and** a `Bug` field present (implicit yes) |
  | `DEBIAN_ONLY` | `Forwarded: no` or `Forwarded: not-needed`; or `Origin` category `vendor` |
  | `UNKNOWN` | no DEP-3 metadata at all, or metadata present but no forwarding/origin/bug signal |

  Deliberate deviation from DEP-3's implicit default: a patch
  with a description but no forwarding/origin/bug signal is
  `UNKNOWN`, **not** assumed `DEBIAN_ONLY`. The project's
  no-cry-wolf principle means we do not assert divergence
  without evidence.
- **HTTP client extension.** Add `get_text(url, ...)` to
  `HttpClient` for raw (non-JSON) content, sharing the same
  politeness + cache path as `get_json`. Patch content is
  immutable for a fixed (package, version), so the divergence
  cache uses a long TTL (e.g. 30 days).
- **Request volume + politeness.** A full-machine scan is
  1 series request + N patch requests per source — potentially
  large. Mitigations: immutable long-TTL caching (reruns are
  free), progress to stderr, and a `--limit N` flag to cap the
  number of source packages processed in one run. Do **not**
  run an unbounded full-machine live scan as a smoke test.
- **Dedup by source package**, exactly as the staleness command
  does — one set of requests per source.
- **CLI.** Add `divergulent divergence` (`--json`, `--all`,
  `--limit N`). Default output: packages carrying at least one
  `DEBIAN_ONLY` patch, ranked by Debian-only count (then total
  patch count, then name), as a table (source, version, total,
  debian-only, forwarded, unknown). `--all` includes packages
  with no Debian-only patches and the NATIVE/UNKNOWN states.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 3a | medium | sonnet | none | Extend `divergulent/http.py` with `get_text(url, *, cache_namespace, cache_key, ttl_seconds) -> str | None`, mirroring `get_json` (same User-Agent, timeout, rate limiting, caching, and graceful-degradation-to-None), but returning the decoded body instead of parsed JSON. Refactor the shared fetch path so `get_json` and `get_text` do not duplicate the request/throttle/cache logic. Add tests (offline, injected urlopen/clock/sleep): text round-trip, cache hit skips network, errors return None. |
| 3b | high | opus | none | Implement `divergulent/dep3.py`: a pure parser/classifier with no I/O. `PatchClass` enum (FORWARDED / DEBIAN_ONLY / UNKNOWN). `parse_header(text) -> dict` reads the RFC-2822 header block from the top of a patch (handle folded/continuation lines; stop at the first blank line or a line that is `---` or a diff marker such as `--- a/`, `+++ `, `diff `, `Index:`, `@@`). `classify(text) -> PatchClass` applies the table in Design decisions (Applied-Upstream / Forwarded / Origin category / implicit-yes-via-Bug → FORWARDED; Forwarded no|not-needed / Origin vendor → DEBIAN_ONLY; otherwise UNKNOWN). This is high effort: real patches come in many shapes (DEP-3, git-format-patch, bare diffs), and the honest UNKNOWN handling matters. Tests cover: a DEP-3 vendor patch (DEBIAN_ONLY), Forwarded: no (DEBIAN_ONLY), Forwarded: yes/URL (FORWARDED), Origin: upstream (FORWARDED), Applied-Upstream (FORWARDED), implicit-yes via Bug (FORWARDED), a bare diff with no header (UNKNOWN), and a patch with only Description/Author (UNKNOWN). |
| 3c | high | opus | none | Implement `divergulent/sources/debian_patches.py`: `DebianPatchesSource(http_client)` implementing `Source` (`name = 'debian-patches'`). Define `DivergenceState` (PATCHED / CLEAN / NATIVE / UNKNOWN) and `@dataclass(frozen=True) DivergenceResult(source_package, version, source_format, total, debian_only, forwarded, unknown, state)`. `lookup(source_package, version)` fetches `/patches/api/<pkg>/<version>/` via the http client (cache namespace `debian-patches`, long TTL) and returns the parsed dict or None. `divergence(source_package, version)`: resolve format (NATIVE if native; UNKNOWN if unresolved/no series), then for each patch filename fetch its raw content from the `/data/...` path via `get_text` and classify with `dep3.classify`, tallying the three buckets; state is PATCHED if any patches, CLEAN if quilt with zero patches. Build URLs with proper encoding for version and patch path. Verify version-path/epoch encoding against a real package during implementation. Tests use a fake http client returning recorded patches-API JSON and recorded patch texts (fixtures); cover quilt-with-patches (counts per class), native, zero-patch quilt (CLEAN), and unresolved (UNKNOWN). |
| 3d | medium | sonnet | none | Add the `divergence` CLI command. In `cli.py` add a `divergence` sub-parser (`--json`, `--all`, `--limit`). Build the inventory, dedup by source package (reuse `_dedup_sources`), apply `--limit`, construct a `DebianPatchesSource` over an `HttpClient` over the default cache, call `divergence()` per source, and render: by default only packages with ≥1 DEBIAN_ONLY patch, ranked by debian_only desc then total desc then name, as a table (SOURCE, VERSION, TOTAL, DEBIAN-ONLY, FORWARDED, UNKNOWN); `--all` includes CLEAN/NATIVE/UNKNOWN; `--json` mirrors it; counts/progress to stderr. Tests: mock the source so no network is touched; assert dedup, `--limit`, default-vs-`--all` filtering, ranking, and both output modes. |
| 3e | low | sonnet | none | Documentation. Update `ARCHITECTURE.md` (add `dep3.py`, the debian-patches source, `get_text`, and the divergence data flow), `AGENTS.md` (the request-volume note and immutable long-TTL caching for version-pinned patch content), and `README.md` (a `divergulent divergence` usage example, the DEP-3 classification meaning, and that it is heuristic — many patches lack DEP-3 metadata and are reported UNKNOWN). |

Steps run in order: 3b depends on 3a only loosely (independent,
can parallel); 3c depends on 3a + 3b; 3d on 3c; 3e on all.
Commit per step per the master plan.

## Testing requirements

- The whole suite stays **offline**: all HTTP mocked. No test
  hits sources.debian.org.
- DEP-3 classification is tested against recorded patch texts
  covering every bucket and several real-world header shapes
  (DEP-3, git-format-patch, bare diff).
- The adapter is tested against recorded patches-API JSON plus
  patch-text fixtures for quilt/native/clean/unresolved.
- The CLI dedups, honours `--limit`, and ranks by Debian-only.

## Success criteria for this phase

- `divergulent divergence` returns a correct ranked report on a
  real Debian machine (operator smoke check, on a small
  `--limit`).
- Patches are classified by DEP-3 evidence; patches without
  evidence are UNKNOWN, not assumed Debian-only (no cry-wolf).
- Native packages are reported as NATIVE, not zero-divergence;
  unresolved packages are UNKNOWN.
- sources.debian.org is queried politely; version-pinned patch
  content is cached with a long TTL so reruns are free.
- `tox -epy3` and `tox -eflake8` pass; docs updated.

## Open questions for this phase

- **Version path encoding.** Confirm how sources.debian.org
  encodes the version in `/patches/api` and `/data` paths,
  especially epochs (`1:2.3-4`) and `+`/`~`. Resolve in 3c with
  a real epoch package; fall back to UNKNOWN on 404 rather than
  guessing.
- **Weighting.** For now divergence is ranked by *count* of
  Debian-only patches. Weighting by patch size/hunks is
  deferred to Phase 4 scoring — confirm count-only is fine for
  this phase.
- **`--limit` default.** Should `divergence` require `--limit`
  (or prompt) given the full-machine request volume, or run
  unbounded with cached reruns? Proposed: unbounded but with
  clear stderr progress and immutable caching; confirm.

## Out of scope (later phases)

- Combining staleness and divergence into one per-package
  signal and a whole-machine summary (Phase 4).
- Patch-content/size weighting, and fetching only patch headers
  via HTTP range requests (Future work).
- A server/aggregator and other sources (UDD/DEHS/Wikidata).

## Back brief

Before executing, back brief the operator on your
understanding of this phase and how the intended work aligns
with it.
