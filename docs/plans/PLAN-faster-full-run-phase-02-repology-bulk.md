# Phase 2 — Repology bulk staleness

Part of [PLAN-faster-full-run.md](PLAN-faster-full-run.md).
High effort: a new bulk-fetch path with pagination, caching, and
local matching, plus honest UNKNOWN handling.

**Status: complete.** `build_staleness_map` pages
`/api/v1/projects/?inrepo=debian_unstable` into a cached
`{srcname: newest}` map; `RepologyBulkSource` serves staleness
from it (absent → UNKNOWN). The whole-machine commands
(`staleness`, `score`, `score --classify`) use it; `show` keeps
the per-package resolver. Verified live: a cold whole-machine
`staleness` (building the map) took ~4m38s and a warm run
~0.12s; verdicts agree with the per-package path.

## Prompt

Read `divergulent/sources/repology.py` (the per-package
`project-by` resolver, `newest_version()` selection, and the
CURRENT/BEHIND/UNKNOWN logic), `divergulent/http.py`, and
`divergulent/cli.py` (the staleness/score gather loops).
Research the Repology bulk API:

- `GET /api/v1/projects/` returns up to 200 projects; page by
  appending the last project name: `/api/v1/projects/<name>/`.
- `?inrepo=debian_unstable` restricts to projects packaged in
  Debian unstable; each project's entries carry per-repo
  `srcname`, `version`, `status`.

Repology mandates ≤1 req/s and an identifying User-Agent.

## Objective

Replace ~1 Repology request per source with one cached sweep of
the whole `debian_unstable` project set, so staleness lookups
become local map hits — making the staleness cost per-archive
(~150 cached requests) rather than per-machine.

## Design decisions

- **Bulk map.** Build `{debian srcname: newest stable version}`
  by paging `/api/v1/projects/?inrepo=debian_unstable`. For each
  project, take the `srcname` of its `debian_unstable` entry and
  the newest version via the existing `newest_version()` logic
  (prefer `status == newest`, skip ignored/untrusted, etc.).
- **Cache the assembled map** under a `repology-bulk` namespace
  (key e.g. `debian_unstable`) with a ~24 h TTL, so it is built
  once per day. Paging respects the ≤1 req/s Repology limit.
- **Staleness via the map.** Add a bulk-backed path: given the
  map, `staleness(source, installed_version)` compares the
  installed *upstream* version against the map's newest (same
  comparison as today). A source absent from the map →
  UNKNOWN (do not fall back to a per-package request; see the
  master plan's open question).
- **Reuse, don't fork, the comparison/selection logic** in
  `repology.py` so the bulk and per-package paths agree. Keep
  the per-package `project-by` path for `show`/single-package
  use; the whole-machine commands use the bulk map.
- **Honesty unchanged** — heuristic name matching, unresolved →
  UNKNOWN, never a false BEHIND.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 2a | high | sonnet | none | In `repology.py`, add a bulk loader that pages `/api/v1/projects/?inrepo=debian_unstable` via the `HttpClient` (cache namespace `repology-bulk`, ~24h TTL), builds `{srcname: newest_version}` using the existing `newest_version()` selection, and stops when a page returns <200 projects. Factor the newest-selection/upstream-comparison so it is shared with the per-package path. Add a bulk-backed `staleness()` that looks up the map and returns CURRENT/BEHIND/UNKNOWN (UNKNOWN when the srcname is absent). Tests: paging assembles the map across two fake pages; staleness from the map matches the per-package result on shared cases; an absent package is UNKNOWN; cached map avoids re-paging. |
| 2b | medium | sonnet | none | Wire the whole-machine commands (`staleness`, `score`) to build the bulk map once and use the bulk-backed lookup instead of per-package `project-by`. Keep `show` on the per-package path. Update tests (`test_cli_*`) for the bulk source seam. Live-smoke a full (or large `--limit`) `score` to confirm the staleness half drops from ~1 request/source to the cached sweep, and that results still look right. |

## Testing requirements

- Offline; HTTP mocked (fake paged responses).
- Bulk map assembly (pagination), map-based staleness vs.
  per-package agreement, UNKNOWN for misses, and cache reuse.

## Success criteria

- A cold full `score` no longer makes ~1 Repology request per
  source; the staleness half is a ~150-request cached sweep.
- Staleness verdicts are unchanged in meaning and agree with the
  per-package path on a sample.
- Combined with phase 1, a cold full run is a few minutes (down
  from ~19).
- `tox -epy3` / `tox -eflake8` pass; docs updated.

## Open questions

- Per-package `project-by` fallback for sources absent from the
  bulk map vs. UNKNOWN (proposed UNKNOWN).
- Whether to page the whole archive or only batches covering the
  installed set (proposed: whole archive, cached).

## Out of scope

- Bounded-concurrency fetching (Future work).

## Back brief

Before executing, back brief the operator on your understanding
of this phase.
