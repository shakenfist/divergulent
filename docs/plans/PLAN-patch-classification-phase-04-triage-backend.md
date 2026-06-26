# Phase 4 (sub-plan) — LLM triage backend: cached rubric + usage/cost telemetry

Part of [PLAN-patch-classification.md](PLAN-patch-classification.md), reworking the
phase-4 [LLM triage tier](PLAN-patch-classification-phase-04-llm-triage.md)
backend (steps 4a–4c). The triage *logic* — claim-blind draft, adversarial
verify, trust-but-verify routing — is unchanged. What changes is the **transport**:
how the prompt is sent and billed, and what we measure.

**Status: planned (not started).** This document is the proposal for review.

## Why

Today `triage_and_verify` makes **two** `call(prompt, model) -> str` invocations
per patch (claim-blind draft + adversarial verify). The default backend
(`claude_cli_call`) shells out to `claude -p` once per call. Two problems:

1. **The rubric is re-sent and re-billed on every call.** `build_prompt` /
   `build_verify_prompt` put the full category rubric + output schema in the
   *user* prompt, ahead of the diff. The static rubric is the bulk of the input
   and it is paid for on every one of the ~2·N calls in a run.
2. **We measure nothing.** `call` returns a bare string; token counts, cache
   behaviour, and cost are discarded, so we cannot reason about run cost or prove
   a backend change helped.

A handoff from a parallel session proposed moving to the **Claude Agent SDK**.
Investigation (against the live `claude` CLI 2.1.x and the SDK docs) found a
lighter path that fits divergulent's dependency-minimalism better:

- `claude -p` **already** prompt-caches server-side across separate `-p`
  processes (a trivial test call showed `cache_read_input_tokens` > 17k), and
  `--output-format json` **already** returns a full `usage` block
  (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`, the 5m/1h split) **and** `total_cost_usd` — the cost
  telemetry the handoff reached for OpenTelemetry to get.
- The CLI has `--system-prompt` (full replace → an ideal stable cache prefix) and
  `--json-schema` (enforced structured output).
- The **Agent SDK** is a Python package that *wraps the `claude` CLI over stdio*
  (a new dependency **on top of** the CLI we already use), exposes neither
  cache-control nor per-response usage as first-class (OTel only), and is shaped
  for agentic tool-use — a mismatch for our deliberately claim-blind, no-tools,
  two-pass design. Rejected.

So: capture the wins (cache the rubric, stop re-billing it, measure tokens/cache/
cost) by **enhancing the existing backends**, adding **no runtime dependency** and
staying on the subscription billing path. The one thing this does not add is a
warm persistent process — that is a latency win, not a cost win (caching already
removes the token re-billing), and triage is a budgeted batch where latency is
cheap.

## Prompt

Explore before changing:
- divergulent/classify/triage.py — `build_prompt` / `build_verify_prompt` (the
  rubric is static and leads; the diff is the variable tail — already the right
  shape to split), the injected `call(prompt, model) -> str` boundary, the two
  real backends `claude_cli_call` (subprocess `claude -p`, subscription, no dep)
  and `anthropic_call` (lazy `anthropic` SDK, API key, behind the `triage`
  extra), and `triage` / `verify` / `triage_and_verify`. The ledger keys verdicts
  on `(model, PROMPT_VERSION)` / `(model, VERIFY_PROMPT_VERSION)`.
- divergulent/classify/triage_driver.py — `run_triage` (the per-item loop calling
  `triage_and_verify`), `TriageRunStats`, `render_run_report` / `print_run_summary`
  (where per-run totals are already surfaced — the place to add cost/cache lines).
- divergulent/tests/test_triage.py and test_triage_driver.py — the injected fake
  `call` is used throughout; the boundary change touches every fake.

The constraint is **no new runtime dependency** and **preserve the triage logic
exactly** — claim-blind input, adversarial two-pass verify, no tools, no agentic
loop. This is a transport + telemetry change, not a reasoning change.

## Objective

- **Cache the rubric.** Move the static rubric out of the per-patch user prompt
  into a stable **system prompt** sent as the cache prefix, so it is written to
  cache once per run and read back cheaply for every subsequent call, on both
  backends.
- **Measure.** Capture per-call token usage (input/output, cache creation, cache
  read) and cost, aggregate per-patch and per-run, and surface tokens, cache-hit
  ratio, and cost (the CLI's own `total_cost_usd` for `claude -p`; derived at
  API rates for the metered path) in the existing findings note and run summary.
- **No new dependency, subscription preserved.** `claude -p` stays the default;
  the `anthropic` backend gains the same treatment for the eventual metered path.
- **Identical verdicts.** The rubric *content* is preserved verbatim (only
  relocated system↔user), so verdict meaning — and the `(model, prompt_version)`
  ledger identity — is unchanged; validated on a real sample before rollout.

## Design decisions

### The `call` boundary becomes (system, user) → (text, usage)
`call(prompt, model) -> str` becomes
`call(system, user, *, model) -> CallResult`, where `CallResult` carries the
model text and a normalised `Usage` (input/output/cache-creation/cache-read
tokens and an optional `cost_usd`). Splitting the prompt into a **cacheable
system** part and a **variable user** part is what lets every backend put the
rubric where caching applies; returning `usage` is what lets us measure. The
boundary stays injected, so the test suite stays offline (the fake returns a
`CallResult` with canned text + a fake `Usage`).

### Prompt split — rubric to system, diff to user
- Triage: `triage_system_prompt(*, prompt_version)` = the category definitions,
  the claim-blind instruction, and the output schema (constant per version);
  the user message is the diff body. The rubric **text is moved verbatim**, not
  reworded.
- Verify: `verify_system_prompt(*, prompt_version)` = the adversarial
  confirm/refute instruction + output schema (constant); the user message is the
  diff **plus the proposed category** (which varies per patch, so it cannot live
  in the cached prefix).
- Because the content is unchanged, `PROMPT_VERSION` / `VERIFY_PROMPT_VERSION`
  stay put (so the run does **not** re-triage the ~2.2k already-verified
  fingerprints). See the open question for the validation gate behind this.

### Backends place the rubric where caching applies, and report usage
- `claude_cli_call`: `claude -p --model M --system-prompt <rubric>
  --output-format json`, diff on stdin. Parse the JSON result for the answer
  text and the `usage` block + `total_cost_usd`. (`--system-prompt` fully
  replaces Claude Code's default system prompt, so our rubric is the whole, stable
  cache prefix — no dynamic Claude Code sections to bust the cache.)
- `anthropic_call`: `system=[{type:'text', text:<rubric>, cache_control:
  {type:'ephemeral','ttl':'1h'}}]`, `messages=[{role:'user', content:<diff>}]`;
  return `response.usage` normalised. Lazy import unchanged, still behind the
  `triage` extra.
- Both return the same `CallResult`, so the driver is backend-agnostic.

### Telemetry: aggregate and surface honestly
`TriageRunStats` gains usage totals (input, output, cache-creation, cache-read,
derived/ reported cost). The driver sums each call's `usage`. The findings note
and the printed summary gain a **Cost & cache** section: total tokens, **cache-hit
ratio** (cache-read ÷ cacheable input — the number that proves caching landed),
and cost. For `claude -p` the cost is the CLI's reported `total_cost_usd`; for the
API path it is derived from a small, clearly-dated `RATES` table (cache reads at
0.1×, writes at 1.25× 5m / 2× 1h). The derived number is shown even on
subscription, so we always see "what this would cost metered" — the input to the
prototype→pivot decision.

### What we are NOT doing
No tools, no agentic loop, no persistent process, no Agent SDK, no Batches API in
this sub-plan. The model still sees only the diff (claim-blind) and still gets
adversarially verified. This is transport + measurement only.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| T1 | high | opus | none | **The boundary + prompt split (behaviour-preserving).** Add `Usage` + `CallResult` dataclasses. Change `call` to `(system, user, *, model) -> CallResult`. Split `build_prompt`/`build_verify_prompt` into `*_system_prompt(...)` (verbatim rubric) + a user-message builder (diff, plus proposed category for verify). Update `triage`/`verify`/`triage_and_verify` and **every injected fake `call`** in the tests. Rubric content byte-identical; `PROMPT_VERSION`/`VERIFY_PROMPT_VERSION` unchanged. Suite green. One commit. |
| T2 | high | opus | none | **Enhance both real backends.** `claude_cli_call`: `--system-prompt` + diff on stdin + `--output-format json`; parse text + `usage` + `total_cost_usd` into `CallResult`. `anthropic_call`: `cache_control` on the system block + `response.usage`. Tests drive a fake `subprocess.run` returning the real JSON shape, and a fake anthropic response object; assert the rubric goes to the system prompt and usage is parsed. One commit. |
| T3 | med | opus | none | **Telemetry aggregation + report.** Extend `TriageRunStats` with usage/cost totals; the driver sums per-call usage; add a `RATES` table + derived-cost helper; extend `render_run_report` and `print_run_summary` with the Cost & cache section (tokens, cache-hit ratio, cost). Tests assert the aggregation and that the report shows the cache-hit ratio and a cost figure. One commit. |
| T4 | low | opus | none | **Hardening + docs (optional `--json-schema`).** Optionally pass `--json-schema` to `claude -p` for enforced output, keeping the existing parser as the fallback for the API path. Update AGENTS.md / ARCHITECTURE.md (the backend now caches + reports cost) and the phase-4 findings (the measured cache-hit ratio + cost-per-patch from the validation run). One commit. |

T1 → T2 → T3 → T4. T1 is behaviour-preserving and must leave verdicts identical;
T2 makes the rubric actually cache and usage actually flow; T3 surfaces it; T4
hardens + documents. Every step is offline-tested (injected fake `call` /
subprocess / SDK — no real `claude`, no network).

## Testing requirements

- The injected `call` fake returns a `CallResult` (text + fake `Usage`); no test
  ever runs a real `claude -p` or hits the network.
- `claude_cli_call` is tested against a fake `subprocess.run` returning the real
  `--output-format json` shape (the structure captured in this plan), asserting:
  the rubric is passed via `--system-prompt`, the diff via stdin, and the
  text/usage/cost are parsed.
- The prompt split is byte-stable: `triage_system_prompt(...)` is constant for a
  fixed `prompt_version`, and the concatenation of system+user preserves the
  rubric content (a regression test pins the rubric text).
- Telemetry aggregation: a run of K patches sums to the expected token/cost
  totals and cache-hit ratio (driven by fake usages).
- `pre-commit run --all-files` passes; house style.

## Success criteria for this phase

- The rubric is sent as a cached system prefix; a real validation run shows
  `cache_read_input_tokens` rising after the first patch (caching landed), and the
  run summary reports the **cache-hit ratio** and **cost per patch**.
- Verdicts are unchanged: a sample triaged under the new split agrees with the old
  flat prompt (no re-triage of existing verified fingerprints; `prompt_version`
  unchanged).
- **No new runtime dependency**; `claude -p` stays the default subscription
  backend; the `anthropic` extra path gains the same caching + usage.
- The triage logic is untouched — claim-blind, adversarial two-pass, no tools.

## Open questions for this phase

- **Prompt-version bump vs preserve.** Relocating the rubric system↔user *could*
  shift model behaviour even with identical words. Lean: **preserve the version**
  (avoid a costly full re-triage of ~2.2k verified fingerprints) and gate it on a
  **validation run** — triage a representative sample with the old flat prompt and
  the new split, and only ship if verdicts agree to a high rate; if they diverge,
  bump the version and accept the re-triage. Decide the agreement threshold.
- **Cost rates table.** Hardcode indicative API rates (dated, "update me"), or
  read them from a small config? Lean: a dated module constant — the CLI's own
  `total_cost_usd` is authoritative for the subscription path anyway; the derived
  number is a planning estimate.
- **`--json-schema` now or later.** Enforced structured output hardens the
  `claude -p` path but has no clean symmetric form on the API backend (would use a
  tool/JSON mode). Lean: optional in T4, keep the existing robust parser as the
  common path.
- **Persistent process.** Explicitly deferred. If a future run is latency-bound
  (not cost-bound), revisit a warm process — but that is the Agent SDK's territory
  and a separate decision with its own dependency cost.

## Out of scope (later / never)

- The Claude Agent SDK, a persistent process, and the Message Batches API — a
  separate plan if latency or bulk-async ever justifies the dependency/complexity.
- Any agentic tool-use, context-pulling, or multi-step reasoning in triage — the
  claim-blind, adversarially-verified, human-confirmed design is deliberate.
- OpenTelemetry export — the `--output-format json` usage block and `response.usage`
  give us per-call numbers directly; OTel is only worth it if we later want
  cross-run dashboards.

## Back brief

Before executing any step, back brief the operator: T1 is a behaviour-preserving
transport refactor that must leave verdicts (and `prompt_version`) identical; the
rubric content is relocated verbatim, never reworded; the triage *logic* is
untouched; and the goal is to cache the rubric and measure tokens/cache/cost on
the existing subscription backend, adding no runtime dependency.
