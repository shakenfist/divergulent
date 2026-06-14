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
  network-backed sources fetch through: identifying User-Agent, request
  timeout, ≤1 request/second rate limiting, on-disk caching, and
  graceful degradation (failures return `None`). Stdlib `urllib`; the
  urlopen/clock/sleep are injectable for offline tests.
- `divergulent/sources/base.py` — the `Source` protocol that
  data-source adapters implement.
- `divergulent/sources/repology.py` — the Repology adapter (staleness
  axis). Resolves a Debian source name to its Repology project via the
  `project-by` resolver, picks the newest stable upstream version, and
  compares it against the installed *upstream* version. Yields CURRENT /
  BEHIND / UNKNOWN (unresolved is UNKNOWN, never BEHIND).
- `divergulent/tests/` — testtools tests run via stestr/tox; every
  external effect is mocked or driven from a fixture so the suite runs
  offline.

## Data flow (today)

```
inventory:  dpkg-query  ->  inventory.list_installed()  ->  [InstalledPackage]  ->  cli (table / JSON)

staleness:  inventory  ->  dedup by source  ->  RepologySource.staleness()  ->  cli (ranked table / JSON)
                                                      |
                                          HttpClient (cache + politeness)  ->  repology.org
```

## Planned

- **Phase 3** — a sources.debian.org adapter for the divergence axis
  (`debian/patches` + DEP-3 classification).
- **Phase 4** — a scoring model combining the two axes into a ranked,
  whole-machine report.
