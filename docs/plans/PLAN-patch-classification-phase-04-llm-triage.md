# Phase 4 — LLM triage tier (curation-side, verified)

Part of [PLAN-patch-classification.md](PLAN-patch-classification.md).
Plan this phase at **high effort**: it introduces the one non-deterministic,
non-free, off-box decider in the whole system, and the project's
trustworthiness depends on it being *bounded*, *verified*, and *never run on a
client*.

**Status: implemented (operational run pending).** All steps are built,
tested, and committed: 4a the claim-blind LLM boundary (`triage.py`, default
`claude -p` backend), 4b the adversarial verifier + routing, 4c the ledger
integration (schema v2: `verified` flag, signature columns, review queue; the
`human > verified-llm > heuristic > unverified-llm` precedence), 4d the bounded
prioritised driver + rule-discovery report, and 4e the local Sigstore-signed
human-review CLI showing diffs in sources.debian.org original-source context.
The suite is fully offline (injected LLM/signer/fetch). **The actual triage +
review pass is the operator's separate, budgeted step** — run
`python -m divergulent.classify.triage` over a small `--limit` slice (it spends
subscription/API budget), then `python -m divergulent.classify.review` to
sign-off the routed items; that produces the phase-4 findings.

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
A `triage(diff, *, call, model, prompt_version) -> LlmVerdict(category,
reasoning, confidence)` boundary, **blind to the author's claim** (the DEP-3
header is stripped before the diff is sent — the LLM analyses *content*, exactly
as the deterministic content rules do). The `call` is injected, so the model
backend is swappable and tests run offline against a fake. Two real backends
ship, both curation-side, neither a runtime import:

- **`claude -p` (default).** Shell out to the local Claude Code CLI in print
  mode, so triage is billed against the operator's **Claude subscription**
  rather than separate API calls. This needs **no Python dependency at all** —
  just the `claude` CLI on `PATH` — which suits divergulent's
  dependency-minimalism best, so it is the default.
- **Anthropic API (optional alternative).** For separately-billed API use, an
  `anthropic`-SDK backend behind an **optional extra**
  (`pip install divergulent[triage]`), imported lazily exactly like
  `verify.py`/sigstore.

Either way the runtime never triages — clients consume a signed bundle
(phase 5) and never call an LLM. The injected fake keeps the suite offline and
free.

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

### A human verdict is a *signed* ManualDecision — non-repudiation
A `kind='human'` decision is the top of the precedence, so it is the most
trusted verdict in the system; it should also be the most *accountable*. A
human review is recorded as a **ManualDecision carrying a signature** over the
decision (fingerprint, category, what was reviewed, timestamp) bound to the
reviewer's identity — **non-repudiation**: the reviewer cannot later deny the
call, and any consumer can verify the human review is authentic and by whom.
This reuses the project's **existing Sigstore posture** (the cache bundle is
already Sigstore-signed; `sigstore` is already an optional extra), so a reviewer
signs with their keyless OIDC identity and the `decision` row gains a
`signature` + verified `signed_by` identity. The published classification bundle
(phase 5) can then *prove* "this patch was human-reviewed by ⟨identity⟩ on
⟨date⟩" — a strong, checkable trust signal, not just a flag.

### Human review runs locally and interactively — *not* in CI
The deterministic tiers and the LLM triage are batch, curation-side, and belong
in CI; **human review is interactive and identity-bound and does not**. A
GitHub Actions job cannot sit a person in front of a diff, and the reviewer's
signing identity lives on *their* machine, not in CI. So the work splits
cleanly:

- **CI / curation-side (batch):** run the deterministic + LLM tiers, build and
  publish the **prioritised human-review queue** (high-value, not-yet-reviewed
  fingerprints — review-flagged, dangerous-construct, high-occurrence first).
- **Local (the reviewer's machine, interactive):** a small **review tool**
  pulls the next high-value un-reviewed item, shows the diff + the LLM draft +
  the author's claim + any flags, takes the human's verdict (accept the LLM /
  override / `unknown` / defer), and records a **signed ManualDecision** into
  the ledger.

This is the clean answer to "awkward in GitHub Actions": don't do it there. The
review tool starts as a **local CLI** (`python -m divergulent.classify.review`)
— dependency-free, fits the curation-CLI posture, runs where the identity is —
and can grow into a **tiny local web UI** (stdlib `http.server`, no dependency)
if reading diffs in a browser is nicer at volume. Either way it is local,
never a runtime client feature.

### The reviewer sees the diff *in the context of the original code*
A unified diff's two or three lines of context are not enough to judge what a
change really *does*. So the review tool **fetches the original upstream source**
for the file(s) the patch touches and renders the diff **in context** (the
surrounding original code, ideally the whole file) — what the human actually
needs to make a confident call. This is **on-demand, per reviewed item** (a
human looks at a handful at a time), which is exactly why it is cheap and why it
**does not undo the bulk corpus's deliberate skip of `.orig`**: the 60k-package
crawl stays lean; only the few patches under active review pull their original
source. The fetch reuses the existing apt-source / sources.debian.org access
(reconstructing the file state the patch applies against — `.orig` plus the
earlier series patches), and is local, on-demand, and curation-side like the
rest of review.

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
| 4a | high | opus | none | Add `divergulent/classify/triage.py`: the injectable LLM boundary. `LlmVerdict` dataclass (category, reasoning, confidence, model, prompt_version, raw_response); `triage(patch, *, call, ...) -> LlmVerdict` that strips the DEP-3 header (claim-blind) and prompts for a category from the diff alone; `PROMPT_VERSION` constant. TWO backends: `claude_cli_call` (default — shell out to `claude -p`, subscription-billed, NO Python dependency) and `anthropic_call` (optional API alternative behind the `triage` extra, lazy import + clear error, mirroring `verify.py`/sigstore). Tests inject a fake `call` (offline): claim-blindness (the header is not in the prompt), verdict parsing, the absent-extra path, and the `claude -p` subprocess invocation (mock `subprocess.run`). |
| 4b | high | opus | none | Add the **verifier**: an adversarial `verify(diff, draft, *, call=...) -> Verification(agrees, reasoning)` — an independent, claim-blind call prompted to confirm or REFUTE the drafted category, defaulting to refuse when unsure. A `triage_and_verify(...)` that returns a draft + verification + a routing decision: `verified` (agree, high confidence) vs `needs_human` (disagree / low confidence / claim mismatch / dangerous-construct present). Pure given an injected `call`; offline tests for agree→verified, refute→needs_human, and the claim-mismatch and dangerous-construct routing. |
| 4c | high | opus | none | **Ledger integration + precedence.** Add a `verified` notion to the ledger for LLM decisions (schema bump: a `verified` column on `decision`, or a verification record — pick one and version it), a `signature` + `signed_by` column for human decisions (4e), and a human-review queue table. Refine `verdict.current_verdict` precedence to `human > verified-llm > heuristic > unverified-llm` (an unverified LLM decision never wins over a heuristic). A recorder path that appends an LLM decision (`kind='llm'`, `decided_by` = the prompt id, `rule_version` = model+prompt, `evidence` = the model response, `verified` set by 4b) and enqueues `needs_human` items. Tests: a verified LLM decision overrides the heuristic; an unverified one does not; a human confirmation (kind=human) tops both; the queue holds the routed items. |
| 4d | high | opus | none | The **triage driver + rule discovery + findings**, over a *bounded* slice (NOT the whole 43k). A `python -m divergulent.classify.triage` CLI that pulls the queue, orders it (review-flagged, dangerous-construct, high-occurrence first), triages within a `--budget`/`--limit` using the `claude -p` backend by default, records via 4c, and reports: verdicts by category, verified vs human-queued, claim/content mismatches, and **candidate rules** (clusters of identical LLM verdicts on look-alike patches, surfaced for human approval — never auto-applied). Run it over a small, reviewed sample to produce a findings note. Tests use the fake `call`; the run is a reviewed operational step. |
| 4e | high | opus | none | The **local, signed human-review tool** — `python -m divergulent.classify.review`. Pulls the next highest-priority *un-reviewed* item from the human-review queue, **fetches the original upstream source** for the touched file(s) on-demand and shows the diff **in the context of the original code** (reusing the apt-source / sources.debian.org access; reconstruct the pre-patch file state) alongside the LLM draft + the author's claim + flags, takes the human's verdict (accept-LLM / override-category / `unknown` / defer), and records a **signed ManualDecision** (`kind='human'`) into the ledger — reusing the Sigstore signing already in the cache pipeline, so the decision carries a verifiable `signed_by` identity (non-repudiation). Local and interactive (never CI, never a client feature); start as a CLI, with the option to grow into a stdlib `http.server` local web UI. Sign/verify behind the existing `verify` extra; tests inject a fake source-fetch, signer/verifier, and stdin, fully offline. |

4a → 4b → 4c → 4d, with 4e (the local review tool) after 4c (it needs the
human-decision schema). One commit per step. Steps 4a–4c and 4e are fully
offline (injected `call`, fake signer, fake stdin); only 4d's *operational
sample run* spends real subscription/API budget, and that is a reviewed step —
not part of the test suite.

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
  free (no real API call and no real `claude -p` subprocess, ever, in tests —
  the `claude -p` backend is tested with a mocked `subprocess.run`).
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
- A human-review queue for what the verifier/claim/flags surface, drained by a
  **local, interactive** review tool (not CI) that records a **signed
  ManualDecision** (`kind='human'`, Sigstore identity → non-repudiation) topping
  the precedence.
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
- **Signing mechanism for ManualDecisions** — Sigstore keyless (reuses the cache
  pipeline, OIDC identity, no key management) vs a local GPG/SSH signature
  (offline, no browser). Lean Sigstore for consistency; confirm it works for an
  interactive local reviewer, and decide what the signature covers (a canonical
  decision record) and how phase 5 re-verifies it on export.
- **Review-tool form** — start with the local CLI, or go straight to the stdlib
  `http.server` local web UI (nicer diff reading, side-by-side, syntax
  highlighting)? And how to present the LLM draft + claim + flags so the human
  decides fast without anchoring on the LLM.
- **Original-context fetch** — full `apt-get source` + quilt to reconstruct the
  exact pre-patch file state (complete but heavier), vs a targeted
  sources.debian.org fetch of just the touched file(s) at the version (lighter,
  but needs care to land the patch in the right pre-patch context). Either way
  it is on-demand per reviewed item; pick the one that renders trustworthy
  context with the least machinery.

## Out of scope (later phases)

- The **signed classification bundle & client display** (phase 5) — phase 4
  fills the ledger; phase 5 exports the derived view and ships it to clients.
- **External rules** (BTS / upstream cross-reference, phase 6).
- Any client-side LLM use — explicitly never.

## Back brief

Before executing any step, back brief the operator on your understanding of
this phase and how the intended work aligns with it.
