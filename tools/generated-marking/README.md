# Generated-marking measurement prototype

Evaluation prototype for
[PLAN-generated-marking-phase-01-scanner.md](../../docs/plans/PLAN-generated-marking-phase-01-scanner.md)
(phase 1, step S2). This is a measurement tool, not shipped classifier code:
it runs `divergulent.classify.generated.scan` (and its measurement-only
`candidate_banner_hits` sweep) over a whole patch corpus and reports the
numbers phase 1's findings document adjudicates. No ledger writes, no
routing, no network.

## measure.py

Zero heavyweight dependencies (stdlib + `sqlite3` + `divergulent` itself).
Walks one representative body per fingerprint (`MIN(raw_sha256)` per
fingerprint in `fingerprints.sqlite`'s `patch` table), scans each body from
`bodies/<sha[:2]>/<sha>`, and emits:

* per-name match counts across the name set, and what fraction of each
  name's matches also carry a corroborating banner;
* the name-set **gap list** -- banner-only marked files (outside the name
  set), grouped by basename with example paths and generators seen;
* the patch **coverage distribution** (0 / <50% / 50-90% / >=90%), plus the
  full >=50% population as records (phase 3's exact target);
* a **low-frequency-name** adjudication list -- every distinct matched path
  for any name whose total count is <= 20, for human eyeballing;
* the **acinclude.m4** check -- how many touched files have that basename
  and how many of those carry a banner, even though the name is deliberately
  outside the v1 name set;
* a **multi-banner** report -- files whose region carries more than one
  distinct `(generator, version)` banner claim (`scan()` itself reports only
  the first; this informs the plan's first-banner-wins vs
  added-lines-preferred open question);
* the **candidate do-not-edit family** -- hit counts and sample snippets for
  the generic banner family `scan()` never marks on;
* the **translation candidates** (phase 5,
  [plan](../../docs/plans/PLAN-generated-marking-phase-05-translations.md)) --
  per-extension corroboration rates for the `.ts`/`.po`/`.pot` candidate
  (`generated.candidate_translation_hits`, also never marking), the
  uncorroborated `.ts` population (TypeScript -- the collision that makes
  extension-only marking unsafe), how `content._classify_file` types the
  corroborated files today, the would-be coverage distribution, the
  oversized-unlock arithmetic (union with existing generated marks), the
  dangerous-construct hits sitting inside corroborated catalogues, and an
  informational compiled-catalogue (`.qm`/`.mo`/`.gmo`) tally.

Results feed
[PLAN-generated-marking-phase-01-findings.md](../../docs/plans/PLAN-generated-marking-phase-01-findings.md)
(step S3), which is a separate, opus-authored step against the real corpus.
The phase-5 sections feed the phase-5 findings the same way; that
full-corpus run is committed as `results-translations-full-corpus.json`.

### Usage

Must be run from this worktree checkout with `PYTHONPATH=.` -- the
operator's editable install of divergulent tracks `main`, not this branch,
and would otherwise silently scan with a different (or absent) `generated`
module.

```bash
PYTHONPATH=. python3 tools/generated-marking/measure.py \
    /path/to/reviews/corpus --output /tmp/generated-measure.json
```

Use `--limit N` for a smoke run over the first N fingerprints before
committing to a full-corpus pass (see step S3).

Output: a compact human summary to stdout (top entries per section) and,
with `--output`, the full report as sorted, deterministic JSON.

### Tests

`divergulent/tests/test_generated_measure.py` builds a tiny fixture corpus
(sqlite index + bodies) in a tempdir and loads `measure.py` directly via
`importlib` -- fully offline, no subprocess, no real corpus. The real-corpus
run is step S3, never CI.
