# Patch classification: turning "60k carried patches" into a trustworthy answer

A master plan for classifying Debian's carried patches so a user can ask
"so what *are* those patches?" and get an honest, explainable answer —
and so the genuinely interesting residue (undocumented, behaviour-changing,
or security-relevant patches) is surfaced for review rather than lost in
the noise. Created from [PLAN-TEMPLATE.md](../../PLAN-TEMPLATE.md) in
spirit; phases graduate to their own per-phase plans when picked up.

## Why

The published cache shows Debian trixie carries **~60,000 patches** across
~18,600 source packages (≈half the archive), ~3 each on average. That
number is attention-getting but meaningless on its own: 60k FSF-address
updates in licence headers is a non-story; 60k behaviour changes is
interesting; a single planted backdoor is a headline. The value is not the
count — it is **classification**: narrowing 60k to the few hundred a human
should actually look at, with every verdict carrying its own justification
and its own undo.

divergulent's job here is **narrow and track**, never "detect the attack":
it surfaces *candidates* and explains *why*, and a human (or, later, a
community) makes the calls. "No cry wolf" applies throughout — including to
the classifier itself.

## Grounding evidence (cron, gnupg2, grub2 study)

This plan is unusually well-grounded: before writing it we pulled the three
most-patched packages and classified them by hand and by script. What we
learned shapes every decision below.

- **The published count is capped at 60 per package.** Real series: cron
  **85**, gnupg2 **61**, **grub2 148**. The sources.debian.org patches API
  truncates at 60, so the ~60k is an undercount on the heavy tail. *Fixing
  this is a prerequisite.*
- **DEP-3 provenance metadata is almost entirely absent** — cron 85/85 and
  grub2 148/148 have no `Forwarded:` field. We **cannot** rely on DEP-3 to
  classify; the structured provenance simply isn't there.
- **But there is rich free-text signal.** Debian organises patches into
  `fixes/`, `features/`, `docs/` directories, writes clear descriptions,
  and sometimes embeds references (cron has `…-CVE-2006-2607.patch`). These
  are usable — *but author-controlled* (see trust model).
- **Most patches touch code, not docs/licences** — so the hopeful "they're
  all trivial" is false.
- **Deterministic Python classified all 334 patches with zero LLM calls** —
  directory taxonomy, file types, CVE refs, and a code-vs-prose
  claim/content check — narrowing to a small, explainable lead set.
- **A naive content check cries wolf**, and fixing it taught us the model:
  a string-grep flagged 5 manpages that *mention* `/bin/sh` as text; a
  *code-aware* version (scan only code hunks) flagged **0**. We went rule
  v1 → v2 in two iterations — a live demonstration of why rule-versioned
  provenance matters.

## Design decisions

### Key by patch fingerprint, deduplicated
A patch's classification is a property of its **content**, not of any
machine or version — the same diff gets the same verdict everywhere. So the
key is `sha256(normalised_diff)`. Normalise first (strip `@@` offsets, line
numbers, pure-context noise) so trivially-different copies share a
fingerprint. **Measuring the distinct-patch count was the first task.**

**Measured (phase 1, falsifying the original premise):** dedup is **1.02x** —
**≈61,572 carried patches → 60,640 distinct**, with **99.2% of distinct
patches appearing in exactly one package**. The hoped-for collapse ("60k raw
is probably far fewer distinct patches") **did not happen**: Debian's carried
patches are overwhelmingly bespoke. The recurring tail (~488 fingerprints in
2+ packages) is real and is exactly the trivial boilerplate — quilt `.pc`
ignores, permission-only changes, ecosystem-wide build patches — but it is
<1% of distinct patches. So **there is no dedup shortcut**, and the
fingerprint's value is provenance, idempotent re-runs, and handling the small
tail, not scale reduction. The leverage must come from *category* rules
(phase 2), not fingerprint identity. See
[PLAN-patch-classification-phase-01-findings.md](PLAN-patch-classification-phase-01-findings.md).

### Claim vs content — content is ground truth
Every "helpful" signal (directory, description, DEP-3, CVE ref) is written
by whoever submitted the patch and is therefore **attacker-controllable**.
A malicious diff with a `docs/typo-fix.patch` name and a "fix spelling"
description is exactly what must not get a free pass. So:

- Classify the **claim** (from metadata) and the **content** (from the
  diff) *separately*.
- **Their disagreement is the loudest signal** — especially "claims benign,
  changes code/logic / touches a sensitive surface."
- Content analysis must work at the **right semantic level** (code vs prose,
  and eventually substantive vs cosmetic) or it cries wolf on every manpage.
- **Trust-but-verify is recursive:** verify the descriptions against
  content, verify the heuristics (false positives), and verify the LLM.

### Prefer deterministic rules; LLM is the last, verified tier
Deterministic Python is preferred not only for cost but because it *serves
the reproducibility goal*: `(fingerprint, rule_version) → category` is a
pure function — reproducible, free to recompute, self-auditing. An LLM
verdict is non-deterministic, must store its response as evidence, and is
costly to reproduce. Classification is a **cost-ordered sieve**:

1. Normalise + fingerprint → dedup.
2. Ledger lookup → already-classified fingerprints are free.
3. Deterministic rules settle what they can at high confidence and peel it
   off (directory/description/ref extraction, file-type classification,
   claim-vs-content mismatch, trivial-only detection, dangerous-construct-
   in-code).
4. **Only the residue** reaches the **LLM triage tier — always verified** —
   then a human queue for what the LLM flags or is unsure about.

Two dynamics keep the LLM bounded:
- **The LLM is also a rule-discovery tool:** recurring LLM judgements get
  crystallised into new deterministic rules (human-approved, version-
  stamped), so the deterministic set grows and the LLM residue shrinks.
- **Classification is curation-side, like the cache builder.** The rules
  (and any LLM) run centrally; **clients never run an LLM** — they consume a
  signed classification bundle. This keeps the client minimal and
  deterministic, the posture `dep3.py` already embodies and consistent with
  `sigstore` being an opt-in extra, not a core dependency.

### Provenance: a rule registry + an append-only decision ledger
Every verdict records *what decided it*, so a wrong rule is a surgical redo,
not a restart.

- **Rule registry:** `rule_id, version, kind (heuristic|llm|human),
  purity (pure|external), description/changelog`.
- **Decision ledger (append-only, never overwritten):** `patch_fingerprint,
  category, decided_by, rule_version, confidence, evidence, decided_at`
  (plus an **input snapshot / freshness** for `external` rules that consult
  mutable state like the BTS or upstream).
- **Current verdict is derived,** not stored: per fingerprint, the
  highest-precedence live decision (`human > verified-LLM > heuristic >
  default`).
- **Invalidate a rule** → mark its decisions superseded → recompute the view
  → fingerprints left with no live decision re-enter the queue. (LLM "rule
  version" = model id + prompt version, and the response is stored as
  evidence since it is non-deterministic.)
- **Version the category enum and the bundle schema** too, so changes are
  tracked migrations, not silent drift.

### Category enum (provisional, from the cron study)
`packaging` · `documentation` · `bugfix` · `security` · `feature` ·
`unknown`, carried alongside a **confidence** and a **claim/content
consistency** flag. (cron's 85 broke down roughly as ~30 feature, ~25
bugfix, ~12 docs, ~8 packaging, ~10 security — a real, displayable summary.)
The enum is provisional until we know what users actually want to see.

### A separate, signed classification bundle
Distinct from the divergence bundle (different lifecycle: it *grows* as
patterns get classified, rather than being recomputed daily). Keyed by
fingerprint, schema-versioned, signed and published like the cache so
clients consume it with the same trust model and **never run a classifier
themselves**. The client display becomes "*85 patches — 30 features, 10
security…*", each patch linkable to its category **and the evidence/rule
that decided it**.

## Prerequisites

- [x] **Counts no longer capped.** The divergence *count* now comes from the
      patches API's `count` field (grub2 reads 148, not 60) — done and live.
      See PLAN-release-1.0.md §8.
- [x] **Acquire the full patch set + bodies.** Done in phase 1: the corpus
      builder reads the uncapped series straight from each `.debian.tar.*`
      (reusing `apt_patches`) and stored 61,572 patch bodies content-addressed
      across 18,820 patched packages.
- [x] **Normalised-diff fingerprinting** defined (what to strip). Done in
      phase 1: canonical v1 frozen as `strip_path=True, drop_context=False`
      (the distinct count is insensitive to the choice, <2.5% across variants).

## Phases (each graduates to its own plan)

| Phase | Focus |
|-------|-------|
| 1. **Fingerprint & dedup** | Normalise + hash patch bodies; **measure the distinct-patch count across the archive** (the single number that reframes the scale). |
| 2. **Deterministic signal extractors** | Directory taxonomy, description/CVE/bug-ref parsing (as *claims*), file-type classification, code-vs-prose-aware claim/content mismatch, trivial-only and dangerous-construct-in-code detection. |
| 3. **Rule engine, registry & ledger** | The provenance data model: versioned rules, append-only decisions with evidence, derived current-verdict, supersession/redo, pure vs external. |
| 4. **LLM triage tier (optional, curation-side, verified)** | Diff summarisation/category draft *blind to the author's claim*, then compared; human-verify queue; rule-discovery feedback into phase 2. |
| 5. **Classification bundle & client display** | Publish a signed fingerprint→verdict bundle; client shows per-package category breakdowns with per-patch "why", never running a classifier. |
| 6. **BTS / upstream cross-reference** | The `external` rules: does a declared bug exist / is it fixed upstream — with input snapshots so freshness is tracked. |

**Reorder note (after phase 1):** the deterministic signal extractors and the
rule engine/ledger were swapped. Phase 1 found no dedup shortcut (≈60,640
distinct patches), so the *category rules* are where all the leverage lives —
build them first, on the real corpus, and let the rules' shape inform the
ledger schema rather than guessing it up front. This is *build* order, not the
runtime *sieve* order: at classification time the ledger is still consulted
before rules run (a cached verdict is free). Phase 2 can emit a plain
fingerprint→category table; phase 3 then wraps it in the versioned, append-only
provenance ledger (rule id/version, evidence, supersession/redo).

## Execution

| Phase | Plan | Status |
|-------|------|--------|
| 1. Fingerprint & dedup | [PLAN-patch-classification-phase-01-fingerprint.md](PLAN-patch-classification-phase-01-fingerprint.md) · [findings](PLAN-patch-classification-phase-01-findings.md) | **Done** — ≈61.5k patches → 60,640 distinct (dedup 1.02x; no shortcut) |
| 2. Deterministic signal extractors | [PLAN-patch-classification-phase-02-extractors.md](PLAN-patch-classification-phase-02-extractors.md) · [findings](PLAN-patch-classification-phase-02-findings.md) | **Done** — 29.2% settle deterministically; 70.8% (~43k) substantive residue → phase 4 |
| 3. Rule engine, registry & ledger | [PLAN-patch-classification-phase-03-ledger.md](PLAN-patch-classification-phase-03-ledger.md) · [findings](PLAN-patch-classification-phase-03-findings.md) | **Done** — append-only ledger reproduces the distribution with provenance; queue = 42,907 residue, derived |
| 4. LLM triage tier | [PLAN-patch-classification-phase-04-llm-triage.md](PLAN-patch-classification-phase-04-llm-triage.md) | Planned (not started) |
| 5. Classification bundle & client display | — | Not started (no detailed plan yet) |
| 6. BTS / upstream cross-reference | — | Not started (no detailed plan yet) |

## Success criteria

- "60k carried patches" is replaced by "**N distinct** patches, of which the
  vast majority are classified deterministically, and here is the small
  residue worth review." (Phase 1 measured **N ≈ 60,640** — dedup does *not*
  shrink it, so "classified deterministically" must come from category rules,
  not duplicate-collapsing.)
- Every verdict carries `rule_id + version + evidence`; re-running after a
  rule fix re-classifies **only** the affected fingerprints.
- The LLM is invoked only on the residue, is always verified, and shrinks
  over time as its judgements become deterministic rules.
- A user can see a meaningful per-package classification — and for any
  patch, *why* it was classified that way.
- Clients run **no** classifier and **no** LLM; they consume a signed
  bundle, consistent with the project's minimal, deterministic posture.

## Out of scope / honest boundaries

- **Not a security audit and not automated malice detection.** Content
  analysis raises the bar and surfaces *candidates*; a human finds the
  attack in the narrowed queue. We never pronounce "malicious".
- **Deep semantic diff-equivalence** beyond fingerprint normalisation
  (genuinely hard; out of scope).
- **Reading every diff line-by-line** at archive scale by hand (the thing
  this plan exists to avoid).

## Open questions

- The **distinct-patch count** — measure in phase 1 before committing to
  scale assumptions.
- **Category enum** — validate against what users find useful.
- **Where the ledger lives** — a private repo first; a shared, community
  classification ledger later (like the published cache) so no one
  re-classifies the same Debian-wide patch.
- **LLM provider/model/prompt versioning, cost budget, and evidence
  storage.**
- **How much content analysis is "enough"** — we flag suspicion, not malice;
  set the bar so the residue stays trustworthy and small.

## Relationship to other plans

- Extends the **"no cry wolf" validation** workstream in
  [PLAN-release-1.0.md](PLAN-release-1.0.md) and is the substance behind the
  long-standing "patch hygiene & justification" idea.
- Reuses the **published-cache** infrastructure (central builder, signing,
  signed bundle, the consume/verify trust model).
- The **60-cap fix** overlaps the builder-robustness workstream.

## Administration

- Registered in [docs/plans/index.md](index.md).
- Each phase graduates to its own detailed `PLAN-…` when scheduled; this
  master plan tracks the overall effort and is updated as phases land.

## Back brief

Before executing any phase, back brief the operator on your understanding
of the plan and how the intended work aligns with it.
