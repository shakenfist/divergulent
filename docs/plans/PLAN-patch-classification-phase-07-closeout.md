# Phase 7 — close out the patch-classification plan

Part of [PLAN-patch-classification.md](PLAN-patch-classification.md). Phases 1–6
are implemented and the master plan itself says the remainder is *"operational, not
architectural"*. This phase makes that true on paper as well as in the code: it
runs the pre-push audit over the whole plan's accumulated work, moves the two
genuinely-outstanding measurements out of plan files and into the issue tracker,
writes the operator runbook the ongoing grind has been living without, and marks
the plan `Complete` in both places it is tracked.

Nothing in this phase changes classifier behaviour. Every code-shaped item found
along the way is recorded, not fixed (see *Out of scope*).

**Planning effort:** high — the judgement here is what "complete" is allowed to
mean while ~43k fingerprints remain unreviewed, and whether a plan reaching
`Complete` now must carry a push-audit phase. The execution steps are mostly
mechanical.

## Scope

**In:**

- The pre-push audit (`PUSH-AUDIT.md`) over the accumulated diff of phases 1–6,
  with the merge record reconstructed and written down.
- GitHub issues for the two outstanding measurements (phase 6 E6's verdict split,
  phase 4 R4's risk-gate validation), linked from the plans they came from.
- `docs/classification-runbook.md` — the recurring operator loop, which exists
  nowhere today.
- The master plan's Execution table: vocabulary-compliant `Status` cells, an
  `Outcome` column for the prose that is there now, a `Merged` column, and the
  `Future work` / `Bugs fixed during this work` close-out sections.
- `docs/plans/index.md`, `docs/status.md`: status and framing.

**Out:**

- **Running the deferred measurements.** E6's confirmed:contradicted:unknown split
  needs an operator Security-Tracker/BTS snapshot run; R4 needs a hand-labelled
  sample and operator LLM spend. Both become issues, and the plan is `Complete`
  without them (Decision 6).
- **The ongoing triage/review grind.** ~43k residue is a budgeted operational
  activity, documented by the runbook, not tracked as plan work.
- **Fixing anything the audit finds.** Audit findings land as their own pull
  request, per the push-audit shared block; this plan's job is to run the audit and
  record the result.
- **Retro-fitting other plans' tables** to the status vocabulary (Decision 3).

## What the survey found

Verified against the tree at `d311faf` on 2026-09-04. **The master plan's
description of the code is accurate** — every phase-6 and risk-gate artifact it
claims exists does exist, and the four relevant test modules pass. The staleness is
all in the *paperwork*, and there is one omission that matters:

1. **Phase 6 and the risk gate are genuinely implemented.**
   `divergulent/classify/cross_reference.py:136` (`verify_cve`) and `:203`
   (`verify_bugs`), the external pass in `record.py:187-211`, the confirmed-CVE
   bundle reason at `classification_bundle.py:97`, `security_tracker.py`, `bts.py`,
   `risk.py`, and `.github/workflows/build-bts.yml` (weekly, Mondays 04:17 UTC) are
   all present and on `develop`. `tox -e py3 -- 'test_(cross_reference|risk|security_tracker|bts)'`
   is green (63 tests, 6.3 s). Nothing in the survey contradicted a claim in the
   master plan or in the phase-6 plan — worth saying explicitly.
2. **Only two things are actually outstanding, and both are measurements of a
   shipping system, not missing code.** Phase 6 **E6**'s
   confirmed:contradicted:unknown split awaits an operator record-with-snapshot run
   (`PLAN-patch-classification-phase-06-findings.md`, *"What is NOT yet measured"*);
   phase 4 **R4** (`PLAN-patch-classification-phase-04-risk-gate.md:174`) awaits a
   larger hand-labelled validation to firm the ≥elevated threshold and the model
   default. **Neither has a GitHub issue** — they exist only inside plan files that
   stop being read the moment the plan says `Complete`. That is the failure mode this
   phase exists to prevent.
3. **No operator runbook exists anywhere.** `docs/workflow.md` explains the ten
   pipeline stages and `docs/patch-classification.md` explains the modules, but
   neither gives the recurring command sequence, its cadence, or what is automated:
   `docs/workflow.md` names `divergulent-classify` twice, in prose. So the "runbook
   entry" this closeout needs is a new document, not an edit (Decision 4).
4. **The master plan's own Execution table violates the `plan-status-vocabulary`
   shared block.** The block (quoted in `PLAN-TEMPLATE.md:78-106`) says a status
   cell — *"in the master plan's own Execution phase table"* — holds exactly one
   vocabulary term *"and nothing else"*. All six rows carry prose:
   `**Done** — ≈61.5k patches → 60,640 distinct (dedup 1.02x; no shortcut)`,
   `**Implemented; operating** — …`, `**Implemented (E1–E5)** — …`.
   `docs/plans/index.md` is compliant (its `Status` cell is bare `In progress`; the
   prose lives in the `Phases` cell, which the block permits).
5. **The master plan is missing the `plan-closeout-sections` sections.** It has
   `Out of scope` and `Open questions` but no `Future work` and no `Bugs fixed
   during this work`; `PLAN-initial.md` (a `Complete` plan) has both. A closeout is
   exactly when those get written.
6. **A push-audit phase is arriving in this repo's template.** Open PR #87 adds the
   `plan-push-audit-phase v2` shared block to `PLAN-TEMPLATE.md` (issue #79's
   finding), which makes a final PUSH-AUDIT phase the mandatory last row of every
   master plan's Execution table, requires a `Merged` column recording what landed
   each phase, and says a plan that *"has the phase runs it even if it reaches
   `Complete` before the phase does"* — while a plan **already** `Complete` without
   the phase is not reopened. `PLAN-patch-classification.md` is not yet `Complete`,
   so the exemption does not apply to it (Decision 2).
7. **`docs/status.md:3` frames the pipeline as "in flight"** and its
   *"The patch-classification pipeline"* section describes it as in progress. That
   framing needs to follow the status change, and it is the one place a reader
   outside the plans directory would look.
8. **Precedent, and a live inconsistency in it.** The published-cache closeout (open
   PR #88, `published-cache-closeout`) is the shape to follow — one commit, plan
   tables plus `index.md`, a dated *"Plan complete"* paragraph — but it keeps prose
   in the master plan's `Status` cells (`Complete (~0.73 MB; …)`), i.e. it carries
   finding 4 forward. This phase does not copy that (Decision 3).

**Corrections made at source as part of the planning commit:** none were needed —
the survey found no false factual claim in the master plan or the phase plans. The
paperwork defects (4, 5, 6, 7) are the *work* of this phase, fixed by its steps
rather than pre-emptively by the planning commit.

## Decisions

1. **This closeout is a numbered phase with its own plan file, not an ad-hoc
   commit.** PR #88 closed out the published-cache plan as a single unreviewed
   commit, which was proportionate there. Here the closeout carries a pre-push audit
   over three months of merged work, two new issues, and a new document — enough
   judgement to be worth reviewing before it happens.
2. **The plan acquires a push-audit phase, and the audit runs before the plan is
   marked `Complete`.** *This is the decision a reviewer is most likely to argue
   with*, so the reasoning, and the counter-argument, in full. The
   `plan-push-audit-phase` block makes the audit mandatory for any plan that carries
   the phase, and exempts only plans that were *already* `Complete` without it.
   `PLAN-patch-classification.md` is `In progress` today, and this phase is the act
   of completing it — so choosing not to carry the phase would be using the
   exemption to duck an audit rather than because history had made one impossible.
   Against: the block is not yet on `develop` (PR #87 is open), the audit is the
   single most expensive item in this phase, and the closeout is otherwise
   paperwork. The deciding consideration is that this plan produced ~30 merged pull
   requests' worth of the project's most security-sensitive code (an LLM tier, a
   signing path, a signed public bundle); if any plan in this repository deserves a
   whole-plan audit, it is this one. If PR #87 has not landed when step C1 runs, the
   phase is carried anyway.
3. **`Status` cells are normalised to bare vocabulary terms; the prose moves to a
   new `Outcome` column, and a `Merged` column is added last.** No information is
   lost, and the block's requirement is met exactly. The `Merged` column goes last
   so a row that omits it still reaches `Status`, as the block specifies. Other
   plans' tables carry the same defect; fixing them is out of scope here (they are
   not the plan being closed) and belongs with issue #79's consistency work.
4. **The runbook is a new `docs/classification-runbook.md`, not a section of
   `docs/patch-classification.md`.** That document explains *how the pipeline is
   built*; a runbook says *what the operator types, in what order, how often, and
   what to check*. Mixing them makes both worse, and the runbook is the document
   someone reads at 9pm when the bundle looks stale. Linked from `docs/index.md`,
   `docs/patch-classification.md` and `docs/workflow.md`.
5. **Deferred work goes to GitHub issues, with the plan files pointing at them.** A
   `Complete` plan is read for history, not for a to-do list. The issues are the
   durable record; the `Future work` section cites them by number rather than
   restating them.
6. **The plan is `Complete` without E6's split or R4's validation.** Both are
   measurements *of a shipping system*: the classification bundle is published
   daily, phase 6's tier is live and records its own stats lines, and the risk gate
   already reorders the queue. Holding a plan open for operator-budgeted spend that
   has no architectural consequence is what keeps every plan in this repository
   permanently `In progress`. What the plan may **not** do is let them vanish —
   hence Decision 5.
7. **The audit scope is written down, not derived at run time.** Base `d7c030e4`
   (PR #20, the merge that introduced the master plan) to `develop`, restricted to
   the classification path set in step C4's brief. Unrelated work interleaved on
   `develop` across those three months (published-cache, renovate, consistency) is
   excluded by the path filter, and the phase records that the range was
   reconstructed.

## The reconstructed merge record

Front-loaded here so C1 does not repeat the archaeology. From `gh pr list --state
merged`; every entry is a merge commit, so `git show --first-parent <sha>` is the
whole of what landed.

| Phase | Merged as |
|-------|-----------|
| (master plan + the 60-cap count fix) | `d7c030e4` (#20) |
| 1. Fingerprint & dedup | `f53ce737` (#22) |
| 2. Deterministic signal extractors | `b57a512e` (#23) |
| 3. Rule engine, registry & ledger | `98233b03` (#24) |
| 4. LLM triage tier | `8828cb16` (#25), `201bc2ee` (#26), `228affb3` (#27), `b02976f3` (#28), `a21033a2` (#29), `af8b1b9b` (#30), `e56fd08d` (#31), `f51da19a` (#32), `d680ef3f` (#33), `fe1fea74` (#44), `d9d5ad6c` (#58), and the prompt-injection pair `fae9f258` (#48), `dd2abb51` (#50) |
| 5. Classification bundle & client display | `4c655626` (#34), `3cff4ea3` (#35), `2be2b108` (#61), `d311fafe` (#85) |
| 6. BTS / upstream cross-reference | `53e0deaa` (#42 — branch named `update-phase-05-status`, contents are phase 6 E1–E4), `f5d0a7a8` (#43, BTS hosting) |
| (docs for the above) | `f5d8a81a` (#45), `9c91e835` (#66) |

Reconstructed after the fact, as the block anticipates. Two traps worth carrying
into the audit: #42's branch name says phase 5 and its contents are phase 6, and the
generated-marking PRs (#51, #52, #55, #56, #59) touch `divergulent/classify/` but
belong to a *different* master plan and are not part of this plan's diff.

## Step plan

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| C1 | low | opus | none | **Restructure the Execution table.** In `docs/plans/PLAN-patch-classification.md`, rewrite the Execution table to `\| Phase \| Plan \| Outcome \| Status \| Merged \|` (Merged last, per `plan-push-audit-phase`). Move each row's existing prose verbatim into `Outcome`; set each `Status` cell to exactly `Complete` (phase 6 included — E1–E5 shipped, and the unmeasured split becomes an issue cited in `Outcome`). Fill `Merged` from *The reconstructed merge record* above, and say in one sentence under the table that the record was reconstructed from `gh pr list --state merged`. Add a final row `7. Pre-push audit + closeout` pointing at this plan file, `Status` `In progress`. Do **not** yet mark the plan itself Complete — that is C5. One commit. |
| C2 | low | opus | none | **File the deferred work.** Two GitHub issues on `shakenfist/divergulent`: (a) *"Measure the phase-6 cross-reference verdict split"* — pull a Security Tracker + BTS snapshot, `divergulent-classify record`, report `external decisions appended/skipped/superseded` and the confirmed:contradicted:unknown split, calibrate the freshness TTL and how hard to treat wrong-source contradictions, append to `PLAN-patch-classification-phase-06-findings.md`; quote its *"What is NOT yet measured"* section. (b) *"Validate and tune the risk gate threshold (R4)"* — re-run the bake-off at larger N on a hand-checked label set, confirm recall/false-alarm at ≥elevated, finalise the model default and threshold, record in the phase-4 findings; note the labelling circularity the plan flagged at `PLAN-patch-classification-phase-04-risk-gate.md:205` and the ~$245–$1.2k spend range the choice governs. Then add a `**Deferred to issue #N.**` line to the status paragraph of each of `PLAN-patch-classification-phase-04-risk-gate.md` and `PLAN-patch-classification-phase-06-cross-reference.md`. One commit (the issues are created, not committed). |
| C3 | med | opus | none | **Write `docs/classification-runbook.md`.** The recurring operator loop, in order, with the real verb names from `divergulent/classify/cli.py`: refresh snapshots (`popcon`, `security-tracker`, `bts` — all corpus-only), `record` to re-apply rules, `risk`, `triage`, `review`/`web`, `export`, then commit+push the JSONL to `shakenfist/divergulent-reviews`, which is the sole human-in-the-loop gate; `status` as the one-screen orientation. State the cadence and what is automated *and cite the workflow*: `build-classification.yml` daily at 04:31 UTC, `build-bts.yml` Mondays 04:17 UTC, the ledger export sharded by ISO week (#85). Say plainly which steps cost operator LLM budget. Register it in `docs/index.md`'s Contents list, and link it from `docs/patch-classification.md` and `docs/workflow.md`. Keep `docs/` links relative and links out of `docs/` absolute (`docs/plans/index.md` states the rule). Update `docs/status.md` so the pipeline is no longer "in flight": the architecture is complete and the review grind is ongoing. Do not restate the pipeline explanation — link to it. One commit. |
| C4 | high | opus | worktree | **Run the pre-push audit over the whole plan.** Follow `PUSH-AUDIT.md`. Scope, fixed by Decision 7: `git diff d7c030e4..develop -- divergulent/classify/ divergulent/tests/ docs/patch-classification.md docs/deterministic-rules.md docs/workflow.md .github/workflows/build-classification.yml .github/workflows/build-bts.yml tools/`. Wave 1 (`pre-commit run --all-files`, `tox`, offline-suite confirmation, the style/hygiene greps) first; wave 2's four judgment agents in parallel, as `PUSH-AUDIT.md` directs — this is an explicit instruction from the operator's plan, so spawn them. Exclude the generated-marking PRs (#51/#52/#55/#56/#59): they touch the same package and belong to another plan. Findings are **recorded, not fixed** here: write them into this plan file under *Audit findings*, ranked, each marked `fix` (its own follow-up PR), `decline` (with the reason, in writing) or `already tracked` (with the issue number). If the audit finds nothing, say so in one sentence — that is a real result. |
| C5 | low | opus | none | **Mark it complete.** Only after C4's findings are each resolved or declined in writing. In `docs/plans/PLAN-patch-classification.md`: add the `Future work` section (E6/R4 by issue number, the ongoing review grind pointing at the runbook, and the master plan's own unresolved `Open questions` — the category enum's validation and the shared community ledger — each labelled kept-open or dropped) and `Bugs fixed during this work` (scan the tracker; the plan's own history names several review/ledger bug-fix PRs — #26, #27, #28 — so summarise rather than enumerate), set the phase-7 row and the plan's own status to `Complete` with a dated *"Plan complete"* paragraph naming what moved to issues, and state the audit result in one sentence. In `docs/plans/index.md`: the patch-classification row's `Status` cell becomes exactly `Complete`, the `Phases` cell's ◐ markers become ✓ with the deferred measurements cited by issue number, and the `Description` stays. Run the verification script below. One commit. |

C1 → C2 → C3 may land as one pull request; C4's findings land as their own, per the
push-audit block; C5 is last and gates on both.

## Verification you can run

The status-vocabulary claim is falsifiable by script, so it is written here rather
than promised. Run from the repository root:

```bash
python3 - <<'EOF'
import pathlib
import sys

TERMS = {'Proposed', 'Not started', 'In progress', 'Blocked', 'Complete',
         'Abandoned', 'Superseded'}
bad = []
for path in [pathlib.Path('docs/plans/PLAN-patch-classification.md'),
             pathlib.Path('docs/plans/index.md')]:
    col = None
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.startswith('|') or set(line) <= set('|- :'):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if 'Status' in cells:                      # a header row: find the column
            col = cells.index('Status')
            continue
        if col is not None and len(cells) > col and cells[col] not in TERMS:
            bad.append('%s:%d: status cell is %r' % (path, n, cells[col]))
print('\n'.join(bad) or 'all status cells hold exactly one vocabulary term')
sys.exit(1 if bad else 0)
EOF
```

(The script tracks the `Status` column position per file, so it works on both the
five-column Execution table and `index.md`'s wider one. `index.md` rows for *other*
plans are checked too — a bonus, and any failure there is reported, not fixed.)

## Risks and mitigations

| Risk | Mitigation |
|------|-----------|
| `Complete` reads as *"the 60k patches are classified"* when ~43k fingerprints are still unreviewed. | C5's *"Plan complete"* paragraph and C3's runbook both state that the bundle ships and *grows*, and `docs/status.md` keeps the residue number visible. The operator reviews that wording specifically at the C5 back brief — it is the one sentence an outside reader will quote. |
| The audit over three months of merged work surfaces a long finding list and the closeout stalls indefinitely. | The push-audit block permits findings to be *"resolved or explicitly declined in writing"*. C4 records and classifies; only `fix`-marked findings become follow-up PRs, and a decline with a stated reason is a legitimate close. The audit is not a licence to re-open phases 1–6. |
| The audit range is wrong, so it silently audits nothing (the block's named failure). | The base commit and path set are fixed in Decision 7 and in C4's brief, not derived at run time; C4 must print the diffstat of its scope before wave 2 and stop if it is implausibly small. A stale local `develop` widens the range, so C4 fetches first (the template's *"In this project"* note). |
| `docs/plans/index.md` conflicts with open PRs #88 (published-cache closeout) and #87 (template blocks). | Different rows and different files; rebase on `develop` before pushing. If #87 lands first, C1 must reconcile with the `plan-push-audit-phase` text it adds — which is the text this phase is already written against. |
| The runbook drifts from the workflows the moment a cron changes. | C3 cites the workflow filenames and schedules rather than paraphrasing them, and the definition of done requires no fact about automation to differ between the runbook, `docs/workflow.md` and `docs/status.md`. |
| Marking phase 6 `Complete` hides that its headline number was never measured. | The `Outcome` cell names the issue, `Future work` names it, and the issue exists before C5 runs. C5 is explicitly gated on C2. |

## Definition of done

- The verification script above exits 0: every `Status` cell in
  `PLAN-patch-classification.md`'s Execution table and in the plan's `index.md` row
  holds exactly one vocabulary term.
- `docs/plans/index.md`'s patch-classification row reads `Complete`, and no `◐`
  remains in its `Phases` cell.
- The Execution table has a `Merged` column, last, and every phase row names at
  least one merge commit; the table is followed by a sentence saying the record was
  reconstructed.
- Two GitHub issues exist (E6's split, R4's validation), each linked from the phase
  plan it came from, and each quoting the specific unmeasured claim.
- `docs/classification-runbook.md` exists, is linked from `docs/index.md`,
  `docs/patch-classification.md` and `docs/workflow.md`, and names every verb the
  recurring loop uses. Each verb it names is present in
  `divergulent/classify/cli.py` — check by grep, not by memory.
- No fact about automation is stated differently on two pages: the classification
  build cadence, the BTS refresh cadence, and the weekly ledger export shard read
  the same in the runbook, `docs/workflow.md` and `docs/status.md`.
- The master plan has both `Future work` and `Bugs fixed during this work`, and
  every item in the master plan's `Open questions` section is either resolved in
  place, moved to `Future work` with an issue number, or explicitly dropped with a
  reason.
- The push-audit phase row exists, records the merge range, and states the audit's
  result in a sentence; every finding is marked `fix` (with a PR), `decline` (with a
  reason) or `already tracked` (with a number).
- `pre-commit run --all-files` is green, and `tox` passes offline.
- Nothing under `divergulent/` changes as part of C1–C3 or C5 — this closeout ships
  no behaviour change (`git diff --stat develop...HEAD -- divergulent/` is empty,
  except for anything a `fix`-marked audit finding pulled in, which lands in its own
  pull request).

## Audit findings

*(C4 writes here. If the audit comes back clean, this section says so in one
sentence and that is the whole record.)*

## Back brief

Before executing: this phase closes the plan out; it does not extend it. The code is
done and the survey confirmed it — what is missing is a whole-plan pre-push audit, a
durable home for two deferred *measurements* (not missing features), an operator
runbook, and status hygiene. The contentious call is Decision 2: the plan acquires a
push-audit phase and runs it before going `Complete`, rather than using the shared
block's "already complete" exemption. `Complete` here means *the architecture is
finished and the system is shipping* — the ~43k-fingerprint review grind continues
under the runbook, and saying so plainly is part of the deliverable.

**Gate before C4.** The audit is cheap to propose and expensive to redo. Confirm the
base commit (`d7c030e4`), the path set, and the exclusion of the generated-marking
PRs with the operator *before* wave 2 spends on four judgment agents.

**Gate before C5.** The operator reads the *"Plan complete"* wording and the
`docs/status.md` change before they land: this is the sentence that tells an outside
reader whether Debian's 60k carried patches have been classified or merely made
classifiable.
