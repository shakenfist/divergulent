# Phase 4 (sub-plan) — a security-risk gate as a new prioritisation axis

Part of [PLAN-patch-classification.md](PLAN-patch-classification.md), extending the
phase-4 [LLM triage tier](PLAN-patch-classification-phase-04-llm-triage.md). This
adds a **new axis** — an estimate of a patch's *security risk* — that **reorders**
the existing pipeline so the scariest carried patches reach the expensive
category pass and the human first. It does **not** replace the category taxonomy.

**Status: R1–R3 implemented.** The gate + recording (R1), the cascade driver +
security-safe cull (R2), and the prioritisation wiring + CLI + docs (R3) are
built and tested offline. **R4** (a larger hand-labelled validation to firm the
threshold and the model default) remains, as the operator-budgeted step. Backed
by the bake-off below.

## Why

The expensive LLM work and the human's attention are the scarce resources. Today
they are spent in priority order by *blast radius* (occurrence count), which says
nothing about *security*. The counterexample-gate finding showed structure does
not predict the semantic category — but the one judgement that is both hard and
high-value is "could this patch hurt me, security-wise?". A cheap, claim-blind
**risk score** lets us spend the expensive passes highest-risk-first.

This is a cascade: cheap filters first, expensive last.

```
deterministic rules  →  risk gate (cheap LLM)  →  category pass + human
(cull provably-benign)   (score the rest)         (run highest-risk-first)
```

## Evidence (bake-off, 2026-06-26)

Coarse 4-level gate (`none/low/elevated/high`), claim-blind, run via the
cost-stripped `claude -p` invocation (`--tools "" --strict-mcp-config
--setting-sources ""`) against real corpus patches, with the existing verified
`security` verdicts as weak positives and `documentation`/`packaging` as
negatives:

- **The gate separates security from benign cleanly.** With a refined rubric
  ("use *elevated* generously for any security-sensitive surface; reserve *high*
  for a plausible vulnerability"), **Opus scored 100% of `security` patches
  ≥elevated and 0% of benign (doc/packaging) ≥elevated (AUC 1.000)**; Sonnet 73%
  recall / 3% false-alarm (AUC 0.949). Documentation lands 93% `none` for both.
- **Model matters for recall.** Opus and Sonnet never differ by >1 level
  (within-1 = 100%), but Sonnet misses ~27% of security at the ≥elevated cut.
  For a security gate, recall is the metric you cannot trade away → **Opus**.
- **Avoid Haiku.** It emitted ~1,600 hidden *thinking* tokens/call (visible answer
  ~40), making it 3× Sonnet's cost *and* less reliable (over-flagged a benign
  build script). Sonnet/Opus respected the "≤20 words" instruction (~30–76 out).
- **Cost is affordable, one-time.** At the stripped-down sizes a gate call is
  ~$0.003–0.03 (clean-sequential vs concurrent/large-diff). The gate scores the
  **whole corpus** (~60k unique fingerprints), not just the residue, because a
  settled `packaging` patch can still be security-relevant; the deterministic cull
  carves off ~7% (mostly doc-only), leaving ~56k LLM calls -- **~$340 Opus / ~$170
  Sonnet at API rates**, i.e. *quota, not cash* on subscription, one-time.
- **`dangerous-construct` is NOT a risk proxy.** Many flagged constructs are
  benign build-script shell-outs, which Opus correctly rated low. Keep the two
  signals separate.

### Recalibration to prompt v2 (2026-06-26)

The v1 rubric above looked perfect on the bake-off because it only measured the
two *extremes* (`security` vs `doc`/`packaging`) and never the ordinary-code
*middle*. In use, the operator saw the head of the `risk --limit` queue come back
~80% `elevated`. A second bake-off (v1 vs a candidate v2, same Opus model, across
**all** strata incl. a random `unknown`/`bugfix`/`feature` middle) found:

- **The skew was a *slice* artifact, not miscalibration.** With nothing scored
  yet, the queue orders by `dangerous-construct` then occurrence, so `--limit`
  hits the genuinely-scariest head (every item `dc=1`): ~54–58% ≥elevated under
  *both* prompts. On a random middle the rate is only **~18%** (doc→`none`,
  packaging→`low`) — the gate is well-calibrated there. The operator was looking
  at the scary end of the queue, on a noisy run (v1's "round up when unsure"
  turns model uncertainty into a pile on the `elevated` bucket).
- **v1 keys on the *surface*, which mislabels both ways.** On the labelled
  positives v1 scored **two real `security` patches `low`** (8/10 recall) while
  over-using `high`. **v2 keys on what the *change does*** (alters a security
  mechanism), not which file it sits in: recall **10/10 ≥elevated**, `high`
  reserved (slice high 8→4), middle/negatives unchanged. Adopted as
  `RISK_PROMPT_VERSION = 2`. The score is advisory (reorder-only), so a model/
  prompt swap just supersedes the prior observation and re-scores.
- **`dangerous-construct` already pre-selects the head**, so the gate earns its
  keep mainly on the *non*-flagged bulk (separating the ~18% that touch a real
  mechanism), not on the dangerous-construct slice where the two signals agree.

## Objective

- A cheap, claim-blind **risk gate** that assigns each patch a coarse ordinal
  security-risk level (`none/low/elevated/high`) + a one-line reason, recall-biased.
- It is **advisory**: it *reorders* the human/category queue, it is not itself a
  verdict — so, unlike the category pass, it needs **no adversarial verify**.
- Its result is recorded with full **provenance** — `(model, risk_prompt_version)`
  — and is **supersedable**, exactly like the LLM triage decisions, so a model
  swap or prompt tweak is a new identity and old scores can be re-scored.
- It slots **between** the deterministic rules and the LLM-category pass; nothing
  in the existing category/verify/human machinery is removed.

## Design decisions

### The scale: a coarse 4-level ordinal, recall-biased
`none / low / elevated / high` (stored 0–3). Not a continuous 0.00–1.00 — LLMs are
poorly calibrated on fake-precise probabilities, and a coarse ordinal is honest,
rankable, and cheap to emit. The validated rubric makes `elevated` fire on any
security-sensitive *surface* (memory, input parsing, auth, crypto, privilege,
network, build hardening) even when benign-looking, and reserves `high` for a
*plausible* vulnerability; "prefer the higher level when unsure" (a missed risky
patch is worse than an over-flagged benign one). Output is `{"risk": "...",
"reason": "<=20 words"}`, parsed by the existing robust parser (no `--json-schema`).

### Model: Opus, advisory and unverified
The bake-off makes Opus the pick (100% recall / 0% false-alarm on the sample vs
Sonnet's 73%/3%). Because the score only *reorders* a queue a human still works,
it does **not** need the adversarial second pass — that is what licenses using one
cheap call. Sonnet stays available as a cost-sensitive fallback (accepting ~27%
miss at the threshold); the model is recorded so the choice is auditable and
swappable. (Sample is small and the `security`-category ground truth is itself
LLM-derived — see Open questions — so the model default is revisitable.)

### Where it records: a `security-risk` observation in the ledger
The risk level is a **signal, not a category verdict**, so it fits the existing
**observation** shape (like `dangerous-construct`), not a `decision`:

- `kind='security-risk'`, `detail=<level>` (e.g. `'elevated'`),
  `evidence=<reason>`, `observed_by='risk-gate:<model>'`,
  `rule_version=RISK_PROMPT_VERSION`, append-only and **supersede-on-change** —
  reusing `append_observation` / `live_observations` / the supersede path.
- This gives the `(model, prompt_version)` provenance the operator asked for,
  identical in spirit to `decided_by='llm-triage:<model>'` /
  `rule_version=<prompt_version>` on the triage decisions, and makes a model/prompt
  bump a new identity whose old scores can be superseded and re-scored.

### It feeds prioritisation, not the verdict
The current verdict precedence (`human > verified-llm > heuristic >
unverified-llm`) is untouched — a risk observation never changes a category. What
it changes is **order**: the triage driver's work-list and the review queue
priority incorporate the risk level (highest risk first), so the expensive
category pass and the human reviewer reach risky patches first under a bounded
budget. (`triage_driver._priority_key` and the `review_queue.priority` are the
seams.)

### The deterministic cull must be security-SAFE (narrower than "packaging")
The first cascade stage culls *provably* benign patches so the gate never runs on
them: FSF-address changes, pure whitespace, translations (`*.po`/`*.pot`),
changelog/copyright-only. It must **not** cull "packaging" wholesale — a
`debian/rules` change can flip a build-hardening flag (RELRO, stack-protector,
PIE, fortify), which is security-relevant. So the risk-cull predicate is its own
conservative rule, distinct from the packaging *category* rule.

### Reorder vs gate (the product dial — start with reorder)
Two ends of a spectrum:
- **Reorder** (safe, default): the gate sets priority; the category+human passes
  still process everything, just risky-first. A false `low` only *delays* a patch.
- **Gate** (cheaper, sharper): `risk < elevated` skips the expensive category pass
  (structural category only); only `≥elevated` gets the full treatment + human. A
  false `low` now *skips* a patch — so this leans hard on the recall bias.

Start with **reorder** (no patch is ever dropped); revisit gating once the gate's
recall is trusted on a larger labelled set.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| R1 | med | opus | none | **The gate + its prompt + ledger recording.** Add `risk_gate.py` (or extend `triage.py`): `RISK_PROMPT_VERSION`, the validated v2 system prompt, `risk_system_prompt()` / `risk_user_message(diff)`, a `score_risk(diff, *, call, model) -> RiskScore(level, reason, usage)` over the same injected `call` boundary, parsed by the robust parser (recall-biased; no `--json-schema`). Record as a `security-risk` observation keyed `observed_by='risk-gate:<model>'` / `rule_version=RISK_PROMPT_VERSION`, supersede-on-change. Offline tests with an injected fake call. One commit. |
| R2 | med | opus | none | **The cascade driver + safe cull.** A bounded `run_risk_gate` (mirroring `run_triage`): pull un-scored fingerprints, apply the **security-safe deterministic cull** (mark culled ones `none` deterministically, no LLM), score the rest, record observations, sum cost/cache telemetry into a run report. Reuse the cost-stripped `claude -p` backend. One commit. |
| R3 | med | opus | none | **Wire risk into prioritisation.** Fold the risk level into `triage_driver._priority_key` and the `review_queue` priority so the category pass and human review run highest-risk-first. CLI flag to choose reorder (default) vs gate. Tests assert ordering. Update README/AGENTS/ARCHITECTURE + the runbook. One commit. |
| R4 | low | opus | none | **Validate + tune on a larger labelled sample.** Re-run the bake-off harness at larger N with a hand-checked label set; confirm the recall/false-alarm at the ≥elevated threshold, finalise the model default and the threshold, record the numbers in the phase-4 findings. (Operator-budgeted LLM spend.) |

R1 → R2 → R3 → R4. Everything offline-tested (injected fake gate `call`); the real
LLM pass is the operator's budgeted step.

## Testing requirements

- The gate `call` is injected; tests run offline against a fake returning a canned
  level — no real `claude -p`, no network.
- The security-safe cull is unit-tested (FSF/whitespace/translation → culled;
  `debian/rules` hardening change → NOT culled).
- A `security-risk` observation records the level + reason + `(model,
  prompt_version)` and supersedes a prior one on a version change.
- Prioritisation: a seeded mix orders highest-risk-first; reorder never drops a
  patch.
- House style; `pre-commit run --all-files`.

## Success criteria

- Each residual patch carries a recorded, provenance-stamped `security-risk`
  observation; the category pass + human review reach high-risk patches first.
- On a validation sample the gate's recall at ≥elevated is high (bake-off: Opus
  100% / Sonnet 73%) with a low benign false-alarm rate (Opus 0% / Sonnet 3%).
- No regression to the verdict precedence or the category/verify/human path; the
  gate is advisory.
- Cost is bounded and reported (Cost & cache telemetry), a one-time low-hundreds-
  of-dollars-at-rates pass on subscription.

## Open questions

- **Ground truth.** The bake-off used the LLM-derived `security` category as the
  positive label (mild circularity) on a small sample. R4 needs a hand-checked set
  to firm the recall/threshold and the model default.
- **Reorder vs gate.** Start reorder; decide later whether `<elevated` may skip the
  expensive pass (depends on trusting recall).
- **Score the author's claim too?** A high content-risk score against an author
  who calls it trivial is the loudest signal — worth surfacing the claim-vs-risk
  mismatch (the claim is already parsed).
- **Negative vs relevant.** This scores *negative* impact (could harm). A patch
  that *fixes* a vulnerability is also review-worthy but scores low here; decide if
  a separate "security-relevant" flag is wanted.
- **Haiku thinking tokens.** If they can be disabled, Haiku might become the
  cheapest gate; worth a quick check before settling on Opus for cost-sensitive
  runs.

## Out of scope

- Replacing the category taxonomy — this is an *added* axis; categories still
  explain "what the drift is made of".
- An adversarial verify for the gate — it is advisory by design.
- A continuous 0–1 probability — coarse ordinal only.

## Back brief

Before executing, back brief the operator: the gate is a new *advisory*
prioritisation axis (not a verdict, no verify), recorded as a supersedable
`security-risk` observation with `(model, prompt_version)` provenance; it reorders
(does not drop) the existing pipeline; the deterministic cull must be
security-safe (narrower than the packaging category); and the model/threshold are
set from a validation run, defaulting to Opus on the current evidence.
