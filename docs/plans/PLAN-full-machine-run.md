# Make a full-machine run viable (tiered patch data)

## Prompt

Before responding to questions or discussion points in this
document, explore the divergulent codebase thoroughly. Read the
relevant source files and ground answers in what the code does
today rather than speculating: `divergulent/http.py` (the polite
HTTP client and its global rate limiter), `divergulent/cache.py`,
`divergulent/sources/debian_patches.py` (`details()` /
`divergence()` and the patches-API/`/data` fetch path),
`divergulent/sources/repology.py`, `divergulent/score.py`, and
`divergulent/cli.py`.

Where a question touches external concepts, research as needed
rather than guessing. Key references for this plan:

- **Debian source packages** — the `.dsc` plus, for
  `3.0 (quilt)`, a `.debian.tar.*` bundling the whole `debian/`
  dir (series + all patches + `debian/source/format`); served
  by the Debian mirror network (built for bulk), unlike the
  single sources.debian.org web service.
- **apt source toolchain** — `apt-get source`,
  `apt --print-uris source`, `deb-src` entries; downloads from
  the user's configured mirror.
- **sources.debian.org patches API** — `/patches/api/<pkg>/<ver>/`
  returns `format` + `count` + patch names in one request.
- **DEP-3** and the existing `divergulent/dep3.py` classifier.

Flag uncertainty explicitly. Where a tier relies on a heuristic
or a precondition (e.g. `deb-src` enabled), say so rather than
presenting its output as ground truth.

All planning documents go into `docs/plans/`. Detailed per-phase
planning lives in separate files named
`PLAN-full-machine-run-phase-NN-descriptive.md`, tracked in the
Execution table below.

One commit per logical change, at minimum one per phase. Each
commit should build, pass tests, and have a clear message.

## Situation

The first swing (phases 1–5 of [PLAN-initial.md](PLAN-initial.md))
is complete: `inventory`, `staleness`, `divergence`, `score`,
`show` all work. But a whole-machine `score`/`divergence` does
not scale politely. Staleness is ~1 Repology request per source
package; divergence fetches **every patch body** (1 series + 1
base + N patches per source) from sources.debian.org at the
mandated ≥1 req/s. On a real machine that is thousands of
requests against a single web service — tens of minutes to
hours. A naive "remove `--limit`" would itself be impolite.

The guiding principle: **if a full run cannot be done politely,
the tool is not usable** — so scoping CI with `--limit` would
hide the defect, not fix it.

What the ecosystem already gives us:

- The patches API returns a patch **count** in one request — a
  cheap divergence overview.
- A **source package** bundles every patch in one small
  `.debian.tar.*`, served by the **mirror network** (designed
  for bulk) — full classification without hammering a web
  service.
- `show` already does the per-package deep dive via the patches
  API.

## Mission and problem statement

Make a full-machine run polite and usable, via a **tiered**
design where cost and detail rise together, each tier using the
appropriate data source:

- **Tier 1 — default overview** (`score`/`divergence`): patches
  -API **count**, one request per source. Fast, polite, the
  everyday whole-machine run.
- **Tier 2 — `--classify` mode**: full DEP-3 classification
  across the machine by fetching **source packages via the local
  apt/mirror** and extracting `debian/patches`. Opt-in, heavier,
  on bulk-built infrastructure; recovers the Debian-only /
  forwarded / unknown breakdown at scale.
- **Tier 3 — `show <package>`**: per-package per-patch detail +
  bug references via the patches API. Already implemented
  (phase 5); unchanged here.

Then a CI job proves the default full run is polite by scoring
the Debian 13 runner itself.

## Open questions

Cross-cutting decisions for this plan (resolve, or proceed on
the stated default):

- **`--classify` flag name** — `--classify` (default) vs
  `--deep` / `--patches`.
- **Score weights** — Tier 1 weights `behind` + total carried
  patches; Tier 2 reintroduces Debian-only weighting. All
  provisional; tune once the CI artifact yields real numbers.
- **CI trigger** — `pull_request` + `workflow_dispatch` (the
  persisted cache keeps PR runs cheap) or `workflow_dispatch`
  only at first? Default: both.
- **sources.debian.org per-host interval** — keep ≥1s (it has no
  documented limit but be conservative) or allow faster? Default
  ≥1s.

## Execution

Delivered in three phases; each is roughly one PR. Phases 1–2
are the tool changes (PR A then PR B); phase 3 is the CI job and
docs (PR C, depends on phase 1).

| Phase | Plan | Status |
|-------|------|--------|
| 1. Tier 1 — polite default overview | PLAN-full-machine-run-phase-01-overview.md | Not started |
| 2. Tier 2 — opt-in `--classify` via apt source packages | PLAN-full-machine-run-phase-02-classify.md | Not started |
| 3. CI full-run sample output + docs | PLAN-full-machine-run-phase-03-ci.md | Not started |

## Agent guidance

This plan follows the execution model, effort/model rubric, and
review checklist in [PLAN-TEMPLATE.md](../../PLAN-TEMPLATE.md) —
refer to it for the full detail rather than duplicating it here.
In summary:

- **Execution model.** Implementation is done by sub-agents per
  the step tables in the phase plans; the management session
  plans, reviews the actual files (not just the summary), and
  commits. Use `isolation: "worktree"` only for risky/parallel
  changes.
- **Planning effort.** This master plan and the
  scoring/classification phases (1 and 2) warrant high-effort
  planning — they change user-visible behaviour and the score
  model, and Tier 2 adds a new data source with a `deb-src`
  precondition. The CI phase (3) is medium.
- **Step-level guidance.** Each phase plan carries a
  `Step | Effort | Model | Isolation | Brief` table; briefs
  front-load the research so a lighter model can succeed. Skew
  to the more capable model when in doubt.
- **Management session review checklist.** After each step:
  files actually changed; no unrelated files touched;
  `pre-commit run --all-files` / `tox` green; tests stay offline
  (HTTP and the apt/download boundary mocked); changes match the
  brief's intent; commit message follows project conventions.

## Administration and logistics

### Success criteria

We will know this plan has been implemented when:

* Default `divergulent score` over a real Debian 13 machine runs
  in reasonable time at a polite request volume (per-host
  ≤1 req/s; ~1 request per source per axis cold; near zero
  warm), with **no** `--limit`.
* `--classify` reproduces the Debian-only / forwarded / unknown
  breakdown across the machine via the mirror, and degrades with
  a clear message (not wrong numbers) when `deb-src` is absent.
* `show` is unchanged and still gives the authoritative
  per-package per-patch detail and bug references.
* CI publishes a downloadable rendered full-score artifact for
  the Debian 13 runner, backed by a persisted cache.
* External access stays polite (per-host rate limiting, caching,
  graceful degradation) and the tool does not cry wolf
  (whole-machine output honestly labels a patch *count* vs the
  classified breakdown).
* `tox -epy3` and `tox -eflake8` pass; the CI workflow passes
  `actionlint` and its script passes `shellcheck`.
* `README.md`, `ARCHITECTURE.md`, and `AGENTS.md` are updated for
  the tiers, per-host throttling, and the sample-output job.

### Future work

- **Repology bulk `/api/v1/projects/`** for staleness (~N/200
  requests) — a further politeness win; deferred, revisit if
  per-source staleness becomes the bottleneck.
- **Cheap Debian-only estimate from patch filenames** (deb-*/
  debian-) in the Tier 1 overview, at no extra request cost —
  deferred to avoid an estimate-vs-authoritative discrepancy.
- A server/aggregator and other sources (UDD, DEHS, Wikidata),
  per `PLAN-initial.md`.

### Bugs fixed during this work

None yet. Record any encountered here; scan the issue tracker
(once one exists) for directly related issues.

### Documentation index maintenance

This master plan is registered in
[docs/plans/index.md](index.md). As phases complete, update
their status in the Execution table above and in `index.md`.
Phase files are linked from the Execution table; they do not
need a separate navigation entry.

### Back brief

Before executing any step, back brief the operator on your
understanding of the plan and how the intended work aligns with
it.
