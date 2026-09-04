# Title for the plan

## Prompt

Before responding to questions or discussion points in this
document, explore the divergulent codebase thoroughly. Read
relevant source files, understand existing patterns (the
client that reads local dpkg state, the data-source adapters
that talk to external services, the caching layer, the
two-axis scoring model for staleness and divergence, and the
optional server/aggregator), and ground your answers in what
the code actually does today. Do not speculate about the
codebase when you could read it instead.

Where a question touches on external concepts, research as
needed to give a confident answer rather than guessing.
Divergulent sits on top of a lot of Debian and ecosystem
machinery, and getting the details right matters because the
whole point of the tool is to be trustworthy about supply
chain risk. Key external references include:

- **dpkg state** — `dpkg-query -W`, `/var/lib/dpkg/status`,
  binary-to-source package mapping, version epochs/revisions.
- **Debian source format `3.0 (quilt)`** — Debian's delta
  from upstream lives as an explicit patch series in
  `debian/patches/series`. Counting and classifying these is
  the divergence signal.
- **DEP-3 patch headers** — `Origin:`, `Forwarded:`, `Bug:`.
  These distinguish a benign forwarded-upstream patch from a
  pure distro-only divergence (the motivating pngtools case).
- **Repology** (`repology.org/api`) — cross-distro version
  aggregation with a per-project `status` (`newest`,
  `outdated`, ...). The fastest path to the staleness axis.
- **sources.debian.org** — HTTP/JSON access to every source
  package's `debian/patches/` without downloading tarballs.
- **UDD** (`udd.debian.org`) — the Ultimate Debian Database,
  a public PostgreSQL warehouse of archive metadata.
- **uscan / DEHS / `debian/watch`** — Debian's own upstream
  version checker.
- **Wikidata** — an author-editable, Repology-ingested
  version source (P348); see also the upstream-signed-feed
  idea that motivates the project.

Flag any uncertainty explicitly rather than guessing. When a
data source is heuristic (Repology name-matching, missing
`debian/watch` files, absent DEP-3 headers), say so in the
plan rather than presenting it as ground truth.

Consult `ARCHITECTURE.md` for the system architecture
overview (client, data-source adapters, cache, scoring,
optional server). Consult `CLAUDE.md` / `AGENTS.md` for build
commands and project conventions. Consult `GOALS.md` (if
present) for current development priorities.

<!-- shared-block: plan-file-conventions v1 -->
Plan file conventions (shared block; do not edit -- the canonical
copy lives in shakenfist/development at
`templates/shared-blocks/plan-file-conventions.md`):

- All planning documents live in `docs/plans/`.
- Detailed planning gets one plan file per phase. Phase files are
  named for their master plan, sit in the same directory as it,
  and append `-phase-NN-descriptive` before the `.md` extension.
- The master plan tracks its phases in a table under its Execution
  section:

  | Phase | Plan | Status |
  |-------|------|--------|
  | 1. Schema migration | PLAN-thing-phase-01-schema.md | Not started |
  | 2. Public API | PLAN-thing-phase-02-api.md | Not started |

- One commit per logical change, and at minimum one commit per
  phase. Unrelated changes are not batched into a single commit.
  Each commit is self-contained: it builds, passes tests, and has
  a message explaining what changed and why.
<!-- shared-block-end -->

<!-- shared-block: plan-status-vocabulary v1 -->
Plan status vocabulary (shared block; do not edit -- the canonical
copy lives in shakenfist/development at
`templates/shared-blocks/plan-status-vocabulary.md`):

A status cell -- in the master plan's own Execution phase table, and
in the row `docs/plans/index.md` carries for the plan -- holds
exactly one of these terms and nothing else:

- `Proposed` -- written down as a concept, not yet scheduled.
- `Not started` -- scheduled, but no work has begun.
- `In progress` -- work has begun and has not finished.
- `Blocked` -- cannot proceed until something outside the plan
  changes. Say what, in the plan.
- `Complete` -- the work is done.
- `Abandoned` -- deliberately dropped without being done.
- `Superseded` -- replaced by another plan, which the plan names.

The term is the whole cell. No dates, no phase arithmetic, no
parenthetical qualifiers, no summary of what happened: a status is
read to decide whether a plan still wants attention, and prose in
that column has repeatedly grown until it could no longer be read
either by a person scanning the table or by tooling. Detail belongs
in the plan file, and a one-line summary belongs in the index's own
Intent column.

Matching is case-insensitive, so `In Progress` is accepted, but the
spelling above is the one to write.
<!-- shared-block-end -->

## Situation

...

## Mission and problem statement

...

## Open questions

Divergulent is a young project; a few cross-cutting
decisions will shape most plans and should be resolved (or at
least explicitly assumed) here:

- **Implementation language.** Python (decided — matches the
  dpkg/API-glue nature and house conventions).
- **Client/server split.** Thin client querying public APIs
  directly, a precomputed server/cache, or both? Most of the
  expensive mapping is already done by Repology / UDD /
  sources.debian.org, so the server may be a thin aggregator
  rather than a VCS crawler.
- **Privacy.** The tool reads the user's installed-package
  inventory. Any design that sends that list off-box needs a
  stated privacy posture (local-only mode, hashing, opt-in).
- **Trust model.** Which sources are treated as authoritative
  for "true upstream latest", and how do we avoid presenting
  editable/heuristic data (e.g. Wikidata, Repology matching)
  as fact?

...

## Execution

...

<!-- shared-block: plan-push-audit-phase v2 -->
Push audit phase (shared block; do not edit -- the canonical
copy lives in shakenfist/development at
`templates/shared-blocks/plan-push-audit-phase.md`):

- Every master plan ends with a phase that runs the repository's
  `PUSH-AUDIT.md` over the whole plan's work. It is the last row of
  the Execution table and it is not optional. The rule binds every
  plan that carries the phase, which is decidable from the plan file
  alone: a plan that is already `Complete` and does not carry the
  phase is not reopened to acquire one, and a plan that has the
  phase runs it even if it reaches `Complete` before the phase does.
- That phase audits the accumulated diff of every phase in the plan
  against the default branch, not the diff of the last phase alone.
  Auditing one phase at a time would miss what the phases did to
  each other -- the duplicated helper that only exists once phases
  three and six have both landed, the doc page that phase two made
  wrong and phase five never revisited.
- Once the plan's phases have merged, a diff against the default
  branch is empty and would read as a clean audit. The range is not
  reliably derivable after the fact either: unrelated work lands on
  the default branch between phases, so anything anchored on "since
  the plan file appeared" is far too wide. It has to be recorded. As
  each phase lands, what put it on the default branch goes into the
  plan: the merge commit of its pull request, whose diff against its
  first parent is the whole of what landed, or -- where the phase
  landed directly -- every commit of the phase, or its `first..last`
  range. A single commit is only ever enough when it is a merge
  commit.
- Where the Execution phases are a table, that record is a `Merged`
  column, added last so that a row which omits it still reaches
  `Status`; where they are prose sections it is a `Merged:` line in
  the phase's own section. The `Status` column keeps its single
  vocabulary term and nothing else (see `plan-status-vocabulary`).
  A phase that landed in another repository records `<repo> <sha>
  (#pr)` and is audited against that repository's default branch, as
  part of the pull request that lands it; the plan's own push-audit
  phase cites that audit rather than re-running it.
- Phases that landed before the plan started recording them are
  reconstructed rather than left blank. Recover what you can from
  `gh pr list --state merged` and `git rev-list --first-parent`, and
  say in the plan that the range was reconstructed. Do not trust a
  path-filtered `git log` on its own: it lists the commits that
  touched a path without saying which arrived directly and which
  arrived inside a pull request, and recording a commit that came in
  under a merge audits one commit of that pull request rather than
  the pull request. A reconstructed record may be a summary table in
  the audit phase's own section rather than a column or a line in
  the Execution table, which keeps retrospective archaeology out of
  a table that tracks live status. Where a phase accreted over
  months of unrelated commits and no range is recoverable, say that
  instead and name the paths the audit read -- an audit that says
  what it could not scope is a result; one that silently audits
  nothing is not.
- Findings land as their own pull request against the default
  branch, and the plan is not complete until they are resolved or
  explicitly declined in writing. A finding that is declined says
  why, in the plan, where the next reader will find it.
- Where the audit finds nothing, record that in the plan in one
  sentence. It is a real result, and a run of them is the evidence
  for making the phase conditional rather than mandatory.
- A repository with no `PUSH-AUDIT.md` still carries the phase, and
  the phase says that the runbook does not exist yet and what was
  done instead. Silently omitting it is what let the audit go
  untriggered for as long as it did.
<!-- shared-block-end -->

!!! note "In this project"

    `PUSH-AUDIT.md` exists at the repository root and is
    referenced from `AGENTS.md`, so the final phase runs it
    rather than explaining its absence. Every diff command in
    it is written against `develop...HEAD`, this project's
    default branch: a stale local `develop` silently widens the
    audit to unrelated history, so fetch before starting, or
    read it as `origin/develop...HEAD`.

    The two shared blocks in this file disagree about the
    Execution table. The worked example in
    `plan-file-conventions` predates the `Merged` column that
    the block above requires, so copying that example produces
    a table this block declares non-conformant. New plans here
    write `| Phase | Plan | Status | Merged |`. Reconciling the
    two is a change to shakenfist/development, not to this
    file — neither block may be edited in place.

## Agent guidance

### Execution model

<!-- shared-block: subagent-execution-model v1 -->
Sub-agent execution model (shared block; do not edit -- the
canonical copy lives in shakenfist/development at
`templates/shared-blocks/subagent-execution-model.md`):

All implementation work is done by sub-agents, never in the
management session. The management session is reserved for
planning, review, and decision-making. This keeps the management
context lean and avoids drowning it in implementation diffs.

The workflow is:

1. **Plan** at high effort in the management session.
2. **Spawn a sub-agent** for each implementation step with the
   brief from the plan, at the recommended effort level and model.
3. **Review** the sub-agent's output in the management session.
   Check the actual files -- the sub-agent's summary describes
   what it intended, not necessarily what it did.
4. **Fix or retry** if the output is wrong. Diagnose whether the
   brief was insufficient (improve it) or the model was too light
   (upgrade it), then re-run.
5. **Commit** once the management session is satisfied.

This applies to all steps, including high-effort ones. If a
sub-agent cannot succeed even with a detailed brief and the right
model, that is a signal the brief needs improving, not that the
management session should do the implementation itself.

Use `isolation: "worktree"` for sub-agents when the change is
risky or experimental; the worktree is discarded if the output is
unsatisfactory. For safe, well-understood changes, sub-agents can
work directly in the main tree.
<!-- shared-block-end -->

### Planning effort

<!-- shared-block: plan-planning-effort v1 -->
Planning effort (shared block; do not edit -- the canonical copy
lives in shakenfist/development at
`templates/shared-blocks/plan-planning-effort.md`):

The master plan itself is always created at **high effort** -- it
requires broad codebase understanding, cross-referencing several
source files, and judgment calls about scope and sequencing.

Each phase plan states the recommended effort level for planning
that phase. Phases that turn on design decisions, cross-component
coordination, protocol changes, or subtle correctness questions
should be planned at high effort. Phases that are mechanical, or
that follow a pattern already established elsewhere in the
codebase, can be planned at medium effort.
<!-- shared-block-end -->

!!! note "In this project"

    Phases involving the scoring model, cross-source data
    reconciliation, cache invalidation, or subtle correctness
    questions (version comparison, patch classification,
    false-positive avoidance) should be planned at high effort.
    Phases that mirror an already-established pattern -- adding
    another data-source adapter alongside an existing one, for
    example -- can be planned at medium effort.

### Step-level guidance

<!-- shared-block: subagent-step-guidance v1 -->
Sub-agent step guidance (shared block; do not edit -- the
canonical copy lives in shakenfist/development at
`templates/shared-blocks/subagent-step-guidance.md`):

Each phase plan includes a table like this:

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 1a | medium | sonnet | none | One-sentence summary of what to do and which files to touch |
| 1b | high | opus | worktree | Why this needs high effort: requires understanding X to do Y |

**Effort levels**, from cheapest to most thorough:

- **low** -- Purely mechanical changes: rename, reformat, add a
  log line, regenerate generated code. The brief is a complete
  instruction.
- **medium** -- The plan provides enough context to follow a clear
  brief. The sub-agent may read a few files, but the approach is
  already decided.
- **high** -- Requires reading several files, making judgment
  calls, or understanding non-obvious invariants. The sub-agent
  needs to think about edge cases.
- **xhigh** -- The setting for hard coding and agentic steps:
  long-horizon changes, or steps where the sub-agent must both
  research and implement.
- **max** -- Correctness matters more than cost. Expect
  diminishing returns and occasional overthinking; reserve it for
  steps where a wrong answer would be expensive to detect.

**Brief for sub-agent:** this is the key field. Write it as if
briefing a colleague who has never seen the codebase. Include what
to change, which files to touch, what patterns to follow, and any
non-obvious constraints.

A good brief front-loads the research the planner already did, so
the implementing agent does not repeat it. Instead of "add storage
functions for the new object", name the functions to add, the file
they belong in, the existing equivalent to mirror (with line
numbers), and any registration the change also needs.

The better the brief, the lower the effort level needed and the
lighter the model that can succeed.
<!-- shared-block-end -->

!!! note "In this project"

    High effort typically means the scoring model,
    version-comparison logic, or patch classification, or
    research into an external format (Debian source format,
    DEP-3 semantics, a source's API quirks); medium effort
    typically means a data-source adapter parallel to an
    existing one, or a cache backend; low effort typically
    means a rename, a log line, or wiring a CLI flag.

    A worked brief for this codebase: instead of "add a UDD
    adapter", write "add a UDD adapter under
    `divergulent/sources/udd.py`, mirroring the Repology
    adapter's `fetch()` / `normalise()` shape in
    `divergulent/sources/repology.py`. UDD is a public
    PostgreSQL endpoint; use the read-only connection string
    documented at udd.debian.org and the `sources` /
    `upstream_metadata` tables. Cache results through the
    shared cache layer in `divergulent/cache.py` keyed by
    source package + suite, and respect the polite-usage rules
    in `AGENTS.md` (User-Agent, rate limit, on-disk TTL)."

### Model choice

<!-- shared-block: subagent-model-roster v1 -->
Sub-agent model roster (shared block; do not edit -- the canonical
copy lives in shakenfist/development at
`templates/shared-blocks/subagent-model-roster.md`):

The planner recommends which model is best suited to each step.
This is a judgment call, not a rigid rule -- the right model
depends on what the step requires, not on whether it is "planning"
or "implementation". The models available to sub-agents are:

- **fable** -- The most capable model available, for the hardest
  reasoning and the longest-horizon work: multi-step changes a
  single sub-agent must carry end to end, or steps whose
  correctness depends on holding a whole subsystem in mind at
  once. It costs materially more than opus, so reserve it for
  steps that have already defeated opus or are expected to.
- **opus** -- The default for steps needing deep reasoning,
  architectural understanding, subtle correctness judgment
  (locking, state machines, migrations), or intricate
  implementation that would be costly to debug if it were wrong.
- **sonnet** -- A good default for well-briefed implementation
  work. Faster and cheaper than opus, and effective when the plan
  front-loads the research and the brief leaves no broad judgment
  calls to make.
- **haiku** -- Suitable for purely mechanical tasks:
  search-and-replace, regenerating generated code, adding log
  lines, running commands. The brief must be a near-complete
  instruction.

Model choice interacts with effort level and brief quality. A
detailed brief compensates for a lighter model -- sonnet at medium
effort with a thorough brief often matches opus at medium effort
with a vague brief. The planner's job is to write briefs good
enough that the recommended model can succeed.

The model also determines the context window: fable, opus and
sonnet have 1M tokens, haiku has 200K. A step that must hold many
files in context at once may need one of the larger-context models
for that reason alone, even when the reasoning itself is
straightforward.

**When in doubt, skew to the more capable model.** Saving money
only matters if the outcome is still acceptable. A failed or
low-quality implementation wastes more time -- and therefore more
money -- than the heavier model would have cost. Recommend a
lighter model only when you are confident the brief is detailed
enough for it to succeed.
<!-- shared-block-end -->

### Management session review checklist

<!-- shared-block: plan-review-checklist v1 -->
Management session review checklist (shared block; do not edit --
the canonical copy lives in shakenfist/development at
`templates/shared-blocks/plan-review-checklist.md`):

After a sub-agent completes, the management session verifies:

- [ ] The files that were supposed to change actually changed --
      read them, do not trust the summary.
- [ ] No unrelated files were modified.
- [ ] The changes match the intent of the brief: not merely
      syntactically correct, but semantically right.
- [ ] The project's own pre-merge checks pass, including any
      generated code that has to be regenerated and committed
      (see the project-specific checks below).
- [ ] The commit message follows project conventions, including
      the `Co-Authored-By` line recording model, context window,
      and effort level.
<!-- shared-block-end -->

!!! note "In this project"

    The project-specific checks referred to above are:

    - [ ] The code passes `pre-commit run --all-files` (lint,
          tests, type checking).
    - [ ] Network access to external sources is mocked in
          tests, not hitting live services on every run.

## Administration and logistics

### Success criteria

We will know when this plan has been successfully implemented
because the following statements will be true:

* The code passes `pre-commit run --all-files` (lint, unit
  tests, and type checking).
* New code follows existing patterns: data-source adapters
  share a common fetch/normalise interface; all external
  access goes through the caching layer rather than hitting
  the network ad hoc.
* External services are queried politely: a descriptive
  User-Agent, respect for documented rate limits and terms
  of use, on-disk caching with a sensible TTL, and graceful
  degradation when a source is unavailable.
* The tool does not cry wolf. Heuristic or unverified signals
  (Repology name-matching misses, missing `debian/watch`,
  absent DEP-3 headers) are surfaced as uncertainty, not
  presented as confirmed divergence. Version comparison uses
  Debian version ordering semantics, not naive string compare.
* Privacy is respected: the installed-package inventory is
  not sent off-box except under an explicit, documented,
  opt-in path.
* There are unit tests for core logic (version comparison,
  patch classification, scoring) with external responses
  mocked, and ideally an end-to-end test against recorded
  fixtures.
* Lines are wrapped at 120 characters, single quotes for
  strings, double quotes for docstrings (Python house style).
* Documentation has been updated to describe any new
  features, commands, data sources, or scoring changes.
* `ARCHITECTURE.md`, `README.md`, and `AGENTS.md` have been
  updated if the change adds or modifies modules, adapters,
  the cache, the scoring model, or the server.

### Documentation index maintenance

When creating a new master plan from this template, update
`docs/plans/index.md` — add a row to the *Plan Status* table
with a link to the plan, its phase breakdown, initial status,
and a one-line description. Keep entries grouped by master
plan. Phase files are linked from the master plan's Execution
table and from `index.md`; they do not need a separate
navigation entry.

When all phases of a plan are complete, update the status
column in `docs/plans/index.md`.

<!-- shared-block: plan-closeout-sections v1 -->
Plan close-out sections (shared block; do not edit -- the
canonical copy lives in shakenfist/development at
`templates/shared-blocks/plan-closeout-sections.md`):

### Future work

We should list obvious extensions, known issues, unrelated bugs we
encountered, and anything else we should one day do but have
chosen to defer to here, so that we do not forget them.

...

### Bugs fixed during this work

This section should list any bugs we encounter during development
that we fixed. You should also scan the project's issue tracker,
where one exists, for directly related issues that we should
either resolve as part of this master plan or at least be aware of
while planning it.

...

### Back brief

Before executing any step of this plan, please back brief the
operator as to your understanding of the plan and how the work you
intend to do aligns with that plan.
<!-- shared-block-end -->
