# Phase 2 — Deterministic signal extractors

Part of [PLAN-patch-classification.md](PLAN-patch-classification.md).
Plan this phase at **high effort**: the content rules decide what we say
about a patch, and the whole project's "no cry wolf" promise rides on them
classifying at the right semantic level and never pronouncing malice.

**Status: complete.** All four steps (2a claim, 2b content profile, 2c rules
+ dangerous-construct scan, 2d driver) are implemented, tested, and committed,
and the classifier has been run over the whole phase-1 corpus. **Result:
29.2% of the 60,640 distinct patches settle deterministically
(packaging/documentation); 70.8% (~43k) are the substantive residue for phase
4.** The run also confirmed the dangerous-construct backtick sub-pattern cries
wolf on JS/Lisp backticks (the #1 follow-up: make the scan language-aware) and
that 58% of patches carry no usable DEP-3 claim. Full analysis in
[PLAN-patch-classification-phase-02-findings.md](PLAN-patch-classification-phase-02-findings.md).

## Why this is phase 2 now

Phase 1 found **no dedup shortcut** — ≈60,640 distinct patches, 99.2% unique
to one package ([findings](PLAN-patch-classification-phase-01-findings.md)).
So the leverage cannot come from collapsing duplicates; it must come from
**deterministic rules that classify many distinct-but-similar patches by what
they do**. That is this phase, promoted ahead of the ledger so the rules are
built against the real corpus and their shape informs the phase-3 ledger
schema rather than the other way round.

## Prompt

Explore before changing:
- `divergulent/classify/` — `corpus.py` (the body store + `patches.jsonl`
  manifest), `fingerprint.py` (`normalise`/`fingerprint`, canonical v1 =
  `strip_path,keep_context`), `measure.py` (the sqlite index this phase reads:
  `patch(source_package, version, patch_name, raw_sha256, fingerprint)` + a
  `meta` table). Reuse `measure.read_body()` to fetch a body by `raw_sha256`.
- `divergulent/dep3.py` — REUSE for claim parsing: `parse_header(text)` →
  the DEP-3 field dict (`Description`, `Forwarded`, `Origin`, `Bug`…),
  `bug_references(text)` → `BugRef`s, `classify(text, name)` →
  DEBIAN_ONLY/FORWARDED/UNKNOWN (the forwarded-ness signal), and the
  `deb-`/`debian-` filename + `# DP:` heuristics.
- [PLAN-patch-classification-phase-01-findings.md](PLAN-patch-classification-phase-01-findings.md)
  — the recurring-tail taxonomy is ground truth for the first rules: quilt
  `.pc` ignore tweaks, permission-only mode changes (empty normalised body),
  ecosystem build patches (node/d3, Perl `versioneer`, autotools), `python`→
  `python3` sweeps.

Research the unified-diff structure (file headers, hunk targets, `+`/`-`
lines) and DEP-3 semantics. The corpus from phase 1 is the test bed — validate
rules against real patches, not just synthetic fixtures.

## Objective

Build **deterministic, pure** signal extractors that, for each distinct
fingerprint, classify the patch by **what it does** — emitting a plain
`fingerprint → classification` table (no ledger yet; phase 3 wraps it). The
classification separates **claim** (author-controlled metadata) from
**content** (the diff, ground truth), settles the easy categories with high
confidence, surfaces the claim/content mismatches, and hands the genuinely-
substantive residue to phase 4 — **without ever pronouncing a patch malicious**.

This phase also produces the **measurement** that matters now that dedup gave
nothing: *of the ≈60,640 distinct patches, how many settle deterministically
(packaging/documentation), how large is the substantive residue, and how many
carry a review flag?*

## Design decisions

### Claim and content are classified separately; content is ground truth
Two independent extractors. The **claim** comes from author-controlled
metadata (DEP-3 description, `Forwarded`/`Bug`/CVE refs, the patch filename,
the `debian/patches/` subdirectory) and is never trusted as truth. The
**content** comes from the diff alone. Their **disagreement is the loudest
signal** — especially "claims benign, changes code". This is the core of the
plan's threat model: a malicious diff with a `docs/typo.patch` name and a "fix
spelling" description must not get a free pass.

### Deterministic rules settle the easy categories; they never pronounce malice
The provisional enum is `packaging · documentation · bugfix · security ·
feature · unknown` (+ confidence + a claim/content-consistency flag). Be honest
about what deterministic rules *can* settle from content:
- **`packaging`** — trivial-only (whitespace/comment/mode-only), ignore-file
  tweaks (`.pc`, `_build/`), `debian/`-only, build-system files
  (`configure.ac`, `Makefile`, `CMakeLists.txt`, `*.m4`). High confidence.
- **`documentation`** — touches *only* documentation files (manpages `*.[0-9]`,
  `*.md`/`*.rst`/`*.txt`, `doc/`/`docs/` trees, `*.pod`). High confidence.
- **substantive (residue)** — everything else stays **`unknown` content-category
  with a `substantive` marker**. Deterministic rules deliberately do **not**
  split `bugfix`/`feature`/`security` from content — that needs phase 4. Claiming
  otherwise would cry wolf.
- **`security` is a *flag/candidate*, not a content verdict.** A CVE reference is
  a *claim*; a dangerous construct added in code is a *candidate worth review*.
  Neither is pronounced as a confirmed "security patch".

### Content analysis works at the right semantic level (code vs prose)
The motivating lesson: a string-grep for shell constructs **cried wolf on a
manpage** mentioning `/bin/sh`; a code-aware check did not. So every file in a
diff is typed (**code / doc / build / data / test**) by extension and path, and
content rules that look for executable behaviour run **only on `+` lines in
*code* files**. A construct mentioned in prose is not the same as one added to
code.

### Dangerous-construct detection surfaces candidates, never verdicts
A code-aware scan of added lines for executable-behaviour changes — shell-out
(`system(`, `exec`, backticks), network-fetch-piped-to-shell (`curl … | sh`),
`eval`/dynamic exec, `base64 … | sh`, newly-added URLs/IPs in code, changes to
crypto/key material or auth checks. Output is an **evidence-bearing candidate
flag**, ranked for human/LLM review — explicitly *not* a malice judgement. Most
will be benign (the phase-1 study found ~0 dangerous-in-code across the sample);
the value is that the few that fire are surfaced rather than lost.

### Output: a plain table now, with lightweight rule provenance for phase 3
Phase 2 writes a flat `fingerprint → classification` table (sqlite, alongside
the phase-1 index): `content_category`, `claim_category`, `consistency`,
`confidence`, `signals` (the evidence), `flags` (review/dangerous-construct),
and the **`rule_id`+`rule_version`** that fired. No versioned ledger /
supersession yet (phase 3) — but every verdict is tagged with its rule so phase
3's ledger can formalise the provenance it already carries. Rules are a small
**registry of pure functions**, each with an `id` and `version`, which is the
seed of phase 3's registry.

### Grounded and validated on the real corpus
The recurring-tail taxonomy is the acceptance test: the `.pc`/`_build` ignore
patches and the permission-only (empty-body) patches **must** come out
`packaging`; doc-only patches **must** come out `documentation`. Spot-check
against the cron/gnupg2/grub2 packages from the original study. The category
enum stays **versioned and provisional** until the data shows what users want.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 2a | medium | sonnet | none | Add `divergulent/classify/claim.py`: a pure `extract_claim(name, text) -> Claim` dataclass. Reuse `dep3.parse_header`, `dep3.bug_references`, `dep3.classify`. Capture: forwarded-ness (from `dep3.classify`), `BugRef`s, CVE ids (regex `CVE-\d{4}-\d{4,}` over header text), and a `claimed_category` derived from description keywords + filename + `debian/patches/` subdir (e.g. "typo"/"spelling"/"man page" → documentation; "CVE"/"security"/"overflow" → security; "add support"/"feature"/"new" → feature; "fix"/"crash"/"segfault" → bugfix; `deb-`/`debian/`/`.pc` → packaging). Everything here is a CLAIM, never trusted. Offline unit tests over fixture headers. |
| 2b | high | opus | none | Add `divergulent/classify/content.py`: a pure `profile(text) -> ContentProfile` over the diff body. Type every touched file (code/doc/build/data/test) by extension + path (manpages `*.[1-9]`, `*.md/*.rst/*.txt/*.pod`, `doc(s)/` → doc; `*.c/*.h/*.py/*.go/*.js/*.rs/*.cpp/...` → code; `configure.ac/Makefile*/CMakeLists.txt/*.m4/debian/` → build; tests dirs → test). Compute added/removed line counts per type, hunk count, and trivial-only flags: whitespace-only, comment-only (per-language comment syntax on changed lines), mode-only/empty-after-normalisation, and ignore-file-only (`.pc`,`_build` added to `.gitignore`-like files). Use `fingerprint.normalise` semantics where helpful. Heavy offline tests, including the phase-1 recurring-tail examples. |
| 2c | high | opus | none | Add `divergulent/classify/rules.py`: a registry of pure rules, each `(id, version, fn)`, mapping a `(Claim, ContentProfile)` to a partial verdict. Implement (1) the content-category rules — packaging (trivial-only / build / debian-only / ignore-file), documentation (doc-only), else substantive→unknown; (2) the **code-aware dangerous-construct scan** over `+` lines in *code* files only, emitting evidence-bearing candidate flags (NOT a verdict); (3) confidence assignment. This is the no-cry-wolf core: scan at the right semantic level, surface candidates, never pronounce malice. Tests must include the manpage-mentions-`/bin/sh` false-positive case staying clean, and a real dangerous construct in a code file firing as a *candidate*. |
| 2d | high | opus | none | Add `divergulent/classify/classify.py` (+ a `python -m` entrypoint): for each fingerprint in the phase-1 sqlite index, load one representative body (`measure.read_body`), run claim+content+rules, compute the **claim/content consistency** and **review flag** (claims benign but touches code substantively / adds a dangerous construct), and write a `classification` table (fingerprint, content_category, claim_category, consistency, confidence, signals, flags, rule_id, rule_version). Emit a summary report: counts by content_category, residue size (substantive-unknown), review-flag count, and a sample of each. VALIDATE against the recurring-tail taxonomy (the `.pc`/permission/doc patches classify as expected) and spot-check cron/gnupg2/grub2. Offline tests over a small synthetic index. |

2a is independent; 2b is independent; 2c depends on 2a+2b; 2d depends on 2c.
One commit per step.

## Operational note

The headline measurement (how much settles deterministically vs the
substantive residue) requires running 2d's classifier over the real phase-1
corpus/index — a curation-side, offline, CPU-only pass (no network), so it is
cheap and re-runnable. Treat the run as a reviewed step that produces a
findings note, like phase 1.

## Testing requirements

- All extractors are **pure** and unit-tested offline; no network.
- The recurring-tail taxonomy from phase 1 is an explicit acceptance test.
- The code-vs-prose distinction is tested both ways (manpage mention stays
  clean; code addition fires).
- Claim/content mismatch is tested (benign claim + substantive code change →
  review flag).
- `pre-commit run --all-files` passes; house style (single quotes, 120 cols).

## Success criteria for this phase

- A deterministic `fingerprint → classification` table over the whole corpus,
  each verdict carrying its evidence and the `rule_id`+`version` that produced
  it.
- A measured answer: **what fraction of the ≈60,640 settles deterministically
  (packaging/documentation), how big is the substantive residue (→ phase 4),
  and how many patches carry a review flag.**
- Claim and content are classified independently; their disagreement is
  surfaced.
- No category is pronounced that the content does not deterministically
  support; "security" and "malicious" are never deterministic verdicts —
  only claims or evidence-bearing candidate flags.
- Curation-side only: no client command imports `classify/`.

## Open questions for this phase

- **Category enum** — is packaging/documentation/bugfix/security/feature/unknown
  the right set, or do users want a different cut? Provisional; revisit once we
  see the distribution.
- **One body per fingerprint** — different packages' patches share a canonical
  fingerprint but may differ in their (stripped) metadata/claim. Classify
  content once per fingerprint; decide whether claim is per-fingerprint or
  per-(package,patch). Likely: content per fingerprint, claim summarised across
  occurrences with mismatch noted.
- **Confidence model** — a simple high/medium/low from which rule fired, or a
  score? Start simple.
- **Dangerous-construct rule set** — which constructs, and per which languages,
  without crying wolf. Start narrow and grow from real findings.

## Out of scope (later phases)

- The versioned, append-only decision **ledger** with supersession/redo
  (phase 3) — phase 2 emits a plain table with rule tags only.
- The **LLM triage tier** for the substantive residue (phase 4).
- Any client-facing display or signed classification bundle (phase 5).
- BTS / upstream cross-reference for `external` rules (phase 6).

## Back brief

Before executing any step, back brief the operator on your understanding of
this phase and how the intended work aligns with it.
