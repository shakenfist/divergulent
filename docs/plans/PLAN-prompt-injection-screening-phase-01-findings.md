# Prompt-injection screening — findings (phases 1 and 2)

Measured on the real reviews corpus (60,642 deduplicated fingerprints,
~634MB of patch bodies) on 2026-07-12, with the prototypes in
`tools/injection-screening/`.

## Phase 1: the deterministic tripwire

Full-corpus scan: **60,642 patches in 73 seconds, 81 patches hit
(0.13%)** — small enough to adjudicate every hit by hand, which we did.

| Family / region | Patches | Adjudication |
|-----------------|---------|--------------|
| large-base64-blob / diff | 56 | All benign: embedded `data:` images, PEM test keys, hex test fixtures, generated documentation assets. |
| invisible-unicode / diff+header | 23 | All benign: emoji ZWJ sequences (U+200D) in test strings and translations, zero-width chars in natural-language locale data. |
| instruction-phrase / diff+header | 2 | Both benign English: kwin ("attract user's attention to the system prompt"), projectile (elisp `projectile-completion-system prompt choices` spanning the phrase). |
| bidi-control / diff | 1 | Benign: legitimate Arabic RTL formatting in a gimp-data-extras `.po` file — the predicted translation-file case; one hit in 60k does not justify a carve-out. |

Synthetic positives (direct instruction, instruction-in-C-comment,
chat-template block, tag-block invisible text, RLO override) all fire.

**Headline finding: zero suspected injection attempts in the wild.** The
Debian trixie patch corpus contains, as of this snapshot, nothing that
looks like an attack on an LLM classifier. A quiet tripwire at a 0.13%
raw hit rate (every hit cheap to adjudicate, most retirable by tuning the
base64 family) is exactly the "cheap insurance" the plan hoped for.

## Phase 2: learned encoder classifiers

### LLM Guard (via its underlying model)

`pip install llm-guard` does not build on Python 3.13 (pinned
`spacy==3.7.1` → `blis` 0.7 build failure) — a real dependency-weight
finding. We drove its underlying scorer
(`protectai/deberta-v3-base-prompt-injection-v2`, ungated, DeBERTa-v3
~184M params) directly via transformers: 512-token chunks, max-over-chunks
per patch, CPU inference.

Sample: 1,000 seeded-random fingerprints + all 81 tripwire hits +
3 synthetics.

Results (1,077 scored on CPU, 3,905 chunks, 11 patches hit the
100-chunk cap, 1.45s/patch — a full-corpus scan would be ~24 CPU-hours
vs the tripwire's 73 seconds):

- Synthetics all fire: 0.998–1.000. The model works as designed on
  chat-shaped attacks.
- **306 of 1,077 patches (28.4%) score >= 0.5; 221 (20.5%) score >= 0.9**
  — on a random sample of Debian patches, essentially all benign. The
  score distribution in tenths was
  `[635, 58, 33, 21, 24, 15, 18, 21, 31, 221]`: bimodal as advertised,
  but with the "injection" mode packed with ordinary patches (nginx,
  calibre, rubocop, libgpiod, fonts-osifont...). Diffs of code are far
  outside the model's chat-text training distribution, and it confidently
  misfires on them.
- No threshold rescues it: even at >= 0.999 the false-positive rate
  extrapolates to thousands of patches over the full corpus, every one a
  human adjudication, for a technique whose true-positive population
  (per the tripwire sweep) is currently zero.

### Llama Prompt Guard 2

Blocked at evaluation time: the official weights
(`meta-llama/Llama-Prompt-Guard-2-86M`) sit behind a manual Hugging Face
license gate and no local token was configured. We refuse unofficial
re-uploads of gated weights on provenance grounds. The gate itself is a
finding: a curation-side CI job would need a Meta-licensed HF token as a
secret, which is real operational friction compared to the ungated
Protect AI model. To complete this leg: accept the license on the model
page, `hf auth login`, then re-run `model_scorer.py
--model meta-llama/Llama-Prompt-Guard-2-86M`.

## Recommendation

**Graduate the deterministic tripwire; drop the learned classifiers.**

- The tripwire is cheap (73s over the corpus), quiet (0.13% raw, and the
  base64 family — 56 of the 81 hits, all benign embedded assets — can be
  retired or tightened before graduation, leaving ~25 hits corpus-wide),
  fully offline-testable, zero-dependency, and every hit was adjudicable
  in seconds. It fires on every synthetic attack shape. This is the
  "cheap insurance, quiet tripwire" outcome the plan hoped for, and it
  fits the ledger as a versioned `llm-injection-suspect` observation.
- The learned classifier is the opposite: a 28% false-positive rate on
  benign Debian patches at the vendor's threshold, ~1,200x slower, and a
  torch/transformers dependency wall. Diffs are out-of-distribution for
  chat-trained detectors; the published OOD improvements do not survive
  contact with unified diff format. Not worth completing the Prompt
  Guard 2 leg unless someone wants the number for completeness — the
  architecture (same DeBERTa family, overlapping training data) predicts
  the same failure mode.
- The wild-corpus base rate is zero today. The tripwire's value is (a)
  it would catch the lazy/copy-paste attack the day it appears, (b) its
  hits are so rare that each one deserves — and gets — human eyes, and
  (c) it costs nothing to keep running. That asymmetry, not detector
  accuracy, is the argument for shipping it.
