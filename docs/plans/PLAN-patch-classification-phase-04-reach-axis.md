# Reach axis — install-base (popcon) as a deterministic ranking dimension

A security-impacting patch in `glibc`/`openssl`/`systemd` is on essentially
every machine; the same construct in a package installed on a few hundred hosts
is a far smaller aggregate exposure. divergulent's whole thesis is supply-chain
exposure *to users*, so "how many machines actually run this code" is a
first-class signal for **where a human reviewer should look first** — yet today
nothing captures it.

This adds a deterministic **reach** observation (a t-shirt size `XS`–`XL`)
derived from Debian [popcon](https://popcon.debian.org/) install counts, and
folds it into the review-queue priority as a **secondary key within the security
tier**. Like the [reviewability axis](PLAN-patch-classification-phase-04-reviewability-axis.md)
it is structural, deterministic, claim-blind and free (no model) — measured over
all ~60k fingerprints with zero LLM cost. It is curation-side only and changes no
verdict precedence.

**Status: implemented (R1–R7).** Buckets calibrated against a live `by_inst`
snapshot (2026-06-28, below). `reach.py` (the rule + readers + corpus join),
`popcon.py` (the pinned snapshot, `divergulent-classify popcon` —
longhand `python -m divergulent.classify.popcon <corpus_dir>` → `corpus/popcon.sqlite`), the `.dsc` `Binary:` capture into
`package.binaries`, the deterministic `reach` observation recorded at `ledger
build`/`record` (opt-in on a pinned snapshot; re-records only when a bucket
changes), the priority integration (`risk_rank * 1e9 + reach_rank * 1e6 +
occurrence`, bands non-overlapping so reach never crosses a risk tier), and the
review-UI badge/filter/ordering are built and offline-tested. To take effect on
existing data: pull a snapshot, rebuild the corpus (to populate
`package.binaries`), then re-run `ledger record`.

## The one hard rule: reach multiplies *within* a risk tier, never across it

Popularity is **not** risk. A ubiquitous package carrying a benign patch is not a
security concern. If reach were allowed to outrank the security gate it would just
re-sort the queue by "is this bash/glibc/systemd" every run and bury a genuinely
dangerous patch sitting in an unpopular package. So reach enters strictly *below*
`risk_rank` in the ordering — it breaks ties among comparably-risky patches and
surfaces "security-impacting **and** widely-run" to the top of a tier. It never
promotes a low-risk patch over a high-risk one. The current formula already
encodes that discipline (`risk_rank * WEIGHT + …`); reach slots in as another
minor term, not a new dominant one.

## Reach is NOT redundant with occurrence

The priority formula already has `n_occurrences`. Reach measures a *different*
blast radius, and the one the project actually cares about:

- **occurrence** = how many *source packages* carry this identical fingerprint
  (dedup breadth).
- **reach** = how many *machines* run the package(s) the patch lands in (install
  base).

A patch can be occurrence-1 but ubiquitous (one glibc patch, on every host), or
occurrence-50 but installed nowhere. Occurrence is the dimension *less* aligned
with "how much of my machine is not upstream." Reach is added **alongside**
occurrence (occurrence stays as a lower tie-break), not as a replacement.

## Buckets (calibrated against a live snapshot, 2026-06-28)

Reach is defined **relative to the ceiling**, not as an absolute vote count, so it
stays meaningful as popcon's reporting population drifts and is robust to popcon's
opt-in undercounting (we only need the ordering and rough magnitude, which the
bias preserves). Let `anchor = max(inst)` over the snapshot (the near-universal
base package — `libc6`/`debconf` today, ~279,000 installs) and
`reach = inst(source) / anchor`. Anchoring to `max(inst)` rather than the literal
name `libc6` is deliberate: the base libc package itself can be renamed by a
time64/soname transition (see below).

| Size | `reach` (fraction of anchor) | snapshot count | lands on |
|------|------------------------------|---------------:|----------|
| **XL** | ≥ 50% | 668 | libc6, dpkg, bash, coreutils, systemd, openssl, perl, python3, git, curl, wget, sudo, openssh, tar, gzip, zlib |
| **L** | 10 – 50% | 2,608 | nginx, apache2, vim, ffmpeg, imagemagick, sqlite3, libgnutls30 |
| **M** | 1 – 10% | 6,480 | postgresql, docker.io, nginx-core |
| **S** | 0.1 – 1% | 15,184 | niche |
| **XS** | < 0.1% | 193,330 | the long tail |

The powers-of-ten cuts land on intuitive groupings, and `rman` (137 installs →
**XS**) validates the motivating case: the unsafe-`sprintf()`-in-`rman` patch
*should* rank below the same construct in curl/openssh, and this axis makes that
automatic. The XL plateau is fat (668 packages) because every `Priority:required`
package sits at ~100% of the population; an `XXL` split was considered and
rejected — "on more than half of all reporting machines" is a perfectly good
single top tier, and `git` at 62% genuinely belongs with `bash` for blast radius.

`XS` = ~89% of all packages, so reach only *promotes* the comparatively-few
high-reach patches within a risk tier; it does not reshuffle the long tail.

## Design decisions

### A deterministic `reach` observation
Mirrors `reviewability`: a ledger observation `kind='reach'`,
`detail` ∈ {`XS`,`S`,`M`,`L`,`XL`,`unknown`}, `observed_by='popcon-rule'`,
`rule_version=REACH_VERSION`, recorded by the deterministic pass at
`ledger build` / `record` (append-only, supersede-on-change, free). It rides
ALONGSIDE the category and reviewability — a patch can be an `XL` `oversized`
`bugfix`. Evidence records `{binary, inst, anchor_inst, fraction, bucket,
snapshot_date}` so the badge is explainable and the snapshot is auditable.

### Source → binary aggregation by MAX (and why that is *more correct*, not just convenient)
popcon is per-**binary**; divergulent ranks per-**source**. For each source take
the **max** `inst` over all its binaries — not the sum (summing double-counts a
machine that has several binaries from one source). Max answers "is any of this
source's code on the box," which is the question.

Max also makes the axis resilient to time64/soname transitions that split popcon
across renamed binaries. In the snapshot `libssl3` shows 31% (→ L) while
`openssl` shows 99.7% (→ XL), because the *current* OpenSSL-3 runtime on trixie is
`libssl3t64` and the old `libssl3` name carries only residual installs. Taking the
max over the openssl source's binaries recovers the true XL reach regardless of
which exact binary name is current — so source-level max is the robust choice, and
the same logic is why the anchor is `max(inst)` not a hard-coded `libc6`.

### Where the source→binary map comes from — the package-age precedent
The `Binary:` field of the `.dsc` lists every binary a source builds. The `.dsc`
is **already downloaded** during the corpus crawl (`apt_patches._fetch_source`),
so capturing `Binary:` is free, exactly as `changelog_date` was. Thread it through
`fetch_source_details` → `corpus._process` → the `package_row` → the `package`
table (`measure.write_index`), as a `binaries` column. Like package-age, it only
populates **after the next corpus rebuild + measure**; until then `reach` is
`unknown` and degrades gracefully (no promotion, no error).

### A popcon snapshot — curation-side, downloaded, pinned
`reach` needs a `name → inst` map. Fetch popcon's `by_inst` flat file once (a
curation-side artifact like the cache build, ~5 MB, parsed to a small
`popcon(binary, inst)` table under the data root, with a recorded
`snapshot_date`). The deterministic `record` pass reads it + the `package` table's
`binaries` list, computes max-inst per source → fraction → bucket. No per-patch
network. A stale/missing snapshot → `reach = unknown` (honest, not a guess). The
URL is overridable; default is `https://popcon.debian.org/by_inst`.

### Priority-formula integration (schema-free)
`triage_driver._priority_key` (the in-memory sort) and `_stored_priority` (the
single `review_queue.priority` integer) gain a `reach_rank` (XS=0 … XL=4) term,
**between** the security-tier signals and occurrence:

- `_priority_key`: insert `reach_rank` after `has_dangerous_construct`, before
  `n_occurrences` → `(risk_rank, dangerous, reach_rank, n_occurrences, n_packages,
  fingerprint)`.
- `_stored_priority`: widen the encoding so risk stays dominant and reach
  out-weighs occurrence but cannot cross a risk boundary, e.g.
  `risk_rank * 1_000_000_000 + reach_rank * 1_000_000 + min(n_occurrences,
  999_999)` (bump `RISK_PRIORITY_WEIGHT` to 1e9, add `REACH_PRIORITY_WEIGHT` =
  1e6). `reprioritise_review_queue` re-stamps pending items from the live `reach`
  + `risk` observations (same heal-the-queue path that the risk gate already
  runs), so existing pending items pick up reach without a re-triage.

`WorkItem` gains `reach_rank`, sourced from the `reach` observation by fingerprint
(a new `reach_rank_by_fingerprint(conn)` reader mirroring
`risk_level_by_fingerprint`).

### Review UI: a reach badge + filter
`review_web` gains a `reach` badge on the review page and worklist (next to the
risk and size badges) and a `reach` filter chip, so a reviewer sees *why*
something is near the top ("reach: XL — on basically every machine"). The live
worklist ordering already sorts by `(risk_rank, priority)`; extend it to
`(risk_rank, reach_rank, priority)` so the web view matches the stored ordering.

## Steps

| Step | Effort | Model | Brief |
|------|--------|-------|-------|
| R1 | med | opus | **The rule + reader.** `reach.py`: `REACH_VERSION`, `REACH_LEVELS=('XS','S','M','L','XL')`, fraction thresholds, `bucket_for(inst, anchor)`, `REACH_KIND='reach'`/`REACH_OBSERVED_BY='popcon-rule'`, `reach_rank_by_fingerprint(conn)`. Pure-function unit tests at every boundary + the `unknown` path. One commit. |
| R2 | med | opus | **popcon snapshot.** A fetch+parse of `by_inst` into a pinned `popcon(binary, inst)` table (data root), `snapshot_date`, overridable URL, offline-tested against a fixture file (no live network in tests). One commit. |
| R3 | med | opus | **Capture `Binary:` in the crawl.** Thread the `.dsc` `Binary:` list through `apt_patches.fetch_source_details` → `corpus._process` → `package_row` → `measure.write_index` `package.binaries` column. Mirrors `changelog_date`; graceful when absent. Tests across apt_patches/corpus/measure. One commit. |
| R4 | med | opus | **Record the observation.** `record.py` computes per-source max-inst → bucket from the snapshot + `package.binaries`, appends a `reach` observation (supersede-on-change, skip-if-unchanged), `RecordStats.reach_appended/skipped`. Tests: bucket assignment, supersede, `unknown` when no snapshot/binaries. One commit. |
| R5 | low | opus | **Priority integration.** `reach_rank` into `WorkItem`/`_priority_key`/`_stored_priority`/`reprioritise_review_queue`; `RISK_PRIORITY_WEIGHT`→1e9, add `REACH_PRIORITY_WEIGHT`. Tests: ordering within a risk tier follows reach; reach never crosses a risk boundary. One commit. |
| R6 | med | opus | **Review UI.** reach badge + filter chip in `review_web`; live worklist orders `(risk_rank, reach_rank, priority)`. Offline `test_client` tests. One commit. |
| R7 | low | opus | **Docs.** Runbook + README/AGENTS/ARCHITECTURE; the index.md phase-04 row. One commit. |

R1 → R2 → R3 → R4 → R5 → R6 → R7. Everything offline-tested (fixture popcon file,
temp ledger); no live network in tests.

## Testing requirements
- `bucket_for` returns the right t-shirt size at each fraction boundary; below
  the snapshot / no binaries → `unknown`.
- Max-over-binaries aggregation: a source whose only high-reach binary is a t64
  rename still buckets correctly (the `libssl3`/`openssl` case as a fixture).
- The `reach` observation is recorded/superseded/skipped-if-unchanged correctly by
  the deterministic pass; evidence carries the snapshot date.
- Priority: within one risk tier, a higher-reach item sorts first; an XL low-risk
  item NEVER sorts above an XS high-risk item (the one hard rule, asserted).
- `review_web` renders the reach badge + filter; the live worklist order matches
  the stored priority.
- `pre-commit run --all-files` green; house style (single quotes, 120 cols, no
  trailing whitespace, `from __future__ import annotations`).

## Success criteria
- Every source with known binaries carries a deterministic `reach` level after a
  corpus rebuild + record; no LLM spent on it.
- Within a security tier, "security-impacting **and** widely-run" patches surface
  first; reach never crosses a risk boundary.
- The reviewer can see and filter by reach, and the badge explains *why* (binary,
  inst, fraction, snapshot date).
- Until the next corpus rebuild populates `package.binaries`, reach is `unknown`
  everywhere and nothing regresses.

## Open questions (RESOLVED 2026-06-28)
- **Absent from popcon** → **`XS`** for a source whose binaries are present in the
  archive but absent from the snapshot (absence ≈ "too rare to report").
  `unknown` is reserved for "we have no binary list yet" (pre-rebuild). Keeps the
  scale clean while staying honest about the pre-rebuild gap.
- **`inst` vs `vote`** → default **`inst`** ("installed"). `vote` ("recently
  used") stays available behind a flag, not the default.
- **Snapshot freshness** → **generous window (weeks)**; popcon moves slowly. The
  snapshot date is pinned in evidence regardless of the window.

## Out of scope
- Per-machine reach on the **client** side. popcon is the right *global* proxy for
  the curation/ranking use case; the user's own `dpkg` list is the per-machine
  truth and a separate surface, not this axis.
- Re-weighting risk itself. Reach does not touch the risk gate or its rubric.

## Back brief
Before executing: reach is a DETERMINISTIC observation (a t-shirt size from
popcon), free, claim-blind, alongside category/reviewability — NOT a new LLM axis.
It enters priority strictly BELOW `risk_rank` (multiplies within a tier, never
across), ABOVE occurrence. Source→binary aggregation is by MAX (resilient to t64
renames), the anchor is `max(inst)` not a hard-coded name, and `Binary:` capture +
the `package.binaries` column follow the package-age precedent (populate on the
next corpus rebuild; `unknown` and graceful until then).
