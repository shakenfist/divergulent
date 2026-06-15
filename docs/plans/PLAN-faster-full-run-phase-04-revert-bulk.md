# Phase 4 — Revert whole-machine staleness to per-package

Part of [PLAN-faster-full-run.md](PLAN-faster-full-run.md).
Medium effort: a targeted revert of phase 2's wiring, plus
removing the now-unused bulk code and tests.

**Status: complete.** The whole-machine commands use the
per-package `RepologySource`; `build_staleness_map`,
`RepologyBulkSource`, their constants and tests are removed. The
audit hardening that still applies (the `_select_newest` dict
guard, the `HttpClient` size cap) is retained. Suite green.

## Why

Phase 2 replaced the per-package Repology `project-by` lookup with
a paged sweep of the whole `debian_unstable` project set, on the
theory that ~170 requests beats ~570. Measured on a real cold CI
run (run 27540333227, 34m23s) and confirmed with direct probes,
that theory is wrong for a single machine:

| Path | Per request | Count | Wall-clock | Data |
|------|-------------|-------|------------|------|
| Bulk `/projects/` page | **7.8 s, 3.5 MB** | ~170 pages | **~22 min** | **~600 MB** |
| Per-package `project-by` | ~0.5 s, ~5 KB (1 req/s floor) | ~570 | ~9.5 min | ~3 MB |

The bulk endpoint returns every project's full cross-repo data, so
each page is latency-bound at ~7.8 s and the Repology 1 req/s
throttle is irrelevant. To answer staleness for the ~570 installed
sources it downloads ~600 MB describing all ~34,000 archive
projects. Bulk only wins when the 24 h archive map is shared across
*many* machines; for one machine's first cold run it is strictly
worse, and the cold first run is exactly the UX the plan exists to
fix.

The per-package results are themselves cached (24 h TTL, restored
in CI), so reverting loses nothing for repeat/CI runs.

## Objective

Whole-machine `staleness`, `score`, and `score --classify` resolve
staleness through the per-package `RepologySource` again. Remove
`build_staleness_map`, `RepologyBulkSource`, their bulk cache
constants, and their tests. Keep the genuinely useful hardening
from the audit: `_select_newest`'s `isinstance(entry, dict)` guard
(used by the per-package path) and `HttpClient`'s `max_bytes` cap.

## Steps

| Step | Brief |
|------|-------|
| 4a | In `cli.py`, replace `_bulk_repology()` with a per-package `RepologySource(_http_client())` helper used by `_staleness_command`, `_score_command`, and `_score_classified`. `show` already used `RepologySource`. |
| 4b | Delete `build_staleness_map`, `RepologyBulkSource`, `BULK_*`/`PROJECTS_PER_PAGE`/`BULK_MAX_PAGES` from `repology.py`, and their tests from `test_repology.py`. Drop the unused import in `cli.py`. |
| 4c | Update `ARCHITECTURE.md` / `README.md` staleness flow back to per-package; note the bulk experiment and why it was reverted under "Bugs fixed". |

## Success criteria

- `staleness`/`score` query Repology per source at <=1 req/s and
  cache per package; no whole-archive sweep remains.
- `tox -epy3` / `tox -eflake8` / `pre-commit` pass.
- Staleness meaning unchanged (CURRENT/BEHIND/UNKNOWN).
