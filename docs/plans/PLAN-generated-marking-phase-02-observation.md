# Generated-marking phase 2 — the ledger observation

Phase 1 built and tuned the pure scanner and measured it over the
corpus (429 marked fingerprints; 41 generated-dominated patches;
gatos at 98.7% / 603-line residue — see the
[findings](PLAN-generated-marking-phase-01-findings.md)). Phase 2
records what the scanner sees as a **supersedable ledger
observation** during the deterministic record pass, and documents the
rule. Nothing routes on it yet (phase 3) and nothing renders it
(phase 4): after this phase the mark simply exists in the ledger,
idempotently, with evidence a human can read.

## Design decisions

### One observation per fingerprint, kind `generated-content`

Settling the master plan's naming question: kind
**`generated-content`** (the mark is about what the content claims,
which is exactly the vocabulary the libtool-paste adjudication
settled), `observed_by='generated-scan'`,
`rule_version=GENERATED_RULES_VERSION`. One observation per
fingerprint that carries any marked file — mirroring `reviewability`
(one row, structured evidence), not the per-hit dangerous-construct
shape: gatos's 21 marked files are one claim about one patch, not 21
rows. A fingerprint whose scan marks nothing records **no**
observation — absence means "nothing claimed generation", and the
ledger stays quiet for 99.3% of the corpus.

- `detail`: `'<family>/<percent>'` — the dominant family by generated
  changed lines plus the coverage as a rounded integer percent, e.g.
  `autotools/99` for gatos. Compact enough for worklist badges
  (phase 4) and stable per fingerprint at a fixed rule version.
- `evidence`: canonical JSON (`sort_keys`, stable ordering): the
  per-file breakdown (`path`, `family`, `signals`, `generator`,
  `version`, `added`, `removed`, in diff order) plus
  `generated_changed`, `residue_changed`, `total_changed`. This is
  the record phase 3 reads `residue_changed` from and the review UI
  renders per-file badges from — and the generator versions ride to
  the maintenance-health plan exactly as the master plan promised.

Helpers live in `generated.py` beside the scanner (mirroring
`reviewability.evidence_for`): `detail_for(scan)`,
`evidence_for(scan)`, and the consumer-side
`generated_marks(conn) -> dict[fingerprint, dict]` that parses live
observations back into per-fingerprint records for phases 3–4.

### Record integration: desired-vs-live, the injection shape

The recorder computes the desired observation set for each
fingerprint (empty, or the single `(detail, evidence, version)`
triple) and compares it against the live `generated-content` rows —
exactly the injection tripwire's pattern in `record.py`. Equal sets
skip (no churn on re-runs); a difference supersedes the
fingerprint's prior rows and appends the current one. This one
mechanism covers all three transitions: a new mark, a mark that
changes under a version bump, and a mark that *disappears* (a
tightened rule un-marks a file — the phase-1 `Makefile.in` history
is exactly why retraction must work). `RecordStats` gains
`generated_appended` / `generated_skipped` / `generated_superseded`,
and the record CLI prints them alongside the other axes.

Cost: one extra `scan()` per fingerprint inside the existing
`iter_classified` loop — measured at ~1.3 ms per fingerprint, ~80 s
over the corpus, in line with the other deterministic axes. No rule
registry entry is needed (observation-only sources like
`size-rule` and `injection-scan` are not registered decision rules).

### The rule documented where the others live

`docs/deterministic-rules.md` gains the generated-content entry:
both signals, the `Makefile.in` corroboration tier and why
(measured ~⅔ hand-written without it), the backup-suffix rule, the
banner version semantics (as-the-patch-leaves-it), the measured
corpus rates from the findings, and the honest boundaries — the
mark is never a verdict, and the libtool source package itself is
the known case where the name claim is wrong by construction
(its `libtool.m4`/`ltmain.sh` are hand-written upstream source).

### The real record run is part of the phase

After the code lands, the record pass runs against the real reviews
data root from this worktree (`PYTHONPATH=.` — the operator's
editable install tracks `main`, so running from the branch checkout
is required until merge). Expected: 429 fingerprints gain a live
`generated-content` observation, matching the findings; a re-run
appends nothing. The reviews repo's export → commit → push remains
the operator's publish gate, untouched by this phase.

## Steps

| Step | Effort | Model | Brief |
|------|--------|-------|-------|
| S1 | med | opus | **Observation helpers + tests.** In `generated.py`: `detail_for(scan)` (dominant family by generated changed lines, `'<family>/<percent>'`, percent = `round(coverage * 100)`), `evidence_for(scan)` (canonical JSON per the design), `GENERATED_KIND = 'generated-content'`, `GENERATED_OBSERVED_BY = 'generated-scan'`, and `generated_marks(conn)` parsing live observations via `ledger.live_observations` (lazy import, mirroring `reviewability.reviewability_by_fingerprint`). Tests: detail/evidence for the gatos-shaped fixture (stable across two calls), dominant-family tie handling, `generated_marks` round-trip against a temp ledger. One commit. |
| S2 | med | opus | **Recorder integration + tests.** In `record.py`: after the injection block, compute `generated.scan(record.body)`; desired set = `{}` when no files marked else `{(detail_for, evidence_for, GENERATED_RULES_VERSION)}`; compare with live `generated-content` rows exactly as the injection block does; skip / supersede+append; new `RecordStats` fields; surface the counts in the record CLI's summary output (find where reviewability/injection stats print and match). Tests mirroring `test_injection.py`'s recorder layer: a synthetic corpus with a marked patch, an unmarked patch; assert one live observation with the right detail/evidence; idempotent re-run; retraction when a monkeypatched scan stops marking; export/import round-trip preserves the observation. One commit. |
| S3 | low | sonnet | **Docs.** The `docs/deterministic-rules.md` entry per the design (mirror the injection tripwire entry's structure: what it matches, what it records, measured rates, honest boundaries); a sentence in `docs/workflow.md` where the deterministic axes are enumerated; update ARCHITECTURE.md's `generated.py` bullet (phase 2: recorded by the record pass) and its `record.py` mention. One commit. |
| S4 | — | management | **The real record run.** From this worktree: `PYTHONPATH=. python3 -m divergulent.classify.record` via the `divergulent-classify` verbs against the reviews data root; verify 429 fingerprints carry the live observation, spot-check gatos (`detail='autotools/99'`, 21 files in evidence, residue 603), re-run for idempotency (0 appended). Results recorded in the master plan's execution table row. |

## Testing requirements

- Detail/evidence helpers are pure and stable (two calls, identical
  bytes) — the idempotency skip depends on it.
- Recorder: marked → one live row; unmarked → none; re-run → zero
  appends; version-bump/un-mark → prior row superseded, live set
  correct; the stats fields count each path.
- Export/import round-trips the observation byte-identically.
- A grep-level test (or assertion in review) that no category rule
  consumes `generated-content` — the mark must never become a
  verdict.
- `pre-commit run --all-files` green per step.

## Success criteria

- After S4, 429 fingerprints carry a live `generated-content`
  observation (the findings number), gatos reads
  `autotools/99` with 21 files and `residue_changed` 603 in
  evidence, and an immediate re-record appends nothing.
- The observation survives export → import byte-identically.
- `docs/deterministic-rules.md` documents the rule with its measured
  rates and honest boundaries.
- Nothing anywhere maps the observation to a category, a priority,
  or a route — that is phase 3's explicitly separate decision.

## Out of scope

- Residue-first projection, the oversized unlock, risk re-scoring —
  phase 3.
- Review UI badges/collapse — phase 4.
- Publishing the reviews-repo export (operator's gate).
- Any new scanner tuning — the v1 signal set is frozen as
  adjudicated; changes now are a version bump with findings, not a
  quiet edit.

## Back brief

Before executing: this phase makes the mark *exist* and nothing
more. One observation per marked fingerprint, evidence carrying the
per-file claims and the residue arithmetic; desired-vs-live
recording so marking, un-marking and version bumps all converge; the
rule documented beside its peers; the real run verified against the
findings' 429. Routing and rendering stay out, deliberately.
