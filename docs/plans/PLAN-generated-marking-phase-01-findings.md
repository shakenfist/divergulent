# Generated-marking phase 1 — findings

Measured on the real reviews corpus (60,642 deduplicated fingerprints,
one representative body per fingerprint) on 2026-07-26, with
`divergulent/classify/generated.py` at `GENERATED_RULES_VERSION = 1`
and `tools/generated-marking/measure.py`. Every number below is
traceable to `tools/generated-marking/results-full-corpus.json` or to
a quoted body excerpt; the handful that are not in that file came from
throwaway supplementary scans over the same representative bodies and
are labelled as such.

Nothing here has been applied. This document is the input to the
management adjudication; S4 applies whatever is agreed.

**The headline is a surprise, and it is not a good one.** The name
signal works, the banner signal works, the arithmetic works — but
`Makefile.in`, which is 68% of the whole name-match population, is
matching hand-written `AC_OUTPUT` templates far more often than it is
matching automake output. Roughly two thirds of the ≥50%-coverage
population is a wrong claim. See "The `Makefile.in` problem".

## Headline numbers

- **60,642 fingerprints scanned in 81.4 seconds**, 0 bodies missing.
  (The injection tripwire's comparable full-corpus sweep was 73s.)
  Results file: 239 KB — well under the 1 MB note in the step brief.
- **1,876 name-signal file matches** across **1,272 fingerprints**
  (2.10% of the corpus).
- **203 banner-marked file regions** in total: 179 of them on
  name-matched files (**9.5%** of name matches carry a corroborating
  banner — the master plan's "name leads, banner corroborates" holds),
  and 24 banner-only, spread over 11 basenames (the gap list).
- Wall-clock cost is a non-issue: regex plus the existing section
  parser, 1.34 ms per fingerprint.

### Coverage distribution

| Bucket | Fingerprints | % of corpus |
|--------|-------------:|------------:|
| coverage 0 (nothing marked) | 59,370 | 97.90% |
| 0 < coverage < 0.5 | 316 | 0.52% |
| 0.5 ≤ coverage < 0.9 | 221 | 0.36% |
| coverage ≥ 0.9 | 735 | 1.21% |

The ≥0.5 population is **956 fingerprints**. Its residue-changed-line
distribution is the number phase 3 was going to start from:

| Statistic | Residue changed lines |
|-----------|----------------------:|
| min | 0 |
| median | 0 |
| mean | 26.9 |
| max | 5,410 |

| Residue ≤ | Fingerprints |
|-----------|-------------:|
| 0 | 685 |
| 10 | 887 |
| 50 | 926 |
| 100 | 937 |
| 500 | 948 |
| 2,000 | 951 |

That median of 0 is the tell. The ≥0.5 population is not 956
generated-dominated patches; it is overwhelmingly **tiny patches that
touch one file which happens to be name-matched**. Restricting to
patches large enough for the residue unlock to mean anything:

| Filter | Fingerprints | Residue median | Residue max |
|--------|-------------:|---------------:|------------:|
| coverage ≥ 0.5, total ≥ 100 | 89 | 11 | 5,410 |
| coverage ≥ 0.5, total ≥ 1,000 | 42 | 10.5 | 5,410 |
| coverage ≥ 0.5, total ≥ 5,000 | 24 | 88 | 5,410 |
| coverage ≥ 0.5, total ≥ 10,000 | 13 | 130 | 5,410 |

The master plan's estimate of "~39 patches carry a dominant generated
component" is confirmed at roughly the right order: **42** at
coverage ≥ 0.5 and ≥ 1,000 changed lines. The largest by total changed
lines:

| Fingerprint | Package / patch | Coverage | Total | Residue |
|-------------|-----------------|---------:|------:|--------:|
| `fb22bda9816c` | granule `0005-autotools-updates.patch` | 0.981 | 48,343 | 941 |
| `89fbad7189e8` | gatos `0002-Massive-cleanup-for-libtools.patch` | 0.885 | 47,086 | 5,410 |
| `c1ed3922b369` | gatos `gatos_0.0.5-19.2.diff` | 0.909 | 46,561 | 4,250 |
| `74b82ec4e515` | libstroke `debian-changes.patch` | 1.000 | 43,351 | 10 |
| `b842690b9932` | baycomepp `00-baycomepp-sources.patch` | 0.984 | 26,969 | 440 |
| `4547c1827d10` | xen `0002-Delete-configure-output.patch` | 1.000 | 19,015 | 0 |
| `31a7a74bd23b` | barnowl `debian-changes` | 0.887 | 18,699 | 2,120 |
| `c8e1de9c3ece` | eblook `010_debian.patch` | 0.924 | 16,918 | 1,283 |
| `5dd0e1a3c99b` | granule-manual `0001-autotools-updates.patch` | 0.991 | 15,182 | 130 |
| `0b5fe4380466` | ketm `010_rebootstrap.diff` | 1.000 | 13,479 | 0 |
| `c4b9d41785ae` | geki3 `010_rebootstrap.diff` | 1.000 | 12,917 | 0 |
| `fccf140a5565` | tix `02-autoconf` | 1.000 | 10,518 | 0 |
| `2613dccd14e7` | — | 0.994 | 10,342 | 62 |

That table is the real phase-3 target population, and it is
encouraging: for most of it the residue is two orders of magnitude
smaller than the diff.

## Per-name table

All 27 names in the v1 candidate set, ranked by match count.
`banner` = how many of that name's matches also carried a
corroborating banner.

| Basename | Matches | Banner | Banner fraction |
|----------|--------:|-------:|----------------:|
| `Makefile.in` | 1,280 | 89 | 0.07 |
| `configure` | 362 | 58 | 0.16 |
| `config.h.in` | 63 | 11 | 0.17 |
| `aclocal.m4` | 52 | 18 | 0.35 |
| `config.guess` | 17 | 0 | 0.00 |
| `config.sub` | 17 | 0 | 0.00 |
| `ltmain.sh` | 16 | 1 | 0.06 |
| `install-sh` | 12 | 0 | 0.00 |
| `missing` | 12 | 0 | 0.00 |
| `GNUmakefile.in` | 10 | 0 | 0.00 |
| `depcomp` | 9 | 0 | 0.00 |
| `mkinstalldirs` | 7 | 0 | 0.00 |
| `compile` | 6 | 0 | 0.00 |
| `libtool.m4` | 6 | 2 | 0.33 |
| `ltconfig` | 4 | 0 | 0.00 |
| `ar-lib` | 1 | 0 | 0.00 |
| `test-driver` | 1 | 0 | 0.00 |
| `ylwrap` | 1 | 0 | 0.00 |
| `config.status` | 0 | 0 | — |
| `configure.lineno` | 0 | 0 | — |
| `ltoptions.m4` | 0 | 0 | — |
| `ltsugar.m4` | 0 | 0 | — |
| `ltversion.m4` | 0 | 0 | — |
| `lt~obsolete.m4` | 0 | 0 | — |
| `mdate-sh` | 0 | 0 | — |
| `py-compile` | 0 | 0 | — |
| `texinfo.tex` | 0 | 0 | — |

Total 1,876 matches, 179 with a banner (9.5%).

The zero-banner rate on `config.guess` / `config.sub` is by design:
those files carry no generator banner, only a `timestamp='…'`
datestamp, which the scanner captures as version evidence without
adding the `banner` signal. Both got their datestamps in gatos
(2005-08-03 and 2005-07-08), so that path is exercised.

**Nine of 27 names have zero corpus hits**: `config.status`,
`configure.lineno`, `ltoptions.m4`, `ltsugar.m4`, `ltversion.m4`,
`lt~obsolete.m4`, `mdate-sh`, `py-compile`, `texinfo.tex`. This
directly answers the plan's open question about `config.status` /
`configure.lineno`: they are build-time products, they never appear in
a carried patch, and the corpus confirms it — **0 hits each**.

## The `Makefile.in` problem

This is the finding that should drive the adjudication session.

`Makefile.in` is 1,280 of 1,876 name matches (68%), and **833 of the
1,272 marked fingerprints (65%) are marked by nothing except
`Makefile.in` / `GNUmakefile.in`**. Of the 956-fingerprint
≥0.5-coverage population, **646 (68%) are `*akefile.in`-only**; 511 of
those sit at coverage exactly 1.0, and only **13** of the 646 have
100 or more changed lines. (Supplementary scan.)

That would be fine if `Makefile.in` reliably meant automake output.
It does not. In a plain-autoconf project, `Makefile.in` is the
**hand-written `AC_OUTPUT` template** that `config.status` substitutes
`@VAR@` into — a maintainer-authored file, the direct analogue of
`Makefile.am`, and squarely on the wrong side of this scanner's axis.

Measured: of the 1,280 `Makefile.in` regions, only 204 carry
automake-shaped tokens (`am__`, `DEPDIR`, `AMDEP`, `LTLIBOBJS = @`, …)
anywhere in their visible hunk lines; 1,076 do not. I drew a
seeded-random sample of 30 from that 1,076 and hand-adjudicated the 28
distinct ones (2 were second files of an already-sampled fingerprint):

- **23 of 28 are unambiguously hand-written `AC_OUTPUT` templates.**
  xloadimage (`XLIB = @X_LIBS@ @X_PRE_LIBS@ -lX11 @X_EXTRA_LIBS@`),
  gauche, hylafax `faxd/Makefile.in`, midish, sqlite3
  (`sqlite3$(TEXE): shell.c sqlite3.c`), asymptote, cutils
  `src/cdecl`, rp-pppoe, zsh-antigen, arpwatch, qdbm, tkdesk, auctex
  `doc/`, texmacs plugin example, ifile, aspic, mash, doschk,
  unity-java, sawfish `lisp/`, libjsonparser, blt `library/`,
  mbuffer. Every one of them is a hand edit to a hand-written file,
  and marking it "claims to be generator output" is a false claim.
- **5 of 28 are genuine automake output** whose hunk simply showed
  none of my tokens: mimetic `test/`, barada-pam, metatheme-gilouche
  `icons/16x16/status/`, pipenightdreams `images/arrows_grey/`,
  berusky2 `data/`.

Extrapolated: **roughly 880 of the 1,280 `Makefile.in` matches are
hand-written templates** — about 47% of every name match the scanner
makes.

`GNUmakefile.in` is worse and unambiguous: **all 7 distinct paths are
hand-written**, 0 of 10 carry a banner. Six are GNUstep projects whose
`GNUmakefile.in` is the hand-maintained build file
(`include $(GNUSTEP_MAKEFILES)/framework.make`,
`PDFKIT=@have_pdfkit@`), and the seventh (privoxy) is a plain
hand-written template. Nothing in that population is generator output.

### Modelled fix

I modelled a **corroboration requirement** for the `Makefile.in`
family: mark only when the region also carries a banner or
automake-shaped content. (Supplementary scan; the tell-tale regex is a
measurement heuristic, not a proposed rule text.)

- 1,087 of the 1,201 banner-less `*akefile.in` regions drop; 114 are
  kept by automake tell-tales, plus the 89 that already carry a
  banner. The family goes from 1,290 regions to ~203.
- The ≥0.5-coverage population goes **956 → 323**.
- **gatos is completely unaffected**: all 12 of its `Makefile.in`
  regions carry an `automake 1.9.6` banner, so its coverage stays at
  0.9087 and its residue at 4,250.

That last point matters: the corroboration requirement costs the
motivating case nothing, because a patch that genuinely regenerates
autotools output rewrites the file from line 1 and therefore *shows
the banner*. The patches it drops are exactly the ones that touch four
lines in the middle of a file.

This is a **change to the scanner's shape, not just its name set**, so
it is flagged for adjudication rather than folded into the proposed v1
set below.

## Gap list — banner-only hits outside the name set

24 file regions, 11 basenames. Every one adjudicated.

| Basename | Count | Generators | Disposition |
|----------|------:|------------|-------------|
| `Cargo.lock` | 13 | `@generated` | leave banner-only |
| `Makefile` | 2 | `automake 1.10`, `automake 1.10.1` | leave banner-only |
| `autoconfig.h.in` | 1 | `autoheader` | leave banner-only |
| `computers` | 1 | `libtool` | **pattern artifact — tighten** |
| `config.h` | 1 | `autoheader` | leave banner-only |
| `f-ol-syntax.c` | 1 | `autoheader` | leave banner-only (honest) |
| `hpdf_config.h` | 1 | `autoheader` | leave banner-only |
| `libtool.texi` | 1 | `libtool` | **pattern artifact — tighten** |
| `output.0` | 1 | `autoconf 2.69` | leave banner-only |
| `output.1` | 1 | `autoconf 2.69` | leave banner-only |
| `poetry.lock` | 1 | `@generated` | leave banner-only |

Details and the evidence for each:

- **`Cargo.lock` (13, rust-escargot) and `poetry.lock` (1)** — real
  generated lock files, correctly identified by the modern marker:
  `# This file is automatically @generated by Cargo.` and
  `# This file is automatically @generated by Poetry 2.0.1 and should
  not be changed by hand.` The `@generated` pattern is doing exactly
  its job. **Disposition: leave banner-only.** Lock files are a
  different family from build-system output (they are dependency
  pins, and a malicious pin is precisely the thing a reviewer must
  see), and adding `Cargo.lock`/`poetry.lock` to an `autotools` name
  set would be a category error. If a `lockfile` family is ever
  wanted, it is its own decision with its own posture.
- **`Makefile` (2: baycomepp `ntdrv/Makefile`, cvsgraph
  `contrib/Makefile`)** — genuinely generated: these are
  configure-time products carried into the diff, headed
  `# Makefile.in generated by automake 1.10 from Makefile.am.` /
  `# contrib/Makefile.  Generated from Makefile.in by configure.`
  **Disposition: leave banner-only.** Adding bare `Makefile` to the
  name set would be catastrophic — hand-written `Makefile`s are
  everywhere — and the banner is exactly what distinguishes these two
  from all of them.
- **`autoconfig.h.in` (libjpeg), `config.h` (libimager-qrcode-perl),
  `hpdf_config.h` (libharu)** — autoheader output under non-standard
  names, e.g.
  `/* autoconfig.h.in.  Generated from configure.ac by autoheader. */`
  and
  `/* include/hpdf_config.h. Generated automatically at end of
  configure. */`. Real generated content the name set legitimately
  misses. **Disposition: leave banner-only.** `config.h` in
  particular is very often hand-written; the `Generated from <x> by
  autoheader` banner is the reliable discriminator and it already
  fires.
- **`output.0` / `output.1` (barnowl `autom4te.cache/`)** — the
  autom4te cache carried into the diff; the bodies are the
  quadrigraph-escaped configure text
  (`@%:@ Generated by GNU Autoconf 2.69 for BarnOwl 1.10.`). Real
  generated output. **Disposition: leave banner-only.** A path rule
  for `autom4te.cache/` would work, but n=2 and the banner already
  catches it.
- **`f-ol-syntax.c` (hol88)** — a 7,805-line hand-written C file that
  *embeds a copy of* `h/gclincl.h`, so the autoheader banner
  (`/* h/gclincl.h.in.  Generated from configure.in by autoheader. */`)
  is genuinely present in its content. **Disposition: leave as-is.**
  The scanner's claim — "this file's region carries an autoheader
  banner" — is literally true, and this is the kind of thing the
  banner signal exists to surface. Not a name-set candidate.
- **`computers` (fortune-mod `datfiles/computers`) and `libtool.texi`
  (libtool)** — both false positives of the bare `\bGNU Libtool\b`
  pattern firing on prose:
  `-- "GNU Libtool documentation"` (a fortune cookie) and
  `This manual is for GNU Libtool (version @value{VERSION}...)`.
  **Disposition: pattern artifact — tighten the pattern** (see
  "Proposed v1 signal set").

## Low-frequency adjudication (≤20 matches)

Fourteen names have hits; all their distinct paths were eyeballed,
and a body excerpt read for anything that looked off.

| Name | Paths | FPs | Disposition |
|------|------:|----:|-------------|
| `config.guess` | 9 | 0 | keep |
| `config.sub` | 9 | 0 | keep |
| `ltmain.sh` | 9 | 0 | keep |
| `depcomp` | 8 | 0 | keep |
| `install-sh` | 8 | 0 | keep |
| `missing` | 7 | 0 | keep |
| `GNUmakefile.in` | 7 | **7** | **drop** |
| `compile` | 6 | **1** | keep (flagged) |
| `mkinstalldirs` | 5 | 0 | keep |
| `libtool.m4` | 3 | 0 | keep |
| `ltconfig` | 2 | 0 | keep |
| `ar-lib` | 1 | 0 | keep |
| `test-driver` | 1 | 0 | keep |
| `ylwrap` | 1 | 0 | keep |

The short generic names — the plan's second open question — came out
almost clean:

- **`compile` — one false positive in six.** cricket's
  `cricket-1.0.5/compile` is a **Perl script**, not the automake
  compiler wrapper:

  ```
   BEGIN {
  -	my $programdir = (($0 =~ m:^(.*/):)[0] || "./") . ".";
  -	eval "require '$programdir/cricket-conf.pl'";
  +  require '/etc/cricket/cricket-conf.pl';
   }
  ```

  The other five are the real thing
  (`# Wrapper for compilers which do not understand '-c -o'.`,
  `scriptversion=2018-03-07.03`). **Disposition: keep, flagged.** The
  blast radius is measured and small: that fingerprint scans at
  coverage 0.040 with 143 residue lines out of 149, so no consumer
  threshold is affected. Dropping `compile` would lose five true
  positives to avoid one harmless one.
- **`missing` (7), `install-sh` (8), `ltconfig` (2), `mkinstalldirs`
  (5), `depcomp` (8) — zero false positives.** All are the genuine
  GNU support scripts
  (`# Common stub for a few missing GNU programs while installing.`,
  `# install - install a program, script, or datafile`,
  `# mkinstalldirs --- make directory hierarchy`), and both `ltconfig`
  hits are the real libtool 1.x `ltconfig` (vflib3, patched around
  line 2000 in the `dynamic_linker` case statement). Exact-basename
  matching is precise enough in practice.
- **`mdate-sh` cannot be adjudicated: 0 corpus hits.** No evidence
  either way; it stays on correctness grounds, not measurement.
- **`ar-lib`, `test-driver`, `ylwrap` (1 each)** — all genuine
  automake support scripts, all in `scriptversion=` bumps
  (nted, filtergen). Keep.
- **`GNUmakefile.in` — 7 of 7 false positives.** Covered above.

### One narrow class worth naming

The **libtool source package itself** appears three times
(`libtool-2.5.4/m4/libtool.m4`, `libtool-2.5.4/build-aux/ltmain.sh`,
`libtool-2.5.4/doc/libtool.texi`). In every other package those files
are copied generator output; in `libtool` they are hand-written
upstream source, and the patch is a real code change
(`0030-flang-support.patch` adds a `flang* | f18* | f95*` case). n=3,
no rule proposed — but it is the honest boundary of the "name means
generated" claim and belongs in the rule documentation phase 2 writes.

## `acinclude.m4` — the deliberate-absence question

Measured over the full corpus:

- **67 file regions touch `acinclude.m4`.**
- **0 of them are banner-marked.** The banner signal never fires on
  `acinclude.m4` anywhere in the corpus, so the plan's hoped-for
  "the banner may still fire on its pasted content" did not happen.
  Generators seen: none.
- Median changed lines: **13**. Only 6 regions exceed 100 changed
  lines; only 2 exceed 500. (Supplementary scan.)
- Only **4 of 67** carry libtool-paste markers, and they are almost
  entirely the motivating case: gatos `c1ed3922…` (3,647 changed
  lines), gatos `89fbad71…` (4,785), and smpeg (434).

**Proposed disposition: `acinclude.m4` stays out of the name set — the
data supports the plan's instinct — but the paste case should be
picked up by a content signal instead.**

The argument from the numbers: 63 of 67 `acinclude.m4` regions are
small hand-written maintainer m4 (median 13 lines), exactly as the
convention says. Adding the name would make 63 wrong claims to catch
4 right ones. But those 4 are not incidental — they are the two
largest generated-dominated patches in the corpus, and excluding them
costs gatos 3,647 changed lines of coverage.

The reason the banner missed them is that pasted libtool carries its
own header, which is not in `BANNERS`:

```
-## libtool.m4 - Configure libtool for the target system. -*-Shell-script-*-
-## Copyright (C) 1996-1999 Free Software Foundation, Inc.
```

I measured a candidate pattern
`libtool\.m4 - Configure libtool for the (?:target|host) system`
across the whole corpus: **11 matching regions, 8 of them already
name-matched (`aclocal.m4` ×5, `libtool.m4` ×3), and exactly 3 new —
gatos's two `acinclude.m4` regions and smpeg's. Zero false
positives.** That pattern converts the `acinclude.m4` question from a
name-set argument into a banner-set argument, which is where this
scanner's posture wants it: the file is marked because its *content*
says libtool wrote it, not because of what it is called.

With that pattern, gatos would read coverage **0.987**, residue
**603** — which is what the hand analysis said (see below).

## Multi-banner report

**61 file regions carry more than one distinct
`(generator, version)` pair.** 55 of the 61 are the *same generator at
two versions* — i.e. a plain version bump, old on the removed line and
new on the added line:

| Path | Versions seen |
|------|---------------|
| `configure` | `autoconf 2.59`, `autoconf 2.68` |
| `nbd-3.26.1/configure` | `autoconf 2.71`, `autoconf 2.72` |
| `libieee1284-0.2.11/configure` | `autoconf 2.59`, `autoconf 2.61`, `libtool` |
| `aclocal.m4` | `aclocal 1.11.1`, `aclocal 1.11.5` |
| `Makefile.in` | `automake 1.8.5`, `automake 1.9.5` |
| `Makefile.in` | `automake 1.10.1`, `automake 1.9.2` |

**In 59 of the 61 multi-banner regions, the first banner in line order
sits on a removed (`-`) line.** So the current first-banner-wins rule
reports the **pre-patch** version 97% of the time it has a choice.

Corpus-wide the same skew holds for single-banner files: of all 203
banner-marked regions, the winning line is a removed line in **121**,
an added line in **75**, and a context line in **7**. (Supplementary
scan.)

**Proposed disposition: switch to added-lines-preferred** (prefer the
first hit on a `+` line; fall back to context, then removed). The
argument is not aesthetics — it is that the captured version is
evidence about the file *as the patch leaves it*, and the master plan
names it as the strongest input to `PLAN-maintenance-health.md`'s
fossil dating. Reporting the version being replaced dates the
regeneration backwards, systematically, in the exact cases where a
regeneration happened. The fallback chain loses nothing: a file whose
only banner is on a removed line still reports it.

Cost: for gatos this changes nothing (its banners are all context or
added lines and it reports `autoconf 2.59` either way). Confidence is
high because the failure is one-directional and measured at 59/61.

## Candidate do-not-edit family — go/no-go

The generic `DO NOT EDIT` / `[Dd]o not edit this file` /
`[Aa]utomatically generated` family, measured but never marking, hit
**1,011 file regions**.

Top basenames:

| Basename | Count |
|----------|------:|
| `Makefile` | 11 |
| `setup.ml` | 5 |
| `META` | 3 |
| `_tags` | 3 |
| `maven-build.xml` | 3 |
| `myocamlbuild.ml` | 3 |
| `Abidjan.pm`, `Adak.pm`, `Adelaide.pm`, … | 2 each |

That tail is the whole story: the "top basenames" list past rank six
is `lib/DateTime/TimeZone/…` modules from `libdatetime-timezone-perl`,
hundreds of them, all reading `# Do not edit this file directly.`
Sample snippets from the results file:

```
lib/DateTime/TimeZone/Africa/Abidjan.pm   # Do not edit this file directly.
mp3burn.1        +.\" Automatically generated by Pod::Man 2.22 (Pod::Simple 3.07)
Makefile          # Automatically generated object names
```

**Proposed disposition: NO-GO for a v1 family.** The plan leaned
measured-only and the measurement agrees, for two reasons:

1. **Volume without discrimination.** 1,011 regions versus the whole
   name set's 1,876 — this family would nearly double the marked
   population using a phrase that has no generator, no version, and no
   family. `# Automatically generated object names` is a comment
   *inside* a hand-written `Makefile` about a variable, not a claim
   about the file.
2. **The real populations it found deserve their own families, not
   this one.** `libdatetime-timezone-perl` (generated timezone data),
   Pod::Man-generated man pages, OCaml `setup.ml`/`myocamlbuild.ml`
   (oasis output), `maven-build.xml` (maven-debian-helper output) are
   each a coherent, nameable generator with its own conventions — and
   each would be better served by a pattern that captures the
   generator and version, the way the autotools family does.

Keep `candidate_banner_hits` as measurement-only in the module; revisit
when someone wants a `perl-generated` or `oasis` family and can point
at a population that needs the residue unlock. Nothing in the current
1,011 is stranded in `oversized` the way gatos is.

## The gatos reference check

Fingerprint `c1ed3922b3696eb254ac3c26887d095bfb58892926735b2c398f68e237b00326`,
body sha `bc38a4567efa108af4bc62cefbcc514d6bcaeb5ab01ae396d38d99e3c6610026`
(gatos 0.0.5-22, `gatos_0.0.5-19.2.diff`), scanned directly with
`divergulent.classify.generated.scan`:

- **20 marked file paths**, spanning 9 distinct basenames.
- `total_changed` 46,561; `generated_changed` 42,311;
  **`residue_changed` 4,250**; **coverage 0.9087**.
- The arithmetic invariant holds: 4,250 + 42,311 = 46,561.
- **`src/Makefile.am.ori` is NOT marked** ✅ — it strips to
  `Makefile.am`, which is not in the name set, and its 411 changed
  lines land in the residue. The backup-suffix rule works in the
  direction the plan cared about.
- **`autoconf 2.59` is captured on `configure`** ✅, from a
  `banner,name` file with +15,117/−4,141 (the master plan's 19,258
  changed lines, exactly).

Marked files:

| Path | Signals | Generator / version | + | − |
|------|---------|---------------------|--:|--:|
| `configure` | banner,name | autoconf 2.59 | 15,117 | 4,141 |
| `m4/libtool.m4` | banner,name | libtool | 6,336 | 335 |
| `ltmain.sh` | name | — | 4,306 | 1,327 |
| `aclocal.m4` | banner,name | aclocal 1.4 | 804 | 1,487 |
| `config.guess` | name | config.guess 2005-08-03 | 949 | 456 |
| `config.sub` | name | config.sub 2005-07-08 | 729 | 106 |
| `config.h.in` | banner,name | autoheader | 136 | 86 |
| `missing` | name | — | 156 | 10 |
| `Makefile.in` ×12 | banner,name | automake 1.9.6 | — | — |

The twelve `Makefile.in` files (top level, `docs/`, `gfxdump/`, `m4/`,
`man/`, `man/en/`, `man/fr/`, `po/`, `po/fr/`, `po/model/`, `src/`,
`tech-docs/`) all carry the `automake 1.9.6` banner and total 5,830
changed lines. The mixed-toolchain fossil record the master plan
described is visible and captured: autoconf 2.59, automake 1.9.6,
aclocal **1.4**, config.guess from 2005.

### Where reality differs from the success criterion

The criterion said "ten generated files marked, `Makefile.am.ori` not
marked, residue ≈800 changed lines". Two of three match; the residue
does not, and the file count needs restating.

- **"Ten files" → 20 paths / 9 basenames.** The master plan's prose
  actually enumerates "ten ... output files ... and eleven
  `Makefile.in`s"; the ten was a count of *kinds*, not paths. Measured
  there are 12 `Makefile.in`s, not 11, and 8 other names. No
  discrepancy in substance.
- **"Residue ≈800" → 4,250 measured.** The gap is entirely
  `acinclude.m4` (3,647 changed lines, unmarked by design) plus
  `src/Makefile.am.ori` (411, unmarked by design). The full residue
  breakdown:

  | File | Changed |
  |------|--------:|
  | `acinclude.m4` | 3,647 |
  | `src/Makefile.am.ori` | 411 |
  | `src/xatitv.cpp` | 115 |
  | `configure.in` | 26 |
  | `src/Makefile.am` | 10 |
  | `src/gatos.c` | 8 |
  | `src/xutils.c` | 7 |
  | `src/board.c` | 6 |
  | `m4/gatos.m4` | 4 |
  | `src/i2c.c` | 3 |
  | `docs/Makefile.am`, `man/en/gatos.1`, `man/fr/Makefile.am`, `man/fr/gatos.1`, `src/bogo.h`, `src/i18n.h` | 2 each |
  | `src/gatos-conf.cpp` | 1 |
  | **total** | **4,250** |

  Excluding `acinclude.m4`: **603 changed lines across 16 files**.
  Excluding `acinclude.m4` and `src/Makefile.am.ori`: **192 changed
  lines across exactly 15 files** — which is where the hand analysis's
  "~800 diff lines across 15 files" came from. The file count matches
  exactly; the line count differs because the hand analysis counted
  raw diff lines (headers and context included) rather than changed
  lines. The same conversion explains the master plan's "54,465 lines"
  against the measured 46,561 *changed* lines.
- **"~98% generated" → 90.87% measured.** Also entirely
  `acinclude.m4`: with it marked, coverage is 45,958 / 46,561 =
  **98.70%**. The master plan's 98% figure was computed *including*
  `acinclude.m4`, before the deliberate-absence decision was taken.

None of this is a scanner defect — every marked and unmarked file is
correct under the v1 rules as written. It is a real consequence of
excluding `acinclude.m4`, and it is the strongest argument for the
libtool-paste banner pattern proposed above.

## Proposed v1 signal set

Still `GENERATED_RULES_VERSION = 1` — nothing has shipped.

### Name set: drop 3 of 27, keep 24

**Drop:**

| Name | Reason |
|------|--------|
| `GNUmakefile.in` | 7 of 7 distinct paths are hand-written (GNUstep / plain-autoconf templates); 0 of 10 carry a banner. Measured false-positive rate 100%. |
| `config.status` | 0 corpus hits. Build-time product; answers the plan's open question. |
| `configure.lineno` | 0 corpus hits. Same. |

**Keep, with zero corpus hits, on correctness grounds:**
`ltoptions.m4`, `ltsugar.m4`, `ltversion.m4`, `lt~obsolete.m4`,
`mdate-sh`, `py-compile`, `texinfo.tex`. Unlike `config.status`, these
are genuine *dist* products that ship in a release tarball; their
absence reflects the corpus's era skew (libtool 2.x `lt*.m4` files
postdate most of these patches), not a wrong claim. They cost nothing
and can only be right when they fire.

**Keep everything else**, including `compile` despite its one measured
false positive (5 of 6 correct; the FP's blast radius is coverage
0.040 / residue 143 of 149).

**Add: nothing.** No gap-list basename earned a name-set entry; the
banner signal already covers every genuinely-generated one, which is
the outcome the two-signal design was built for.

### Pattern tuning

| Change | Evidence |
|--------|----------|
| Narrow the bare libtool pattern from `\bGNU Libtool\b` to the boilerplate line `This file is part of GNU Libtool` | The bare pattern fires standalone on 12 regions; 10 are name-matched autotools files where it adds nothing, and 2 are prose false positives (`datfiles/computers`, `libtool.texi`). The narrowed form keeps all 10 and kills both. |
| Add the automake ≤1.4 banner form, `generated automatically by automake <v>` | The current `generated by automake <v>` misses it. Measured: **57 file regions across 7 fingerprints**, versions `1.4-p5` (32), `1.4` (18), `1.4-p4` (5), `1.4-p6` (1), `1.5` (1). This is the era the whole plan is about — pipenightdreams' `images/arrows_grey/Makefile.in` carries `# Makefile.in generated automatically by automake 1.4-p6 from Makefile.am` and gets no banner today. |

Both are low-risk, measured, and net-positive. I propose applying them
in S4 without further debate.

### Flagged for management adjudication

Three items where the data is real but the decision is a judgement
call, not an arithmetic one:

1. **The `Makefile.in` corroboration requirement.** ~880 of 1,280
   `Makefile.in` matches are hand-written `AC_OUTPUT` templates, and
   `*akefile.in`-only marks are 65% of all marked fingerprints and 68%
   of the ≥0.5-coverage population. Requiring a banner or
   automake-shaped content takes the ≥0.5 population from 956 to 323
   and costs gatos nothing. **But** it changes the scanner's shape
   from "name leads, banner corroborates" to "name leads except for
   one family", it needs its own tuning pass for the tell-tale set,
   and it makes the name signal non-uniform. The alternative — accept
   the noise on the grounds that the mark is never a verdict and phase
   3 only cares about large patches — is defensible, but it means the
   observation's `coverage` field says "100% generated" about 511
   patches where it is flatly untrue. My recommendation is to require
   corroboration, but this is the one decision I do not want to make
   silently.
2. **The libtool-paste banner pattern for `acinclude.m4`.**
   `libtool\.m4 - Configure libtool for the (?:target|host) system`
   matches 11 regions corpus-wide, 8 already name-matched, 3 new
   (gatos ×2, smpeg), zero false positives — and it is what brings
   gatos to the 98.7% / 603-residue figure the hand analysis produced.
   The reason to hesitate is posture, not data: it marks
   `acinclude.m4`, a maintainer-*written* file, on the strength of
   what somebody pasted into it. That is arguably the honest answer
   (the mark says "claims generated", and the pasted content does
   claim it) and arguably a step toward the `copied-input`
   disposition the plan floated as an alternative. Needs a decision on
   vocabulary as much as on the regex.
3. **Added-lines-preferred banner selection.** 59 of 61 multi-banner
   regions, and 121 of 203 banner regions overall, currently report a
   version read off a removed line. The fix is mechanical and lossless;
   the reason it is flagged is that phase 2's evidence format and the
   maintenance-health plan both consume this field, so the semantics
   ("the version the file claims *after* the patch") should be written
   down deliberately rather than changed as a bug fix.

Not flagged, because the data settled them: `config.status` /
`configure.lineno` (drop — 0 hits), the short generic names (keep —
1 FP in 41 matches across five names), and the `DO NOT EDIT` family
(no-go for v1).

## Adopted v1 set (post-adjudication, S4)

The management session adjudicated the three flagged items above.
Still `GENERATED_RULES_VERSION = 1` — nothing has shipped. This
section records what was applied to `generated.py` and the
re-measured numbers; it does not rewrite anything above, which stays
the pre-tuning record.

### The three adjudicated decisions

1. **`Makefile.in` corroboration — adopted, banner-only.** The name
   signal for `Makefile.in` now counts only alongside a banner hit in
   the same region; with no banner the file is not marked at all (see
   `_NAME_REQUIRES_BANNER` in `generated.py`). Precision over recall:
   a missed mark is honest, a false mark is not, and a genuine
   regeneration rewrites the file from line 1, so it shows its banner
   anyway — the requirement costs the target population nothing.
2. **The libtool-paste banner — adopted as a content-claim banner.**
   `libtool\.m4 - Configure libtool for the (?:target|host) system`
   is now in `BANNERS`. It marks `acinclude.m4` (and anything else
   carrying the pasted header) banner-only, on the strength of what
   the file's *content* claims, not its name — the honest resolution
   of the `acinclude.m4` deliberate-absence question.
3. **Added-lines-preferred banner selection — adopted.** `_banner_hit`
   now groups a region's lines by first character (`+` added, `-`
   removed, else context) and prefers the first hit among added
   lines, then context, then removed. The captured version is
   evidence about the file *as the patch leaves it*; a file whose
   only banner is on a removed line still reports it unchanged.

### Applied drops and pattern changes

- Name set: dropped `GNUmakefile.in` (7/7 measured hand-written),
  `config.status` and `configure.lineno` (0 corpus hits each).
- `BANNERS`: narrowed the bare libtool pattern from `\bGNU Libtool\b`
  to `This file is part of GNU Libtool` (kept all 10 real hits,
  killed both prose false positives — `datfiles/computers` and
  `libtool.texi`); added the automake ≤1.4 form
  (`generated automatically by automake <v>`); added the
  libtool-paste header pattern (decision 2 above). `_VERSION` grew a
  hyphen to its allowed character class so the automake ≤1.4 form's
  hyphenated versions (`1.4-p6`, `1.4-p5`, …) capture whole.

### Re-measured numbers

Full corpus, same 60,642 fingerprints, 0 bodies missing, 75.8s wall
clock (`tools/generated-marking/results-full-corpus.json`, which this
run overwrote — the pre-tuning numbers above remain this document's
history).

- **Name-signal file matches: 698, across 429 fingerprints** (down
  from 1,876 across 1,272 — almost entirely the `Makefile.in`
  corroboration requirement: 1,280 matches fell to 112, all 112 of
  them now banner-corroborated).
- **Banner-marked file regions: 229 total** — 204 on name-matched
  files (**29.2%** of the 698 name matches now carry a corroborating
  banner, up from 9.5% pre-tuning, exactly as expected once
  banner-less `Makefile.in` noise is gone) and 25 banner-only outside
  the name set (up from 24: `computers`/`libtool.texi` dropped out,
  `acinclude.m4` entered with 3 hits from the libtool-paste pattern).
- **Any-signal marked files: 723, across 442 fingerprints** (this is
  the population phase 2/3 would observe — includes the 25
  banner-only files the name-match count above excludes).

Coverage distribution:

| Bucket | Fingerprints | % of corpus |
|--------|-------------:|------------:|
| coverage 0 | 60,200 | 99.27% |
| 0 < coverage < 0.5 | 151 | 0.25% |
| 0.5 ≤ coverage < 0.9 | 85 | 0.14% |
| coverage ≥ 0.9 | 206 | 0.34% |

The ≥0.5 population is **291 fingerprints** (down from 956). Its
residue-changed-line distribution:

| Statistic | Residue changed lines |
|-----------|----------------------:|
| min | 0 |
| median | 0 |
| mean | 54.8 |
| max | 3,575 |

| Residue ≤ | Fingerprints (of 291) |
|-----------|-----------------------:|
| 0 | 167 |
| 10 | 242 |
| 50 | 265 |
| 100 | 275 |
| 500 | 282 |
| 2,000 | 288 |

**≥0.5 coverage and ≥1,000 changed lines: 41 fingerprints** — in line
with the pre-tuning modelled estimate of ~42 and the master plan's
original ~39. The corroboration requirement removed the tiny
`*akefile.in`-only patches that dominated the pre-tuning ≥0.5
population (median residue was 0 there; here mean residue rises to
54.8 because the population is now almost entirely genuine
generated-dominated patches, not one-file coincidental matches).

### The gatos reference re-check

Fingerprint `c1ed3922b369…`, re-scanned directly with the adopted
rules:

- **Coverage 0.9870** (≈0.987, as the libtool-paste pattern's earlier
  measurement predicted) — up from 0.9087 pre-tuning.
- **Residue 603 changed lines** — down from 4,250, matching the
  hand analysis's second figure exactly. The full pre-tuning residue
  was `acinclude.m4` (3,647) + `src/Makefile.am.ori` (411) + 192
  lines of genuine hand-written source; only the last 192 plus
  `src/Makefile.am.ori`'s 411 remain (603 total) now that
  `acinclude.m4` is marked banner-only.
- **`acinclude.m4` is now marked**, banner-only (`('banner',)`),
  generator `libtool`, no version — exactly the disposition decision
  2 adopted. `src/Makefile.am.ori` is still correctly unmarked (it
  strips to `Makefile.am`, outside the name set).
- **21 marked files** (up from 20: the 12 `Makefile.in`s keep their
  `automake 1.9.6` banner and both signals unchanged; `acinclude.m4`
  is the one addition).
- The arithmetic invariant still holds: 45,958 + 603 = 46,561.

No surprises here: every number the adjudication predicted (coverage
≈0.987, residue ≈603, `acinclude.m4` marked banner-only) landed
exactly on the predicted value, because the libtool-paste pattern was
already measured against this exact fingerprint in the pre-tuning
pass.
