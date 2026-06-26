# Reviewability axis + diff cap — size as a deterministic dimension

Patch *size* is a real axis, but a different KIND from category and security-risk:
those are semantic LLM judgments (each one multiplies cost), whereas size is
**structural and deterministic** — measured for free over all 60k fingerprints,
no model. So it is cheap to add in exactly the way the LLM axes are not, and it
doubles as the fix for three problems the risk run surfaced:

1. A handful of giant diffs (max ~2M changed lines / 5.4 MB) **overflow the model
   context** — the risk gate's `errored 1` was one of these, recorded `elevated`
   on failure rather than scored.
2. Those same giants are dollar-spikes (one 5.4 MB diff ≈ $18 in a single call).
3. A 2M-line diff is **not line-reviewable by a human** — calling it `elevated
   risk` is wrong; it needs its own disposition (trust-upstream / spot-check).

This adds a deterministic **reviewability** observation (`normal` / `large` /
`oversized`) and a **diff-size cap** on what reaches the LLM. It is curation-side
only and changes no verdict precedence.

**Status: planned, thresholds agreed.** `large` = 500, `oversized` = 5,000 changed
lines; metric = changed (`+`/`-`) lines; LLM diff cap = 40,000 chars. Ready to
implement (S1).

## What this does NOT do

It does **not** materially reduce the ~$1,000–1,200 whole-corpus Opus cost. That
cost is call-count (~60k) × Opus per-call (~$0.02, measured), with the ~600-token
rubric re-sent uncached every call (`cache-hit 0%`). The `oversized` bucket is
<0.3% of patches, so skipping the LLM on it is negligible $$; the diff cap removes
the giant-diff spikes and the context error (correctness), not a big slice of the
bulk. The real cost dial — **model** (Sonnet ≈ 5×, ~$245 vs ~$1,229), **scope**
(residue ~$871), **caching** (API backend's `cache_control` vs the subscription
`claude -p` path) — is a separate decision (see Out of scope).

## Evidence (full-corpus scan, 2026-06-27)

All 60,642 fingerprints, changed (`+`/`-`) lines of the representative body:

| metric | changed lines |
|---|---|
| p50 | 7 |
| p90 | 58 |
| p95 | 130 |
| p99 | 888 |
| p99.9 | 12,917 |
| max | 2,029,637 |

Survival (count over a threshold) is a smooth power law — no natural elbow:

| > T changed lines | fingerprints | % |
|---|---|---|
| 500 | 946 | 1.56% |
| 1,000 | 549 | 0.91% |
| 2,000 | 319 | 0.53% |
| 5,000 | 153 | 0.25% |
| 10,000 | 74 | 0.12% |
| 20,000 | 36 | 0.06% |

Char-size (for the LLM cap): >40k chars (~10k tok) = 781 fps (1.29%); >20k =
1,552 (2.56%).

## Tiers (agreed)

Measured by **changed (`+`/`-`) lines** (what a human actually reads), not total
diff lines. Recorded as a deterministic, versioned observation.

| tier | changed lines | fps | LLM passes | review disposition |
|---|---|---|---|---|
| `normal` | ≤ 500 | ~98.4% | scored normally | normal queue |
| `large` | 500 – 5,000 | ~1.3% | scored, **diff-capped** + badged | normal queue, "large" badge |
| `oversized` | > 5,000 | ~0.25% (153) | **skipped** (no call) | own "not line-reviewable" bucket |

Rationale: 500 changed lines is already a big-but-doable read; >5,000 is past what
anyone line-reviews. The operator floated 20,000 as "definitely not reviewable"
(36 fps) — a conservative alternative for the `oversized` cut; the choice barely
affects cost (it only changes how many of ~0.1–0.25% skip the LLM), so pick it on
"can a human review this?", not on $. **Diff cap (independent of tier):** 40,000
chars (~10k tokens) on whatever IS sent to the LLM — truncates 1.3% of patches,
plenty of context for a coarse read.

## Design decisions

### A deterministic `reviewability` observation
Mirrors `dangerous-construct`: a ledger observation `kind='reviewability'`,
`detail` ∈ {`normal`,`large`,`oversized`}, `observed_by='size-rule'`,
`rule_version=REVIEWABILITY_VERSION`, recorded by the deterministic pass at
`ledger build` / `record` (append-only, supersede-on-change, free). It rides
ALONGSIDE the category — a patch can be an `oversized` `bugfix` — so category
semantics stay clean. Changed/total line counts come from the diff body via the
phase-2 `content.profile`, extended with the counts.

### A shared diff cap in the LLM call path
`cap_diff(text, max_chars) -> (capped_text, was_truncated, original_len)`, used by
BOTH `risk.score_risk` and `triage` (draft + verify). Truncates to `max_chars`,
appends an explicit `[... diff truncated: N of M chars shown ...]` marker (so the
model knows it is partial), and the caller records `truncated=True` in the
evidence + counts it in the run summary (no silent caps). `--max-diff-chars`
overrides the default. `oversized` patches never reach the LLM, so the cap only
bites the `large` middle.

### Short-circuit `oversized` in both LLM passes
`run_risk_gate` and the triage driver SKIP fingerprints observed `oversized`: no
call, no `security-risk`/category LLM decision spent on an unreviewable diff. The
run summary prints a loud `N oversized skipped (not line-reviewable)`. Their
disposition is the `reviewability` observation itself; they surface in the review
UI's oversized bucket, not the risk-ordered queue.

### Review UI: an oversized bucket + a large badge
`review_web` gains a `reviewability` filter (like the category chips) and a badge
on the review page, plus an audit-style "oversized — not line-reviewable" view so
the operator handles them deliberately (trust upstream / spot-check / mark) rather
than being asked to line-review a 2M-line diff.

## Steps

| Step | Effort | Model | Brief |
|------|--------|-------|-------|
| S1 | med | opus | **Size profile + the rule.** Extend `content.profile` with `changed_lines`/`total_lines`; add `reviewability(profile, *, version) -> level` with the agreed thresholds + `REVIEWABILITY_VERSION`. Record a `reviewability` observation in the deterministic `ledger build`/`record` pass (append-only, supersede-on-change). Offline unit tests across the tiers + boundary. One commit. |
| S2 | med | opus | **Diff cap in the call path.** `cap_diff` helper; wire into `risk.score_risk` and `triage` draft/verify; record `truncated` in evidence + run-stats; `--max-diff-chars`. Tests: a capped call sends ≤ N chars with the marker, stats count truncations. One commit. |
| S3 | low | opus | **Short-circuit oversized.** `run_risk_gate` + triage driver skip `oversized` (no call), counted + printed loudly. Tests: an oversized fingerprint is skipped by both, never hits the fake `call`. One commit. |
| S4 | med | opus | **Review UI.** `reviewability` filter + page badge + oversized bucket in `review_web` (reuse the chip/audit machinery). Offline `test_client` tests. One commit. |
| S5 | low | opus | **Docs.** Runbook + README/AGENTS/ARCHITECTURE; correct the risk-gate plan's stale `~$340` to the empirical `~$1.0–1.2k` Opus whole-corpus (+ the model/scope/caching levers). One commit. |

S1 → S2 → S3 → S4 → S5. Everything offline-tested (injected fake `call`, temp
ledger); no network, no real LLM.

## Testing requirements
- `reviewability` thresholds unit-tested at each boundary; the observation is
  recorded/superseded correctly by the deterministic pass.
- `cap_diff` truncates at the limit with the marker; un-truncated diffs pass
  through byte-identical; truncation is surfaced in stats.
- An `oversized` fingerprint is skipped by both the risk gate and triage (the
  fake `call` is never invoked for it) and counted.
- `review_web` renders the reviewability filter/bucket; an oversized patch is
  reachable there and NOT in the risk-ordered worklist.
- `pre-commit run --all-files` green; house style.

## Success criteria
- Every fingerprint carries a deterministic `reviewability` level after a ledger
  build/record; no LLM spent on it.
- The risk gate no longer errors on giant diffs (they are skipped, not failed).
- The LLM never receives more than the cap; truncations are visible, not silent.
- The operator can filter to "oversized / not line-reviewable" and handle those
  deliberately, out of the normal line-review flow.

## Open questions
- **Final thresholds** — RESOLVED: `large`=500, `oversized`=5,000 changed lines
  (the operator's 20,000 was the conservative alternative; 5,000 chosen because
  nobody line-reviews a 5,000-line diff and the cost difference is negligible).
  Metric = changed (`+`/`-`) lines.
- **Oversized risk score** — leave un-scored (proposed) or record a sentinel
  `security-risk` (e.g. `none` with reason "oversized: not scored")? Un-scored
  keeps the audit honest; a sentinel makes the ledger uniform.
- **Re-score the already-scored 100?** The prior run's `errored 1` oversized row
  should be superseded once the rule lands; trivial.

## Out of scope (the actual cost dial — decide separately)
- **Model** (Opus vs Sonnet), **scope** (whole-corpus vs residue), and **rubric
  caching** (subscription `claude -p` vs the API `anthropic_call` backend with
  `cache_control`). These move the ~$1k figure; this plan does not.

## Back brief
Before executing: reviewability is a DETERMINISTIC observation alongside the
category (not a new LLM axis, not a category value); the diff cap is shared by both
LLM passes and never silent; `oversized` skips the LLM entirely; and the plan is
about correctness + review-UX + killing the context error, explicitly NOT about
the headline corpus cost (that is the model/scope/caching decision).
