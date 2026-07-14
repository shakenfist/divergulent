# Prompt-injection screening for LLM-bound patch text

Divergulent's LLM triage tier sends attacker-authorable text — the diff
bodies of ~60k deduplicated carried patches — to a language model
(`triage.py`, the draft and the adversarial verify pass), and a future
"jumbo" agentic tier would widen that surface further. A patch is exactly
the place a supply-chain attacker controls, so patch text aimed at the
*classifier* rather than the *compiler* is a live concern: an embedded
instruction that nudges the triage LLM toward a benign category is cheap
to attempt and invisible in a 50k-line diff.

This plan evaluates three screening techniques that could run **before**
any patch text reaches an LLM, producing a deterministic "injection
suspect" signal:

1. **A regex/Unicode tripwire** — zero-dependency, fully deterministic
   pattern matching (instruction-shaped phrases, chat-template role
   markers, invisible/bidi Unicode, large encoded blobs).
2. **Llama Prompt Guard 2** (Meta, PurpleLlama) — an 86M/22M-parameter
   DeBERTa-family *encoder classifier* (not a generative model — it has
   no instruction-following pathway to hijack) with open weights,
   emitting a 0–1 injection probability.
3. **LLM Guard's `PromptInjection` scanner** (Protect AI, MIT) — a
   Python library wrapping `deberta-v3-base-prompt-injection-v2` with a
   thresholded score.

**Status: evaluation measured (2026-07-12).** The tripwire found zero
suspected attacks in 60,642 patches (81 raw hits, all adjudicated
benign); the learned classifier false-positives on 28% of benign
patches. Recommendation in the
[findings](PLAN-prompt-injection-screening-phase-01-findings.md):
graduate the tripwire as an observation rule, drop the learned
classifiers. Phase 3 awaits the operator's decision. The worst case was
always a slightly interesting blog post; the best case is catching
something real in the wild.

## Why — and the posture

Consistent with "no cry wolf" and the project's layered-tier design:

- **A hit routes to a human; nothing is silently dropped.** For a Debian
  patch, LLM-instruction-shaped text is *itself* a suspicious artifact —
  arguably a stronger signal than anything else we scan for. The right
  action on a hit is: skip the LLM passes, raise review priority, badge
  the review UI — exactly the shape of the existing dangerous-construct
  observation. The failure modes are then asymmetric in our favour: a
  false positive costs one human review (the system's designed
  behaviour); a false negative leaves us exactly where we are today.
- **Screening is defence-in-depth, not the defence.** The triage design
  already bounds the blast radius of a successful injection: no tools,
  output strictly parsed to a JSON category enum, an adversarial verify
  pass, and a human tier above everything security-shaped. The worst
  case today is a wrong category draft. Screening adds detection, not a
  new trust boundary.
- **Tripwire, not shield.** All three candidates are public/open-weight,
  so a targeted attacker can iterate offline against them until their
  payload scores clean. The published evasion literature confirms
  bypasses transfer across these detectors. What a tripwire still buys:
  every lazy, untargeted, or copy-pasted payload, and a forced increase
  in attacker effort. We do not claim more than that.

## Design decisions

### Scan target: the diff body the LLM actually sees, header hits noted separately

Triage is deliberately blind to the author's DEP-3 claim
(`triage.diff_body` strips the header), so the LLM-injection surface is
the diff body. The DEP-3 header *is* shown to humans and rendered by the
review web UI, so header hits are worth recording too — but as a
distinct, lower-priority signal. The prototype measures both and reports
them separately.

### A hit becomes an observation, not a verdict

If a technique graduates, it lands as a versioned observation kind
(working name `llm-injection-suspect`) mirroring the dangerous-construct
scan: recorded with evidence (the matched pattern / the score and model
hash), consulted by the triage driver to skip the LLM and route to a
human with a priority bump, and badged in the review UI. It never
settles a category and never pronounces malice.

### Determinism for model-based scoring

A pinned local model at a fixed threshold is deterministic in the sense
the ledger needs: same bytes in, same score out, on CPU inference.
Weights digest + threshold + chunking policy hash into the
`rule_version`, so bumping any of them supersedes and re-scans exactly
like every other rule. The heavyweight dependencies (torch,
transformers) are curation-side only, behind an optional extra like
`[triage]`/`[verify]` — never on the client path.

### Compose by OR, never AND; prefer mechanism diversity

Prompt Guard 2 and LLM Guard's model are the same architecture family
trained on overlapping corpora — their errors correlate, so ensembling
them buys little. If more than one technique graduates, they compose by
OR (flag if any fires): recall up, and the false-positive cost is human
review we already accept. The genuinely decorrelated pairing is
mechanism diversity: the deterministic tripwire (which cannot be
gradient-optimised against in the same way) plus at most one learned
classifier.

## The tripwire pattern families (technique 1)

- **Instruction-shaped phrases** in added lines: "ignore
  previous/prior/above instructions", "disregard the/your ...", "you are
  now", "system prompt", "new instructions", "respond only with",
  "classify this as", and similar imperatives aimed at a model.
- **Chat-template / role markers**: `<|im_start|>`, `[INST]`, `<<SYS>>`,
  `<system>`/`</system>`, `Human:`/`Assistant:` turn markers,
  `role: system` fragments.
- **Invisible and bidi Unicode**: zero-width characters (U+200B–200F),
  bidi embedding/override controls (U+202A–202E, U+2066–2069 — the
  Trojan Source vector, CVE-2021-42574), interior U+FEFF, and the
  Unicode tag block (U+E0000–E007F — the invisible-instruction vector).
  Expect legitimate RTL text in translation files; the evaluation must
  measure whether a file-type carve-out is needed.
- **Large encoded blobs**: long base64 runs in added lines of
  non-binary files. High false-positive risk (certificates, embedded
  test data); measured before it is believed.

Each family is a separately versioned, separately reportable pattern so
the findings can retire noisy families without losing the quiet ones.

## Evaluation design

- **Corpus**: the real reviews corpus (~60k deduplicated fingerprints,
  content-addressed bodies). The tripwire runs over everything; the two
  model-based scorers run over a stratified sample (they are ~1000×
  slower) plus every tripwire hit and every dangerous-construct
  fingerprint, so the interesting population is fully covered.
- **Chunking**: the classifiers have a 512-token window; a 1.7MB diff is
  thousands of chunks. The prototype scores per-chunk and takes the max —
  the natural "worst chunk" semantics for a tripwire — and records
  latency so a full-corpus run can be costed.
- **Metrics**: hit rate per technique and per pattern family; overlap
  between techniques; manual adjudication of every hit (expected to be
  few); wall-clock per patch; dependency weight; and for Prompt Guard
  the practical friction of gated weights (the official Hugging Face
  repo requires a license click-through, which matters for CI).
- **Ground truth caveat**: we have no known-positive corpus. A zero hit
  rate is a *finding* (nothing out there is trying this today — cheap
  insurance, quiet tripwire), not a failure. Synthetic positives
  (injection payloads spliced into real patches) validate that each
  technique fires at all.

## Phases

| Phase | Plan | Status |
|-------|------|--------|
| 1. Regex tripwire prototype + full-corpus measurement | [findings](PLAN-prompt-injection-screening-phase-01-findings.md) | Measured: 81/60,642 hits (0.13%), all benign |
| 2. Model-based scorers (Prompt Guard 2, LLM Guard) on the sample | (findings in the same document) | Measured: 28% FP; Prompt Guard 2 leg blocked by the HF license gate |
| 3. Decision: graduate a technique to an observation rule, or shelve with findings | — | Recommendation written; operator decision pending |

Phase 3 only gets its own plan file if something graduates; a shelve
decision is recorded in the findings document.

## Success criteria

* Hit rates for all three techniques are measured on the real corpus
  and written down with the adjudication of every hit.
* Synthetic positives confirm each technique actually fires on the
  attack shapes it claims to cover.
* The prototypes are committed (under `tools/injection-screening/`),
  offline-runnable against a corpus path, and dependency-isolated (the
  model-based ones document their venv setup; nothing heavyweight
  touches the package's dependencies).
* A clear go/no-go recommendation for graduating each technique, with
  the observation-kind design sketched if any graduates.
* The honest boundaries are stated wherever results are reported: open
  detectors are evadable by targeted attackers; this is a tripwire.

## Out of scope / honest boundaries

- Defending the *agentic* jumbo tier (shelved) — containment (read-only
  tools, no network) is that tier's defence, not input scoring.
- Any claim of catching adaptive, targeted injection. Open weights are
  white-box to the attacker.
- Client-side anything: screening is curation-side only.
- Building the observation rule itself — that is phase 3, and only if
  the findings justify it.

## Open questions

- Is a file-type carve-out (e.g. `.po`/RTL locales for bidi controls)
  needed, or are raw bidi-control hits rare enough to adjudicate by
  hand? (Measure first.)
- Prompt Guard 2 licensing: the official weights sit behind a Hugging
  Face license gate. Is a gated download acceptable for a curation-side
  CI job, or does that push us to LLM Guard's ungated model?
- Threshold choice for the learned scorers: pick from the corpus score
  distribution (expect bimodal near 0/1) rather than the vendors'
  defaults.

## Administration

Update `docs/plans/index.md` when phases change status. One commit per
logical change; prototypes and findings land together per phase.

## Back brief

Before executing any step of this plan, back brief the operator on the
intended work and how it aligns with this plan.
