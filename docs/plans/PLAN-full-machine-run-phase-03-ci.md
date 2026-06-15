# Phase 3 — CI full-run sample output + docs

Part of [PLAN-full-machine-run.md](PLAN-full-machine-run.md).
Plan this phase at **medium effort**: it is CI/infra plumbing,
but it is also the proof that the Tier 1 full run is polite.
Depends on phase 1.

## Prompt

Explore before changing: `.github/workflows/unit-tests.yml` and
`.github/workflows/release.yml` (self-hosted-runner patterns;
`release.yml` already uses `actions/upload-artifact@v7`),
`.github/actionlint.yaml` (runner-label declarations), and
`tools/` (house rule: non-trivial CI logic lives in a script,
not inline in the workflow). The tool's cache honours
`DIVERGULENT_CACHE_DIR`.

## Objective

Add a CI job that runs a **full** `divergulent score` (no
`--limit`) on a self-hosted Debian 13 runner — the runner's own
packages — and uploads the rendered output as an artifact, with
a persisted cache so reruns are cheap.

## Design decisions

- **Runner is Debian 13 itself** (no container): the job runs on
  a `self-hosted, debian-13` runner and scores its own installed
  packages — a real machine, as an end user would.
- **Logic in `tools/generate-sample-output.sh`** (house rule),
  not inline in the workflow. The workflow checks out, restores
  the cache, runs the script, and uploads the artifact.
- **Full run, no `--limit`.** The point is to demonstrate the
  default full run is polite; `--limit` would defeat it. Tier 1
  (phase 1) is what makes this feasible.
- **Persisted cache.** Point `DIVERGULENT_CACHE_DIR` at a path
  saved/restored with `actions/cache`, so the first run pays the
  cost and reruns are near-free. Version-pinned content is
  immutable, so this is safe.
- **Optional `--classify` sample** if `deb-src` is configured on
  the runner — nice-to-have, not required.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 3a | medium | sonnet | none | Write `tools/generate-sample-output.sh` (shellcheck-clean, `set -euo pipefail`): args for an output path; install/run divergulent from the checkout in a venv; run a full `divergulent score --all` (no `--limit`) and `--json`, honouring `DIVERGULENT_CACHE_DIR`; write both to the output path with a header noting it is a full score of the Debian 13 runner. Optionally also run `--classify` if `deb-src` is configured. Runnable locally for testing. |
| 3b | medium | sonnet | none | Add `.github/workflows/sample-output.yml` on a `self-hosted, debian-13` runner (declare the label in `.github/actionlint.yaml`), triggered on `workflow_dispatch` and `pull_request` to `main`: checkout (fetch-depth 0 for setuptools_scm), `actions/cache` the cache dir, run `tools/generate-sample-output.sh`, and `actions/upload-artifact@v7` the output. Keep all logic in the script; validate with actionlint + shellcheck (pre-commit). |
| 3c | low | sonnet | none | Documentation: update `README.md`/`ARCHITECTURE.md`/`AGENTS.md` for the three tiers, per-host throttling, and the sample-output job (where reviewers find the artifact); register `PLAN-full-machine-run.md` in `docs/plans/index.md`; update the relevant Future-work bullets in `PLAN-initial.md`. |

Commit per step.

## Testing requirements

- `tools/generate-sample-output.sh` runs locally and produces a
  non-empty report.
- The workflow passes `actionlint`; the script passes
  `shellcheck` (both already in pre-commit).
- A `workflow_dispatch` run on the self-hosted pool produces a
  downloadable artifact.

## Success criteria for this phase

- CI publishes a downloadable rendered full-score artifact
  (text + JSON) for the Debian 13 runner, with a persisted
  cache.
- CI logic lives in `tools/`, not inline in the workflow.
- actionlint and shellcheck pass.

## Open questions for this phase

- **PR trigger** — `pull_request` + `workflow_dispatch` (cache
  keeps it cheap) or dispatch-only first? Default: both.
- **Runner label** — confirm which `self-hosted` label is a
  Debian 13 host and declare it in `.github/actionlint.yaml`.

## Out of scope (future work)

- Repology bulk staleness; UDD/DEHS/Wikidata sources.

## Back brief

Before executing, back brief the operator on your understanding
of this phase and how the intended work aligns with it.
