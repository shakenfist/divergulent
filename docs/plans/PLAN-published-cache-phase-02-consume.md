# Phase 2 — Bundle-backed sources + live fallback

Part of [PLAN-published-cache.md](PLAN-published-cache.md).
Medium effort: new consumer source adapters and a fallback wrapper, plus
CLI wiring. No network code changes; the live sources are reused
verbatim as the fallback.

**Status: implemented.** `RepologyBulkSource` is resurrected in
`repology.py`; `sources/bundle_backed.py` adds `BundleDivergenceSource`
and the `FallbackStaleness`/`FallbackDivergence` wrappers;
`cli._usable_bundle` validates a bundle (schema + release) and
`cli._resolve_sources` selects bundle-backed-with-fallback sources for
`staleness`/`divergence`/`score` behind `--bundle PATH`, warning and
running fully live when the bundle is absent, unreadable, unrecognised,
or for a different release. Tests are offline (a written-then-loaded
fixture bundle; a raising HTTP client proves covered packages need no
network); `show` and `--classify` stay live. Suite green;
`pre-commit run --all-files` clean.

## Prompt

Read, and reuse rather than reinvent:

- `divergulent/bundle.py` — the bundle schema and `load()`. Phase 2
  consumes a loaded `Bundle`'s `staleness` (`{srcname: newest}`) and
  `divergence` (`{pkg: {version, format, total, state}}`) maps.
- `divergulent/sources/repology.py` — `RepologySource` (the live
  staleness source), `StalenessResult`/`StalenessState`, and the private
  `_state_for` the resurrected `RepologyBulkSource` reuses so the bundle
  and live paths classify identically.
- `divergulent/sources/debian_patches.py` — `DebianPatchesSource`
  (the live divergence source), `DivergenceSummary`/`DivergenceState`.
- `divergulent/cli.py` — `_repology`, `_http_client`, `_usable`/loader
  patterns, `_detect_release`, and the `staleness`/`divergence`/`score`
  gathers the bundle-backed sources plug into.
- Git history: `RepologyBulkSource` existed before `c6f91c7`; recover its
  shape (it was deferred from phase 1 to here).

## Objective

When a recognised, release-matched bundle is available locally, resolve
staleness and divergence from it (a dict lookup, instant) instead of
querying Repology and sources.debian.org — falling back to the live
sources for anything the bundle does not cover, so results never regress
versus today. Preserve "inventory stays local" (the bundle is read from
disk; nothing about the machine leaves it) and "no cry wolf" (UNKNOWN
only when neither the bundle nor the live source can resolve).

Phase 2 wires this behind an explicit `--bundle PATH`. Automatic
discovery, download, freshness/TTL enforcement and signature
verification belong to phases 3–4; phase 2 is the source mechanics and
the fallback, usable end-to-end against a bundle a human points at (the
artifact phase 1 produces).

## Design decisions

- **`RepologyBulkSource(staleness_map)`** (resurrected into
  `repology.py`): `lookup(name) -> newest | None` and
  `staleness(name, installed_version) -> StalenessResult` reusing
  `_state_for`. The bare source returns UNKNOWN for an absent srcname; it
  is the fallback wrapper, not the source, that decides whether to go
  live.
- **`BundleDivergenceSource(divergence_map)`** (new,
  `sources/bundle_backed.py`): `summary(name, version) ->
  DivergenceSummary | None`. Returns the published summary **only when
  the installed version equals the bundle's** (divergence is
  version-specific); otherwise `None` (a miss). Reconstructs
  `DivergenceState` from the stored string.
- **Fallback wrappers** (`sources/bundle_backed.py`), tried bundle-first,
  live-second — `tries the bundle, then the live source`:
  - `FallbackStaleness(bundle, live)`: live only when the srcname is
    absent from the bundle map.
  - `FallbackDivergence(bundle, live)`: live only when the bundle returns
    a miss (absent or version-mismatch).
  Both fall back **per entry**, not per run. This is the "no cry wolf"
  choice: we report UNKNOWN only when even the live source cannot
  resolve. It stays fast because a release-matched bundle covers nearly
  every *installed* source (the misses are third-party / locally-built /
  version-drifted packages, a small minority), and the live half for
  divergence is the unthrottled, concurrent sources.debian.org. The
  Repology rate limit means a pathological machine with many
  bundle-absent sources still pays ≤1 req/s for those few; acceptable,
  and a strict "bundle-only, no fallback" mode is noted as future work.
- **CLI selection.** A `_usable_bundle(path)` helper loads the bundle and
  returns it only if the envelope and entry schemas are recognised
  (`schema`/`cache_schema`) **and** its `release` matches the running
  system (`_detect_release`); otherwise it warns and returns `None` so
  the command runs fully live — "unrecognised / wrong-release / absent
  bundles degrade to today's behaviour exactly." `--bundle PATH` is added
  to `staleness`, `divergence` and `score`. `show` stays live (a single
  package, and the bundle carries summaries, not per-patch detail).
  `--classify` stays live (it needs patch bodies the bundle does not
  hold).
- **Freshness is deferred to phase 3.** With an explicit `--bundle` the
  user chose the file; staleness from a day-old daily bundle is fine.
  TTL/"too old to trust" enforcement is tied to acquisition (pull) and
  lands in phase 3.

## Steps

| Step | Effort | Model | Brief for sub-agent |
|------|--------|-------|---------------------|
| 2a | medium | sonnet | Resurrect `RepologyBulkSource` into `repology.py` with `lookup()` and `staleness()` reusing `_state_for`; absent srcname → UNKNOWN. Tests: states (behind/current/unknown), `lookup` miss, and agreement with the per-package path for the same map value. |
| 2b | medium | sonnet | Add `sources/bundle_backed.py`: `BundleDivergenceSource` (`summary()` returns a `DivergenceSummary` only on exact version match, else `None`; rebuilds `DivergenceState` from the string) plus `FallbackStaleness` and `FallbackDivergence` wrappers (bundle-first, live on miss). Offline tests for hit, version-mismatch miss → live, absent → live, and that a present hit never calls live. |
| 2c | medium | sonnet | Wire the CLI: `_usable_bundle(path)` (load + schema/cache_schema recognition + release match, warn-and-`None` otherwise), `--bundle PATH` on `staleness`/`divergence`/`score`, and source selection (bundle → fallback wrappers; no/none bundle → live). Tests: a fixture bundle drives a bundle-backed `score`/`staleness`/`divergence`; an unrecognised-schema and a wrong-release bundle fall back to live; no `--bundle` is unchanged. |
| 2d | low | sonnet | Update `ARCHITECTURE.md` / `README.md` / `AGENTS.md` for `--bundle`, the bundle-backed sources and the fallback model; update the phase status here, in the master plan and in the plan index. |

## Testing requirements

- Offline. Build a small `Bundle` in-test (or a recorded fixture) and
  write/load it through `bundle.py`; fake HTTP for the live fallback so a
  miss is observable without the network.
- A bundle-backed run must produce results **identical** to the live
  path for the covered packages (assert on a shared fixture).
- `pre-commit run --all-files` green; tests stay offline.

## Success criteria

- With a recognised, release-matched bundle, `staleness`/`divergence`/
  `score` resolve covered packages from the bundle (no network for those)
  and fall back to the live source only for misses, with output identical
  to the fully-live run on the covered set.
- An absent, unrecognised, or wrong-release bundle leaves behaviour
  exactly as today (fully live).
- The machine's installed-package list never leaves the host (the bundle
  is read locally; only per-miss live lookups go out, as they do today).
- `--classify` and `show` are unchanged (live).

## Out of scope (later phases)

- Downloading / locating the bundle, `--cache-url`, freshness/TTL
  enforcement (phase 3).
- Signature verification and spot-verification (phase 4).
- Scheduled publishing (phase 5).

## Back brief

Before executing, back brief the operator on your understanding of this
phase.
