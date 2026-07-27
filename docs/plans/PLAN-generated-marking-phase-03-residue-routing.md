# Generated-marking phase 3 — residue-first routing

Phases 1–2 built the scanner and put the mark in the ledger: 442
fingerprints carry a live `generated-content` observation whose
evidence includes the residue arithmetic. Nothing consumes it yet.
Phase 3 is the payoff: the LLM passes read the hand-written residue
first, and an `oversized` patch whose residue is small stops being
auto-routed to a human as `unknown` — the gatos dead end this whole
plan exists to fix.

Three changes, all routing, no new rule:

1. **The oversized unlock.** The triage driver and the risk gate
   skip only those `oversized` fingerprints whose *residue* is also
   past the oversized threshold (or which carry no mark). An
   `oversized` patch with a small hand-written residue becomes
   triageable.
2. **Residue-first projection.** For any fingerprint with a live
   mark, the diff text fed to triage (draft + verify) and the risk
   gate is reordered: residue file segments first and verbatim, then
   one explicit note per marked file ("`configure`: 19,258 changed
   lines, marked generated (name+banner, autoconf 2.59), not
   shown"). Loud, never silent — the model is told what it is not
   seeing, and the existing character cap applies *after*
   projection, so the cap now spends its budget on signal.
3. **Targeted re-risk.** The marked patches whose current
   `security-risk` score was computed from a truncated,
   generated-noise head (gatos scored `elevated` off 40k chars of
   `configure`) are re-scored through the projecting gate, and the
   review queue reprioritised.

## Design decisions

### Reviewability stays honest; routing composes two observations

The size axis keeps measuring the total diff — a 47k-line patch *is*
structurally oversized, and its observation does not change. The
unlock is a routing decision that composes two live observations:
`reviewability=oversized` AND the mark's `residue_changed`. The
composition lives in one place — a helper beside the mark's other
consumers in `generated.py` (working name
`residue_unlocked_fingerprints(conn)`): the set of oversized
fingerprints whose mark reports
`residue_changed <= REVIEWABILITY_OVERSIZED_LINES`. Both consumers
(triage driver, risk gate) subtract the same set, so they can never
disagree about who is unlocked.

Honest consequence of the threshold: of the generated-dominated
population, at least one patch stays locked — gatos's sibling
`0002-Massive-cleanup-for-libtools.patch` has residue 5,410, over
the 5,000-line oversized cut — and that is correct: a 5,410-line
residue is not line-reviewable either. The unlock never pretends
otherwise.

### Projection is pure, uniform, and applied before the cap

`project_residue_first(body, files)` in `generated.py`: a pure
function taking the patch body and the mark's per-file evidence,
returning the projected text plus the omitted-file summary the
caller records. Segmentation must reuse the same walk `scan()` uses
so file boundaries can never disagree with the mark. Shape:

- a one-line preamble: how many files are deferred and how many
  changed lines they carry ("N generated-claiming files not shown
  (X of Y changed lines); hand-written residue follows");
- every unmarked file's segment, verbatim, in diff order;
- one note line per marked file: path, changed lines, signals, and
  generator/version where captured.

Projection applies to **every fingerprint with a live mark** that
reaches an LLM pass — not just the unlocked ones. A `large` marked
patch is scored today with its cap reading generated noise; after
this phase its cap reads residue. One uniform rule, recorded in
evidence, rather than a threshold-dependent input format.

The injection tripwire is untouched: it scans the **full** body,
because generated segments are exactly where hidden text would hide,
and its diff-region skip continues to outrank the unlock (an
injection-suspect patch never reaches the LLM, marked or not).

### Provenance: the model's input is never silently modified

Triage decision evidence and the risk observation payload gain the
projection facts: `projected: true`, files omitted, changed lines
omitted. The existing `truncated` flag keeps its meaning (the cap
bit after projection). A reviewer reading a draft verdict can always
reconstruct what the model was shown.

### Targeted re-risk, bounded and supersedable

The re-risk population is precise: fingerprints with a live mark
whose live `security-risk` observation records `truncated: true` —
exactly the scores computed from a generated head. The gate gains a
bounded re-run path that supersedes those observations and re-scores
through the (now projecting) normal flow, then the existing
`reprioritise_review_queue` re-stamps pending items. Expected n is
a few dozen; at measured Opus per-call cost this is under a couple
of dollars. No score is deleted — superseded rows remain the audit
trail, per ledger rules.

## Steps

| Step | Effort | Model | Brief |
|------|--------|-------|-------|
| S1 | high | opus | **Projection + unlock helpers, tests.** In `generated.py`: `project_residue_first(body, files)` returning a small frozen result (projected text, omitted file count, omitted changed lines) with the shape above, reusing the module's own region/section walk for segmentation; `residue_unlocked_fingerprints(conn)` composing `reviewability.oversized_fingerprints` with `generated_marks` per the design (lazy imports, mirroring the module's existing conn helpers). Tests: gatos-shaped fixture (residue segments verbatim and first, per-file notes with generator/version, preamble counts correct); a no-mark body projects to itself untouched (identity — callers need not special-case); unlock set composition against a temp ledger (oversized+small-residue in, oversized+big-residue out, oversized+no-mark out, large+marked not in — it was never locked). One commit. |
| S2 | high | opus | **Triage driver unlock + projection.** In `triage_driver.py`: subtract `residue_unlocked_fingerprints` from the oversized skip set (the routed-to-human reason and stats stay for the still-locked); new stat + summary line for unlocked-by-residue. Where the driver hands the body to the draft and verify calls, project first when the fingerprint has a live mark (thread the marks dict, computed once per run, not per call) and fold the projection facts into the decision evidence. Offline tests with the fake `call`: an unlocked oversized fingerprint reaches the LLM and its input starts with the preamble and residue (assert the fake call's captured prompt), a locked one still routes to human, an unmarked fingerprint's input is byte-identical to today, evidence carries the projection facts. One commit. |
| S3 | high | opus | **Risk gate projection + targeted re-risk.** In `risk.py`: project (same helper, same marks-dict threading) before `cap_diff` in `score_risk`'s caller path; payload gains the projection facts. Add the bounded re-risk path: select live-mark fingerprints whose live `security-risk` evidence records `truncated: true`, supersede and re-score them through the normal gate (flag-gated — the default run's behaviour is unchanged apart from projection), then `reprioritise_review_queue`. Stats + loud summary. Offline tests: projected input reaches the fake call, re-risk supersedes exactly the marked+truncated population and no more, reprioritisation runs, default run untouched otherwise. One commit. |
| S4 | low | sonnet | **Docs.** `docs/workflow.md`: the triage and risk stages now consume the mark (unlock + projection + re-risk); `ARCHITECTURE.md`: `generated.py` bullet (phase 3 consumers), `triage_driver.py`/`risk.py` descriptions; `docs/deterministic-rules.md`: the generated-content entry's "recorded but not yet consumed" language becomes a short "consumed by" paragraph. Master plan + index status updates ride with this commit. One commit. |
| S5 | — | management | **The real runs.** From this worktree against the reviews root: (1) the targeted re-risk of the marked+truncated population (expected a few dozen Opus calls); (2) a triage pass over the newly unlocked oversized patches — the moment gatos finally gets a draft verdict. Record the outcomes (unlock count, re-risk score movements, gatos's draft) in the master plan's execution table; the reviews-repo export/commit stays the operator's publish gate. |

## Testing requirements

- Projection: residue-first shape pinned; identity on unmarked
  bodies; segmentation agrees with `scan()` on the same fixture
  (same file boundaries — a joint test, not two independent ones).
- Unlock: threshold edge (residue exactly at the oversized cut),
  no-mark oversized stays locked, malformed-evidence marks are
  treated as no-mark (the defensive `generated_marks` contract).
- Both consumers use the shared helper — a grep-level assertion
  that neither reimplements the composition.
- Evidence: projection facts present when projected, absent when
  not; `truncated` retains its post-projection meaning.
- Re-risk: population selection exact; superseded rows preserved;
  idempotent (a second re-risk run finds nothing marked+truncated).
- `pre-commit run --all-files` green per step; no real LLM in any
  test (fake `call` throughout).

## Success criteria

- After S5: the unlocked oversized patches carry LLM draft
  verdicts; gatos specifically has a draft category and a risk
  score computed from its 603-line residue with the omission noted
  in evidence; the still-locked (residue > 5,000) population still
  routes to humans with the honest reason.
- No change to any observation's meaning: reviewability still
  measures the total; the mark is still never a verdict; the
  injection skip still outranks everything.
- An unmarked fingerprint's LLM input is byte-identical to before
  this phase.
- The re-risked scores supersede (never overwrite) their
  predecessors, and the review queue order reflects the new scores.

## Out of scope

- Review UI badges/collapse — phase 4.
- Any scanner or threshold tuning (the 5,000-line cut is
  reviewability's, not this phase's to move).
- Dangerous-construct attribution changes (the 128 shell-out rows
  on gatos remain true statements; the phase-4 UI may annotate
  them, routing does not).
- The reviews-repo publish gate.

## Back brief

Before executing: this phase changes *routing only* — which
fingerprints reach the LLM and what text they are shown — composing
two existing observations through one shared helper. Reviewability
keeps its meaning; the mark stays a mark; every projection is
recorded in evidence; the still-unreviewable stay honestly routed
to humans; and the model is always told what it is not being shown.
