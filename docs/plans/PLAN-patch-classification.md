# Patch classification: turning "60k carried patches" into a trustworthy answer

A master plan for classifying Debian's carried patches so a user can ask
"so what *are* those patches?" and get an honest, explainable answer —
and so the genuinely interesting residue (undocumented, behaviour-changing,
or security-relevant patches) is surfaced for review rather than lost in
the noise. Created from [PLAN-TEMPLATE.md](https://github.com/shakenfist/divergulent/blob/develop/PLAN-TEMPLATE.md) in
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

| Phase | Plan | Outcome | Status | Merged |
|-------|------|---------|--------|--------|
| 1. Fingerprint & dedup | [PLAN-patch-classification-phase-01-fingerprint.md](PLAN-patch-classification-phase-01-fingerprint.md) · [findings](PLAN-patch-classification-phase-01-findings.md) | ≈61.5k patches → 60,640 distinct (dedup 1.02x; no shortcut) | Complete | `f53ce737` (#22) |
| 2. Deterministic signal extractors | [PLAN-patch-classification-phase-02-extractors.md](PLAN-patch-classification-phase-02-extractors.md) · [findings](PLAN-patch-classification-phase-02-findings.md) | 29.2% settle deterministically; 70.8% (~43k) substantive residue → phase 4 | Complete | `b57a512e` (#23) |
| 3. Rule engine, registry & ledger | [PLAN-patch-classification-phase-03-ledger.md](PLAN-patch-classification-phase-03-ledger.md) · [findings](PLAN-patch-classification-phase-03-findings.md) | append-only ledger reproduces the distribution with provenance; queue = 42,907 residue, derived | Complete | `98233b03` (#24) |
| 4. LLM triage tier | [PLAN-patch-classification-phase-04-llm-triage.md](PLAN-patch-classification-phase-04-llm-triage.md) · [findings](PLAN-patch-classification-phase-04-findings.md) | claim-blind triage + adversarial verify + ledger precedence + signed human review built. Operating it showed the residue is irreducibly *semantic*: the one deterministic win was a new `test-only` rule (→ `test` category, peels ~15%), applied via the non-destructive `ledger record`. Tooling hardened (per-file original context, Sigstore once-per-session + token refresh, review pager/package-names/`requeue`/`history`, `build` wipe-guard). The triage + review grind is the operator's ongoing budgeted step. R4's validation of the risk-gate threshold is deferred to issue #90 | Complete | `8828cb16` (#25), `201bc2ee` (#26), `228affb3` (#27), `b02976f3` (#28), `a21033a2` (#29), `af8b1b9b` (#30), `e56fd08d` (#31), `f51da19a` (#32), `d680ef3f` (#33), `fe1fea74` (#44), `d9d5ad6c` (#58), `fae9f258` (#48), `dd2abb51` (#50) |
| 5. Classification bundle & client display | [PLAN-patch-classification-phase-05-bundle.md](PLAN-patch-classification-phase-05-bundle.md) | signed fingerprint→verdict bundle (gzipped JSON, like the divergence cache) built by CI from a *committed JSONL export* of the ledger (never the sqlite); `cache pull-classification` + `show` render it, the client hashing patch bodies and running no classifier. Ledger data repo wired (public `shakenfist/divergulent-reviews`, PR #35); first signed classification bundle published live | Complete | `4c655626` (#34), `3cff4ea3` (#35), `2be2b108` (#61), `d311fafe` (#85) |
| 6. BTS / upstream cross-reference | [PLAN-patch-classification-phase-06-cross-reference.md](PLAN-patch-classification-phase-06-cross-reference.md) · [findings](PLAN-patch-classification-phase-06-findings.md) | the `external` rule tier: verifies author-declared CVE/bug claims against Debian's own records (Security Tracker + BTS), bulk-pinned snapshots with recorded freshness (the reserved `input_snapshot`/`input_fresh_until` columns), settling `security` on strong corroboration and only *flagging* contradictions. Real corpus: ~10% of patches carry a bug/CVE reference (1.44% a CVE) — a scalpel, not a broom. Verdict split awaits the first operator snapshot run, deferred to issue #89 | Complete | `53e0deaa` (#42), `f5d0a7a8` (#43) |
| 7. Pre-push audit + closeout | [PLAN-patch-classification-phase-07-closeout.md](PLAN-patch-classification-phase-07-closeout.md) | ran `PUSH-AUDIT.md` over the accumulated diff of phases 1–6 (`d7c030e4..develop`, 85 files, 36,245 insertions), moved the two deferred measurements (phase 6 E6's verdict split → issue #89, phase 4 R4's risk-gate validation → issue #90) out of the plan files and into the issue tracker, wrote the operator runbook, and marks this plan `Complete` | Complete | #102, on top of #99 (a closeout row cannot name the merge that lands it) |

**Plan complete (2026-09-04).** All six phases are implemented and shipping: the
corpus is fingerprinted, ~29% of distinct patches settle on deterministic rules
with a further ~15% from `test-only`, the append-only ledger carries rule, version
and evidence for every verdict, the LLM tier runs claim-blind with adversarial
verification behind a security-risk gate, the external tier verifies declared CVE
and bug claims against Debian's own records, and a signed fingerprint→verdict bundle
is rebuilt daily from the committed export. What continues is review, not
construction: roughly 43k fingerprints of substantive residue remain overwhelmingly
unreviewed, and the bundle grows as that work proceeds. The honest claim is that
Debian's carried patches have been made *classifiable*, not that they are
classified. Two measurements were moved to issues #89 and #90; the pre-push audit's
findings are fixed or filed at #91–#98.

The audit itself came back wave-1 clean — `pre-commit`, `tox`, and the full suite
passing inside a network namespace with only loopback up — with the signing and
publish path clean and four high findings, of which three were fixed and one (the
signing jobs sharing a runner label with `pull_request` workflows) closed as not
exploitable once the operator confirmed the self-hosted runners are ephemeral and
fork PRs get no CI without approval.

The `Merged` record was reconstructed after the fact from `gh pr list --state
merged` and `git rev-list`, so it is a reading of history rather than something
recorded as the work landed. Two of those merges are not phases: `d7c030e4`
(#20) carried the master plan itself plus the patches-API count fix, and
`f5d8a81a` (#45) and `9c91e835` (#66) carried the documentation for the
pipeline.

## Success criteria

**All six delivery phases are implemented; every success criterion below is met**,
and the seventh, administrative phase — the whole-plan pre-push audit and this
closeout — has run, so the plan is `Complete`. The work that continues is
operational, not architectural: the ongoing human-review grind (phase 4, which
enriches an already-shipping bundle rather than gating it) and the first operator
record-with-snapshot run that populates phase 6's verdict split (issue #89).

- ✅ **"60k carried patches" is replaced by a distinct count with a small
  review residue.** Phase 1 measured **N ≈ 60,640** distinct (dedup is only 1.02x —
  no shortcut), and phases 2–4 narrow it: ~29% settle deterministically, a
  `test-only` rule peels ~15% more, and the LLM/human tiers work the rest. So
  "classified deterministically" comes from the category *rules*, exactly as the
  measurement forced.
- ✅ **Every verdict carries `rule_id + version + evidence`; a rule fix
  re-classifies only the affected fingerprints.** The append-only ledger (phase 3)
  records provenance per decision, and `record --reconcile` supersedes only the
  fingerprints whose winning rule changed. Phase 6's `external` verdicts additionally
  record an `input_snapshot` + `input_fresh_until`, so even world-dependent decisions
  say exactly what they saw and when.
- ✅ **The LLM is invoked only on the residue, is always verified, and shrinks as
  its judgements become deterministic rules.** Phase 4 runs claim-blind triage +
  adversarial verify only on the substantive residue; the one deterministic win it
  surfaced (`test-only` → `test`) was crystallised into a rule. Phase 6 shrinks the
  residue further *without* the LLM — a confirmed CVE settles `security` from an
  external snapshot, no model spend.
- ✅ **A user can see a meaningful per-package classification — and for any patch,
  *why*.** Phase 5's signed bundle + client `show` render per-package category
  breakdowns with a per-patch provenance reason, including phase 6's confirmed-CVE
  phrase (id + snapshot date).
- ✅ **Clients run no classifier and no LLM; they consume a signed bundle.** Every
  tier — deterministic rules, LLM triage, human review, and the phase-6 external
  cross-reference — is curation-side. The client only hashes a patch body and looks
  up a verdict, consistent with the project's minimal, deterministic posture.

## Out of scope / honest boundaries

- **Not a security audit and not automated malice detection.** Content
  analysis raises the bar and surfaces *candidates*; a human finds the
  attack in the narrowed queue. We never pronounce "malicious".
- **Deep semantic diff-equivalence** beyond fingerprint normalisation
  (genuinely hard; out of scope).
- **Reading every diff line-by-line** at archive scale by hand (the thing
  this plan exists to avoid).

## Open questions

Every question below is dispositioned at close-out; the two that are still
genuinely open are carried into *Future work* rather than left here to rot.

- The **distinct-patch count** — **answered by phase 1, and it falsified the
  premise**: ≈61,572 carried patches → **60,640 distinct**, dedup only 1.02x, with
  99.2% of distinct patches appearing in exactly one package. There is no dedup
  shortcut, so the leverage had to come from category rules.
- **Category enum** — **still open**, carried to *Future work*. The enum shipped
  and grew a `test` category out of phase 4's one deterministic win, but it has
  never been validated against what a user actually wants to see.
- **Where the ledger lives** — **answered by phase 5**: the ledger's committed
  JSONL export lives in the public `shakenfist/divergulent-reviews`, sharded by ISO
  week, and CI builds the signed bundle from that export and never from a local
  sqlite. The *shared, community* half of the question — third parties
  **contributing** verdicts rather than only consuming them — is still open and is
  carried to *Future work*.
- **LLM provider/model/prompt versioning, cost budget, and evidence
  storage** — **answered in practice by phase 4**: an LLM rule version is model id
  + prompt version, every response is stored as evidence in the append-only ledger
  because it is not reproducible, and spend is bounded by running the model only on
  the residue, in risk order. What is left is the gate's threshold, which is issue
  #90 — a measurement, not an open design question.
- **How much content analysis is "enough"** — **answered as a discipline rather
  than a number**: content analysis raises the bar and surfaces candidates, never
  pronounces malice, and every widening is checked for false positives before it
  ships (the code-aware content check that took rule v1 → v2, the tuned injection
  tripwire). The two live calibrations are issue #90's threshold and issue #98's
  proposed recall increase.

## Future work

- **Measure phase 6's cross-reference verdict split — issue #89.** The
  confirmed:contradicted:unknown split, the `external decisions
  appended/skipped/superseded` counts, and the freshness-TTL calibration all wait
  on an operator Security-Tracker + BTS snapshot run. The tier ships and records
  its own stats lines; it is the headline number that is unmeasured, not the code.
- **Validate and tune the risk gate's threshold (R4) — issue #90.** Re-run the
  bake-off at larger N on a hand-checked label set, confirm recall and false-alarm
  rate at ≥elevated, and finalise the model default. The choice governs a
  ~$245–$1.2k spend range, and the labelling circularity the phase-4 risk-gate plan
  flagged has to be handled in how the sample is drawn.
- **The ongoing human review of the substantive residue.** Roughly 43k fingerprints
  are still overwhelmingly unreviewed, and the published bundle grows as they are
  worked. This is a budgeted operational loop rather than plan work: the commands,
  the cadence, what is automated and which steps cost LLM budget are in
  [the classification runbook](../classification-runbook.md).
- **The pre-push audit's tracked findings — issues #91–#98.** Recorded in
  [phase 7](PLAN-patch-classification-phase-07-closeout.md)'s *Audit findings* and
  deliberately filed rather than fixed inside this plan: #91 the classification
  bundle is unverified on a default install; #92 the corpus download discards the
  checksum apt already provides; #93 the snapshot fetchers have no size or
  decompression bound; #94 the client flattens mixed-provenance signals into one
  line; #95 `record_to_ledger` loses a whole run on one bad patch; #96 a ten-item
  low-severity hardening backlog; #97 `ARCHITECTURE.md` and `AGENTS.md` need
  trimming back to their remit; #98 the chat-marker injection patterns cannot fire
  on diff lines at all.
- **Validate the category enum against real readers — issue #100** (carried from
  *Open questions*). It needs someone reading per-package breakdowns and saying
  which categories changed their mind about a package, rather than a code change.
  The enum is versioned, so revising it is a tracked migration rather than silent
  drift.
- **A path for contributed classification verdicts — issue #101** (carried from
  *Open questions*). Publishing the export and the signed bundle already means
  nobody re-classifies a Debian-wide patch in order to *read* a verdict, but there
  is no path for a third party to *contribute* one — no submission format, no trust
  model for a stranger's signed verdict, and no precedence rule placing it relative
  to `human`. With one reviewer and ~43k fingerprints of residue, contribution is
  the only mechanism that changes the arithmetic; it needs contributors before it
  needs a design.

## Bugs fixed during this work

- **Three rounds of fixes surfaced by first using the phase-4 outputs** — PRs #26,
  #27 and #28. These are the defects that only appear once a human is actually
  working the review queue and the ledger is accumulating real decisions: review
  and ledger correctness, then the fit and polish the queue needed to be usable at
  volume. Summarised rather than enumerated; the PRs and
  [the phase-4 findings](PLAN-patch-classification-phase-04-findings.md) carry the
  detail.
- **Nine more found and fixed by the phase-7 pre-push audit**, one commit per
  finding, in [PR #99](https://github.com/shakenfist/divergulent/pull/99) — which
  is a separate pull request, so until it merges these are fixed *on that branch*
  and not yet on the default one: the security-risk gate bypassed
  the prompt-injection tripwire, so attacker-authored text reached the model it
  targets and a steered verdict could sink its own patch in the human queue; two
  quadratic scanners a patch of blank lines could stall; the review UI's signing
  endpoints had no cross-origin guard; a failed review submission could still land
  its verdict, because nothing rolled back the uncommitted decision; the series
  parser's adversarial cases were untested; a dead precedence helper ranked kinds
  wrongly and invited reuse; a cleanly-verified LLM `security` draft could settle
  without a human ever seeing it, against the promise this plan makes; and the
  reader-facing documentation defects. Three of those fixes change behaviour on
  purpose — [phase 7](PLAN-patch-classification-phase-07-closeout.md)'s *Audit
  findings* says which and why.

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
