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
- `divergulent/sources/base.py` — the `Source` protocol that
  data-source adapters implement.
- `divergulent/tests/` — testtools tests run via stestr/tox; every
  external effect is mocked or driven from a fixture so the suite runs
  offline.

## Data flow (today)

```
dpkg-query  ->  inventory.list_installed()  ->  [InstalledPackage]  ->  cli (table / JSON)
```

## Planned

- **Phase 2** — a Repology adapter (the first `Source`) for the
  staleness axis, plus the shared HTTP client and politeness layer
  (descriptive User-Agent, timeouts, rate limiting, graceful
  degradation) routed through the cache.
- **Phase 3** — a sources.debian.org adapter for the divergence axis
  (`debian/patches` + DEP-3 classification).
- **Phase 4** — a scoring model combining the two axes into a ranked,
  whole-machine report.
