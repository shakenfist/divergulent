# Generated-marking phase 4 — review-UI badges and collapsed segments

Phases 1–3 built the mark, recorded it, and routed on it. Phase 4 is
the human half: the reviewer who receives gatos — now arriving with a
`security/medium` draft — should see at a glance that 21 of its files
claim to be generator output, spend their attention on the 603-line
residue, and still be able to expand any generated segment, because a
marked file is never hidden and never presumed benign. This is the
master plan's final phase.

## Design decisions

### The CLI review view tags, the web view badges and collapses

- **CLI (`review.py`)**: `ReviewContext` gains the fingerprint's mark
  (detail string plus the marked-path set, from `generated_marks` —
  one lookup in `build_review_context`). `_format_file_list` tags
  marked rows (`[gen]` after the path) and appends a one-line mark
  summary to the totals line ("21 of 37 files marked generated
  (autotools/99); residue +a/-b"). The list stays sorted largest
  first — the tag makes the sort's story legible rather than
  changing it.
- **Web worklist (`review_web.py`)**: a `generated` badge carrying
  the mark's `detail` (`autotools/99`), alongside the existing
  reviewability / risk / reach / injection badges — same row-dict,
  chip and template idioms.
- **Web detail page**: the same badge in the header; the
  files-changed list tags marked rows (keeping their anchor links);
  and each marked file's diff block renders **collapsed by default**
  inside `<details>`/`<summary>` — the summary line being the same
  loud fact the projection gives the model (path, changed lines,
  signals, generator/version). Expanding shows the full block
  exactly as today. Unmarked blocks are untouched. No JavaScript —
  `<details>` is native, which suits the plain server-rendered UI.

### Never hidden, never presumed benign

Collapse is presentation, not information loss: the full segment is
in the page, one click away, and the summary states what is
collapsed and why ("claims generated — autoconf 2.59"). The badge
vocabulary says "claims", matching the rule docs. Nothing about the
queue order, the verdict flow, or the signing path changes in this
phase.

### Construct hits located against the residue, at render time

gatos carries 128 `shell-out` observations — true statements that
are all generated `configure`/`ltmain.sh` shell. The observations
stay untouched (they are recorded evidence), but the detail page can
say what the reviewer actually needs: **how many construct hits fall
inside marked files vs the residue**. The ledger rows don't carry
file attribution, so a small pure helper re-runs the existing
dangerous-construct scan per file at render time (one patch, pure
functions, milliseconds) and the page renders e.g. "dangerous
constructs: 128 total — 128 in generated-claiming files, 0 in the
residue". A residue hit is the attention-worthy case and is called
out loudly; an all-generated tally is exactly the reassurance-with-
provenance a reviewer wants before collapsing the bulk. Advisory
display only — no observation is rewritten.

## Steps

| Step | Effort | Model | Brief |
|------|--------|-------|-------|
| S1 | med | opus | **CLI: context + file-list tagging.** `build_review_context` reads `generated_marks(conn)` for the fingerprint; `ReviewContext` gains the mark fields (detail, marked-path set, residue counts); `_format_file_list` (and its caller) tags marked rows and adds the mark summary line. Offline tests: a marked context tags exactly the marked paths, an unmarked context renders byte-identically to today. One commit. |
| S2 | med | opus | **Web: badges + collapsed segments.** Worklist row/badge + detail-page badge via the mark detail (read once per request set, like the injection families dict); files-changed rows tagged, anchors intact; marked file blocks wrapped in `<details>` with the loud summary (path, changed lines, signals, generator/version), unmarked blocks byte-identical. `test_client` tests: badge presence/absence, collapse markup on exactly the marked blocks, anchors still resolve, an unmarked patch's page unchanged. One commit. |
| S3 | med | opus | **Construct-vs-residue tally.** A pure helper (in `generated.py` or beside the scan it reuses — implementer's call, documented) that runs the dangerous-construct patterns per file over one body and splits hit counts into marked vs residue using the mark's path set; the web detail page (and the CLI view's summary) renders the tally, with a loud style on any residue hit. Tests: the gatos-shaped fixture tallies all-generated; a fixture with a residue `system(` call tallies and flags it; no ledger reads or writes beyond the mark. One commit. |
| S4 | low | sonnet | **Docs + statuses.** `docs/workflow.md` review-stage paragraph (badges, collapse, the tally); `docs/deterministic-rules.md` consumed-by paragraph gains the display consumers; `ARCHITECTURE.md` review/review_web/generated bullets; master plan phase-4 row and `docs/plans/index.md`. README untouched (no pitch change — per the readme-discipline policy). One commit. |
| S5 | — | management | **Verification against the real ledger.** Render gatos and an unmarked control through the web app's test client from this worktree against the reviews root (read-only): badge text `autotools/99`, 21 collapsed blocks, tagged file list, the 128-construct tally reading all-generated, and the control page unchanged. Results recorded in the master plan table; if all master-plan phases are then complete, mark the plan complete in `docs/plans/index.md`. |

## Testing requirements

- Unmarked fingerprints render byte-identically in both UIs — the
  same do-no-harm bar phases 2–3 held.
- Collapse markup wraps exactly the marked blocks; expanding is pure
  HTML (`<details>`), no script dependency asserted.
- The tally helper is pure, reuses the existing construct patterns
  (never a second pattern table), and its marked/residue split is
  tested in both directions.
- Anchor links from tagged rows still land on their blocks.
- `pre-commit run --all-files` green per step; web tests offline via
  the Flask test client as the existing suite does.

## Success criteria

- A reviewer opening gatos sees: the `generated` badge, a tagged
  file list led by the mark summary, 21 collapsed generated blocks
  with loud summaries, the residue expanded and immediately
  readable, and "128 dangerous constructs — all in
  generated-claiming files, 0 in the residue".
- A residue construct hit, when one exists, is visually loud.
- Nothing is hidden: every collapsed segment expands to today's
  exact rendering; no observation, verdict, queue order or signature
  path changes.
- With S5 verified, every phase of PLAN-generated-marking.md is
  complete and the index says so.

## Out of scope

- Any change to observations, verdicts, routing or priorities.
- Re-attributing the dangerous-construct *observations* (the tally
  is render-time display; the ledger rows stay as recorded).
- The reviews-repo export/commit (operator's gate).
- Rebuild-and-subtract verification (master plan Future work).

## Back brief

Before executing: this phase is presentation only — tag, badge,
collapse, tally — over the marks that already exist. The bar carried
from earlier phases: unmarked renders byte-identical, marked is
never hidden (collapse expands to today's exact output), and the
vocabulary stays "claims generated", never "safe".
