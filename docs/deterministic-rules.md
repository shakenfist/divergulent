# The deterministic rules

This document describes every deterministic rule in divergulent's
patch-classification pipeline: what each one matches, what it
decides, in what order, and what it deliberately refuses to decide.
It is written for a curious reader — including the author, months
later, checking what already exists — rather than as API reference.

The code is the final authority. The category rules live in
`divergulent/classify/rules.py` (the `_CATEGORY_RULES` table is the
precedence order), and each supporting axis in its own module under
`divergulent/classify/`. Where this document quotes corpus numbers,
they come from the June 2026 measurement of the Debian trixie corpus
— 60,640 distinct patch fingerprints (61,572 carried-patch
occurrences) — recorded in
[the phase-2](plans/PLAN-patch-classification-phase-02-findings.md)
and
[phase-3 findings](plans/PLAN-patch-classification-phase-03-findings.md).
They are indicative, not live: rerunning against a newer corpus will
shift them somewhat.

## Why deterministic rules come first

Classifying ~60k patches with a model costs real money and produces
answers that need verification. A deterministic rule costs nothing,
gives the same answer every time, and can be *explained* — the
verdict carries the rule's id and a human-readable signal, so a
reader can check the reasoning rather than trust it. So the pipeline
settles everything it can structurally before any model runs.

Two design promises govern every rule:

1. **Deterministic rules settle only structurally-determined
   categories.** A change touching only build files *is* packaging; a
   change touching only the test suite *is* test. But
   `bugfix` / `feature` / `security` are statements about *intent*,
   which structure cannot prove — so no deterministic rule ever
   assigns them (with one narrow, externally-corroborated exception:
   the [CVE cross-reference](#the-external-cross-reference) below).
2. **`security` and `malicious` are never guessed.** The
   dangerous-construct scan surfaces *candidates* for review; a
   flagged patch keeps its category. Most flags turn out benign, and
   that is fine — the value is that the few worth a look are surfaced
   rather than lost in 43,000 patches of residue.

## The foundations the rules stand on

Three deterministic layers run before any rule fires, and the rules
are only as trustworthy as these are.

### Fingerprinting: patch identity

Every patch body is normalised — decoration lines, file-header paths
and timestamps, hunk line numbers, and trailing whitespace stripped;
the DEP-3 author comment header skipped entirely — and SHA-256
hashed. `(normalisation version, hash)` is the **fingerprint**: the
identity every decision keys on, and the key clients use to look up
verdicts in the published bundle. Two packages carrying byte-varied
copies of the same logical patch get the same fingerprint; a patch's
author header can be reworded without orphaning its verdict.
(`fingerprint.py`)

### File typing: code vs prose

Every file a diff touches is typed into exactly one of **test /
build / doc / code / data**, by first-match precedence
(`content.py:_classify_file`):

1. **test** — a `test`/`tests`/`spec`/`t` directory component, or a
   `test_*` / `*_test.*` / `*.t` basename. Checked first so a test
   source file (`tests/foo.c`) counts as test, not code.
2. **build** — anything under `debian/` (Debian packaging is build,
   not upstream source), or a build-system basename or extension
   (`Makefile`, `configure.ac`, `*.m4`, `CMakeLists.txt`, ...).
3. **doc** — manpages (`*.1`–`*.9`), doc extensions
   (`*.md`, `*.rst`, ...), documentation directories, `README`-like
   basenames. Doc deliberately beats code, so prose is never typed as
   code.
4. **code** — a recognised source extension. This is the gate for the
   dangerous-construct scan, so it is only reached for genuine
   source.
5. **data** — everything else (`*.json`, `*.po`, images, ...).

The code-vs-prose split is the load-bearing guarantee behind "never
cry wolf": a shell-out mentioned in a manpage must never look like a
shell-out added to a `.c` file.

### Trivial-change detection

Four conservative boolean detections on the diff as a whole: it
normalises to empty (mode/permission-only changes), it touches only
ignore files with ignore-pattern lines, it changes only whitespace,
or every changed line is blank or a comment. Each defaults to false
and is set only when certain — calling a real change trivial is the
dangerous failure, so uncertainty always falls through to
"substantive". (`content.py`)

## The category rules

Eight rules, applied first-match-wins. The first rule that matches
supplies the category, the confidence, and a human-readable signal;
the rule's id is recorded as the decision's provenance.

Measured over the trixie corpus (phase-3 ledger build; `test-only`
had not yet been added — see below):

| # | rule id | category | fingerprints settled | share |
| --- | --- | --- | ---: | ---: |
| 1 | `empty` | packaging | 1 | ~0% |
| 2 | `ignore-file-only` | packaging | 133 | 0.2% |
| 3 | `whitespace-only` | packaging | 70 | 0.1% |
| 4 | `comment-only` | documentation | 571 | 0.9% |
| 5 | `doc-only` | documentation | 3,780 | 6.2% |
| 6 | `build-only` | packaging | 13,178 | 21.7% |
| 7 | `test-only` | test | (added later; peels ~15% of the residue, ≈6,400) | |
| 8 | `substantive` | unknown | 42,907 | 70.8% |

Two rules do nearly all the work: `build-only` alone settles over a
fifth of the whole corpus (quilt `.pc` ignores, `debian/rules`
tweaks, autotools regeneration — the boilerplate of packaging), and
`doc-only` most of the rest. The trivial-change rules earn their keep
in precision rather than volume — and `empty` matched exactly one
distinct fingerprint (a permission-only change, carried by ~30
packages). It stays because it is free, obviously correct, and that
one fingerprint would otherwise consume an LLM call per corpus
rebuild.

### 1. `empty` → packaging

Fires when the diff normalises to nothing: mode-only changes,
pure decoration. There is no content to classify, and shipping a
mode change is a packaging act.

### 2. `ignore-file-only` → packaging

Fires when the patch touches only ignore files (`.gitignore` and
kin) and adds only ignore-pattern lines — the classic "add `.pc` and
`_build` to the ignores" patch that quilt-based packaging generates.

### 3. `whitespace-only` → packaging

Fires when the change differs only in whitespace: a cosmetic
reindent, no semantic change. Ordered before the file-type rules so
that a whitespace-only edit to a `.c` file is settled as packaging
rather than falling through as substantive code.

### 4. `comment-only` → documentation

Fires when every changed line is blank or a comment — prose that
happens to live in a code file. Classified as documentation, not
packaging: the *content* is prose.

### 5. `doc-only` → documentation

Fires when every touched file is typed `doc`. Manpage fixes,
spelling-error patches, README updates — 3,780 fingerprints of the
corpus, the second-largest deterministic win.

### 6. `build-only` → packaging

Fires when the touched files are all `build`, or `build` plus
`data`, with no code or doc — `debian/` changes, `Makefile` and
autotools surgery, CMake version pins. The workhorse: 13,178
fingerprints, almost all of the deterministic `packaging` total.

### 7. `test-only` → test

Fires when every touched file is typed `test`. A patch that changes
only the upstream test suite cannot alter the shipped artifact, so it
is structurally determined and low-risk for divergence — but it gets
its own category rather than `packaging`, because an upstream test
suite is upstream source, not Debian packaging.

This rule has a story the others don't: it was added *after* the
first full corpus run, when operating the LLM triage tier showed
~15% of the residue touched only test files — the one deterministic
pattern the residue still contained
([phase-4 findings](plans/PLAN-patch-classification-phase-04-findings.md)).
It was rolled out with the non-destructive `ledger record` pass,
which superseded exactly the affected fingerprints' decisions and
nothing else. It is the working example of how a new rule enters the
system: observed in the residue, crystallised into code, applied by
supersession with full provenance.

### 8. `substantive` → unknown

Always matches; deliberately last. Everything not settled above —
42,907 fingerprints, 70.8% of the corpus — is handed to the LLM and
human tiers with the honest label `unknown` and low confidence. The
rules do not guess `bugfix` vs `feature` vs `security` here; those
are intent, and structure cannot prove intent.

### Precedence rationale

The trivial-change rules (1–4) come first because they describe
changes with no real semantic content — the most confidently settled
of all, whatever files they touch. The file-type rules (5–7) are
mutually exclusive (a diff cannot be all-doc *and* all-build), so
their relative order is immaterial. `substantive` always matches, so
it is last.

## The dangerous-construct scan

Alongside the category rules, a pattern scan runs over **added lines
in code-typed files only** and emits *flags*: evidence-bearing
candidates for review. A flag is never a verdict — a patch that adds
`system(...)` keeps its category (usually `unknown`/substantive) and
the flag rides alongside, raising its review priority.

The pattern set is deliberately narrow and precise: each regex
requires a syntactic shape genuine source uses to *invoke* behaviour,
not merely mention a word.

| flag detail | matches |
| --- | --- |
| `shell-out` | `os.system(`, `system(`, `popen(`, `Runtime.getRuntime().exec`, `subprocess` with `shell=True` |
| `fetch-piped-to-shell` | `curl`/`wget` piped into `sh`/`bash` (optionally via `sudo`) on one line |
| `decode-piped-to-shell` | `base64 -d`/`--decode` piped into a shell |
| `decode-exec` | `eval(` over `base64`/`atob`, `exec(` over `decode` |
| `embedded-private-key` | a `-----BEGIN ... PRIVATE KEY-----` block added to code |
| `reverse-shell` | `/dev/tcp/`, `nc ... -e` |
| `shell-out` (shell files only) | backtick command substitution — scoped to `.sh`/`.bash` files only |

Precision choices worth knowing:

- `system(` / `popen(` require the trailing `(`, so the word
  "system" in an identifier or comment cannot fire.
- Bare `subprocess` use never fires — only the dangerous
  `shell=True` shape.
- A bare `curl` never fires — the fetch *and* the pipe-to-shell must
  be on the same line.
- Backtick command substitution flags only in shell files. Everywhere
  else a backtick is a JavaScript template literal or a Lisp
  quasiquote; the first corpus run confirmed those cry wolf, and
  scoping the pattern to shell removed ~212 false `shell-out` flags —
  58% of the total at the time.

Over the trixie corpus the scan produced **651 flags**: 649
`shell-out`, one `decode-exec`, one `fetch-piped-to-shell`. That
shape is the point — a signal rare enough that every flag can
actually be looked at.

## The deterministic axes

Beyond the category, three cheap deterministic axes are recorded for
every patch, and one deterministic cull runs inside the risk gate.
Each rides alongside the category as a supersedable observation with
its own provenance.

### Reviewability (size) — `size-rule`

Changed-line count (added + removed — what a human actually reads),
bucketed at thresholds chosen from the full-corpus distribution
(98.4% of fingerprints are ≤500 changed lines; 0.25% exceed 5,000):

| level | changed lines | consequence |
| --- | --- | --- |
| `normal` | ≤ 500 | fully processed |
| `large` | 501 – 5,000 | LLM passes see a capped head of the diff |
| `oversized` | > 5,000 | LLM passes skip it entirely |

An `oversized` diff is not line-reviewable by a human and overflows a
model's context; the honest disposition is its own bucket in the
review UI, not a fake verdict. (`reviewability.py`)

### Reach (install base) — `popcon-rule`

How many machines actually run this code, from a pinned Debian
popcon snapshot, as a t-shirt size. A source's install count is the
**max** over its binary packages (max, not sum — summing would
double-count a machine installing several binaries from one source),
taken as a fraction of the snapshot's largest count (the
near-universal base package):

| level | fraction of anchor | intuition |
| --- | --- | --- |
| `XL` | ≥ 0.5 | the base system |
| `L` | ≥ 0.1 | nginx, apache, vim |
| `M` | ≥ 0.01 | postgresql, docker |
| `S` | ≥ 0.001 | the long tail |
| `XS` | < 0.001 | too rare to report |

The one hard rule: **reach orders patches within a security tier,
never across tiers**. Popularity is not risk — a ubiquitous package
carrying a benign patch is not a concern, and a widely-installed
nothing must never outrank an obscure something. (`reach.py`)

### The provably-benign risk cull — `risk-cull`

The security-risk gate is an LLM pass, but a deterministic cull runs
first: a patch that is empty, whitespace-only, comment-only,
documentation-only, or touches only translation catalogues
(`.po`/`.pot`) and `changelog`/`copyright` files is scored risk
`none` with no LLM call. Deliberately **narrower** than the
`packaging` category — a `debian/rules` change is packaging but can
flip a build-hardening flag, so build files are never culled. Every
sub-check is conservative: anything unsure goes to the model.
(`risk.py:provably_benign`)

### The external cross-reference — `external-cve`

The one place a deterministic rule may settle `security` — because
the evidence is external corroboration, not guessed intent. A
patch's *claimed* CVE identifiers are verified against a pinned
snapshot of the Debian Security Tracker (and its claimed Debian bug
numbers against a BTS snapshot):

- **Confirmed** — the tracker really does associate that CVE with
  this source package — *and* the patch touches code *and* its
  content category is still `unknown`: settles `security`. Both
  guards matter: a manpage that merely cites a CVE is
  high-confidence `documentation` and stays that way.
- **Contradicted** — an invented CVE, a wrong-package id, a
  non-existent bug: records a `claim-unconfirmed` observation. This
  is a review nudge and a badge, never a category and never a malice
  verdict — the plausible innocent explanations (typo, copied
  boilerplate) far outnumber the sinister one.
- **Unknown** — the snapshot cannot answer: no decision at all.

Verdicts carry the snapshot identity and a freshness horizon (30
days); past it, the recorder re-verifies and retracts a
corroboration the tracker no longer supports. The tier is a scalpel,
not a sweep: only 10.1% of corpus patches claim any reference at all
(8.9% a Debian bug, 1.44% a CVE). (`cross_reference.py`)

### The claim extractor — deterministic, but never trusted

For completeness: the DEP-3 claim extraction (`claim.py`) is also
deterministic — claimed category by keyword precedence (security >
packaging > documentation > feature > bugfix), CVE and bug
references, forwarded status. But its input is author-controlled, so
nothing downstream ever treats it as ground truth: it exists to be
*compared* against the content (a disagreement raises review
priority) and to supply the references the cross-reference tier then
verifies independently.

## Precedence and provenance

When tiers disagree, the derived verdict picks one winner per
fingerprint by strict precedence:

> **human > verified-LLM > deterministic heuristic > unverified-LLM**

then most-recent, then confidence, then insertion order. The
deliberate asymmetry: an adversarially-*verified* LLM verdict
outranks a deterministic rule (it read the actual diff with more
context than structure offers), but an *unverified* LLM draft ranks
below one — an unreviewed guess never beats an explainable rule.

Every deterministic tier carries its own version constant
(`RULES_VERSION`, `CONTENT_RULE_VERSION`, `CLAIM_RULE_VERSION`,
`REVIEWABILITY_VERSION`, `REACH_VERSION`, `EXTERNAL_CVE_VERSION`), so
changing a rule or threshold is a new identity: re-recording
supersedes exactly the decisions the old version made, the audit
trail keeps the old ones, and nothing is silently reclassified. The
ledger's rule registry is generated from `_CATEGORY_RULES` itself, so
the registry cannot drift from the code.

## What to update when rules change

If you add, remove, or re-order a category rule, change a pattern in
the dangerous-construct tables, adjust an axis threshold, or bump any
`*_VERSION` constant — update this document (the tables above and,
for a new rule, a short subsection saying what it matches and why it
is safe to settle deterministically). The pre-push checklist
(`PUSH-TEMPLATE.md`) checks for exactly this.
