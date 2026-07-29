# Generated-marking phase 5 — translation catalogues (measurement)

Measure a corroborated translation-catalogue detector — Qt Linguist
`.ts` and gettext `.po`/`.pot` — over the whole corpus **before** it is
allowed to mark anything. This is the master plan's "non-autotools
generator families: measured first, added only when a real corpus
population shows up" clause exercised for the first time, and it
follows phase 1's evaluate-first shape exactly: candidate-only
machinery in the scanner module, the measurement prototype extended to
report on it, a findings document that adjudicates the numbers, and
only then a promotion decision.

## Motivating case

acetoneiso 2.4-6's `translate.patch` (fingerprint `6b51f47b…`): 36 Qt
Linguist `.ts` catalogues, ~58k added lines, plus a ~70-changed-line
hand-written residue (`acetoneiso.pro` / `acetoneiso.qrc` registering
the new locales). Today the pipeline hits every failure mode at once:

- `content.py` types `.ts` as **code** (it is also TypeScript's
  extension), so the dangerous-construct scan ran over 58k lines of
  XML and recorded a `shell-out` observation whose "construct" is the
  GPL license text inside a translation string ("…operating **system
  (if** any)…" matches `\bsystem\s*\(`).
- The risk gate burned an Opus call on a 5.4 MB head-capped diff and
  returned `elevated` — recall-safe, but computed from translation
  noise, and it put the patch in the review queue's top priority band.
- `reviewability` says `oversized`, so triage skipped it and routed it
  to a human as `unknown`.

The shape is exactly gatos: a huge mechanical bulk burying a small
hand-written residue — and phases 2–4 already built everything needed
to route and display that shape. What is missing is only the detector.

## Why the extension alone is not evidence

`.ts` is Qt Linguist **and** TypeScript, and `content._CODE_EXTENSIONS`
already claims it for TypeScript. The `Makefile.in` lesson (phase-1
findings: ~⅔ of extension/name matches were hand-written) says
extension-only marking is unsafe; the phase-5 smoke run confirms it in
the other direction — early-corpus `.ts` touches are overwhelmingly
TypeScript. So each extension gets **content corroborators**, reported
separately so the findings can adjudicate their individual strength:

- `ts-doctype` — `<!DOCTYPE TS>` or a line-anchored `<TS …>` root
  element (the new-file / full-rewrite case; a mid-line TypeScript
  generic like `Promise<TS>` cannot fire it).
- `ts-elements` — two *distinct* message-structure element kinds
  (`<message…`, `<source>`, `<translation…`) in the region (the
  update-hunk-deep-in-the-catalogue case).
- `po-msgid-msgstr` — both a prefix-anchored `msgid "` and
  `msgstr "` (incl. plural `msgstr[n]`) line in the region.
- `po-header` — the catalogue's own `"Project-Id-Version:` header.

## Posture

Identical to the master plan's, and worth restating because
translations sharpen it: **a mark, never a verdict**. Translation
catalogues are a real attack surface — historically format-string
bugs via malicious `msgstr`, and translated UI strings are shown
verbatim to users — so the mark may route and de-emphasise but must
never settle a category. `.po` files are also human-*authored*
(translators) even though tool-managed, so the family's evidence
must carry its own signal names rather than borrowing autotools'
generator/banner vocabulary.

## What this phase ships

1. **Candidate machinery in `generated.py`** — `TRANSLATION_EXTENSIONS`,
   the corroborator patterns, and `candidate_translation_hits(text)`,
   mirroring `candidate_banner_hits`: measured, and `scan()` never
   consults any of it. No rule-version bump — nothing marks.
2. **Measurement prototype extension** — `tools/generated-marking/
   measure.py` gains a `translation_candidates` report section:
   per-extension corroboration rates; the uncorroborated `.ts`
   population (the TypeScript collision, with examples); how
   `content._classify_file` types corroborated files today; the
   would-be coverage distribution and `ge_0_5` population; the
   oversized-unlock arithmetic (union with existing generated marks,
   `combined_residue` against `REVIEWABILITY_OVERSIZED_LINES`, counted
   only when the generated mark alone had not already unlocked it);
   dangerous-construct hits *inside* corroborated catalogue files (the
   false-positive population an eventual mark would re-attribute); and
   an informational compiled-catalogue (`.qm`/`.mo`/`.gmo`) tally.
3. **The full-corpus run + findings** — results committed as
   `tools/generated-marking/results-translations-full-corpus.json`,
   adjudicated in
   [PLAN-generated-marking-phase-05-findings.md](PLAN-generated-marking-phase-05-findings.md).

## Steps

| Step | Effort | Model | Brief |
|------|--------|-------|-------|
| S1 | high | opus | **Candidate machinery + prototype extension + tests.** `TranslationCandidate` / `candidate_translation_hits` in `generated.py` (candidate-only, `scan()` untouched); the `translation_candidates` accumulator/report/summary sections in `measure.py`; unit tests for every corroborator edge (TypeScript decoy incl. the mid-line generic, update-hunk-only corroboration, plural `msgstr[n]`, comment-only `.po` hunk staying uncorroborated, backup-suffix stripping, `scan()` never marking a candidate) and a dedicated fixture corpus proving the measure sections (per-extension counts, typed-as, coverage buckets, the oversized-unlock case, construct attribution, the compiled tally). One commit. |
| S2 | med | opus | **Full-corpus run + findings.** Run the extended prototype over the real corpus (read-only), commit the results JSON, and write the findings doc: corroboration rates per extension and per corroborator, the uncorroborated populations in both directions (TypeScript `.ts`; real catalogues whose hunks never show structure — the recall cost), the would-be marked population and its residue sizes, the newly-unlocked list, the construct-hit population, and a proposed promotion decision (family name, signal names, whether `.pot` and `.desktop` join). One commit. |
| S3 | — | — | **Adjudication.** Management session reviews the findings; promotion (a `translations` family in `scan()`, `GENERATED_RULES_VERSION` bump, re-record, routing/UI riding along for free) is a separate follow-up once adjudicated — it is phase 2–4 plumbing applied to a new family, not new machinery. *Outcome: approved and implemented; see the findings' "Adjudication outcome" section for the real-run numbers.* |

## Testing requirements

- Every corroborator has a positive and a negative test; the
  TypeScript decoy proves the anchoring (mid-line `<TS>` never fires).
- `scan()` provably never marks a candidate (the promotion is a
  decision, not an accident).
- The measure fixtures live in their own corpus so phase 1's
  exact-count assertions stay byte-identical.
- `pre-commit run --all-files` green.

## Open questions (for S2's findings to answer with data)

- Do real Qt catalogues fail to corroborate often enough to matter
  (hunks that touch only `<numerusform>` runs, say)? That is the
  recall cost of corroboration and must be sized, not assumed small.
- Does `.pot` occur enough to keep, or is it noise?
- Do `.desktop`-file locale-key blocks (`Name[de]=`) form a
  population worth a corroborator of their own, or wait like cmake?
- Should the eventual family also say something about binary
  compiled-catalogue (`.qm`/`.mo`) touches, which the text scanners
  cannot corroborate?
- Family and signal naming: "translations" is honest about `.po`
  (human-authored, tool-managed) where "generated" would overclaim —
  does the observation detail need a family-specific vocabulary?

## Out of scope

- Any marking, observation recording, or rule-version bump — that is
  the post-adjudication follow-up.
- Any change to `content.py`'s `.ts`-as-code typing. The construct
  false positives route through the mark's attribution (phase 4's
  tally) once the family is promoted; re-typing `.ts` by content is a
  separate, riskier change to the code-vs-prose gate.
- `.desktop` / cmake / protobuf families (measured later, same shape).

## Back brief

Before executing: this phase ships **candidate machinery and numbers**
— nothing observable changes for the pipeline. The detector is
extension + content corroboration, never extension alone, because
`.ts` is also TypeScript and the corpus's early `.ts` touches *are*
TypeScript. Everything downstream (observation, residue unlock,
projection, UI collapse, construct-tally split) already exists and is
family-agnostic; if the findings support promotion, the follow-up is
small and mechanical. Measurement precedes belief.
