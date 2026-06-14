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

All planning documents should go into `docs/plans/`.

Consult `ARCHITECTURE.md` for the system architecture
overview (client, data-source adapters, cache, scoring,
optional server). Consult `CLAUDE.md` / `AGENTS.md` for build
commands and project conventions. Consult `GOALS.md` (if
present) for current development priorities.

When we get to detailed planning, I prefer a separate plan
file per detailed phase. These separate files should be named
for the master plan, in the same directory as the master
plan, and simply have `-phase-NN-descriptive` appended before
the `.md` file extension. Tracking of these sub-phases should
be done via a table like this in this master plan under the
Execution section:

```
| Phase | Plan | Status |
|-------|------|--------|
| 1. dpkg inventory | PLAN-thing-phase-01-inventory.md | Not started |
| 2. Repology adapter | PLAN-thing-phase-02-repology.md | Not started |
| ...   | ...  | ...    |
```

I prefer one commit per logical change, and at minimum one
commit per phase. Do not batch unrelated changes into a
single commit. Each commit should be self-contained: it
should build, pass tests, and have a clear commit message
explaining what changed and why.

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

## Agent guidance

### Execution model

All implementation work is done by sub-agents, never in the
management session. The management session (this
conversation) is reserved for planning, review, and
decision-making. This keeps the management context lean and
avoids drowning it in implementation diffs.

The workflow is:

1. **Plan** at high effort in the management session.
2. **Spawn a sub-agent** for each implementation step with
   the brief from the plan, at the recommended effort level
   and model.
3. **Review** the sub-agent's output in the management
   session. Check the actual files — the sub-agent's summary
   describes what it intended, not necessarily what it did.
4. **Fix or retry** if the output is wrong. Diagnose whether
   the brief was insufficient (improve it) or the model was
   too light (upgrade it), then re-run.
5. **Commit** once the management session is satisfied with
   the result.

This applies to all steps, including high-effort ones. If a
sub-agent can't succeed even with a detailed brief and the
right model, that's a signal the brief needs improving, not
that the management session should do the implementation
itself.

Use `isolation: "worktree"` for sub-agents when the change is
risky or experimental. The worktree is discarded if the
output is unsatisfactory. For safe, well-understood changes,
sub-agents can work directly in the main tree.

### Planning effort

The master plan itself should always be created at **high
effort** — it requires broad codebase understanding,
cross-referencing multiple source files, and making judgment
calls about scope and sequencing.

Each phase plan should specify the recommended effort level
for planning that phase. Phases involving the scoring model,
cross-source data reconciliation, cache invalidation, or
subtle correctness questions (version comparison, patch
classification, false-positive avoidance) should be planned
at high effort. Phases that are mechanical or follow
well-established patterns (adding another data-source adapter
that mirrors an existing one, for example) can be planned at
medium effort.

### Step-level guidance

Each phase plan should include a table like this:

```
| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 1a   | medium | sonnet | none     | One-sentence summary of what to do and which files to touch |
| 1b   | high   | opus   | worktree | Why this needs high effort: requires understanding X to do Y |
```

**Effort levels:**
- **high** — Requires reading multiple files, making judgment
  calls, understanding non-obvious invariants, or researching
  external references (Debian source format, DEP-3 semantics,
  a source's API quirks). The sub-agent needs to think
  carefully about edge cases. Typical examples: the scoring
  model, version-comparison logic, patch classification.
- **medium** — The plan provides enough context that the
  sub-agent can follow a clear brief. May need to read a few
  files but the approach is well-defined. Typical examples:
  adding a data-source adapter parallel to an existing one,
  adding a cache backend.
- **low** — Purely mechanical changes (rename, reformat, add
  a log line, add a CLI flag). The brief is a complete
  instruction.

**Model choice:** The planner should recommend which model is
best suited for each step. This is a judgment call, not a
rigid rule — the right model depends on what the step
requires, not on whether it's "planning" or "implementation".

- **opus** — Best for steps that require deep reasoning,
  cross-source reconciliation, subtle correctness judgment
  (version ordering, patch classification, cache coherence),
  or careful research of an external data format. Also
  appropriate for intricate implementation where getting it
  wrong would be costly to debug or would erode trust in the
  tool's output.
- **sonnet** — Good default for well-briefed implementation
  work. Faster and cheaper than opus. Works well when the
  plan front-loads the research and the brief is detailed
  enough that the agent doesn't need to make broad judgment
  calls.
- **haiku** — Suitable for purely mechanical tasks:
  search-and-replace, adding log lines, wiring a CLI flag,
  running commands. The brief must be a near-complete
  instruction.

The model choice interacts with effort level and brief
quality. A detailed brief compensates for a lighter model —
sonnet at medium effort with a thorough brief often matches
opus at medium effort with a vague brief. The planner's job
is to write briefs good enough that the recommended model
can succeed.

**When in doubt, skew to the more capable model.** Saving
money only matters if the outcome is still acceptable. A
failed or low-quality implementation wastes more time (and
therefore more money) than using a heavier model would have
cost. Only recommend a lighter model when you are confident
the brief is detailed enough for it to succeed.

**Brief for sub-agent:** This is the key field. Write it as
if briefing a colleague who has never seen the codebase.
Include: what to change, which files to touch, what patterns
to follow, and any non-obvious constraints. The better the
brief, the lower the effort level needed and the lighter the
model that can succeed.

A good brief front-loads the research the planner already
did, so the implementing agent doesn't repeat it. For
example, instead of "add a UDD adapter", write "add a UDD
adapter under `divergulent/sources/udd.py`, mirroring the
Repology adapter's `fetch()` / `normalise()` shape in
`divergulent/sources/repology.py`. UDD is a public
PostgreSQL endpoint; use the read-only connection string
documented at udd.debian.org and the `sources` /
`upstream_metadata` tables. Cache results through the shared
cache layer in `divergulent/cache.py` keyed by source
package + suite, and respect the polite-usage rules in
`AGENTS.md` (User-Agent, rate limit, on-disk TTL)."

### Management session review checklist

After a sub-agent completes, the management session should
verify:

- [ ] The files that were supposed to change actually changed
      (read them, don't trust the summary).
- [ ] No unrelated files were modified.
- [ ] The code passes `pre-commit run --all-files` (lint,
      tests, type checking).
- [ ] Network access to external sources is mocked in tests,
      not hitting live services on every run.
- [ ] The changes match the intent of the brief — not just
      syntactically correct but semantically right.
- [ ] Commit message follows project conventions (including
      the Co-Authored-By line with model, context window,
      effort level, and other settings).

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

### Future work

We should list obvious extensions, known issues, unrelated
bugs we encountered, and anything else we should one day do
but have chosen to defer to here so that we don't forget
them.

...

### Bugs fixed during this work

This section should list any bugs we encounter during
development that we fixed. You should also scan the project's
issue tracker (once one exists) to see if there are any
directly related issues that we should either resolve as part
of this master plan, or at least be aware of when planning.

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

### Back brief

Before executing any step of this plan, please back brief
the operator as to your understanding of the plan and how
the work you intend to do aligns with that plan.
