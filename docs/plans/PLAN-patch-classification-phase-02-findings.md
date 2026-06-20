# Phase 2 findings: deterministic classification of the corpus

Results of running the phase-2 extractors over the phase-1 corpus
([phase-1 findings](PLAN-patch-classification-phase-01-findings.md)), for
[PLAN-patch-classification-phase-02-extractors.md](PLAN-patch-classification-phase-02-extractors.md).
60,640 distinct fingerprints (61,572 carried-patch occurrences), classified
offline in ~42s.

## Headline

> **29.2% of distinct patches settle deterministically; 70.8% are the
> substantive residue handed to phase 4.**

| content category | fingerprints | % | occurrences | % |
| --- | ---: | ---: | ---: | ---: |
| packaging | 13,382 | 22.1% | 13,671 | 22.2% |
| documentation | 4,351 | 7.2% | 4,412 | 7.2% |
| **unknown-substantive (phase-4 residue)** | **42,907** | **70.8%** | 43,489 | 70.6% |

Combined with phase 1 (no dedup shortcut), the real shape of the problem:
~60k distinct carried patches, of which ~43k are genuine code changes whose
category (bugfix / feature / security) deterministic content rules
deliberately do **not** guess. The honest reduction is 60k → ~43k for the
phase-4 tier — useful, not dramatic. The deterministic rules confidently peel
off only what content can confirm (trivial/packaging/doc).

## The claim is usually absent — content carries the load

| consistency | fingerprints |
| --- | ---: |
| claim-unknown | 35,268 (58%) |
| content-substantive | 18,172 (30%) |
| agree | 3,800 |
| disagree | 3,400 |

**58% of patches carry no usable claimed category** — no DEP-3 description, or
one with no category keyword. This confirms the known DEP-3 sparsity and has a
strategic consequence: the "author claims benign but the diff changes code"
signal, central to the threat model, can only fire on the ~42% that *make* a
claim. Most of the classification leverage must come from **content analysis
and the phase-4 tier**, not from catching authors in a lie — they mostly say
nothing. Only 3,400 fingerprints have a claim that *disagrees* with content.

## Review flag: 4.6%, and what it's really made of

Review-flagged: **2,787 fingerprints (4.6%)** — but the composition matters:

- **2,417** are *benign-claim-over-code* with **no** dangerous construct: the
  author claims documentation/packaging but the diff touches code
  substantively. In practice most are benign — e.g. `spelling-errors.patch`
  fixing a typo inside a Perl string or comment, which touches a `.pm` file and
  so isn't `comment_only`. The flag is doing its job (surface for a look,
  pronounce nothing), but it is **noisy**: separating genuine deception from a
  spelling fix in code needs the phase-4 tier or a tighter rule.
- **370** carry a dangerous-construct candidate (see below).

## Dangerous-construct candidates — and a confirmed false-positive source

| detail | fingerprints | occurrences |
| --- | ---: | ---: |
| shell-out | 368 | 399 |
| decode-exec | 1 | 1 |
| fetch-piped-to-shell | 1 | 1 |

The measurement did exactly what "start narrow, grow from real findings" was
for: it **confirmed the backtick command-substitution sub-pattern cries
wolf**. The single most-recurring "shell-out" flag is the d3
`reproducible_build.diff` (29 packages) matching a **JavaScript template
literal** (`` `// ${meta.homepage} v${meta.version} ...` ``); `mew` matches
**Emacs Lisp quasiquote / docstring** notation (`` `symbol' ``). A flat
code-added-lines scan cannot tell a shell `` `cmd` `` from a JS/Lisp backtick.
The genuine signal underneath is real — `jtreg7` adds
`` JTREG_HOME=`readlink -f ...` `` (true shell command substitution), and the
`system(`/`popen(`/`shell=True` patterns are sound — but the shell-out count
is inflated by these false positives.

**Identified fix (the #1 refinement):** make the dangerous-construct scan
**language-aware** — the backtick (and any shell-specific) pattern should fire
only on shell-typed files (`.sh`/`.bash`), not on every code file. This needs
the scan to see each added line's file type, not a flat list. The other
patterns (`system(`, `curl … | sh`, embedded private key, `/dev/tcp/`) are
language-agnostic and stay. After this, the dangerous-construct queue becomes
genuinely reviewable.

## What this means going forward

1. **Phase 4's residue is ~43k substantive patches** — that is the real scale
   of the work the LLM/human tier must triage, and the headline number the
   project now states honestly.
2. **Tighten the backtick scan** (language-aware) before the dangerous-construct
   queue is worth a human's time — a small, well-scoped change the data
   pinpointed.
3. **Claim/content mismatch is a thinner signal than hoped** (58% make no
   claim); lean on content and phase 4, and treat the benign-claim-over-code
   flag as a low-confidence prioritiser, not an alarm.
4. The classification table (`classification.sqlite`, one row per fingerprint
   with its verdict, flags, and rule provenance) is the artifact phase 3 wraps
   in the versioned ledger.
