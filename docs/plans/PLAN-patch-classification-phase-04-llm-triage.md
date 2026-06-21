# Phase 4 — LLM triage tier (curation-side, verified)

Part of [PLAN-patch-classification.md](PLAN-patch-classification.md).
Plan this phase at **high effort**: it introduces the one non-deterministic,
non-free, off-box decider in the whole system, and the project's
trustworthiness depends on it being *bounded*, *verified*, and *never run on a
client*.

**Status: not started.**

## Why this is phase 4

Phases 1–3 settled what content can prove deterministically and put the rest in
a derived **queue of 42,907 substantive fingerprints** ([phase-3
findings](PLAN-patch-classification-phase-03-findings.md)) — genuine code
changes whose *category* (bugfix / feature / security) deterministic rules
deliberately do **not** guess. Phase 4 triages that residue with an LLM, but
under strict discipline: the LLM is the **last** tier, **always verified**, its
output is **just another decision in the ledger** (kind `llm`, only winning
once verified), and it doubles as a **rule-discovery tool** so the residue it
must touch shrinks over time. **Clients never run an LLM** — this is
curation-side, like the cache builder.

## Prompt

Explore before changing:
- divergulent/classify/ledger.py — the ledger this writes into. Note the
  reserved `kind='llm'` and `kind='human'` seats, `KIND_PRECEDENCE`
  (human > llm > heuristic), `append_decision`, and the phase-3 open question
  *"verified-LLM representation — define the mechanism in phase 4"*. Decisions
  store `evidence` and an arbitrary `rule_version`.
- divergulent/classify/verdict.py — `current_verdict` (the precedence this
  phase must refine so an *unverified* LLM decision never outranks a heuristic),
  `queue` (the residue this phase consumes).
- divergulent/classify/{claim,content,rules,classify,record}.py — the claim
  (author-controlled), the content profile, and `iter_classified` (per
  fingerprint: claim + profile + verdict + a representative body).
- divergulent/verify.py and pyproject.toml — the **optional-extra** pattern
  (`sigstore` is a `verify` extra, not a core dep). The LLM client must be an
  optional, curation-side dependency the same way; the runtime never imports it.
- [PLAN-patch-classification.md](PLAN-patch-classification.md) "Prefer
  deterministic rules; LLM is the last, verified tier" — the authoritative
  design (cost-ordered sieve, the two bounding dynamics).

Research the Anthropic API (the latest capable Claude model) for the default
backend, but keep the LLM behind an **injectable boundary** so tests run
offline against a fake and the model is swappable.

## Objective

Triage the substantive residue with a verified, curation-side LLM tier that:

- drafts a category from the **diff alone, blind to the author's claim**, then
  compares the draft to the claim (disagreement is the loud signal);
- is **always verified** before it counts — an independent adversarial check,
  and a **human-review queue** for what the verifier or the claim-comparison
  flags;
- records each verdict as a ledger **decision** (`kind='llm'`, the model+prompt
  as its `rule_version`, the full model response as `evidence`), winning over a
  heuristic **only once verified**;
- **discovers rules**: recurring LLM verdicts on look-alike patches become
  candidate deterministic rules (phases 2/3), shrinking the residue each round;
- stays **bounded and honest about cost** — prioritised, budgeted, iterative;
  43k patches cannot all be sent to an LLM cheaply, and the design says so.

## Design decisions

### The LLM is the last tier, behind an injectable, optional boundary
A `triage(diff, *, model, prompt_version) -> LlmVerdict(category, reasoning,
confidence)` boundary, **blind to the author's claim** (the DEP-3 header is
stripped before the diff is sent — the LLM analyses *content*, exactly as the
deterministic content rules do). The default backend calls the Anthropic API;
it is an **optional extra** (`pip install divergulent[triage]`), never a
runtime import — clients consume a signed bundle (phase 5) and never call an
LLM. Tests inject a fake `triage`, so the suite stays offline and free.

### Always verified — adversarial check, then a human queue
An LLM draft does not count until verified. Verification is an **independent
adversarial pass** (a second call, blind, prompted to *confirm or refute* the
drafted category, defaulting to refute when unsure — the "try to break it"
discipline). Agreement → a **verified** LLM decision. Disagreement, low
confidence, a claim/content mismatch, or a live dangerous-construct observation
→ the **human-review queue** (a ledger-backed worklist), where a human's
confirmation is recorded as `kind='human'` (the top precedence). The LLM never
self-certifies, and it never pronounces malice — `security` from an LLM is a
*candidate for human confirmation*, not a verdict.

### "Verified" is explicit, and an unverified LLM never outranks a heuristic
This resolves the phase-3 open question. Add a `verified` flag to LLM
decisions (a column, or a separate confirming record) and **refine the
precedence**: `human > verified-llm > heuristic > unverified-llm`. An
unverified draft is recorded (audit trail, cache) but **must not** win over the
deterministic heuristic — no cry wolf from an unreviewed guess. Only after the
adversarial pass (or a human) does it outrank the heuristic.

### Non-determinism is handled by the ledger, not wished away
An LLM verdict is not reproducible, so: the **full model response is stored as
`evidence`**; the **`rule_version` is `model-id + prompt-version`** (bumping
either is a new rule version → supersede + re-triage, exactly the phase-3
machinery); and the ledger is a **cache** — a fingerprint already decided is
never re-sent. Determinism lives in the deterministic tiers; the LLM tier is
auditable and re-runnable-by-supersession, not free.

### Bounded by prioritisation and rule discovery
43k × (draft + verify) is real money, so the tier is **iterative and
budgeted**, highest-value residue first: review-flagged fingerprints, those
carrying dangerous-construct observations, and **high-occurrence** fingerprints
(one recurring fingerprint stands for many carried patches). The second
bounding dynamic is **rule discovery**: when the LLM gives the same verdict to
a cluster of look-alike patches, surface a **candidate deterministic rule** for
human approval; an approved rule (phases 2/3) peels those off the residue
deterministically, shrinking what the LLM must ever touch again. The plan
tracks and `log()`s what was and was not triaged — no silent caps.

### Privacy is not a concern here, and that is worth stating
The LLM sees **public Debian patch diffs**, centrally, during curation — never
a user's installed-package inventory or any machine state. The off-box decider
touches only public archive data; the client privacy model (phase-5/1.0) is
untouched.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 4a | high | opus | none | Add `divergulent/classify/triage.py`: the injectable LLM boundary. `LlmVerdict` dataclass (category, reasoning, confidence, model, prompt_version); `triage(diff, *, call=...) -> LlmVerdict` that strips the DEP-3 header (claim-blind) and prompts for a category from the diff alone; a default `call` using the Anthropic API behind an **optional extra** (lazy import, clear error if absent — mirror `verify.py`/sigstore); `PROMPT_VERSION` constant. Add the `triage` extra to pyproject.toml. Tests inject a fake `call` (offline): claim-blindness (the header is not in the prompt), the verdict parsing, and the absent-extra path. |
| 4b | high | opus | none | Add the **verifier**: an adversarial `verify(diff, draft, *, call=...) -> Verification(agrees, reasoning)` — an independent, claim-blind call prompted to confirm or REFUTE the drafted category, defaulting to refuse when unsure. A `triage_and_verify(...)` that returns a draft + verification + a routing decision: `verified` (agree, high confidence) vs `needs_human` (disagree / low confidence / claim mismatch / dangerous-construct present). Pure given an injected `call`; offline tests for agree→verified, refute→needs_human, and the claim-mismatch and dangerous-construct routing. |
| 4c | high | opus | none | **Ledger integration + precedence.** Add a `verified` notion to the ledger for LLM decisions (schema bump: a `verified` column on `decision`, or a verification record — pick one and version it) and a human-review queue table. Refine `verdict.current_verdict` precedence to `human > verified-llm > heuristic > unverified-llm` (an unverified LLM decision never wins over a heuristic). A recorder path that appends an LLM decision (`kind='llm'`, `decided_by` = the prompt id, `rule_version` = model+prompt, `evidence` = the model response, `verified` set by 4b) and enqueues `needs_human` items. Tests: a verified LLM decision overrides the heuristic; an unverified one does not; a human confirmation (kind=human) tops both; the queue holds the routed items. |
| 4d | high | opus | none | The **triage driver + rule discovery + findings**, over a *bounded* slice (NOT the whole 43k). A `python -m divergulent.classify.triage` CLI that pulls the queue, orders it (review-flagged, dangerous-construct, high-occurrence first), triages within a `--budget`/`--limit`, records via 4c, and reports: verdicts by category, verified vs human-queued, claim/content mismatches, and **candidate rules** (clusters of identical LLM verdicts on look-alike patches, surfaced for human approval — never auto-applied). Run it over a small, reviewed sample to produce a findings note. Tests use the fake `call`; the run is a reviewed operational step. |

4a → 4b → 4c → 4d. One commit per step. Steps 4a–4c are fully offline
(injected `call`); only 4d's *operational sample run* touches the real API, and
that is a reviewed, budgeted step — not part of the test suite.

## Operational note (cost and scale)

Triaging all 42,907 residue fingerprints at draft+verify is a deliberate,
budgeted decision, not a test side effect — and it should be approached
**iteratively**: triage the highest-value slice, harvest rule candidates,
get them approved, re-run the deterministic tiers (the residue shrinks), and
only then triage the next slice. The plan validates the machinery on a small
sample; the full sweep is the operator's call with eyes on the cost. Every run
`log()`s how many fingerprints were triaged, verified, human-queued, and left
untouched — no silent truncation.

## Testing requirements

- The LLM boundary is **injected** in all tests; the suite stays offline and
  free (no real API calls, ever, in tests).
- Claim-blindness is tested (the author's description never reaches the prompt).
- Verification routing is tested both ways (verified vs human-queue).
- Precedence is tested: unverified-llm < heuristic < verified-llm < human.
- The absent-extra path degrades clearly (like the missing `sigstore` path).
- `pre-commit run --all-files` passes; house style (single quotes, 120 cols).

## Success criteria for this phase

- A curation-side, **optional**, injectable LLM tier that drafts categories
  blind to the claim and is **always verified** before counting.
- LLM verdicts live in the ledger as decisions (`kind='llm'`, response stored
  as evidence, model+prompt as version) and **only win once verified**;
  unverified drafts never outrank a heuristic; humans top all.
- A human-review queue for what the verifier/claim/flags surface.
- Rule-discovery: recurring LLM verdicts surface **candidate** deterministic
  rules (human-approved, never auto-applied) that shrink the residue.
- Bounded and honest: prioritised, budgeted, iterative; no silent caps;
  validated on a reviewed sample, not a blind 43k sweep.
- **Clients run no LLM**; the tier is curation-side and the LLM sees only
  public patch diffs.

## Open questions for this phase

- **Model + prompt** — which Claude model as default, and the category prompt's
  exact shape (it must produce the provisional enum + a confidence, blind to
  the claim). Versioned via `PROMPT_VERSION`.
- **Verification strength** — one adversarial pass, N-of-M voting, or a
  diverse-lens panel (correctness / security / repro) for the riskier residue?
  Start with one refuting pass; escalate for dangerous-construct/security
  candidates.
- **Rule-discovery mechanics** — what counts as a "cluster" worth proposing a
  rule for (same verdict across K look-alike fingerprints), and the human
  approval surface.
- **Budget model** — per-run `--limit`/`--budget`, and how to prioritise across
  occurrence count vs review-flag vs dangerous-construct.
- **Category enum** — does triage want a richer enum than the deterministic
  tiers (e.g. split `security` into sub-kinds)? Keep versioned and provisional.

## Out of scope (later phases)

- The **signed classification bundle & client display** (phase 5) — phase 4
  fills the ledger; phase 5 exports the derived view and ships it to clients.
- **External rules** (BTS / upstream cross-reference, phase 6).
- Any client-side LLM use — explicitly never.

## Back brief

Before executing any step, back brief the operator on your understanding of
this phase and how the intended work aligns with it.
