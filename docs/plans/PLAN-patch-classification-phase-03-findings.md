# Phase 3 findings: the decision ledger over the corpus

Results of building the ledger over the phase-1 corpus with the phase-3
`python -m divergulent.classify.ledger build`, for
[PLAN-patch-classification-phase-03-ledger.md](PLAN-patch-classification-phase-03-ledger.md).

## Headline

> **The ledger reproduces the phase-2 distribution exactly — now with full
> provenance, a derived queue, and working supersession.**

60,640 decisions (one per distinct fingerprint), derived current verdict:

| category | fingerprints |
| --- | ---: |
| unknown (phase-4 residue) | 42,907 |
| packaging | 13,382 |
| documentation | 4,351 |

Identical to the phase-2 measurement — but each verdict is now an immutable,
append-only `decision` carrying `decided_by + rule_version + evidence`, and the
view is **derived** (computed from the live decisions), not stored.

## Decisions by rule (which rule settled what)

| rule | fingerprints |
| --- | ---: |
| substantive | 42,907 |
| build-only | 13,178 |
| doc-only | 3,780 |
| comment-only | 571 |
| ignore-file-only | 133 |
| whitespace-only | 70 |
| empty | 1 |

`build-only` carries almost all of `packaging`; the lone `empty` decision is
the single permission-only/mode-change fingerprint (one distinct body, ~30
occurrences). This per-rule breakdown is new visibility the flat phase-2 table
did not give — and it is exactly what makes a wrong rule a surgical redo:
superseding `doc-only` would re-queue precisely those 3,780 fingerprints and
nothing else.

## Queue, observations, audit trail

- **Queue = 42,907** — the `unknown`/substantive residue, derived (not stored)
  by `verdict.queue`; this is what the phase-4 LLM tier consumes by appending
  its own (higher-precedence) decisions into the same ledger.
- **651 observations** (dangerous-construct candidates, one per offending
  line): shell-out 649, decode-exec 1, fetch-piped-to-shell 1. **154 identical
  lines were correctly deduped** by the idempotency check (e.g. a patch that
  adds the same `` `readlink …` `` line twice records the observation once).
  Observations ride alongside the category decision and never become a verdict.
- **Superseded decisions = 0** on a fresh build; supersession is exercised by
  the unit tests (retiring a rule re-queues exactly its fingerprints, audit
  trail intact).

## What this confirms

1. The provenance model works end to end: every verdict is reproducible from
   `(fingerprint, rule_id, rule_version)`, the current view cannot drift from
   the ledger, and the human/LLM seats are reserved so phase 4 slots in with no
   schema change.
2. The queue is the same ~43k residue, now a live view rather than a number —
   phase 4 appends into the ledger and the queue shrinks automatically.

## Performance: per-row commits batched (fixed)

The first build took **~11 minutes** for 60,640 decisions because the append
primitives `commit()`-ed per row — 60k fsyncs. The recorder now appends with
`commit=False` and commits **once** at the end (one transaction), which cut the
build to **~3m40s** for the same output. The fsync storm is gone; the remaining
time is the genuine compute floor — the classification pass (loading 60k bodies
and running the rules, ~42s alone) plus the 60k per-fingerprint idempotency
checks. A further optional win (skipping the idempotency `SELECT` when building
into a fresh, empty ledger) could trim it more, but is not needed: the
transactional fix removed the dominant cost.
