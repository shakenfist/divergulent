# Make the cold full-machine run fast and legible

## Prompt

Before responding to questions or discussion points in this
document, explore the divergulent codebase and ground answers in
what it does today: `divergulent/http.py` (per-host rate
limiting and the on-disk cache), `divergulent/sources/repology.py`
(the `project-by` per-package resolver and newest-version logic),
`divergulent/cache.py`, `divergulent/cli.py` (how `score` /
`staleness` build the HTTP client and query per source).

Where a question touches external concepts, research rather than
guess. Key references:

- **Repology bulk API** — `/api/v1/projects/` returns up to 200
  projects per request; ranges page by project name
  (`/api/v1/projects/<name>/`). `?inrepo=debian_unstable`
  restricts to projects packaged in that repo. Each entry
  carries the per-repo `srcname` and `version`/`status`.
- **Repology rate limit** — ≤1 request/second is mandated; an
  identifying User-Agent is required for bulk use. sources.debian.org
  has **no documented rate limit**.

Flag uncertainty explicitly (e.g. Repology name-matching is
heuristic; surface unresolved packages as UNKNOWN, never as a
false "behind").

All planning documents go into `docs/plans/`. Per-phase plans
are named `PLAN-faster-full-run-phase-NN-descriptive.md` and
tracked in the Execution table. One commit per logical change,
at minimum one per phase.

## Situation

The full-machine run is polite but **slow cold**: a real
debian-13 CI run took ~19 minutes. The cause is structural — we
make ~1 request per source package on *each* axis at the
mandated ≥1 request/second per host, so wall-clock ≈ the number
of sources (in seconds), for both the staleness (Repology) and
the divergence-count (sources.debian.org) halves.

The persisted cache makes *reruns* fast, but the cold cost is
paid on a user's **first** run — and "a 19-minute first run" is
poor UX. By the project's own principle (a user must be able to
run it), that is the real problem; the CI timing was the
empirical signal.

## Mission and problem statement

Bring a *cold* full-machine `score` from ~19 minutes toward a
few minutes, without becoming a worse API citizen, via two
levers:

1. **Per-host rate-limit tuning.** Keep Repology at the mandated
   ≤1 req/s, but let sources.debian.org (no documented limit)
   run a few requests/second. Cuts the divergence-count half
   several-fold; small, low-risk.
2. **Repology bulk staleness.** Replace ~1 Repology request per
   source with a paged sweep of the whole `debian_unstable`
   project set (~150 requests, cached ~24h), matched locally.
   This makes the staleness cost per-*archive*, not per-machine,
   and is the larger reduction.

Separately, make a long run **legible**: emit live progress so
the user can see it is working rather than wondering whether it
has hung. This addresses *perceived* wait and complements the
actual speedups (and matters even after them, since a cold run
is still a couple of minutes).

Non-goals: bounded-concurrency / async fetching (a heavier
change that could push the cold run under a minute) — recorded
as Future work.

## Open questions

- **sources.debian.org interval.** Proposed ~3 req/s (0.34 s).
  Confirm the value; it is a single web service, so stay
  moderate.
- **Repology bulk scope.** Page the whole `debian_unstable` set
  (covers everything, ~150 requests, cacheable) vs. only the
  installed sources in batches. Proposed: whole set, cached —
  simpler and reusable across runs.
- **Staleness fallback.** If a source is absent from the bulk
  map (e.g. third-party, or not in unstable), keep the existing
  per-package `project-by` lookup as a fallback, or just report
  UNKNOWN? Proposed: UNKNOWN (avoids reintroducing per-package
  requests); revisit if coverage suffers.

## Execution

Three phases, each roughly one PR; all three are independent, so
they can land in any order.

| Phase | Plan | Status |
|-------|------|--------|
| 1. Per-host rate-limit tuning | PLAN-faster-full-run-phase-01-rate-limits.md | Complete |
| 2. Repology bulk staleness | PLAN-faster-full-run-phase-02-repology-bulk.md | Reverted (see phase 4) |
| 3. Progress reporting for long-running commands | PLAN-faster-full-run-phase-03-progress.md | Complete |
| 4. Revert whole-machine staleness to per-package | PLAN-faster-full-run-phase-04-revert-bulk.md | Complete |
| 5. Bounded-concurrency fetching | PLAN-faster-full-run-phase-05-concurrency.md | Complete |

## Agent guidance

Follows the execution model, effort/model rubric, and review
checklist in [PLAN-TEMPLATE.md](../../PLAN-TEMPLATE.md). In
summary: sub-agents implement per the phase step tables; the
management session reviews the actual files and commits; tests
stay offline (HTTP mocked); skew to the more capable model when
in doubt. Phase 1 is medium-effort (small, well-understood);
phase 2 is high-effort (a new bulk-fetch path, pagination,
caching, and local matching with honest UNKNOWN handling).

## Administration and logistics

### Success criteria

* A cold full `divergulent score` over a real Debian 13 machine
  completes in a few minutes (down from ~19), with no `--limit`.
* Repology is still queried at ≤1 req/s with an identifying
  User-Agent; sources.debian.org at a moderate, configurable
  per-host rate.
* Staleness results are unchanged in meaning (CURRENT / BEHIND /
  UNKNOWN; unresolved is UNKNOWN, never a false BEHIND); the
  bulk path is verified to agree with the per-package path on a
  sample.
* `tox -epy3` / `tox -eflake8` pass; tests stay offline.
* `README.md` / `ARCHITECTURE.md` / `AGENTS.md` updated for the
  per-host intervals and the Repology bulk path.

### Future work

- **Bounded-concurrency fetching** — issue several requests in
  flight per host (within the rate limit) to push the cold run
  under a minute; a heavier async/threaded change. *(Now phase 5.)*
- **Published precomputed cache (the bigger win).** Staleness and
  divergence are functions of `(source_package, version)` plus the
  upstream world, not of the user's machine, so they are fully
  shareable. A scheduled central job sweeps Repology +
  sources.debian.org politely and publishes a small, signed static
  bundle (staleness refreshed daily; divergence is immutable per
  version). Clients download the **whole bundle** (preserving the
  "inventory stays local" privacy property — the host never learns
  which packages you have) and match locally, turning a cold run
  into seconds while being a far better API citizen (one crawler,
  not N). To be designed in its own plan after phase 5; concurrency
  then becomes the cache-miss fallback rather than the primary
  cold-run lever. Design notes to start from:
  - **Implement as a cache pre-seed, not a new source tier.** The
    bundle is just a serialized dump of the existing on-disk `Cache`
    (entries are already `{stored_at, ttl, value}`, keyed
    machine-independently). `divergulent cache pull <url>` unpacks
    it into the cache dir; the existing "check cache -> else fetch
    live -> store" paths then find warm entries and live-fetch only
    the gaps. No new client logic, no new runtime deps. The
    publisher is "run divergulent over the whole archive on a
    server, then export its cache".
  - **Freshness via the existing TTL.** Staleness (24h TTL) expires
    and refetches if the bundle is stale; divergence (30d, immutable
    per version) persists. `stored_at` is absolute, so publish the
    staleness half daily.
  - **Versioning / schema (the bundle is optional, so any mismatch
    = ignore it).** Stamp the bundle with an envelope schema version
    and a cache-entry-value schema version, both signed. Client
    ignores an unrecognised bundle schema (run live) and unknown
    entry fields; it never fails on a mismatch. Keep a cache-schema
    tag on the local cache dir (a `VERSION` file or a namespace
    component) so bumping it silently invalidates stale-shaped
    entries on upgrade. Host bundles at versioned paths
    (`/cache/v1/...`) to decouple client and publisher release
    cycles. Migration is the publisher's job, not the client's.
  - **Trust.** Sign with the existing Sigstore tooling and have the
    client spot-verify a random sample against live origins ("no
    cry wolf").
- **Persisted apt-download cache** for `--classify` so its
  source-package downloads survive between runs.

### Bugs fixed during this work

- **Phase 2 bulk staleness was a cold-run regression.** A real CI
  sample run took 34m23s — *worse* than the 19 min it set out to
  fix. Measured: each Repology bulk `/projects/` page is ~3.5 MB and
  ~7.8 s, and `debian_unstable` is ~170 pages, so the whole-archive
  sweep is ~22 min and ~600 MB to serve staleness for ~570 installed
  packages. The per-package `project-by` path is ~5 KB / ~0.5 s each
  (1 req/s floor ≈ 9.5 min) and fetches only what is installed. Bulk
  only pays off when the 24 h archive map is shared across many
  machines. Reverted in phase 4; the real cold-run win comes from
  phase 5 concurrency on the unthrottled sources.debian.org half.

### Documentation index maintenance

Registered in [docs/plans/index.md](index.md). Update phase
statuses there and in the Execution table as phases complete.

### Back brief

Before executing any step, back brief the operator on your
understanding of the plan and how the intended work aligns with
it.
