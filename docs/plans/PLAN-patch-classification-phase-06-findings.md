# Phase 6 findings — the cross-reference universe, measured

Phase 6 verifies the bug/CVE references a patch *declares* against Debian's own
records. Before drawing conclusions about how much it settles, the first question
is simply: **how many carried patches carry a reference at all?** That bounds
everything the tier can do — it can only corroborate or contradict a claim that
exists.

## The measurement

Over the real trixie corpus (the `divergulent-reviews` data root, the same 60,642
distinct fingerprints the whole classification runs on), we extracted the
representative claim for every distinct fingerprint and counted how many declare a
CVE (a `CVE-YYYY-NNNN` anywhere in the header) and/or a Debian bug (a DEP-3
`Bug-Debian:` field). This needs no snapshot — it measures the *input surface*,
not the verdict.

| | count | share of 60,642 |
|---|------:|----------------:|
| **Carry a CVE reference** | 871 | **1.44 %** |
| **Carry a Debian bug reference** | 5,418 | **8.93 %** |
| **Carry either** | 6,133 | **10.11 %** |

(CVE + bug do not sum to "either": 156 patches carry both.)

## What this tells us

**The cross-reference is a scalpel, not a broom.** Only ~1 in 10 carried patches
declares a bug/CVE, so phase 6 acts on a tenth of the corpus at most — and the
category-settling CVE tier on ~1.4 %. This is exactly the shape the master plan
predicted and wanted: the tier *cannot* cry wolf across the archive because it
says nothing about the ~90 % of patches that make no verifiable claim. It sharpens
a small, self-selected slice — the patches whose authors reached for a provenance
claim — and stays silent everywhere else.

**The CVE tier's value is depth, not breadth.** 871 patches is small, but each is a
patch that *claims to be a security fix*. Confirming those against the Security
Tracker settles a genuine `security` category with an auditable snapshot and **no
LLM spend**, and — more importantly — surfaces any that *fail* the check, which is
the single most interesting signal the whole system can produce: a patch that
dresses itself as a known CVE fix but is not one.

**The BTS tier is mostly corroboration.** 8.9 % declaring a Debian bug is a much
larger annotation surface, but a bug reference maps to no category, so its job is
provenance colour and contradiction-flagging, not settling. It confirms the
ordinary case (the bug exists, filed against this source) and flags the anomaly
(a declared bug that does not exist or belongs to another package).

## What is NOT yet measured

The **confirmed : contradicted : unknown** split is a property of a *snapshot*, not
of the corpus, so it is deliberately left to the operator's first
record-with-snapshot run (pull a Security Tracker + BTS snapshot, then
`ledger record`). The `build`/`record` stats lines now print
`external decisions appended/skipped/superseded` and `external provenance
appended/skipped`, so that split reports itself the moment a snapshot is in place.
The **contradiction rate** in particular wants watching: the plan flagged
"wrong-source" as a possibly-legitimate cross-package fix, and only real data can
calibrate how aggressively to treat it as a review nudge versus noise.

## Design decisions this validated

- **Reference prevalence is low enough that inline evidence is a non-issue.** A
  worry behind the phase-5 sharding work was bundle/ledger growth; at 6,133
  reference-carrying patches the external decisions/observations add a rounding
  error to a 144k-row ledger. No new storage pressure.
- **The freshness horizon matters more than the volume.** Because so few patches
  are involved, the cost of phase 6 is dominated by keeping the ~1.4 % CVE verdicts
  *fresh* against a moving tracker, not by the one-time settle. The
  `input_fresh_until` re-verify path (E3) is therefore the load-bearing mechanic,
  exactly as designed.

## Status

E1–E5 implemented and offline-tested; this findings pass measured the input
surface on the real corpus. The quantitative verdict split awaits the first
operator snapshot run.
