# Phase 3 — Graduate the tripwire as an `llm-injection-suspect` observation

Part of [PLAN-prompt-injection-screening.md](PLAN-prompt-injection-screening.md).
Plan this phase at **high effort**: this is the graduation the master plan's
phase 3 row reserved judgement on, and it wires a new signal into the ledger,
the triage driver's skip-LLM path, and the reviewer UI. The "no cry wolf"
promise and the "tripwire, not shield" posture both ride on getting the
family tuning and the routing behaviour right.

**Status: implemented (2026-07-15).** Phases 1–2 measured the three techniques
on the real corpus and recommended graduating the deterministic tripwire and
dropping the learned classifiers
([findings](PLAN-prompt-injection-screening-phase-01-findings.md)). All five
steps below shipped: the tuned scanner (`injection.py`), the ledger
observation (`record.py`), the skip-LLM routing (`triage_driver.py`), the
review badge (`review_web.py`), and the full-corpus re-measurement
([phase 3 findings](PLAN-prompt-injection-screening-phase-03-findings.md): 4
benign hits in 3 patches, zero suspected attacks in the wild).

## Why this is phase 3 now

The measurement is done and one-sided. On 60,642 deduplicated patches the
tripwire ran in 73 seconds with 81 raw hits (0.13%), every one adjudicated
benign by hand; the learned encoder classifier false-positived on 28% of
benign patches and dragged in a torch/transformers dependency wall. The
recommendation — graduate the tripwire, drop the classifiers — is written and
was accepted. What remains is engineering: port the evaluation prototype into
a shipped, versioned observation that the triage driver and review UI
consult. Nothing here re-opens the go/no-go; it executes it.

The graduation is worth doing even though the wild base rate is zero today,
for the three asymmetries the findings named: it catches the lazy or
copy-pasted attack the day it appears, its hits are rare enough that each
earns human eyes, and it costs nothing to keep running. It also closes the
loop the master plan opened: the diff bodies we feed the LLM are exactly the
text a supply-chain attacker controls, and this is the deterministic gate in
front of that surface.

## Prompt

Explore before changing. The integration points are already mapped — verify
them, do not re-derive:

- **The prototype to port** — `tools/injection-screening/tripwire.py`:
  `FAMILIES` (tripwire.py:38-66), `scan_text(text) -> list[tuple[str, str]]`
  (tripwire.py:69-79), `scan_patch(body) -> list[dict]` (tripwire.py:82-94,
  splits `diff` vs `header` region via `triage.diff_body`). This is
  evaluation code under `tools/`, not importable as shipped
  `divergulent.classify`.
- **The observation shape to mirror** — `rules.py`: `Flag` dataclass
  (rules.py:55-73; `kind`, `detail`, `evidence`), `scan_dangerous_constructs`
  (rules.py:314-344), the `_CATEGORY_RULES` registry + `RULES_VERSION`
  (rules.py:48, 234-243). Dangerous-construct is the closest sibling: a scan
  of diff text producing evidence-bearing `Flag`s that are *candidates, never
  verdicts*.
- **How a flag becomes a live observation** — `record.py`: the
  `for flag in verdict.flags:` loop (record.py:296-308) calling
  `ledger.append_observation(...)` guarded by `ledger.live_observation_exists`
  for idempotency, with `_SCAN_RULE_ID = 'dangerous-construct-scan'`
  (record.py:51). The reviewability (record.py:310-332) and reach
  (record.py:334-356) blocks show the separate-observation, supersede-then-
  append pattern with their own KIND/OBSERVED_BY/VERSION constants.
- **The ledger API** — `ledger.append_observation` (ledger.py:498-516),
  `ledger.live_observation_exists`, `ledger.live_observations` (ledger.py:707),
  `ledger.observations_for` (ledger.py:732). The `observation` table columns:
  fingerprint, kind, detail, evidence, observed_by, rule_version, observed_at,
  superseded_at.
- **The skip-LLM hook** — `triage_driver.py`, `run_triage`
  (triage_driver.py:345-456): the oversized branch
  (triage_driver.py:387-396) builds `oversized_fps =
  reviewability.oversized_fingerprints(conn)` (triage_driver.py:371) and, for
  a matching item, calls `triage_record.record_triage_to_human(...)` with a
  reason and `continue`s **before any LLM call**. The raw-char cap
  `MAX_DIFF_CHARS_FOR_LLM` branch (triage_driver.py:60, 400-409) is the second
  existing skip. Contrast the dangerous-construct model
  (`_fingerprints_with_dangerous_construct`, triage_driver.py:108-118) which
  does *not* skip — it only bumps priority and forces the verified result to a
  human.
- **The reviewer badge** — `review_web.py`: `_worklist_row`
  (review_web.py:164-182) emits `reviewability`/`risk`/`reach` badge fields;
  they are threaded from `live_observations` at review_web.py:253-271, with
  `.rev`/`.reach` CSS in the template. `review.py`: `build_review_context`
  (review.py:750-804) assembles the detail-page `ReviewContext`.
- **The findings that constrain tuning** —
  [phase-01 findings](PLAN-prompt-injection-screening-phase-01-findings.md):
  of the 81 hits, 56 were `large-base64-blob` (embedded assets) and 23 were
  `invisible-unicode` (emoji ZWJ / locale data) — all benign; only 2
  `instruction-phrase` and 1 `bidi-control`, also benign. Read this before
  touching the family set.

## Objective

Ship a deterministic, curation-side `llm-injection-suspect` observation that:

1. scans the **LLM-visible diff body** of each fingerprint for injection-
   shaped text, using a **tuned** subset of the prototype's families (near-
   zero false skips, not the raw 81-hit rate);
2. records a hit as a **versioned ledger observation** with evidence, per the
   supersede-then-append pattern — never a classification verdict, never a
   malice pronouncement;
3. causes the triage driver to **skip the LLM and route to a human** with a
   priority bump, so attacker-authored instructions are never fed to the
   model they target;
4. **badges** the reason in the reviewer UI so a human sees *why* the patch
   was diverted;
5. stays entirely curation-side — no client command imports it, no new
   runtime dependency.

## Design decisions

### A hit skips the LLM — it does not merely re-prioritise

This is the one place the injection observation must diverge from its
dangerous-construct sibling. Dangerous-construct bumps priority but still
sends the diff to the LLM, because the goal there is a *better-reviewed*
verdict. Here the whole point is the opposite: the flagged text is a
*candidate attack on the classifier itself*, so feeding it to the LLM is the
thing we are trying to avoid. The observation therefore hooks the **oversized
skip branch** model (triage_driver.py:387-396), not the dangerous-construct
priority model: build a `suspect_fps` set from live observations and, before
the LLM call, record the item to a human with reason
`llm-injection-suspect: <families>` and `continue`. The failure modes stay
asymmetric in our favour — a false skip costs one human review (the designed
behaviour); the LLM never sees the payload.

### Only diff-region hits skip the LLM; header hits are recorded, not routed

The prototype scans `diff` and `header` regions separately
(scan_patch, tripwire.py:82-94). The LLM sees only the diff body
(`triage.diff_body` strips the DEP-3 claim), so **only a diff-region hit
justifies skipping the LLM**. A header-region hit is still worth recording as
a distinct, lower-priority observation detail (humans and the web UI *do* see
the header), but it must not divert triage — routing on text the model never
reads would be crying wolf. Encode the region in the observation `detail`
(e.g. `instruction-phrase/diff` vs `instruction-phrase/header`) and gate the
skip on region == diff.

### Tune the family set before graduation; drop the noisy families

The raw prototype fires 81 times, but 79 of those are two benign-noisy
families. For an observation that *skips the LLM*, every false skip is a
wasted human review, so the shipped family set must be tuned toward the ~3
real-signal hits, not the 81:

- **Drop `large-base64-blob` entirely.** A long base64 run is not
  instruction-shaped and was 56/81 hits, all benign embedded assets. It is
  the weakest injection signal and the noisiest; it does not belong in a
  skip-the-LLM gate.
- **Refine `invisible-unicode`.** Split it: keep the **Unicode tag block**
  (U+E0000–E007F — the invisible-instruction vector, effectively zero
  legitimate use in a Debian patch) as a strong family, and **narrow the
  zero-width sub-pattern** so emoji ZWJ (U+200D between emoji, the source of
  the 23 benign hits) does not fire. Measure the residue; a standalone
  zero-width space in ordinary text stays suspicious, an emoji ZWJ does not.
- **Keep `instruction-phrase`, `chat-template-marker`, and `bidi-control`
  as-is** — these are the strong, low-noise families. (The 2
  instruction-phrase and 1 bidi hits in the corpus were benign English /
  legitimate Arabic RTL; one bidi hit in 60k does not justify a file-type
  carve-out, per the phase-1 finding, but re-confirm on the tuned run.)

Each family stays **separately versioned and separately reportable** (the
prototype's design), so a future noisy family can be retired without
disturbing the quiet ones.

### The scanner is a new shipped module, not an import from `tools/`

Port `FAMILIES` / `scan_text` / the region split into a new pure module
`divergulent/classify/injection.py` exposing
`scan_injection(diff_body: str) -> list[Flag]` returning
`Flag(kind='llm-injection-suspect', detail='<family>/<region>',
evidence='<snippet>')`. The `tools/injection-screening/` prototype stays as
the evaluation harness (it also drives the model scorer and the findings). The
shipped module and the prototype should share the *pattern definitions* by the
shipped module being the source of truth and the prototype importing from it —
or, if that couples `tools/` to the package awkwardly, by a documented
copy with a test asserting the two family sets agree. Prefer the shared-source
approach; decide during 3a.

### Circular-import care when wiring the scan

`scan_injection` needs the diff-body region, which comes from
`triage.diff_body`. Do **not** import `triage` from `rules.py` (risk of a
cycle, and the injection scan is not a content-category rule). Instead run the
scan in `record.py` — which already imports both `triage`/`content` and the
ledger — as its **own observation block** mirroring the reviewability and
reach blocks (record.py:310-356), with its own
`_INJECTION_KIND = 'llm-injection-suspect'`,
`_INJECTION_RULE_ID = 'injection-scan'`, and `INJECTION_RULES_VERSION`
constants, supersede-then-append and idempotent via `live_observation_exists`.
This keeps the content rule engine untouched and the new signal decoupled.

### Versioned, supersedable, re-runnable like every other rule

`INJECTION_RULES_VERSION` folds the family set + tuning into the observation's
`rule_version`. Bumping it (e.g. retiring a family) supersedes prior
observations and re-scans exactly like the dangerous-construct and
reviewability rules already do. The observation is deterministic: same diff
bytes in, same families out, offline, CPU-only, no network.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 3a | high | opus | none | Add `divergulent/classify/injection.py`: port `FAMILIES` + `scan_text` from `tools/injection-screening/tripwire.py`, **tuned** per Design decisions — drop `large-base64-blob`, split `invisible-unicode` into tag-block (keep) and a narrowed zero-width sub-pattern that does not fire on emoji ZWJ, keep the other three families. Expose `scan_injection(diff_body) -> list[Flag]` returning `Flag(kind='llm-injection-suspect', detail='<family>/<region>', evidence=snippet)`. Make the shipped module the single source of the pattern set and have the `tools/` prototype import it (or, if awkward, add a test asserting the family sets agree). Keep all non-ASCII patterns as `\u`/`\U` escapes (never literal invisible/bidi chars in source — hostile to review and to Edit). Heavy offline unit tests: each family fires on its synthetic positive (direct instruction, C-comment instruction, chat-template block, tag-block invisible text, RLO override); emoji-ZWJ and embedded-base64 fixtures stay clean; region split (diff vs header) is exercised. |
| 3b | high | opus | none | Wire the scan into `record.py` as its own observation block, mirroring the reviewability/reach blocks (record.py:310-356). Add `_INJECTION_KIND`, `_INJECTION_RULE_ID`, `INJECTION_RULES_VERSION` constants. For each fingerprint, run `scan_injection` over `triage.diff_body(body)` for the diff region and over the header region separately; append one observation per (family, region) hit with evidence, superseding prior live injection observations for the fingerprint and idempotent via `live_observation_exists`. Do NOT touch `rules.py`/`classify_content`. Offline tests over a synthetic ledger: a diff-region hit and a header-region hit both record with the right detail; re-running is idempotent; a version bump supersedes. |
| 3c | high | opus | none | In `triage_driver.py`, add a skip-LLM branch mirroring the oversized branch (triage_driver.py:387-396): build `suspect_fps` = fingerprints with a live `llm-injection-suspect` observation **in the diff region** (a helper like `_fingerprints_with_injection_suspect(conn)`, following `_fingerprints_with_dangerous_construct`, triage_driver.py:108-118, but filtered to diff-region details). Before the LLM call, if `item.fingerprint in suspect_fps`, call `triage_record.record_triage_to_human(...)` with reason `llm-injection-suspect: <families>`, bump the appropriate stat (e.g. `skipped_injection`/`needs_human`), and `continue`. Header-only hits must NOT skip. Offline tests: a diff-region suspect fingerprint is routed to human without an LLM call; a header-only hit is not skipped; the reason string names the families. |
| 3d | medium | sonnet | none | Badge the observation in the reviewer UI. In `review_web.py` add an `injection` badge field to `_worklist_row` (review_web.py:164-182) fed from `live_observations` filtered by `llm-injection-suspect` (thread it in at review_web.py:253-271 like `risk`/`reach`), with matching template + CSS (mirror `.rev`/`.reach`). Surface the families/evidence on the detail page via `build_review_context` (review.py:750-804) using `ledger.observations_for`. Add tests mirroring the existing reviewability/reach badge tests. Keep the badge honest: it says "injection-suspect — human-routed", never "malicious". |
| 3e | — | — | none | **Operational, reviewed step (not a code sub-agent).** Re-run the tuned scanner over the real corpus/ledger, curation-side, offline, CPU-only. Confirm the tuned family set fires on far fewer than 81 patches (target: the handful of real-signal hits, ~3 plus whatever the narrowed zero-width family now yields), adjudicate every hit by hand, and confirm no benign patch is now being skipped from LLM triage in a way that surprises us. Write the numbers into the phase-01 findings document (or a short phase-03 findings note) and flip the master-plan phase-3 row + `docs/plans/index.md` status to reflect the graduation. |

3a is independent; 3b depends on 3a; 3c depends on 3b; 3d depends on 3b; 3e
depends on 3b–3d being in. One commit per step.

## Testing requirements

- `scan_injection` is **pure** and unit-tested offline; no network, no model.
- Every shipped family fires on a synthetic positive **and** stays clean on
  its known benign driver (emoji ZWJ, embedded base64) — both directions
  tested, mirroring the code-vs-prose tests in phase 2.
- The diff-vs-header region split is tested, and the **skip gate is proven to
  fire only on diff-region hits**.
- Observation recording is idempotent and supersedes on a version bump
  (ledger-level test).
- The triage skip is tested end-to-end at the driver level: suspect
  fingerprint → human, **no LLM call**.
- No literal invisible/bidi Unicode in source — patterns use `\u`/`\U`
  escapes; a test or grep asserts the module is ASCII-only.
- `pre-commit run --all-files` passes; house style (single quotes,
  double-quote docstrings, 120 cols, trimmed trailing whitespace).

## Success criteria for this phase

- A shipped `divergulent/classify/injection.py` with a tuned family set whose
  full-corpus hit rate is measured, adjudicated, and materially lower than the
  raw prototype's 81 (the noisy families are gone).
- An `llm-injection-suspect` ledger observation, versioned and supersedable,
  carrying evidence, recorded idempotently — never a verdict, never a malice
  claim.
- The triage driver **skips the LLM and routes to a human** on a diff-region
  hit; header-only hits are recorded but do not divert triage.
- The reviewer UI badges the diversion with an honest, human-routed reason.
- Curation-side only: no client command imports `injection.py`; no new
  runtime dependency.
- The honest boundary is restated where it is surfaced: this is a tripwire for
  lazy/untargeted payloads, not a shield against an adaptive attacker who can
  iterate offline against open patterns.

## Open questions for this phase

- **Narrowed zero-width pattern** — what exact rule separates emoji ZWJ
  (legit) from a suspicious standalone zero-width space? Candidate: fire on
  zero-width chars **not** immediately flanked by emoji codepoints, or only on
  runs/counts above a threshold. Decide from the tuned re-measurement (3a/3e),
  not a priori.
- **Shared pattern source vs documented copy** — does making the shipped
  `injection.py` the source of truth and importing it from the `tools/`
  prototype create an awkward `tools/`→package coupling in the eval venv? If
  so, fall back to a copy plus an agreement test. Resolve in 3a.
- **Stat/skip naming** — is `skipped_injection` the right counter, and should
  the review queue show injection-skipped items above or below oversized ones
  in priority? Both are human-routed; pick an order and note it.
- **Does a header-only hit deserve any badge at all**, or is recording it
  (for provenance) without surfacing it enough? Lean toward a quiet,
  lower-priority badge so a human can still notice a header-region oddity.

## Out of scope (deliberately)

- The learned encoder classifiers (Prompt Guard 2, LLM Guard) — dropped in
  phase 2 on measured false-positive and dependency-weight grounds. Not
  revisited here.
- Defending the shelved agentic "jumbo" tier — its defence is containment
  (read-only tools, no network), not input scoring.
- Any claim of catching adaptive, targeted injection; open patterns are
  white-box to an attacker.
- Client-side anything: screening stays curation-side.

## Back brief

Before executing any step, back brief the operator on your understanding of
this phase and how the intended work aligns with it. In particular, confirm
the skip-the-LLM routing choice (vs the dangerous-construct priority-bump
model) and the family-tuning decisions before writing code.
