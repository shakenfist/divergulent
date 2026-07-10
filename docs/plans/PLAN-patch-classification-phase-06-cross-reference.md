# Phase 6 — BTS / upstream cross-reference (the `external` rule tier)

A carried patch often *claims* a provenance: a DEP-3 `Bug-Debian: #NNNNNN`, a
`Bug: <upstream-url>`, or a `CVE-YYYY-NNNN` in its header. Every phase so far has
treated those references as **claims** — author-controlled strings we extract but
never check (`claim.py` surfaces `Claim.bugs` and `Claim.cves`, and nothing
consults them). Phase 6 is the first tier that **leaves the diff and consults the
world**: it verifies the declared bug/CVE against Debian's own records, and folds
the result back as a provenance signal.

This is the master plan's `external` rule tier, and it is deliberately last: it is
the only classifier input that depends on **mutable external state** (the Debian
Security Tracker, the BTS), so it must record *what it saw and when* — an **input
snapshot** and a **freshness horizon** — or its verdicts rot silently. The ledger
schema has reserved `decision.input_snapshot` and `decision.input_fresh_until`
(nullable, unused by the pure phase-2/4 rules) since schema v2 precisely for this
phase, so **no migration is needed** — the columns are waiting.

**Status: planned.** Nothing implemented yet.

## The thesis: verification, not trust — and disagreement is the signal

Consistent with the whole plan's claim-vs-content spine, the value here is *not*
"read the CVE and believe it." A malicious diff can write `CVE-2021-44228` in its
header exactly as easily as a benign one. The value is the **cross-check**:

- **Corroboration** — the declared CVE *exists* in the Security Tracker and is
  recorded against *this source package*, and the patch touches code (not just a
  manpage that mentions it). That is genuine external evidence the patch is a
  security fix, and it can settle a `security` category **without an LLM**, with
  the tracker snapshot as auditable evidence. A meaningful slice of the ~43k
  "substantive residue" carries a real CVE/bug reference; settling those cheaply is
  the primary payoff.
- **Contradiction** — the patch claims `CVE-2023-99999` but the tracker has no such
  id, or the id is real but is recorded against a *different* package, or the
  declared `Bug-Debian:` number does not exist / is not filed against this source.
  This is the **loudest** signal in the whole system: a provenance claim that does
  not survive verification. Per "no cry wolf," Phase 6 does **not** pronounce
  malice — it raises the item's review priority with an explainable
  `claim-unconfirmed` flag and leaves the call to a human.
- **Silence** — the tracker/BTS snapshot is missing, stale, or simply has no entry
  (most patches carry no reference at all). The honest output is *no signal*: no
  category change, no flag, `provenance = unknown`. Absence of a reference is not
  suspicious; the overwhelming majority of Debian's carried patches are plain
  packaging work with no bug at all.

## Sources, bulk-first (never per-patch network)

The one hard constraint inherited from the reach axis: **fetch bulk, pin a
snapshot, then look up locally.** Per-patch network against bugs.debian.org across
60k fingerprints is both impolite and unreproducible. Every source below is a
single pinned download under the data root, exactly like `popcon.sqlite`.

| Source | What it answers | Bulk artifact | Priority |
|--------|-----------------|---------------|----------|
| **A. Debian Security Tracker** | Does this CVE exist? Is it recorded against this source? What is its status (open / resolved / not-affected) and fixed version? | The tracker's machine-readable JSON (`https://security-tracker.debian.org/tracker/data/json`), source→CVE→status, pinned to `corpus/security_tracker.sqlite` with a `snapshot_date`. | **Primary** — cleanest data, highest value, one download. |
| **B. Debian BTS** | Does this bug number exist? Is it filed against this source? Open or done? | The bulk bug index (UDD `bugs`/`bugs_packages` tables, or the BTS bulk status export) pinned to `corpus/bts.sqlite`. **Never** per-bug `bugreport.cgi`. | Secondary — messier schema, added after A proves the mechanics. |
| **C. Upstream-fixed** | Is the referenced fix actually released upstream? | *Mostly derived from A* — the Security Tracker already records upstream/released status per CVE. Generic upstream-repo verification (arbitrary `Bug:` URLs → real upstream state) is **out of scope** (see boundaries). | Derived / deferred. |

Source A carries most of Phase 6's value on its own and is a single ~30 MB JSON
that reduces to a compact per-(source, cve) status table. B and C are strictly
additive and each lands as its own step; the phase is useful after A alone.

## Design decisions

### External rules emit an `external`-purity decision, carrying the snapshot
An external rule registers as `RegisteredRule(kind='heuristic', purity='external',
…)` — `purity='external'` already exists in `PURITIES` and is the flag that says
"this consulted the world." When it settles a category it calls the existing
`append_decision(…, input_snapshot=<compact json>, input_fresh_until=<iso8601>)`:

- `input_snapshot` — the **specific** evidence this decision consulted, not the
  whole file: e.g. `{"cve":"CVE-2021-3999","source":"glibc","status":"resolved",
  "fixed_version":"2.31-13+deb11u4","snapshot_date":"2026-07-08"}`. Self-auditing:
  the decision row alone explains itself.
- `input_fresh_until` — `snapshot_date + TTL`. TTL is generous (default **30
  days**); the Security Tracker moves, but a settled CVE status rarely flips.

The full pinned snapshot lives under the data root (reproducibility + the next
re-verify), mirroring `popcon.sqlite`.

### Two outputs, deliberately asymmetric: settle security, but only *flag* contradiction
This is the load-bearing safety decision and it falls straight out of no-cry-wolf:

- **Confirmed** → a `security` **category decision** (`kind='heuristic',
  purity='external'`), but *only* under strong corroboration: the CVE exists in the
  tracker **for this source**, **and** the patch touches code, not just prose
  (reuse phase-2's code-vs-prose awareness — a manpage that merely cites a CVE must
  not become `security`). Confidence is `high` when the tracker records it resolved
  with a fixed version, `medium` otherwise.
- **Contradicted** → **never** a category. A failed verification cannot *invent* a
  verdict. It records a `provenance` **observation** (`detail='claim-unconfirmed'`,
  snapshot_date in evidence) that raises review priority so a human looks. Emitting
  `security` — or any category — from a *failed* check would be the exact cry-wolf
  the project forbids.
- **Silence** → nothing written; `provenance = unknown`.

### Precedence: external corroboration must not overrule strong pure content
External decisions rank at `decision_rank` = 1 (heuristic), the same tier as the
pure phase-2 content rules, tie-broken by `decided_at`/confidence/id. That creates
a real hazard: a patch whose *content* is high-confidence `documentation` or
`test` must not flip to `security` just because its header cites a CVE. So the
external `security` rule **defers**: it does not emit when a live pure-content
decision already settled the fingerprint at `high` confidence in a
non-`unknown`/non-`bugfix` category. It settles the *residue* (`unknown` /
low-confidence), which is exactly where the CVE reference adds information. This
deference is asserted in tests (a high-confidence `documentation` patch that cites
a CVE stays `documentation`; only its priority is nudged by the provenance
observation).

### Freshness: idempotent, supersede-on-change, re-verify-when-stale
The external pass mirrors reach's three-way reconcile, plus a staleness axis:

- **Fresh & unchanged** (`now < input_fresh_until`, snapshot answer identical) →
  skip. Idempotent re-runs are free.
- **Changed** (the tracker now says something different) → supersede the old
  decision, append the new one. The append-only ledger keeps the history.
- **Stale** (`now ≥ input_fresh_until`) → re-verify against the current snapshot
  even if nothing else changed, and re-stamp `input_fresh_until`. This is the whole
  reason the freshness columns exist: an `external` verdict is only as trustworthy
  as its snapshot's age.

### Curation-side only; the client learns nothing new
Like every classifier tier, this runs centrally at `record` time and ships only in
the derived, signed classification bundle. The client still hashes a patch body and
looks up a verdict — it never queries the BTS, never sees a snapshot, and gains no
new dependency. Phase 5's bundle already carries a short provenance reason; a
`security` verdict decided by the external rule simply reads
`"security — confirmed CVE-2021-3999 (security-tracker 2026-07-08)"`.

### Where the external pass hooks in
`record.py::record_to_ledger` gains a new pass **after** the deterministic content
pass and the reach/reviewability observations (post the current external-free
region), gated on a `security_tracker_path` argument exactly as reach is gated on
`popcon_path`. Absent snapshot → the pass is a no-op and everything degrades to
`provenance = unknown`. The per-fingerprint `Claim` (already threaded through the
classify records) supplies `claim.cves` and `claim.bugs`; the pass looks each up in
the pinned snapshot, applies the deference rule, and appends decisions/observations.

## Steps

| Step | Effort | Model | Brief |
|------|--------|-------|-------|
| E1 | med | opus | **Security-Tracker snapshot.** `security_tracker.py`: fetch + parse the tracker JSON into a pinned `security_tracker.sqlite` (`cve(source, cve, status, fixed_version)` + `meta(snapshot_date, source_url, row_count)`), `divergulent-classify security-tracker` verb, overridable URL, refuse-to-write-empty (popcon's guard). Offline-tested against a trimmed JSON fixture; **no live network in tests**. One commit. |
| E2 | med | opus | **The external CVE rule.** `cross_reference.py`: `EXTERNAL_CVE_RULE` (`kind='heuristic'`, `purity='external'`, versioned), `verify_cve(claim, source, snapshot)` → `confirmed`/`contradicted`/`unknown` with the compact `input_snapshot` dict and `input_fresh_until`. Pure-function unit tests at every boundary (exists-for-source, exists-for-other-source, malformed id, absent). One commit. |
| E3 | med | opus | **Record the external pass.** Thread `security_tracker_path` into `record_to_ledger`; add the post-deterministic external pass: confirmed→`security` decision (with the deference rule + code-touch gate), contradicted→`provenance` observation, idempotent/supersede/re-verify-when-stale, `RecordStats.external_*`. Tests: settle, defer to high-confidence content, contradiction flag, staleness re-verify. One commit. |
| E4 | low | opus | **Priority + bundle + UI surfacing.** `claim-unconfirmed` nudges review priority (a minor term below risk, like reach); the classification bundle's provenance reason renders the confirmed-CVE phrase; the review UI shows a provenance badge (`confirmed` / `unconfirmed`) with the snapshot date. Offline tests. One commit. |
| E5 | med | opus | **BTS bug-existence (source B).** `bts.py`: pin the bulk bug index to `bts.sqlite` (UDD bulk export, **not** per-bug cgi), extend the external rule to verify `claim.bugs` Debian references (exists / filed-against-this-source / open|done) as corroboration/contradiction. Same snapshot+freshness mechanics. Offline fixture. One commit. |
| E6 | low | opus | **Findings + docs.** A `PLAN-patch-classification-phase-06-findings.md` measuring how much residue the CVE cross-reference actually settles and the confirmed:contradicted:unknown split on the real corpus; runbook + README/AGENTS/ARCHITECTURE; the master-plan phase-6 row and index.md. One commit. |

E1 → E2 → E3 give a shippable, valuable phase (the CVE cross-reference) on their
own; E4 surfaces it; E5 adds the BTS; E6 measures and documents. Everything
offline-tested against fixtures — no live network in the suite.

## Testing requirements
- `verify_cve` returns `confirmed` only when the CVE exists **for this source**;
  `contradicted` for a malformed/nonexistent id or a right-id/wrong-source; `unknown`
  when the snapshot lacks the id or is absent.
- The external pass settles `security` on a code-touching patch with a confirmed
  CVE, but **defers** — a high-confidence `documentation`/`test`/`packaging`
  content verdict is never overruled (asserted directly); a manpage that merely
  cites a CVE does not become `security`.
- Idempotency: a second `record` with the same fresh snapshot writes nothing new;
  a *changed* snapshot supersedes; a *stale* snapshot triggers re-verify and
  re-stamps `input_fresh_until`.
- `input_snapshot` on the decision row is self-describing (cve, source, status,
  snapshot_date) and `input_fresh_until` = snapshot_date + TTL.
- Contradiction raises review priority via the `provenance` observation and never
  writes a category.
- No live network anywhere in the suite; snapshots come from trimmed fixtures.
- `pre-commit run --all-files` green; house style (single quotes, 120 cols, no
  trailing whitespace, `from __future__ import annotations`); runtime stays
  stdlib + python-debian (the tracker/BTS fetch is curation-side, reusing the
  existing `urllib`/`HttpClient` seam, no new runtime dependency).

## Success criteria
- Every fingerprint carrying a CVE/bug reference gets a deterministic provenance
  outcome — `confirmed` / `contradicted` / `unknown` — with a dated, auditable
  snapshot, and confirmed-security patches in the residue are settled **without an
  LLM**.
- No `external` verdict is trusted past its freshness horizon: a stale snapshot
  forces re-verification, and every external decision records exactly what it saw
  and when.
- A declared reference that fails verification surfaces for human review (raised
  priority + explainable flag) and is **never** auto-pronounced malicious or
  silently believed.
- The client gains no new capability or dependency; it consumes the same signed
  bundle, now with a provenance-backed reason on the settled patches.

## Open questions
- **Freshness TTL** — 30 days is a starting guess; E6's findings should confirm the
  Security Tracker's real churn rate for *settled* CVEs (likely allowing a longer
  horizon).
- **BTS bulk source** — UDD (`udd.debian.org` / the `bugs` tables) vs the BTS's own
  bulk status export; pick in E5 by which gives source-package association cleanly
  without per-bug fetches.
- **Contradiction precision** — how aggressively to flag. A wrong-source CVE may be
  a legitimate cross-package fix, not deception; the flag must stay a *review nudge*,
  not an accusation. Calibrate against E6's real-corpus contradiction set.

## Out of scope / honest boundaries
- **Arbitrary upstream verification.** Following a free-text `Bug:` URL into an
  arbitrary upstream tracker/repo to confirm a fix is *actually* merged is
  genuinely hard (auth, rate limits, a hundred forge APIs) and out of scope. We use
  the Security Tracker's *own* upstream/released status (source C, derived from A);
  general upstream state is future work.
- **Pronouncing malice.** As everywhere: a contradiction is a *candidate for
  review*, never a verdict of attack.
- **Per-patch live queries.** All external state is bulk-pinned; the classifier
  never hits the network per fingerprint, and tests never hit the network at all.
- **Client-side cross-reference.** The client runs no classifier and no lookups;
  this is curation-side, shipped only as bundle verdicts.

## Back brief
Before executing: Phase 6 is the `external` tier — it *verifies author-declared
bug/CVE claims against Debian's own records*, it does not trust them. The schema
already has `input_snapshot`/`input_fresh_until` (no migration). Sources are
**bulk-pinned** snapshots (Security Tracker first, BTS second), never per-patch
network, mirroring the popcon precedent. The output is asymmetric by design:
strong corroboration *settles* `security` (deferring to high-confidence pure
content), but a failed verification only *flags for review* — it never invents a
category and never pronounces malice. Every external decision records what it saw
and when, and a stale snapshot forces re-verification. Curation-side only; the
client gains nothing new.
