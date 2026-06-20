# Phase 1 — Fingerprint & dedup: the distinct-patch count

Part of [PLAN-patch-classification.md](PLAN-patch-classification.md).
Plan this phase at **high effort**: the normalisation choice determines
the headline number *and* the fingerprint key every later phase joins on,
and the archive-wide crawl has scale and politeness constraints.

**Status: complete.** All steps (1a corpus builder, 1b fingerprint, 1c
measure) are implemented, tested, and committed, and the crawl has been run
over the whole trixie archive. **Result: ≈61,572 carried patches → 60,640
distinct (dedup 1.02x) — the premise that dedup would collapse the scale was
falsified; 99.2% of distinct patches are unique to one package.** Full
analysis in
[PLAN-patch-classification-phase-01-findings.md](PLAN-patch-classification-phase-01-findings.md).
Connection reuse and transient-failure hardening were added to the crawler
along the way (a real run rate-limited a home DNS resolver and surfaced a
resume-skips-failures gap).

## Prompt

Explore before changing. The acquisition this phase needs already exists
in miniature: read `divergulent/sources/apt_patches.py` — `_download_source`
fetches only the `.dsc` + `.debian.tar.*` (skipping the huge `.orig`), and
`_extract_patches` reads the **full** `debian/patches/series` straight from
the `.debian.tar.*` and returns `{patch_name: text}`. That series read is
**uncapped by construction** (it parses the real series file, so it sees all
148 of grub2's patches, not the 60 the sources.debian.org patches API
renders). Also read `divergulent/builder.py` (`enumerate_archive`,
`latest_versions`) for how the archive Sources index is walked, and
`divergulent/sources/debian_patches.py` for the `DivergenceState` /
`PackagePatches` model. Research `3.0 (quilt)` series semantics and unified
diff structure (hunk `@@ -a,b +c,d @@` headers, `---`/`+++` file headers,
`a/`…`b/` prefixes, git `index`/`diff --git` decoration) so normalisation
strips what varies without changing meaning.

This is **curation-side** code: it runs centrally (the builder / a
deb-src-enabled Debian environment), never on a client. Clients never
fingerprint or classify — they consume a signed bundle in a later phase.

## Objective

Replace "**≈60k carried patches**" with "**N distinct** patches" — the
single number that reframes the scale — by:

1. Building a central corpus of **every carried patch body** across the
   trixie archive (uncapped, via the apt-source lean fetch).
2. Normalising and fingerprinting each patch (`sha256(normalised_diff)`).
3. Deduplicating, and measuring: the distinct count, the dedup ratio, the
   recurrence distribution, and the most-duplicated patches.

The phase produces both a **finding** (the number, written up) and reusable
**infrastructure** (a content-addressed corpus + a versioned fingerprint
index) that phases 2–6 consume without re-crawling. **No classification
happens here** — no rules, no categories, no LLM. Only fingerprint, dedup,
and measure.

## Why this is first, and what it unblocks

- It is the master plan's headline success criterion ("N distinct").
- It produces the corpus (bodies) and fingerprint index that the ledger
  (phase 2), deterministic rules (phase 3) and LLM tier (phase 4) all read.
  Without it, every later phase would re-crawl the archive.
- It **closes the remaining data-acquisition prerequisite**. The merged
  counts fix corrected the *count* from the API's `count` field, but the
  rendered names/bodies are still capped at 60. The corpus builder here is
  the "builder fetches the full names + bodies" prerequisite, realised.

## Design decisions

### Acquisition: reuse the apt-source lean fetch, centrally, archive-wide
Reuse `apt_patches._download_source` / `_extract_patches` rather than the
sources.debian.org patches API: the API's rendered `patches` array is capped
at 60, so it **cannot enumerate** the full series for the heavily-patched
packages that matter most. The `.debian.tar.*` series read is uncapped.

- **Runs centrally**, in a deb-src-enabled environment (CI's
  `build-cache.yml` already enables deb-src; a `debian:trixie` container also
  works and aligns with the matrix plan). Not on clients.
- **Drive the crawl off the PATCHED set.** The freshly built divergence map
  already lists every source package with `state == PATCHED` (~18.6k); use it
  (or `enumerate_archive` filtered to quilt format) as the work-list, so we
  only fetch packages that actually carry patches.
- **Politeness at scale.** ~18.6k downloads of small `.debian.tar.*` files
  (KB–low MB) from the **mirror** (built for bulk), not the sources.debian.org
  web service. Bounded concurrency, the descriptive `DEFAULT_USER_AGENT`
  already set, and one download per package gets *all* its patches. This is a
  periodic central job, not per-client traffic.

### Separate acquisition (network, once) from fingerprinting (offline, re-runnable)
This is the load-bearing architectural choice. Store every raw patch body in
a **content-addressed corpus** keyed by its raw `sha256`, with an index row
per `(source_package, version, patch_name) → raw_sha256`. Version-pinned, so
re-crawls are incremental (skip versions already present). Fingerprinting and
measurement then read the corpus **offline**. Because tuning normalisation
changes the headline number, we must be able to re-normalise and re-measure
**without re-crawling**.

### Normalisation — defined, versioned, and its sensitivity *measured*
Normalise a unified diff to a canonical form that strips what varies without
changing meaning, then `fingerprint = sha256(normalised)`. Carry a
`normalisation_version` (a fingerprint is `(normalisation_version, digest)`)
because every later phase's key depends on it.

**v1 strips (uncontroversial):**
- hunk `@@ -a,b +c,d @@` line-number ranges (and the volatile function-context
  tail after the second `@@`),
- git decoration: `diff --git …`, `index <hash>..<hash>`, `new/old mode`,
- `---`/`+++` file-header timestamps and the `a/`…`b/` path prefixes,
- trailing whitespace; normalise line endings.

**Two knobs that materially move the distinct count — decide by measuring,
not a priori:**
- **Path in the fingerprint?** Stripping the target path merges "the same
  diff applied to different files/packages" — which is exactly the recurring
  boilerplate bucket (FSF-address updates, autotools/`config.guess` regen)
  that we expect to be most of the mass. Keeping it treats per-file patches
  as distinct.
- **Context lines in the fingerprint?** Including the unchanged ` ` context
  splits otherwise-identical edits whose surroundings differ; fingerprinting
  only the `+`/`-` lines merges them.

Phase 1 computes the distinct count under a small **matrix** (path in/out ×
context in/out), reports the sensitivity, and *then* freezes the canonical
choice as `normalisation_version = 1`, documenting exactly what it strips.

### Deliverables / measurement
- **Headline:** total carried patches in corpus, **distinct fingerprints**,
  dedup ratio.
- **Recurrence distribution:** histogram of fingerprint multiplicity (how
  many fingerprints appear in 1, 2, …, k packages). The high-multiplicity
  tail is the boilerplate that a single phase-3 rule can peel off.
- **Top-recurring fingerprints:** the most-duplicated patches with a sample
  body — the likely-trivial mass.
- **Per-package:** patches vs distinct.
- **Honest accounting:** how many packages/patches were skipped as non-quilt
  (`1.0` `.diff.gz`, native) or lost to fetch failures — **no silent
  truncation**.
- A short **findings note** capturing the number and what it means — the
  artifact that "reframes the scale".

### Artifacts (reusable infrastructure)
- **Corpus:** raw patch bodies, content-addressed by raw `sha256` (so even
  pre-normalisation identical bodies dedup, and bodies are fetched once). A
  build artifact / cache dir, **not** committed (large); the small index +
  findings are.
- **Fingerprint index:** rows of `(normalisation_version, fingerprint,
  source_package, version, patch_name, raw_sha256, normalised_size)`. This is
  what phase 2's ledger and phase 3's rules join against. Lean to **sqlite**
  (`sqlite3` is stdlib — no new runtime dep; clients never load it) for the
  index; keep bodies as files.

### Code location
A new curation-side subpackage `divergulent/classify/` (`corpus.py`,
`fingerprint.py`, `measure.py`). It is **builder-only** — not imported by any
client command — consistent with "clients never run a classifier". Mirrors
how `apt_patches.py` already ships builder/curation logic in the package.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 1a | high | opus | none | Add `divergulent/classify/corpus.py`: a central corpus builder. Given a work-list of `(source_package, version)` (the PATCHED entries from the divergence map, or `builder.enumerate_archive` filtered to quilt format), reuse `apt_patches._download_source` + `_extract_patches` to fetch the **full** patch series per package, store each raw body content-addressed by its raw `sha256` under a cache dir, and record index rows `(source_package, version, patch_name, raw_sha256)`. Bounded concurrency; resumable/incremental (skip `(package, version)` already in the corpus); make the download boundary injectable (mirror `apt_patches`' `download=`/`fetch=` seams) so tests run offline against a fixture `.debian.tar.*`. Account explicitly for non-quilt skips and fetch failures (counts, not silent drops). Do **not** download `.orig`. Tests offline only. |
| 1b | high | opus | none | Add `divergulent/classify/fingerprint.py`: pure functions `normalise(diff_text, *, version=1) -> str` and `fingerprint(diff_text, *, version=1) -> tuple[int, str]` (`(version, sha256_hex)`). Implement the v1 strip-set from this plan (hunk line-numbers + function-context tail, git `diff --git`/`index`/mode lines, `---`/`+++` timestamps, `a/`…`b/` prefixes, trailing whitespace, line endings). Keep path-handling and context-handling behind explicit parameters so 1c can run the sensitivity matrix. Heavy unit tests with fixture diffs: trivially-different copies (offsets/whitespace/timestamps) share a fingerprint; genuinely-different diffs do not; the recurring-boilerplate case (same edit, different file) merges only when paths are stripped. |
| 1c | high | opus | none | Add `divergulent/classify/measure.py` (and a thin `tools/` entrypoint): read the corpus, apply 1b across the normalisation matrix (path in/out × context in/out), and emit the distinct count, dedup ratio, multiplicity histogram, top-recurring list with sample bodies, per-package patches-vs-distinct, and the non-quilt/failure accounting. Write the fingerprint index to sqlite. Produce a findings note (`docs/plans/PLAN-patch-classification-phase-01-findings.md` or `docs/`). Tests: a small synthetic corpus yields a known distinct count and histogram. |
| 1d | medium | sonnet | none | After the crawl is run and the number is known: write up the finding, freeze `normalisation_version = 1` (record the chosen path/context decision and why), update the master plan's Execution table + reconcile its prerequisites, and update `ARCHITECTURE.md` / `AGENTS.md` to describe the new curation-side `classify/` subpackage and the corpus/index artifacts. |

1b is independent of 1a; 1c depends on both; 1d follows the operational
crawl. One commit per step (house rule).

## Operational note (running the crawl)

1a/1c produce the tooling; the headline number requires actually running the
crawl once in a **deb-src-enabled trixie environment**. Options, cheapest
first: the local Kasm host if deb-src is enabled, a `debian:trixie` Docker
container (aligns with the matrix plan and keeps the host clean), or a CI
job. The corpus is cacheable/immutable, so this is a one-time cost that later
phases reuse. Treat the run as a reviewed operational step, not a silent
side effect of a test.

## Testing requirements

- Suite stays **offline**: the apt invocation/download is injected with a
  fixture `.debian.tar.*` or a fake `_extract_patches`; no real apt/network.
- Fingerprint tests prove normalisation merges trivially-different copies and
  separates genuinely-different diffs, including the path/context knobs.
- Measurement tests run against a small synthetic corpus with a known answer.
- `pre-commit run --all-files` passes; 120-col, single-quote house style.

## Success criteria for this phase

- A reproducible **distinct-patch count** across the trixie archive, with the
  dedup distribution and the top-recurring patches, captured in a findings
  note — the number that reframes "60k".
- A reusable **content-addressed corpus + versioned fingerprint index** that
  later phases consume without re-crawling.
- `normalise()` is a **pure, versioned, unit-tested** function; the v1
  strip-set is documented and its path/context sensitivity reported.
- **Honest accounting** of what was excluded (non-quilt, fetch failures).
- The corpus/index code is curation-side only — no client command imports it.

## Open questions for this phase

- **Normalisation v1 exact strip-set**, especially paths and context —
  resolved empirically in 1c, then frozen.
- **Index store** — sqlite (lean to this; stdlib, join-friendly for later
  phases) vs JSONL.
- **Corpus retention** — cache dir / build artifact (not committed) vs a
  published artifact; the index + findings are small enough to commit.
- **Non-quilt `1.0` (`.diff.gz`) sources** — scope phase 1 to quilt series
  (the overwhelming majority of carried patches) and *report* the excluded
  set; single-combined-diff handling is future work.
- **Where the crawl runs** operationally — ties into the multi-release matrix
  plan.

## Out of scope (later phases)

- Any classification, rules, categories, or DEP-3-as-verdict (phase 3).
- The rule registry / decision ledger data model (phase 2).
- The LLM triage tier (phase 4).
- Any client-facing display or signed classification bundle (phase 5).

## Back brief

Before executing any step, back brief the operator on your understanding of
this phase and how the intended work aligns with it.
