# Phase 1 findings: the distinct-patch count

Results of the first crawl, for [PLAN-patch-classification-phase-01-fingerprint.md](PLAN-patch-classification-phase-01-fingerprint.md).
Corpus built from the trixie divergence bundle's PATCHED set on 2026-06-20:
18,822 source packages, full uncapped patch series fetched per package.

## Headline

> **≈61,572 carried patches → 60,640 distinct.** Dedup ratio **1.02x**.

The premise this phase set out to test — that "≈60k raw patches" would
collapse to *far* fewer distinct ones once deduplicated (the "they're
probably all the same dumb boilerplate" hypothesis) — is **falsified by the
data**. Deduplication removes ~1.5% and no more. Debian's carried patches
are overwhelmingly **bespoke**: each is essentially unique to its package.

The multiplicity histogram makes this unambiguous:

| distinct packages a fingerprint recurs across | fingerprints |
| --- | ---: |
| 1 | 60,152 |
| 2 | 368 |
| 3–5 | 101 |
| 6–10 | 9 |
| 11–50 | 9 |
| 51+ | 1 |

**60,152 of 60,640 distinct patches (99.2%) appear in exactly one package.**

## The choice barely matters (low sensitivity)

The distinct count is robust to the normalisation knobs — all four variants
land within 2.5% of each other:

| variant | distinct fingerprints | dedup ratio |
| --- | ---: | ---: |
| `strip_path,keep_context` (**canonical, frozen as v1**) | 60,640 | 1.02x |
| `strip_path,drop_context` | 59,427 | 1.04x |
| `keep_path,keep_context` | 60,894 | 1.01x |
| `keep_path,drop_context` | 60,569 | 1.02x |

**Canonical v1 = `strip_path=True, drop_context=False`.** `strip_path`
merges the same change applied to differently-named files (a real, if tiny,
effect); `keep_context` is the conservative choice — two patches with
identical `+`/`-` lines but different surrounding code are *different*
changes and should not be merged (`drop_context` would over-merge ~1,200 of
them). Because the headline is insensitive to the choice, the number is
trustworthy regardless.

## The recurring tail is real but tiny — and it *is* the boilerplate

The ~488 fingerprints that recur across 2+ packages are exactly the
"trivial mass" the original question worried about — and they are <1% of
distinct patches (~2% of rows). They cluster into recognisable kinds:

- **Quilt/packaging hygiene** — adding `.pc` / `/.pc/` / `_build/` to ignore
  files (top fingerprint: 53 packages, mostly the Perl library ecosystem).
- **Permission-only changes** — `mode change 100755 → 100644` with no content
  diff (30 packages). These normalise to an *empty* body and all share the
  sha256-of-empty fingerprint, which correctly clusters "changes nothing but
  file modes". (Normalisation nuance: v1 drops mode lines as decoration, so a
  mode-only patch is content-empty; a future version could preserve the mode
  signal if we want to classify rather than just cluster these.)
- **Ecosystem-wide build patches** — the `node-d3-*` family sharing identical
  `SOURCE_DATE_EPOCH` reproducible-build and `require()`-conversion patches.
- **Language/toolchain compat sweeps** — `python` → `python3` shebang fixes,
  `versioneer.py` `SafeConfigParser` → `ConfigParser` (Python 3.12), autotools
  `AC_PATH_PROG` → `AC_PATH_TOOL` (cross-compilation), `debian/Makefile.plb`
  includes (Perl).

So the boilerplate exists and is cleanly identifiable — but it is a rounding
error against the ~60k genuinely distinct patches.

## Honest accounting

- 18,822 packages processed; **18,820 patched, 2 fetch failures, 0 non-quilt
  skipped**. A 0.01% failure rate, recorded (not silently dropped); a re-run
  retries the two transient failures.
- 61,572 patch rows; 61,191 distinct raw bodies; 60,640 distinct canonical
  fingerprints.
- DNS-safe: with per-worker keep-alive connection reuse, the whole crawl
  touched the resolver a handful of times, not ~37k.

## What this means for the classification plan

This **inverts the plan's framing** and should reshape the strategy:

1. **There is no dedup shortcut.** We cannot turn 60k into a few hundred by
   collapsing duplicates. ~60k distinct patches is the real working set.
2. **Leverage must come from *category* rules, not fingerprint identity.**
   The win is classifying many *distinct-but-similar* patches with cheap
   deterministic rules (directory taxonomy, "permission-only", "ignore-file
   tweak", "SafeConfigParser→ConfigParser", DEP-3/content patterns), not from
   exact-match dedup. This makes the *deterministic-rules-first* posture more
   important, not less: ~60k patches cannot be economically sent to an LLM,
   and dedup will not rescue the budget.
3. **The fingerprint ledger's value is reframed** — provenance, idempotent
   re-runs, caching verdicts, and handling the small recurring tail — not
   scale reduction.
4. **The answer to the original question** ("is it 60k FSF-address updates no
   one cares about, or substantive change?") is now evidence-based: it is
   ~60k substantive, distinct changes. The trivial boilerplate is real but
   tiny. The divergence is genuine and large — which makes "what do these
   patches actually *do*?" the real question, and worth answering well.

The success criterion "N distinct, of which the vast majority classified
deterministically" still stands — but "N" is ~60k, not a few hundred, so the
deterministic rule set (phase 3) is where the leverage now lives.
