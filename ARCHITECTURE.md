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
  identifying User-Agent, request timeout, ≤1 request/second **per host**
  rate limiting, on-disk caching, and graceful degradation (failures return
  `None`). Stdlib `urllib`; the urlopen/clock/sleep are injectable for
  offline tests.
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
  resolves one package at a time via the `project-by` resolver (used by
  `show`); `build_staleness_map` + `RepologyBulkSource` build one cached
  whole-archive `{srcname: newest}` map (`/api/v1/projects/` paginated)
  for the whole-machine commands, so staleness is per-archive not
  per-source. Both share the newest-selection/comparison logic.
- `divergulent/sources/debian_patches.py` — the sources.debian.org
  adapter (divergence axis). Reads a source package's quilt series from
  the patches API, fetches each patch under its pool `raw_url`, and
  classifies it with `dep3`. `summary()` is the cheap one-request
  overview (patch count + state) used by the whole-machine commands;
  `details()` fetches and classifies every patch body (`PatchDetail`:
  classification, description, bug references) for `show`. Both yield
  PATCHED / CLEAN / NATIVE / UNKNOWN. Version-pinned patch content is
  cached with a long TTL.
- `divergulent/sources/apt_patches.py` — the Tier 2 classification
  provider. Resolves a source package's mirror URLs via `apt-get source
  --print-uris`, fetches only the `.dsc` and `.debian.tar.*` (not the
  `.orig` tarball), extracts `debian/patches`, and classifies with
  `dep3` — the full breakdown across the machine via the mirror network.
  Requires `deb-src` (`deb_src_available()`).
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

divergence: inventory  ->  dedup by source  ->  DebianPatchesSource.summary()  ->  cli (count table / JSON)
                                                      |
                                          HttpClient (per-host throttle)  ->  sources.debian.org patches API

score:      inventory  ->  dedup by source  ->  staleness (cached bulk map) + divergence summary
                                            ->  score.combine()  ->  cli (ranked report + whole-machine summary)

--classify: inventory  ->  dedup by source  ->  AptSourcePatches.details() (apt mirror, per source)
                                            ->  dep3.classify() per patch  ->  cli (per-class breakdown)

show:       resolve one installed package  ->  staleness + details (one shared HttpClient)
                                            ->  cli (per-patch detail + Debian bug links)
```

## Planned

- See `docs/plans/` Future work for BTS cross-referencing (open Debian
  bugs a package's patches do not reference) and the candidate "patch
  hygiene & justification" master plan.
