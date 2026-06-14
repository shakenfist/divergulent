# Phase 4 — Scoring & whole-machine report

Part of [PLAN-initial.md](PLAN-initial.md). Plan this phase at
**high effort**: the scoring model is a judgment call, and the
honest framing of "behind upstream" (which is normal on a
stable release) versus "carries distro-only patches" is what
keeps the headline answer trustworthy rather than alarmist.
This phase completes the first swing.

## Prompt

Phases 1–3 are merged; explore the existing code before adding
to it: `divergulent/inventory.py`, `divergulent/sources/repology.py`
(`StalenessResult` / `StalenessState`), `divergulent/sources/debian_patches.py`
(`DivergenceResult` / `DivergenceState`), `divergulent/http.py`,
`divergulent/cache.py`, and `divergulent/cli.py` (the dedup,
table, summary and per-command patterns to mirror).

This phase adds no new external source — it combines the two
axes we already compute. The hard part is presenting them
honestly. Keep the no-cry-wolf principle central: staleness
against pure upstream is *expected* on a stable Debian release
and must not be presented as alarming on its own; carried,
undocumented distro-only code is the supply-chain-relevant
signal.

## Objective

Add a `divergulent score` command that, for each installed
source package, combines its staleness (Repology) and
divergence (sources.debian.org) into one transparent
per-package signal, ranks packages by overall drift, and
prints a whole-machine summary answering the founding question:
*how divergent from pure upstream is this machine?*

End state: `divergulent score` produces a ranked report plus a
whole-machine summary on a real Debian box; the suite passes
offline with both axes mocked; the command reuses the existing
caches and is polite and `--limit`-able.

## Design decisions

- **Decomposed, not a magic number.** The output always shows
  the two axes separately (staleness state + carried-patch
  counts); the composite score exists only to *rank*, never to
  replace the breakdown. A user must always be able to see
  *why* a package ranked where it did.
- **Scoring model (transparent weighted sum).** Per package:
  ```
  score = (behind        ? W_BEHIND          : 0)
        + debian_only_patches * W_DEBIAN_ONLY
        + unknown_patches     * W_UNKNOWN_PATCH
  ```
  with documented default weights **W_DEBIAN_ONLY = 3**,
  **W_UNKNOWN_PATCH = 1**, **W_BEHIND = 2**, and forwarded
  patches weighted **0** (benign drift headed upstream).
  Rationale: undocumented distro-only code is the strongest
  signal; an unclassified carried patch is weaker (we know
  code is carried, not its nature); being behind upstream is a
  mild signal because it is normal on stable. Weights are
  module constants so they can be revisited.
- **Honest unknowns.** A package whose *both* axes are UNKNOWN
  scores 0 but is **not** "clean" — it is unassessed. The
  summary reports coverage explicitly (how many packages we
  could not assess on each axis). Default view hides score-0
  packages; `--all` shows them with their states visible, so
  "unassessed" is never silently rendered as "fine".
- **One shared HTTP client.** `score` constructs a single
  `HttpClient` shared by both sources, so the ≥1 request/second
  rate limit is global across Repology *and* sources.debian.org
  — maximally polite. It reuses the on-disk caches, so running
  `staleness` and/or `divergence` first warms them and makes
  `score` fast.
- **Request cost.** `score` is the heaviest command (both axes
  for every source). `--limit N` caps the source packages
  processed; progress goes to stderr; the docs steer users to
  `--limit` for a first look and note that a full run is slow
  on a cold cache.
- **Ranking & default filter.** Rank by score descending, then
  by debian-only count, then name. Default shows packages with
  score > 0; `--all` includes everything; `--json` mirrors the
  selection.
- **Module.** Add `divergulent/score.py` with the weights, a
  `@dataclass(frozen=True) PackageDrift(source_package,
  version, staleness: StalenessResult, divergence:
  DivergenceResult, score: int)`, and a pure
  `combine(staleness, divergence) -> PackageDrift`. The CLI
  orchestrates fetching; `score.py` stays I/O-free and
  unit-testable.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 4a | medium | sonnet | none | Implement `divergulent/score.py`: the weight constants (W_DEBIAN_ONLY=3, W_UNKNOWN_PATCH=1, W_BEHIND=2), a `@dataclass(frozen=True) PackageDrift(source_package, version, staleness, divergence, score)`, and a pure `combine(staleness: StalenessResult, divergence: DivergenceResult) -> PackageDrift` computing the score per Design decisions (behind contributes W_BEHIND; debian_only and unknown patch counts contribute their weights; forwarded contributes 0; UNKNOWN staleness contributes 0). No I/O. Tests: a clean+current package scores 0; behind-only; debian-only-patches-only; both; that forwarded patches add nothing; that a both-UNKNOWN package scores 0; and the ranking order of a small set. |
| 4b | medium | sonnet | none | Add the `score` CLI command. In `cli.py` add a `score` sub-parser (`--json`, `--all`, `--limit`). Build the inventory, dedup by source (reuse `_dedup_sources`, honour `--limit`), construct ONE `HttpClient(Cache(default_cache_dir()))` shared by a `RepologySource` and a `DebianPatchesSource`, and for each source call both `staleness()` and `divergence()` then `score.combine()`. Print a whole-machine summary to stderr (assessed count; how many behind, how many carry Debian-only patches, how many both; total Debian-only patches; and unassessed counts per axis). Render selected packages (default score>0, ranked by score then debian-only then name) as a table (SOURCE, VERSION, STALENESS, NEWEST, DEB-ONLY, PATCHES, SCORE); `--all` includes score-0; `--json` mirrors with full breakdown. Tests: mock both sources (no network); assert dedup, `--limit`, ranking, default-vs-`--all`, summary counts, and both output modes. |
| 4c | low | sonnet | none | Documentation and roadmap. Update `ARCHITECTURE.md` (add `score.py`, the combined data flow, and that `score` shares one HTTP client across both axes), `AGENTS.md` (the score weights and that staleness-on-stable is expected, not alarming), and `README.md` (a `divergulent score` usage example and the whole-machine-summary framing, noting it is the heaviest command and `--limit`-able). Mark phase 4 complete and the first swing done in the master plan and index. |

Steps run in order: 4b depends on 4a, 4c on both. Commit per
step per the master plan.

## Testing requirements

- The whole suite stays **offline**: both sources mocked. No
  test hits the network.
- `combine()` is unit-tested for every axis combination and
  the ranking order.
- The CLI dedups, honours `--limit`, ranks correctly, reports
  honest coverage (unassessed counts), and never renders an
  unassessed package as clean.

## Success criteria for this phase

- `divergulent score` returns a correct ranked report and a
  whole-machine summary on a real Debian machine (operator
  smoke check, on a small `--limit`).
- The score is decomposed and transparent; staleness and
  divergence are always visible; unassessed packages are
  reported as such, never as clean.
- One shared, polite HTTP client across both axes; caches
  reused.
- `tox -epy3` and `tox -eflake8` pass; docs updated; the first
  swing (phases 1–4) is complete.

## Open questions for this phase

- **Default weights.** W_DEBIAN_ONLY=3 / W_UNKNOWN_PATCH=1 /
  W_BEHIND=2 are a starting point; confirm or tune. Should
  weights be user-overridable (flags/config) now or later?
- **Should `score` subsume `staleness`/`divergence`?** Keeping
  all three is proposed (they are cheaper, single-axis views);
  confirm we are not collapsing them.
- **Summary destination.** Summary to stderr (so stdout stays
  clean for `--json`) — confirm.

## Out of scope (later phases / future work)

- The per-package detail view (Phase 5) and BTS cross-
  referencing.
- The "patch hygiene & justification" work (separate future
  master plan): deterministic Lintian-via-UDD compliance
  signals plus optional, opt-in, clearly-labelled LLM
  categorization.
- A server/aggregator and other sources (UDD, DEHS, Wikidata).

## Back brief

Before executing, back brief the operator on your
understanding of this phase and how the intended work aligns
with it.
