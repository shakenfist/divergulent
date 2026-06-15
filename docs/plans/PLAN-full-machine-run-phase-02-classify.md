# Phase 2 — Tier 2: opt-in `--classify` via apt source packages

Part of [PLAN-full-machine-run.md](PLAN-full-machine-run.md).
Plan this phase at **high effort**: it adds a new data source
with an external precondition (`deb-src`) and reintroduces the
classified breakdown at whole-machine scale.

**Status: complete.** All steps (2a apt source provider, 2b
`--classify` CLI) are implemented and committed; `score
--classify` / `divergence --classify` work on real data and the
suite passes via `tox -epy3`. The provider fetches only the
`.dsc` + `.debian.tar.*` (skipping the `.orig` tarball) via
`apt-get source --print-uris`, and falls back to patch counts
with a clear message when `deb-src` is unavailable.

## Prompt

Explore before changing: `divergulent/dep3.py` (the classifier
to reuse), `divergulent/sources/debian_patches.py`
(`PatchDetail`, `details()`, the state model), and `cli.py`
(`score`/`divergence`). Research the apt source toolchain
(`apt-get source --download-only`, `apt --print-uris source`,
`deb-src` requirements) and `.dsc` / `.debian.tar.*` layout.
This tier downloads from the user's configured mirror (bulk
infrastructure), not from a single web service.

## Objective

Add an opt-in `--classify` mode to `score`/`divergence` that
recovers the Debian-only / forwarded / unknown breakdown across
the whole machine by fetching source packages via the local apt
toolchain and classifying their patches locally — degrading
clearly when `deb-src` is not available.

## Design decisions

- **New apt source provider** `divergulent/sources/apt_patches.py`:
  for a source package + version, obtain the source via the
  local apt toolchain (downloading from the configured mirror),
  extract `debian/patches/*` from the `.debian.tar.*` (stdlib
  `tarfile`/`lzma`; `.dsc` parsed via `python-debian`, already a
  dep), and classify each patch with the existing `dep3` logic.
  Returns the same per-class counts as the old expensive path,
  but via one mirror download per source.
- **Subprocess/download boundary is injectable** so tests run
  offline against fixtures (a fake that yields a prepared
  `.debian.tar.*` or extracted tree).
- **Clear degradation.** When `deb-src` is not enabled (or apt
  source is otherwise unavailable), do not silently produce
  wrong numbers — report the limitation and how to enable it,
  and fall back to the Tier 1 count.
- **`--classify` flag** on `score`/`divergence` selects this
  provider: the command then renders the Debian-only / forwarded
  / unknown breakdown and the score weights Debian-only patches
  (as the pre-tiering model did), at the cost of source-package
  downloads.
- **Reuse, don't duplicate.** Classification goes through the
  same `dep3.classify` (DEP-3 + `# DP:`/deb-* heuristics) as
  `details()`; only the *fetch* differs (mirror tarball vs
  patches API).

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 2a | high | sonnet | none | Add `divergulent/sources/apt_patches.py`: given source package + version, fetch the source via the local apt toolchain (download-only from the configured mirror), extract `debian/patches/series` and patch files from the `.debian.tar.*` (stdlib `tarfile`/`lzma`; `.dsc` via `python-debian`), classify each with `dep3.classify`, and return per-class counts aligned with the existing model. Make the apt-invocation/download boundary injectable. Detect `deb-src`/apt-source unavailability and surface it explicitly. Tests run offline against a fixture tarball/tree; cover the classified result and the deb-src-missing path. |
| 2b | medium | sonnet | none | Add `--classify` to `score`/`divergence` in `cli.py`: when set, use the apt source provider and render the Debian-only / forwarded / unknown breakdown (and score by Debian-only count); otherwise use the Tier 1 summary. On a deb-src-missing/degraded result, print a clear message (and fall back to counts). Tests: the flag selects the provider; the breakdown renders; the graceful message appears when deb-src is missing. |

2b depends on 2a. Commit per step.

## Testing requirements

- Suite stays **offline**: the apt invocation and any download
  are mocked or fixture-fed; no real apt/network.
- The classified counts match `dep3` classification of the
  fixture patches.
- The deb-src-missing path produces a clear message and a
  count fallback, not wrong numbers.

## Success criteria for this phase

- `divergulent score --classify` / `divergence --classify`
  reproduce the per-class breakdown across the machine via the
  mirror.
- Without `deb-src`, the mode degrades with a clear,
  actionable message.
- `tox -epy3` and `tox -eflake8` pass.

## Open questions for this phase

- **Flag name** — `--classify` vs `--deep` / `--patches`.
- **apt mechanism** — `apt-get source --download-only` + manual
  extract vs `apt --print-uris` + our own download; pick the
  simpler one that works without polluting the cwd (use a temp /
  cache dir).
- **Debian-only score weighting** under `--classify` —
  provisional; align with the pre-tiering weights, tune later.

## Out of scope (later phases / future work)

- The CI job and docs sweep (phase 3).
- Repology bulk staleness; UDD/DEHS/Wikidata sources.

## Back brief

Before executing, back brief the operator on your understanding
of this phase and how the intended work aligns with it.
