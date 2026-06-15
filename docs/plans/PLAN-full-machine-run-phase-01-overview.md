# Phase 1 — Tier 1: polite default overview

Part of [PLAN-full-machine-run.md](PLAN-full-machine-run.md).
Plan this phase at **high effort**: it changes user-visible
behaviour (the whole-machine commands stop reporting the
per-class breakdown by default) and the score model.

## Prompt

Explore before changing: `divergulent/http.py` (the global rate
limiter to make per-host), `divergulent/sources/debian_patches.py`
(`_series()`, `details()`, `divergence()`, `DivergenceResult`),
`divergulent/score.py` (`combine()` / weights), and
`divergulent/cli.py` (`divergence`/`score`/`show` commands and
the shared table/summary helpers). Keep the no-cry-wolf
principle central: the default overview must label its number
honestly as a patch *count*, not a classified verdict.

## Objective

Make the default whole-machine run cheap and polite: per-host
rate limiting, a one-request-per-source divergence `summary()`,
and `score`/`divergence` reworked to use it. After this phase a
full `divergulent score` is ~1 request per source per axis on a
cold cache, with no `--limit`.

## Design decisions

- **Per-host throttling.** `HttpClient` tracks the last-request
  time per URL host and applies `min_interval` per host, so
  Repology and sources.debian.org each get ≤1 req/s but do not
  serialise against each other.
- **Cheap summary.** Add `DivergenceSummary(source_package,
  version, source_format, total, state)` and `summary(source,
  version)` to `DebianPatchesSource`, using only the one-request
  patches API (format + count + names) — no `_raw_base`, no
  patch-body fetches. `details()` is unchanged (Tier 3 / `show`).
- **Default commands use the summary.** `divergence`/`score`
  report *total carried patches* (+ state); the Debian-only /
  forwarded / unknown columns are dropped from the default view
  (they return under `--classify` in phase 2 and in `show`).
- **Score model.** `DivergenceResult` is replaced by
  `DivergenceSummary`; `score.combine(staleness, summary)`
  weights `behind` and `summary.total` (provisional weights).
- **Honesty.** Output and docs state plainly that the default
  whole-machine view is a patch count, and that `show` (or
  `--classify`) gives the classified breakdown.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 1a | medium | sonnet | none | Make `HttpClient` rate-limit per host: replace the single `_last_request` with a per-host map keyed by `urllib.parse.urlparse(url).netloc`, applying `min_interval` per host. Update `divergulent/tests/test_http.py`: two requests to the same host are spaced by `min_interval`; two requests to different hosts do not wait on each other. |
| 1b | medium | sonnet | none | Add `@dataclass(frozen=True) DivergenceSummary(source_package, version, source_format, total, state)` and `summary(source_package, version) -> DivergenceSummary` to `divergulent/sources/debian_patches.py`, using the shared `_series()` and deriving `total` (len of patch names) and `state` (PATCHED/CLEAN/NATIVE/UNKNOWN) from format + names only — no `_raw_base`/patch-body fetches. Leave `details()` unchanged. Tests: `summary()` makes exactly one HTTP call and returns correct total/state for quilt-with-patches / native / clean / unresolved. |
| 1c | high | sonnet | none | Replace `DivergenceResult` with `DivergenceSummary`. Update `divergulent/score.py` so `combine(staleness, summary)` weights `behind` + `summary.total` (drop the Debian-only/unknown weighting; keep weights as named constants). Update `cli.py`: `divergence` and `score` call `summary()` and render total carried patches (+ state), dropping the per-class columns; `show` keeps using `details()` and tallies its own per-patch counts for its display. Update all affected tests (`test_score.py`, `test_cli_divergence.py`, `test_cli_score.py`, `test_cli_show.py`, `test_debian_patches.py`). Ensure output wording marks the default view as a patch count. |

1a and 1b are independent; 1c depends on both. Commit per step.

## Testing requirements

- Suite stays **offline**; all HTTP mocked.
- Per-host throttle: same-host spaced, cross-host not blocked.
- `summary()` proven to make a single request and derive the
  right total/state without fetching patch bodies.
- Reworked `score`/`divergence`/`show` keep full coverage;
  `show`'s per-patch detail is unchanged.

## Success criteria for this phase

- A full default `divergulent score` is ~1 request per source
  per axis (cold), per-host ≤1 req/s, no `--limit`.
- Whole-machine output reports a patch *count*, labelled as
  such; `show` still gives per-patch detail.
- `tox -epy3` and `tox -eflake8` pass.

## Open questions for this phase

- **Score weights** for `behind` + `total` — provisional;
  defer tuning.
- Whether to keep a `DivergenceResult`-shaped type for `show`'s
  internal tally or compute counts inline (implementer's call).

## Out of scope (later phases)

- `--classify` and the apt source provider (phase 2).
- The CI job and docs sweep (phase 3).

## Back brief

Before executing, back brief the operator on your understanding
of this phase and how the intended work aligns with it.
