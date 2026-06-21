# Phase 3 — Rule engine, registry & decision ledger

Part of [PLAN-patch-classification.md](PLAN-patch-classification.md).
Plan this phase at **high effort**: it is the provenance backbone — getting
the decision/derivation model right is what makes a wrong rule a surgical redo
instead of a restart, and what lets human, LLM, and heuristic verdicts coexist
without overwriting each other.

**Status: complete.** All four steps (3a schema+registry, 3b recorder, 3c
derived view+queue, 3d supersession+CLI) are implemented, tested, and committed,
and the ledger has been built over the real corpus. **Result: the append-only
ledger reproduces the phase-2 distribution exactly (60,640 decisions: 42,907
unknown / 13,382 packaging / 4,351 documentation) with full provenance; the
derived queue is the 42,907-fingerprint residue, and supersession re-queues
surgically.** Full analysis in
[PLAN-patch-classification-phase-03-findings.md](PLAN-patch-classification-phase-03-findings.md).
The build's per-row commits were batched into one transaction, cutting it from
~11 min to ~3m40s.

## Why this is phase 3 (after the extractors)

Phases 2 and 3 were deliberately swapped: phase 1 showed no dedup shortcut, so
the deterministic rules are the leverage and were built first, on the real
corpus. Phase 2 emitted a **plain `fingerprint → classification` table**
(`classification.sqlite`), each verdict tagged with the `rule_id`+`version`
that fired but with no versioned provenance, no supersession, and no way for a
later human or LLM verdict to coexist with the heuristic one. Phase 3 wraps
that output in the **append-only decision ledger** the plan always intended —
now with a real rule set and table shape to model rather than a guess.

## Prompt

Explore before changing:
- `divergulent/classify/rules.py` — the deciders to register: `_CATEGORY_RULES`
  (each `(id, version, fn)`: empty / ignore-file-only / whitespace-only /
  comment-only / doc-only / build-only / substantive), the dangerous-construct
  scan, and `RULES_VERSION`. Also `claim.py` (`CLAIM_RULE_VERSION`,
  `_classify_category`) and `content.py` (`CONTENT_RULE_VERSION`) — all current
  rules are `kind=heuristic`, `purity=pure`.
- `divergulent/classify/classify.py` — `classify_index` (per-fingerprint
  `Classification`) and `write_classification` (the flat table this phase
  replaces with the ledger). Reuse its corpus/index reading.
- `divergulent/classify/measure.py` — the phase-1 index (`patch` table) and
  `read_body`.
- [PLAN-patch-classification.md](PLAN-patch-classification.md) "Provenance: a
  rule registry + an append-only decision ledger" — the authoritative design.
- [PLAN-patch-classification-phase-02-findings.md](PLAN-patch-classification-phase-02-findings.md)
  — the real distribution the ledger must hold (29.2% settled, ~43k residue,
  158 flags, 58% claim-unknown).

This phase is **curation-side only** and adds **no new client surface**; it
restructures how verdicts are stored and derived. Nothing here runs an LLM —
that is phase 4; phase 3 only reserves the model's place in the precedence.

## Objective

Replace the flat phase-2 table with a **rule registry** + an **append-only
decision ledger** from which the current verdict is **derived, never stored**,
so that:

- every verdict carries `rule_id + version + evidence` and is reproducible;
- retiring or bumping a rule **supersedes** exactly its decisions and re-queues
  only the affected fingerprints — a surgical redo;
- **human > verified-LLM > heuristic** decisions coexist per fingerprint and
  the highest-precedence live one wins, so phase 4 (LLM) and human review slot
  in without schema change;
- the **pure vs external** distinction is modelled now (external rules carry an
  input snapshot + freshness), even though external rules arrive in phase 6.

## Design decisions

### The ledger is the source of truth; the verdict is a view
Three tables in a curation-side sqlite ledger:

- **`rule` (registry):** `rule_id, version, kind (heuristic|llm|human),
  purity (pure|external), category_enum_version, description, retired (bool)`.
  Populated from the phase-2 deciders; the seed of an ever-growing registry.
- **`decision` (append-only, never updated in place):** `id, fingerprint,
  category, confidence, decided_by (rule_id), rule_version, kind, evidence,
  decided_at, superseded_at (nullable), input_snapshot (nullable, external
  only), input_fresh_until (nullable, external only)`. A row is written once;
  it is only ever *superseded* (a timestamp set), never edited or deleted.
- **`current_verdict` (DERIVED):** per fingerprint, the non-superseded decision
  of highest-precedence `kind` (human > verified-LLM > heuristic), tie-broken
  by recency then confidence. A query (or a materialised cache rebuilt from the
  query) — **never hand-written**, so it cannot drift from the ledger.

### Append-only + supersession = surgical redo
A pure decision is reproducible from `(fingerprint, rule_id, rule_version)`, so
recording is **idempotent**: never append a `(fingerprint, rule_id,
rule_version)` that already exists live. To retire or fix a rule, mark its
`rule` row `retired` (or register a new `version`) and **supersede** its
decisions (set `superseded_at`); recompute the view; fingerprints left with no
live decision **re-enter the queue**. No decision is ever destroyed, so the
ledger is a full audit trail and a redo touches only the affected rows.

### Precedence reserves the LLM/human seats now
All phase-2 decisions are `kind=heuristic`. The derived view already ranks
`human > verified-LLM > heuristic`, so when phase 4 appends an LLM decision for
a residue fingerprint, or a human overrides a heuristic, the view picks it up
with no schema change. The **queue** is exactly the fingerprints whose only (or
best) live decision is the `substantive`/`unknown` heuristic — the ~43k residue
phase 4 consumes, plus anything a supersession re-queues.

### Pure vs external, modelled now
Phase-2 rules are all `purity=pure` (a function of the diff alone, no clock, no
network). The schema reserves `input_snapshot` + `input_fresh_until` for
`purity=external` rules (phase 6: "does the declared bug exist / is it fixed
upstream") so an external verdict records *what it saw and when*, and the view
can treat it as stale past `input_fresh_until`. No external rule is implemented
here — only the columns and the staleness semantics.

### Versioning is explicit
The `category_enum_version` travels on every rule and decision (the enum is
still provisional). The ledger has its own `schema_version` in a `meta` table.
Bumping either is a tracked migration, never a silent reinterpretation.

### Flags are observations, not category decisions
The dangerous-construct flags from phase 2 are **not** category verdicts (a
flagged patch is still `unknown`/substantive). Record them as a separate
`observation` kind (or an `observation` table) keyed by fingerprint + rule, so
they ride alongside the category decision and feed the review queue without
ever becoming a category. This keeps "never pronounce malice" structural.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 3a | high | opus | none | Add `divergulent/classify/ledger.py`: the schema + registry. Define the sqlite `rule`, `decision`, `observation`, and `meta` tables (with a `schema_version`), and a `register_rule(...)`/registry structure capturing every phase-2 decider as a `rule` row (`kind=heuristic`, `purity=pure`, with its id/version/description and `category_enum_version`). Pure read/write helpers; append-only invariant enforced (an `_append_decision` that refuses to edit, only insert; a `supersede(...)` that only sets `superseded_at`). No business logic yet. Offline tests for the schema, the registry population from rules.py, and the append-only/supersede invariants. |
| 3b | high | opus | none | Add the **decision recorder**: run the registered pure rules over the phase-1 index (reuse `classify.classify_index`'s per-fingerprint claim+content+rules pass) and append one category `decision` per fingerprint (`decided_by` = the winning content rule id, with confidence + evidence = the verdict signals) plus `observation` rows for each dangerous-construct flag. Idempotent: skip a `(fingerprint, rule_id, rule_version)` already live. Refactor `classify.py` so its per-fingerprint logic is shared (do not duplicate). Tests over a synthetic corpus+index: decisions + observations recorded once; a second run appends nothing. |
| 3c | high | opus | none | Add the **derived current-verdict view + queue**: a query (and a `rebuild_current_verdict` materialiser) selecting, per fingerprint, the non-superseded decision of highest-precedence kind (human > verified-llm > heuristic), tie-broken by `decided_at` then confidence; and `queue()` returning fingerprints whose live verdict is `unknown`/substantive (the phase-4 residue) plus any with no live decision. A summary report: verdicts by category, queue size, decisions by rule, observation counts. Tests assert precedence (an llm/human decision beats a heuristic one for the same fingerprint) and the queue contents. |
| 3d | high | opus | none | Add **supersession/redo + a ledger CLI** (`python -m divergulent.classify.ledger`): `supersede_rule(rule_id, version)` marks that rule's decisions superseded and re-queues fingerprints left with no live decision; `retire`/`re-register a new version` flows; and CLI subcommands to build the ledger from a corpus, show the current-verdict/queue report, and supersede a rule. Tests: superseding a rule re-queues exactly its fingerprints and leaves the rest of the ledger and the audit trail intact; a higher-version re-registration takes precedence on recompute. |

3a → 3b → 3c → 3d in order (each builds on the last). One commit per step.

## Operational note

Building the ledger over the real corpus is the same offline, CPU-only pass as
phase 2 (no network) — fast and re-runnable. Treat the build + the
current-verdict/queue report as a reviewed step producing a findings note, like
phases 1 and 2. The headline to confirm: the derived verdicts reproduce the
phase-2 distribution (29.2% settled, ~43k residue), now as a ledger with full
provenance and a working queue.

## Testing requirements

- All schema/registry/recorder/view logic is unit-tested **offline** against a
  small synthetic corpus + index; no network.
- The **append-only invariant** is tested: a decision is never edited or
  deleted, only superseded.
- **Supersession** re-queues exactly the affected fingerprints and preserves
  the audit trail (superseded decisions remain, marked).
- **Precedence** is tested: human > verified-LLM > heuristic for one fingerprint.
- Idempotent recording: a second build appends nothing.
- `pre-commit run --all-files` passes; house style (single quotes, 120 cols).

## Success criteria for this phase

- A rule registry + append-only decision ledger replacing the flat phase-2
  table, with the current verdict **derived** (never stored) at
  human > verified-LLM > heuristic precedence.
- Retiring/bumping a rule supersedes exactly its decisions and re-queues only
  the affected fingerprints, leaving a full audit trail.
- The queue is the phase-4 residue (the ~43k substantive fingerprints), ready
  for the LLM tier to append decisions into the same ledger.
- Pure vs external is modelled (external columns + staleness semantics present,
  unused until phase 6); the category enum and schema are versioned.
- Curation-side only; no client command imports the ledger; nothing runs an LLM.

## Open questions for this phase

- **Materialised `current_verdict` vs a pure query** — start with a query;
  materialise only if the report is slow over 60k.
- **Evidence storage** — the phase-2 `signals`/flag evidence as text vs JSON;
  enough to reconstruct *why*, without storing whole diffs.
- **"verified-LLM" representation** — phase 4 will need a way to mark an LLM
  decision verified; reserve the `kind` value and a `verified` notion now,
  define the mechanism in phase 4.
- **One decision per fingerprint per rule, or per occurrence?** Content is per
  fingerprint (decided once); the claim can differ per occurrence (phase-2 open
  question). Decide whether claim-derived signals live on the fingerprint
  decision or a per-occurrence side table.
- **Ledger ↔ phase-5 bundle** — the published classification bundle (phase 5)
  is the *current_verdict view*, signed; confirm the derived view is the right
  export boundary.

## Out of scope (later phases)

- The **LLM triage tier** that appends decisions for the residue (phase 4).
- The **signed classification bundle & client display** (phase 5) — phase 3
  produces the derived view that phase 5 exports.
- **External rules** themselves (BTS / upstream cross-reference, phase 6) —
  phase 3 only reserves their schema and staleness semantics.

## Back brief

Before executing any step, back brief the operator on your understanding of
this phase and how the intended work aligns with it.
