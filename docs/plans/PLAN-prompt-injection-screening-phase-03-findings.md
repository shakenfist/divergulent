# Prompt-injection screening — phase 3 findings (graduation)

Phase 3 graduated the deterministic tripwire into divergulent proper as a
versioned `llm-injection-suspect` ledger observation that skips the LLM and
routes a patch to a human. This note records the tuned family set, the
full-corpus re-measurement, and the adjudication of every surviving hit.

Measured on the real reviews corpus (60,642 deduplicated fingerprints,
~634 MB of bodies) on 2026-07-15 with the SHIPPED module
(`divergulent/classify/injection.py`), splitting each body into the LLM-visible
diff region and the header region exactly as `record.py` does.

## The tuned family set

Ported from the phase-1 prototype (`tools/injection-screening/tripwire.py`) and
tuned per the phase-3 plan:

* **Dropped `large-base64-blob`** — 56 of the raw prototype's 81 hits, all
  benign embedded assets (data URIs, PEM test keys, hex fixtures). A long
  base64 run is not instruction-shaped and has no place in a gate that skips
  the LLM.
* **Split the old `invisible-unicode`** into:
  * `invisible-tag-block` — the Unicode tag block (U+E0000–E007F), the
    invisible-instruction vector, kept whole (near-zero legitimate use);
  * `zero-width` — a RUN of **four or more** zero-width characters, with U+200D
    (the emoji joiner) EXCLUDED. The threshold and the exclusion between them
    retire the 23 benign phase-1 hits (emoji ZWJ sequences; typographic ZWSP
    pairs).
* **Kept `instruction-phrase`, `chat-template-marker`, `bidi-control`** as-is —
  the strong, low-noise families.

Each family stays separately versioned and separately reportable, folded into
`INJECTION_RULES_VERSION` so a future retune supersedes and re-scans.

## Full-corpus result

**60,642 patches in 66 s, 4 hits in 3 distinct patches** (raw prototype: 81 /
60,642). Every hit adjudicated by hand — all benign:

| Package | Family / region | Adjudication |
|---------|-----------------|--------------|
| kwin | instruction-phrase / header | Benign English: "...attract user's attention to the system prompt." The phrase "system prompt" in ordinary prose. Header-only — does NOT skip the LLM. |
| projectile | instruction-phrase / diff | Benign elisp: `projectile-completion-system prompt choices` — the token boundary accidentally spans "system prompt". |
| gimp-data-extras | zero-width / diff | Benign Khmer translation (`.po`): a run of ZWSP (U+200B) used as Khmer word boundaries. |
| gimp-data-extras | bidi-control / diff | Benign Arabic RTL override in the same `.po` (the predicted translation-file case, unchanged from phase 1). |

**Headline: still zero suspected injection attempts in the wild.** The two
gimp-data-extras hits are both benign translation data in one package; as in
phase 1, one package's locale file does not justify a file-type carve-out.

Effect on triage: the diff-region hits fall on **two** patches (projectile,
gimp-data-extras), so at this snapshot the observation would skip exactly two
patches from LLM triage to a human — both benign, both adjudicable in seconds.
The kwin header-only hit is recorded for provenance but does not divert triage
(the LLM never reads the header).

## What shipped

* `divergulent/classify/injection.py` — the pure scanner + the ledger read
  helpers (`injection_suspect_fingerprints`, `injection_by_fingerprint`).
* `record.py` — records `llm-injection-suspect` observations (diff + header
  regions) with supersede-then-append + idempotency, mirroring the
  reviewability/reach blocks; `body` is surfaced on the classify record so the
  diff is read once.
* `triage_driver.py` — a skip-LLM branch checked BEFORE every other skip: a
  diff-region suspect is routed to a human and never sent to the model, with an
  injection priority band (below risk, above provenance) that never crosses a
  risk boundary.
* `review_web.py` — a worklist badge and a review-page banner, both honestly
  worded ("injection-suspect — not sent to the LLM", never "malicious").

## Honest boundary (restated)

A tripwire, not a shield. The patterns are public, so a targeted attacker can
iterate offline until a payload scores clean. What it buys: every lazy,
untargeted, or copy-pasted payload the day it appears, each hit rare enough to
earn human eyes, at a cost of ~two benign human reviews across the whole
corpus. That asymmetry — not detector accuracy — is the argument for shipping
it.
