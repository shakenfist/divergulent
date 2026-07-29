# Generated-marking phase 5 — findings (translation catalogues)

Full-corpus measurement of the corroborated translation-catalogue
candidate ([plan](PLAN-generated-marking-phase-05-translations.md),
step S2). Run: 60,642 fingerprints in 89.2 s, 0 bodies missing;
machine-readable results committed as
`tools/generated-marking/results-translations-full-corpus.json`.
Everything below is measured from the reviews corpus; nothing marks
yet — promotion is the adjudication this document exists to inform.

## Per-extension table

| Extension | Files touched | Corroborated | Fraction |
|-----------|---------------|--------------|----------|
| `.po`     | 1,306         | 1,206        | 0.92     |
| `.pot`    | 42            | 39           | 0.93     |
| `.ts`     | 901           | 408          | **0.45** |

## The `.ts` collision — the headline number

`.ts` splits **45% Qt Linguist / 55% TypeScript** (408 corroborated,
493 not). Extension-only marking would be wrong more often than right
— worse than the `Makefile.in` case (~⅔ hand-written) that forced
the corroboration tier in phase 1. The uncorroborated examples are
unambiguous TypeScript (`src/builder.ts`, jest's
`packages/jest-config/src/Defaults.ts`, `rollup.config.ts`, …) plus
one delightful third meaning: TeXmacs style files
(`TeXmacs/packages/customize/spacing/old-spacing.ts`). **Adjudication:
the corroboration requirement is settled; extension-only `.ts`
marking must never ship.**

Every corroborated `.ts` file is typed **code** by
`content._classify_file` today (408 of the 1,653 corroborated files;
the `.po`/`.pot` balance falls to data, plus 5 test / 2 doc for
catalogues under test/doc trees — genuine catalogues, harmless
typing). That code-typing is the false-positive channel the mark
would drain: see the construct section below.

## Corroborator strength

| Corroborator     | Files fired |
|------------------|-------------|
| `po-msgid-msgstr`| 1,217       |
| `po-header`      | 462         |
| `ts-elements`    | 407         |
| `ts-doctype`     | 42          |

`.ts` combinations: `ts-elements` alone 366, both 41, `ts-doctype`
alone 1 — ulcc's `010_fix-translation-pt_BR.patch`, a 1+/1− hunk at
the top of a genuine catalogue (correct fire). **Adjudication: keep
both.** `ts-elements` carries the population (update hunks deep in a
catalogue never show the DOCTYPE); `ts-doctype` catches top-of-file
hunks the element bar can miss, at one-in-the-corpus cost of nothing.
For `.po` the pair corroborator dominates; the header corroborator
adds header-only hunks. Keep both.

## The recall cost of corroboration — negligible

100 `.po` + 3 `.pot` files match the extension but fail every
corroborator. Sampled, they are all *real* catalogues whose hunks
never show catalogue structure:

- header-metadata-only hunks (udevil: `Last-Translator:` continuation
  strings);
- edits inside multi-line `msgstr` continuation strings (awffull's
  "timout"→"timeout" across every locale — also all 3 `.pot` misses);
- comment/flag-only hunks (mate-utils: an added `#, fuzzy` line);
- obsolete entries (gtkballs: `#~ msgstr` lines, which the anchored
  patterns deliberately skip).

Measured cost: those files total **591 changed lines corpus-wide**
(`.pot`: 10) against 2,190,653 corroborated `.po` changed lines —
0.03%. As with `Makefile.in` in phase 1, corroboration costs the
target population essentially nothing; a missed mark is honest.
**Adjudication: accept the miss.** A `po`-comment-marker corroborator
(`#~`/`#,`/`#:` prefixes) could recover most of it, but 591 lines
does not buy its pattern-maintenance keep — precision over recall.

## The would-be marked population

250 fingerprints carry ≥1 corroborated catalogue: coverage <0.5 for
32 (minor translation components in bigger patches), 0.5–0.9 for 29,
≥0.9 for **189** — translation patches are overwhelmingly
translation-dominated, even more sharply than the autotools family.

## The oversized unlock — 10 new fingerprints

13 oversized fingerprints carry a corroborated catalogue; **10 are
newly unlocked** (combined residue ≤ 5,000 where the phase-3
generated mark alone had not already unlocked them) — a 40% increase
on phase 3's 24:

| Package (patch) | Total changed | Combined residue |
|-----------------|---------------|------------------|
| gimp-data-extras (`po-move-all-existing-translations…`) | 2,029,633 | 85 |
| acetoneiso (`translate.patch`, the motivating `6b51f47b…`) | 74,347 | 68 |
| pianobooster (`debian-changes`) | 49,363 | 2,205 |
| blackbox-terminal (`debian-changes`) | 14,712 | 702 |
| schroot (`…portuguese-translations…`) | 12,313 | 10 |
| net-tools (`translations.patch`) | 9,587 | 0 |
| sgt-puzzles (`0003-Add-German-translation…`) | 8,395 | 0 |
| expeyes-doc (`poFiles.patch`) | 7,504 | 107 |
| sgt-puzzles (`206_translate-docs.diff`) | 6,535 | 41 |
| aumix (`16_potfiles.patch`) | 5,360 | 0 |

Three have a residue of literally zero — pure catalogue refreshes a
human currently owns as `unknown` at oversized priority.

## Construct hits inside corroborated catalogues

**35 hits across 33 files — every one of them acetoneiso, every one
`shell-out`**, and every one the same GPL-2 license text embedded in
translation strings ("…of the specific operating **system (if**
any)…" matching `\bsystem\s*\(`), once per locale file. This is the
entire false-positive population the mark would re-attribute: with
the family promoted, phase 4's construct tally reads acetoneiso as
35-in-marked / 0-in-residue at render time, exactly as gatos reads
151/0 today. No other package has a construct hit inside a
corroborated catalogue.

## Compiled catalogues

Text diffs touching compiled catalogues are all but absent: `.mo` 1,
`.gmo` 0, `.qm` 0. (Binary payloads mostly do not survive into
carried patch text; acetoneiso's `.qm` additions arrive via its
`.qrc` resource list, not as diff content.) **Adjudication: v1 of
the family says nothing about compiled catalogues.**

## Proposed promotion (for the management session)

1. A **`translations` family** in `scan()`: extension match +
   mandatory content corroboration — `_NAME_REQUIRES_BANNER`
   generalised, with the corroborator names recorded per file in
   evidence where autotools records generator/version. No version to
   capture; the per-file signal names are the honest vocabulary
   (`.po` content is human-authored and tool-managed, so "generated"
   would overclaim — the family name `translations` says what is
   actually claimed).
2. `GENERATED_RULES_VERSION` 1 → 2; the desired-vs-live record pass
   re-converges every fingerprint (expected: 442 live marks grow by
   ~250; existing autotools marks re-record unchanged).
3. Everything downstream rides along free: the 10 unlocks enter
   triage/risk residue-first (three of them with zero residue —
   consider whether a zero-residue projection needs a floor, or the
   note-only diff is the correct text to score), the review UIs
   badge/collapse, the construct tally re-attributes acetoneiso's 35.
4. Re-risk the newly unlocked, phase-3 style (n=10, cheap): their
   current `elevated`-band scores were computed from catalogue-noise
   heads — acetoneiso's among them.
5. `.pot` joins (39 corroborated, same mechanics); `.desktop`
   locale-key blocks, cmake, and protobuf stay future families,
   measured first, same as this one was.

## Open questions carried out of this phase

- The zero-residue projection floor (point 3 above).
- Whether promotion should also feed the phase-4 findings' naming
  vocabulary back into `detail_for` (`translations/98` vs a
  family-qualified detail) — cosmetic, settle at implementation.
