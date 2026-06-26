# Phase 4 findings: operating the LLM triage + human review tier

Results and decisions from running the phase-4 pipeline over the corpus, for
[PLAN-patch-classification-phase-04-llm-triage.md](PLAN-patch-classification-phase-04-llm-triage.md).
Unlike phases 1–3, phase 4 has an **operational** half (spending real
LLM/review budget) that accrues over many sessions; this note records what that
operating has taught us so far and the tooling it forced.

## Headline

> **The substantive residue is irreducibly *semantic*. One structural class
> (`test-only`) was the single deterministic win; everything else genuinely
> needs the LLM/human tier.**

Profiling all **42,909** substantive fingerprints by structural shape (file
types touched + shape) showed that **every** bucket's verified verdicts span
multiple categories — `single-file:code` (35% of residue) is bugfix *and*
packaging *and* security *and* documentation *and* feature. There is no coarse
structural rule that cleanly separates `bugfix` / `feature` / `security`,
because those describe *intent*, not structure. So the LLM/human tier is not a
stopgap to be rules-engineered away — it is doing the real work, and the
deterministic tier can only peel off what structure genuinely determines.

## The one deterministic win: `test-only` → `test`

The exception the profiling found: **~15% of the residue touches only test
files**, and a change that touches only tests cannot alter the shipped
artifact. That *is* structurally determined, so it became a deterministic rule
(`rules._rule_test_only`) producing a new `test` category (CATEGORY_ENUM v2).
Validated against a copy of the live ledger, applying it reclassified:

| | before | after |
| --- | ---: | ---: |
| `unknown` (residue) | 42,347 | 35,978 |
| `test` (new) | 0 | 6,369 |

6,433 `substantive` decisions superseded; **zero** fingerprints left with two
live heuristic decisions; all `llm`/`human` verdicts preserved. (The 64-row gap
between 6,433 reclassified and 6,369 `test` *verdicts* is correct precedence:
those 64 already had a higher-ranked LLM/human verdict and keep it.)

Translation-only (0.4%) and symbols-only (<0.1%) are also structurally clean but
too small to be worth a rule. We explicitly **rejected** an "occurrence →
benign" rule: 99.2% of fingerprints occur once, the tail is tiny and dominated
by single-team boilerplate, and high occurrence means high *blast radius* — the
wrong thing to de-prioritise.

## Tooling the operational run forced

Operating the pipeline for real surfaced gaps the offline suite could not:

- **Original-source context** fetched per touched file by its real path (not the
  patch filename), with epoch-stripped version fallback — the review diff is
  shown against the actual upstream file.
- **Sigstore** now authenticates **once per session** and **refreshes on token
  expiry** (a long read no longer loses a verdict at sign time).
- **Review UX**: a pager for big diffs; the carrying **package name(s)** shown
  (blast radius); `requeue` (re-open a fingerprint) and `history` (reconsider a
  past verdict) subcommands.
- **Local web review UI** (`review_web.py`): several CLI review sessions made the
  case for a browser. The **"Review-tool form" open question is resolved — both
  ship**: the web UI reuses the CLI's read + signed-verdict paths verbatim (web
  and CLI verdicts are byte-identical), and adds the slices the linear queue
  cannot — review **by category**, **cherry-pick by fingerprint**, and an
  **audit/spot-check view** over settled patches *not* in the queue (to confirm a
  deterministic rule is right, and re-queue a misfire without recording a
  decision). Flask + Jinja2 live behind a new `review` extra (off the default
  scan/report path). Specced + built in
  [PLAN-patch-classification-phase-04-review-web.md](PLAN-patch-classification-phase-04-review-web.md).
- **Triage backend caching + cost telemetry** (`triage.py`/`triage_driver.py`):
  the model-call boundary is now `call(system, user, *, model, schema) ->
  CallResult(text, usage)` -- the static rubric is a **cached system prompt**
  (relocated verbatim, so verdicts and `prompt_version` are unchanged) and the
  diff is the variable user message, so the rubric is billed once per run instead
  of on every one of the ~2N calls. The default `claude -p` backend uses
  `--system-prompt` + `--json-schema` (enforced structured output) +
  `--output-format json`, capturing the token-usage block and `total_cost_usd` --
  **no new dependency, still subscription-billed**. Each run now reports a **Cost
  & cache** section (tokens, cache-hit ratio, reported + at-API-rates cost per run
  and per patch). We chose this over the Claude Agent SDK (which wraps the CLI we
  already use, with opaque caching/usage and an agentic shape we do not want); see
  [PLAN-patch-classification-phase-04-triage-backend.md](PLAN-patch-classification-phase-04-triage-backend.md).
  - **Cost lever found while reading the telemetry**: an early run reported an
    82.5% cache-hit ratio but a ~$0.14/patch at-rates cost, dominated by
    `cache_creation`. Measuring showed `claude -p` was caching the *whole* prompt
    -- including ~17k tokens of built-in **tool definitions** we never use, plus
    the per-patch diff and (briefly) the `--json-schema` scaffolding -- and
    re-*writing* the volatile parts (at ~2x) every call, never recouping them.
    Fix: run with **`--tools "" --strict-mcp-config --setting-sources ""`** and
    **drop `--json-schema`** (the robust parser already degrades malformed output
    safely to needs_human). Those flags strip everything Claude Code injects that
    a one-shot classification does not use -- built-in tools (~17k tokens/call),
    local MCP, and project/global `CLAUDE.md` + settings (~2.8k tokens/call) --
    shrinking each request from **~66k → ~640 tokens** (rubric + diff as plain
    input, zero `cache_creation`): ~100x less input, API-level token efficiency
    **on the subscription path**, no new dependency. The remaining lever is
    **output verbosity** (the model writing well past the one-or-two-sentence
    reasoning asked for -- output bills at ~5x input); after that it is
    diminishing returns short of a model-tier change (e.g. Haiku for the bulk
    draft), which is a quality decision, not a free win.
- **Ledger safety**: `build` now refuses to silently wipe a populated ledger;
  **`ledger record`** applies new/changed rules to an existing ledger
  non-destructively (append-only re-record + supersede-on-change), so a rule
  added mid-operation never costs the LLM/human work already banked.

## Operational state (accruing)

As of this writing, of the substantive residue the LLM has verified ~454
decisions and a human has signed ~20; the bulk remains untriaged. This is the
budgeted grind that continues across sessions — `triage` a bounded slice, then
`review` the routed items — shrinking the residue while the deterministic tier
holds it at the structural floor.

## What remains in phase 4

- Apply `test-only` to the production ledger (`ledger record`), dropping the
  residue ~42k → ~36k.
- Continue the triage + review loop to whatever coverage the operator wants.
- The candidate-rule miner now has a **counterexample gate** (done): it surfaces
  a `(category, structural key)` cluster only when that key carries ONE category
  across the settled population (current verdicts, so a human override counts),
  and otherwise REFUSES it with the conflicting spread. Run against the production
  ledger this collapsed the candidate list to **0 sound rules, 15 refused** --
  e.g. `types={code};shape=code-only` spans 6 categories (bugfix 868, packaging
  136, security 131, feature 91, documentation 70, test 3), so it is not a rule.
  This confirms the structural signature does not determine the semantic category:
  the deterministic tier can only peel *structural* categories (`test-only →
  test`), and the residue genuinely needs the LLM/human tiers.
