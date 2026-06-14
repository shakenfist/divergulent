# Divergulent: initial implementation

## Prompt

This is the master plan for divergulent's first swing at
implementation. Divergulent is currently greenfield — there
is no code to explore yet — so this plan is grounded in the
design discussion that motivated the project rather than in
an existing codebase. As code lands, future planning should
explore it thoroughly and ground answers in what the code
actually does, per [PLAN-TEMPLATE.md](../../PLAN-TEMPLATE.md).

Where a question touches external concepts, research as
needed rather than guessing. Divergulent sits on top of a lot
of Debian and ecosystem machinery and the whole point of the
tool is to be trustworthy about supply-chain risk, so the
details matter. Key external references:

- **dpkg state** — `dpkg-query -W`, `/var/lib/dpkg/status`,
  binary-to-source package mapping, version epochs/revisions.
- **Debian source format `3.0 (quilt)`** — Debian's delta
  from upstream lives as an explicit patch series in
  `debian/patches/series`; counting and classifying these is
  the divergence signal.
- **DEP-3 patch headers** — `Origin:`, `Forwarded:`, `Bug:`;
  these distinguish a benign forwarded-upstream patch from a
  pure distro-only divergence (the motivating pngtools case).
- **Repology** (`repology.org/api`) — cross-distro version
  aggregation with a per-project `status`; the fastest path
  to the staleness axis.
- **sources.debian.org** — HTTP/JSON access to every source
  package's `debian/patches/` without downloading tarballs.
- **UDD**, **uscan/DEHS/`debian/watch`**, **Wikidata** —
  later sources (see Future work).

Flag uncertainty explicitly. When a data source is heuristic
(Repology name-matching, missing `debian/watch`, absent DEP-3
headers), say so rather than presenting it as ground truth.

All planning documents go into `docs/plans/`. Per-phase plans
are named `PLAN-initial-phase-NN-descriptive.md` in this
directory and tracked in the Execution table below.

One commit per logical change, at minimum one per phase. Each
commit should build, pass tests, and have a clear message.

## Situation

The project was prompted by a real experience: Debian chose
to carry a patch against an upstream package (pngtools) that
upstream did not agree with. That is within Debian's rights,
but it highlighted a general problem — it is very hard, as a
user of a distribution, to tell how stale or how divergent
the software packaged for you is compared to pure upstream.
That gap is also a supply-chain attack surface: malicious
change can be introduced at the *distribution* layer (a
carried patch), not only at the upstream-author layer.

What the ecosystem already provides:

- **Staleness is largely solved, just not framed for this
  question.** Repology aggregates distros + language
  registries and computes an `outdated`/`newest` status with
  a public API. Debian's own DEHS/uscan/`debian/watch`
  machinery and the UDD warehouse also track upstream
  versions. Crucially, Repology has *no author-submission
  path* and no first-class notion of "true upstream latest"
  independent of the sources it ingests.
- **Divergence (carried patches) is mostly invisible to
  users.** Because `3.0 (quilt)` is the standard source
  format, Debian's deltas live as a machine-readable patch
  series, and DEP-3 headers declare whether each patch is
  forwarded upstream or distro-only. sources.debian.org
  exposes this over HTTP. Nothing surfaces it to end users as
  "your machine carries N distro-only patches."

The gap divergulent fills: no tool answers *"how divergent is
this whole machine, weighted and ranked"* by combining
version lag and carried-patch weight across the packages a
user actually has installed.

## Mission and problem statement

Let a user ask **"how divergent from pure upstream is this
machine?"** and get a reasonable, ranked answer without a
large amount of work.

Concretely, the first swing should:

- Enumerate installed packages and map them to source
  packages.
- Measure the **staleness** axis (version lag vs upstream).
- Measure the **divergence** axis (carried distro-only
  patches, weighted by DEP-3 classification).
- Combine the two into a per-package signal, rank packages by
  their contribution, and produce a whole-machine summary.
- Be honest: surface heuristic/unverified signals as
  uncertainty rather than as confirmed drift. The value is
  *visibility and ranking*, not a verdict — a large trusted
  patch set is normal, not an alarm.
- Be a good citizen: respect external APIs (User-Agent, rate
  limits, caching, graceful degradation) and respect privacy
  (the installed-package inventory does not leave the machine
  in the default, local-only posture).

Explicitly **out of scope** for the first swing: the
server/aggregator, UDD/DEHS/Wikidata sources, multi-machine
"fleet" views, and upstream-signed release feeds. These are
recorded under Future work.

## Open questions

- **Implementation language.** Python (decided).
- **Client/server split.** Start as a thin local client that
  queries public APIs directly; defer any server/cache to
  Future work. (Most expensive mapping is already done by
  Repology / sources.debian.org.)
- **Privacy.** First swing is local-only by default; no
  inventory leaves the box. Revisit if/when a server exists.
- **Trust model.** Treat Repology version matching as
  *heuristic*, `debian/patches` contents as *factual* about
  what Debian ships, and (later) Wikidata as *editable*.
  Output must label confidence accordingly.
- **Scoring (needs design in Phase 4).** How to combine
  staleness and divergence into a single comparable signal
  without letting one noisy input dominate, and how to weight
  patches by DEP-3 status and size. Resolve at high effort.
- **Output format.** Human-readable ranked table for v1;
  consider a `--json` mode for later tooling. To be settled
  in Phase 4.
- **Which suite(s)** to compare against (installed vs
  testing/unstable/upstream) — settle in Phase 2.

## Execution

The first swing is a single vertical tool delivered in four
phases. Phases 1–2 deliver an end-to-end staleness MVP
(prove the pipeline works); Phases 3–4 add the divergence
axis and the combined ranked report.

| Phase | Plan | Status |
|-------|------|--------|
| 1. Project skeleton & dpkg inventory | PLAN-initial-phase-01-inventory.md | Complete |
| 2. Repology adapter & staleness axis | PLAN-initial-phase-02-staleness.md | Complete |
| 3. Divergence axis (debian/patches + DEP-3) | PLAN-initial-phase-03-divergence.md | Complete |
| 4. Scoring & ranked report | PLAN-initial-phase-04-scoring.md | Not started |

### Phase 1 — Project skeleton & dpkg inventory

Stand up the Python project: package layout, CLI entry point,
`pyproject.toml`, pre-commit config (lint + tests + type
checking), and the test harness with external access mocked.
Implement the inventory module: enumerate installed packages
and versions via `dpkg-query` (arguments passed as a list,
never a shell string), map binary packages to source packages
and source versions, and parse Debian versions
(epoch/upstream/revision) using proper Debian version
semantics. Establish the shared cache layer and the
data-source adapter interface (fetch/normalise) that later
phases plug into, even though Phase 1 has no remote source
yet.

### Phase 2 — Repology adapter & staleness axis

Add the first data-source adapter (Repology) behind the cache
layer, with a descriptive User-Agent, request timeouts, rate
limiting, and graceful degradation. Compute the staleness
axis per source package: behind / current / unknown, where
"Repology has not matched this package" and "no upstream feed
exists" are reported as *unknown*, not as confirmed
staleness. End of Phase 2 is a usable MVP: `divergulent`
prints the installed packages that are behind upstream,
worst first.

### Phase 3 — Divergence axis (debian/patches + DEP-3)

Add a sources.debian.org adapter that fetches each source
package's `debian/patches/series` and patch files. Parse
DEP-3 headers and classify each patch: forwarded-upstream /
upstream-origin = benign drift; `Forwarded: no` / vendor
origin = real divergence; missing header = *unknown* (not
zero). Produce a per-package divergence summary (patch count,
classification breakdown, rough size).

### Phase 4 — Scoring & ranked report

Design and implement the combined scoring model (high
effort): fold the staleness and divergence axes into a
per-package signal, roll up to a whole-machine summary, and
rank packages by their contribution so the headline output is
"the N packages making your machine most divergent." Keep
uncertainty visible. Provide a human-readable report (and
decide on `--json`).

## Agent guidance

This plan follows the execution model, effort/model guidance,
and review checklists in
[PLAN-TEMPLATE.md](../../PLAN-TEMPLATE.md) — refer to it
rather than duplicating it here. In summary:

- All implementation is done by sub-agents; the management
  session plans, reviews (reading the actual files, not the
  summary), and commits.
- Each phase plan must specify, per step, the recommended
  effort level, model, isolation, and a detailed brief that
  front-loads the research. Skew to the more capable model
  when in doubt.
- High-effort phases here: Phase 1 (project shape, Debian
  version semantics) and Phase 4 (the scoring model). Phases
  2 and 3 are mostly well-patterned adapter work once the
  interfaces from Phase 1 exist, but version/patch
  classification edge cases warrant care.

## Administration and logistics

### Success criteria

We will know the first swing has been successfully
implemented because:

- `pre-commit run --all-files` passes (lint, unit tests, type
  checking) and the test suite passes offline (no live
  Repology / sources.debian.org calls; responses mocked or
  from recorded fixtures).
- A user can run `divergulent` on a Debian machine and get a
  ranked report combining staleness and divergence for their
  installed packages, with a whole-machine summary.
- Version comparison uses Debian version ordering (epochs,
  `~` pre-release, revisions), never naive string compare.
- Heuristic/unverified signals are reported as uncertainty,
  not as confirmed drift; the tool does not cry wolf.
- External access goes through the cache layer with a
  descriptive User-Agent, timeouts, rate limiting, and
  graceful degradation; the package inventory never leaves the
  machine.
- Code follows house Python style (single quotes, double
  quotes for docstrings, 120-char lines, no trailing
  whitespace) and the adapter/cache patterns established in
  Phase 1.
- `README.md`, `ARCHITECTURE.md`, and `AGENTS.md` reflect the
  modules, adapters, cache, and scoring model that exist.

### Future work

- **Server / precomputed aggregator** with a bulk endpoint so
  a thin client can score thousands of packages in one
  round-trip; revisit the privacy posture for any off-box
  inventory transmission.
- **More data sources:** UDD (bulk Debian metadata),
  uscan/DEHS/`debian/watch`, and Wikidata (as an editable
  upstream-version hint).
- **Upstream-signed release feed** — let an author publish a
  signed manifest of current releases that divergulent reads
  directly, inverting Repology's trust model and addressing
  the "all distros stale, no upstream feed" blind spot.
- **Other distributions** beyond Debian.
- **Fleet view** — aggregate divergence across many machines.
- **Richer scoring** — patch-content analysis, age-weighting,
  trust tiers per source.

### Bugs fixed during this work

None yet (greenfield). As development proceeds, record bugs
fixed here and scan the project's issue tracker (once one
exists) for directly related issues.

### Documentation index maintenance

This master plan is registered in
[docs/plans/index.md](index.md). When phases complete, update
their status in the Execution table above and in `index.md`.
Phase files are linked from the Execution table; they do not
need a separate navigation entry.

### Back brief

Before executing any step of this plan, back brief the
operator on your understanding of the plan and how the
intended work aligns with it.
