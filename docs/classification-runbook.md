# The classification runbook

What the curation operator actually types, in what order, how often, and
what is already automated. There is exactly one operator today, and this
document exists so that person — or their successor, or the author six
months from now — does not have to re-derive the loop from the code.

This is procedure only. It does not explain the pipeline: for that read
[the processing workflow](workflow.md) (the ten stages and who decides
what) and [patch classification](patch-classification.md) (the modules,
and why each is shaped the way it is). If a command below does not make
sense, the explanation is in one of those two.

Everything here is curation-side. None of it runs on a client machine,
and no client command imports `divergulent/classify/`.

## The shape of the loop

```
status                     ← always start here
  ├── popcon / security-tracker / bts   refresh the pinned snapshots (free)
  ├── record                            re-apply the deterministic rules (free)
  ├── risk                              LLM risk gate      ($ per call)
  ├── triage                            LLM category pass  ($ per call)
  ├── review  /  web                    signed human verdicts (your time)
  └── export → git commit → git push    the publish gate (free)
                                          │
                                          ▼
                    CI builds and publishes the signed bundle, daily
```

Only the last arrow is automated. Nothing pulls a snapshot, spends LLM
budget, or exports on your behalf.

## Before anything: the data root

Every curation verb operates on one **data root** — a directory holding a
`.divergulent` marker file beside `corpus/` (patch bodies,
`fingerprints.sqlite`, `ledger.sqlite`) and `cache/`. The point of the
marker is that you never type a path: `divergulent-classify` resolves the
root and splices the ledger and corpus paths into each command for you.

Discovery is git-style, in this order:

1. an explicit `--data ROOT` flag (it works before *or* after the verb),
2. the `DIVERGULENT_DATA` environment variable,
3. walking up from the current directory looking for `.divergulent`,
4. a lenient fallback: a directory that directly contains
   `corpus/ledger.sqlite`.

If none of those resolve, the command fails with an actionable error
rather than guessing — which is the whole point, because the failure mode
being prevented is silently classifying into the wrong database.
`divergulent-classify init [DIR]` drops a marker and creates `corpus/`
and `cache/` if you are starting fresh.

In practice the data root **is** the local checkout of
[`shakenfist/divergulent-reviews`](https://github.com/shakenfist/divergulent-reviews):
the big regenerable working files (`corpus/bodies/`, the sqlite ledger
and index, the snapshots) are gitignored, and only the `ledger/` export
directory and the analysis notes are tracked. That is why the export step
below is just `git commit` in the directory you were already working in.

### The stale-cache guardrail

Before any data-consuming verb, the CLI checks the *published bundle this
machine has stored* — what `divergulent cache pull` downloads for the
client half — and prints a loud warning if it is missing or more than 14
days old (`CACHE_STALE_DAYS` in `divergulent/classify/cli.py`). It warns;
it never blocks. This is about your local copy of the published cache
going stale under you, not about the ledger. Silence it for a run with
`--no-pull`.

## Start here: `status`

```
divergulent-classify status
```

One screen, no arguments, no cost. It prints the data root, the residue
(fingerprints with no settled verdict), the verdict counts by category,
how many patches the risk gate has scored and their distribution, how
many `elevated`-or-worse patches are *still in the residue* (the line
that tells you where to point your next hour), and how many items are
pending human review. The last line is the cache-freshness report above.

`status` also nags when a `record` run is due — either because a
fingerprint has a verdict but no size tier (a ledger built before an axis
existed), or because the code's rule registry carries a rule id/version
the ledger has never registered (you bumped or added a rule). If it says
so, do step 2 before spending anything.

Run `status` at the start and end of every session. If you only remember
one command from this document, that is the one.

## 1. Refresh the pinned snapshots (free)

Three verbs pull external data into the corpus. All three are
**corpus-only**: they need no ledger, write no decisions, and cost
nothing but a polite HTTP request.

```
divergulent-classify popcon            # → corpus/popcon.sqlite
divergulent-classify security-tracker  # → corpus/security_tracker.sqlite
divergulent-classify bts               # → corpus/bts.sqlite
```

Each pins the snapshot by date (`--date` overrides; `--url` points
somewhere else). They are the inputs to the deterministic reach axis and
the external CVE/bug cross-reference, and every one of them is **opt-in by
presence**: `record` looks for the file and skips the corresponding pass
if it is not there, so an operator who never runs them gets exactly the
old behaviour.

Cadence, honestly:

- `bts` — no more than weekly. The index it downloads is rebuilt by CI
  once a week (see *What is automated* below), so pulling it daily
  re-downloads the same bytes.
- `security-tracker` — whenever you want the CVE cross-reference current.
  The tracker moves continuously, and a confirmed `security` decision
  carries a freshness horizon that is re-verified once it expires, so
  pulling before a `record` run is the useful habit.
- `popcon` — occasionally. Reach is recorded as a t-shirt bucket anchored
  to the snapshot's own maximum, and only when a fingerprint's *bucket*
  changes, so day-to-day install-count drift produces no churn and
  refreshing often buys nothing.

## 2. `record` — re-apply the deterministic rules (free)

```
divergulent-classify record
```

This is the recurring *"I changed a rule, re-apply it"* pass, and it is
the one to reach for after a snapshot pull. It opens the **existing**
ledger and re-runs every deterministic rule append-only: nothing is
edited or deleted, a heuristic decision whose winning rule changed is
*superseded* by a new one, LLM and human decisions are untouched, and any
queued review item a rule has now settled is dequeued. It also runs the
external CVE/bug cross-reference over whatever snapshots step 1 left in
the corpus.

It is free — no model is called — and non-destructive, so when in doubt,
run it. (Its destructive sibling `ledger build`, which recreates the
ledger from scratch and would throw away appended LLM and human work,
deliberately stays longhand: `python -m divergulent.classify.ledger
build`, and it now demands `--force` to wipe a populated ledger.)

It prints a long stats line — `decisions appended/skipped/superseded`,
the per-axis counts, `external decisions appended/skipped/superseded` —
which is the record of what the pass actually did. Read it; a run that
appended nothing means the ledger already reflected the current rules.

## 3. `risk` — the security-risk gate (costs LLM budget)

```
divergulent-classify risk --limit 200
```

A cheap, claim-blind LLM pass that scores un-scored patches' security
risk on a coarse ordinal (`none / low / elevated / high`) and records it
as a supersedable observation. It is **advisory**: it reorders the queue
so triage and human review reach the scariest patches first, and never
sets a category.

What you need to know to drive it:

- `--limit` defaults to **50** — deliberately small, so a stray
  invocation cannot sweep the corpus. Raise it to the size of the batch
  you are willing to pay for.
- `--model` defaults to Opus (chosen in a bake-off: 100% recall / 0%
  false-alarm at `≥elevated`, against Sonnet's 73%/3%).
- The deterministic cull scores provably-benign patches (empty,
  whitespace-only, comment-only, doc-only, translation/changelog-only)
  `none` with **no model call at all** — roughly 7% of the corpus.
- Diffs are capped at `--max-diff-chars` (40,000, head only, truncation
  recorded) and `oversized` patches are skipped, so a giant diff can
  neither overflow the context nor spike the bill.
- `--re-risk-marked` re-scores exactly those generated-content-marked
  fingerprints whose live score was read off a truncated generator head,
  superseding the old score.

The gate runs over the whole corpus, not just the residue, because a
patch the rules settled as `packaging` can still be security-relevant.
That is why it is the expensive-but-once pass rather than a per-session
one: work through it in batches, and once a fingerprint is scored it
stays scored.

## 4. `triage` — the LLM category pass (costs LLM budget)

```
divergulent-classify triage --limit 100
```

The claim-blind LLM draft plus an independent adversarial verification,
over the substantive residue, in risk-then-flags-then-occurrence
priority order. Each verified result becomes an LLM decision; each
`needs_human` result becomes a pending review-queue item.

- `--limit` defaults to **50**, and the run reports
  `untriaged_remaining` so you can see what the cap did not cover. The
  whole residue is never swept by accident.
- `--model` defaults to Sonnet; `--backend` defaults to `claude`, which
  shells out to the `claude` CLI and is subscription-billed. `--backend
  api` uses the Anthropic API instead (`pip install
  divergulent[triage]`).
- `--findings PATH` writes the markdown findings note (default
  `<corpus_dir>/triage-findings.md`), including the candidate
  deterministic rules the run noticed. Those are *candidates for human
  approval*, never auto-applied: if you approve one, it becomes a real
  rule in the registry and the next `record` rolls it out.

Budget arithmetic worth carrying: each fingerprint costs **two** calls
here, a draft and a verify.

## 5. `review` and `web` — the human tier (costs your attention)

```
divergulent-classify review --limit 20     # the terminal pager
divergulent-classify web                   # the local web UI
```

Both record byte-identical verdicts against the same ledger, so they are
interchangeable; pick whichever suits the session. Human verdicts are
signed with Sigstore and top the precedence order — nothing outranks
them.

- `review` drains the priority queue in the terminal, `--limit`
  defaulting to 10, and authenticates to Sigstore **once** per session.
- `web` binds loopback only, port 8765 by default (`--host`/`--port`),
  has no authentication and is single-user. It adds what a linear queue
  cannot: review by category, cherry-pick by fingerprint or package, an
  audit/spot-check view over already-settled patches for confirming a
  deterministic rule is behaving, and signed reviewer notes. It needs the
  optional extras: `pip install divergulent[review]`, or
  `divergulent[review,verify]` to sign.
- Two adjuncts, both free: `divergulent-classify requeue <fingerprint>`
  sends one fingerprint back for re-review (superseding your earlier
  verdict, keeping it in history), and `divergulent-classify history`
  lists recent verdicts including superseded ones, for when you want to
  reconsider a past call.

This step costs no LLM budget. It costs the scarcest input in the whole
system, which is why every cheap tier upstream exists to point it at the
right patches.

## 6. `export`, then commit and push — the publish gate (free)

```
divergulent-classify export
git -C <data-root> add ledger
git -C <data-root> commit -m "Review verdicts for <date>."
git -C <data-root> push
```

`export` serialises the ledger to the committed JSONL export —
`ledger/` at the data root by default — and that commit **is the sole
human-in-the-loop gate for publishing**. The ledger sqlite is never
published, never committed, and never read by CI: it is binary, so its
diffs are unreviewable and unmergeable, and it holds irreproducible human
and verified-LLM verdicts that CI could not regenerate. The text export
exists so that "here are the verdicts I just added" is a diff a human can
read before it goes out.

The layout, as `divergulent/classify/export.py` documents it:
`manifest.json`, the two big append-only tables sharded by **ISO week**
(`decision-2026-W27.jsonl`, `observation-2026-W27.jsonl`), and the small
bounded tables whole (`review_queue.jsonl`, `rule.jsonl`, `note.jsonl`,
`meta.jsonl`). Weekly shards mean a normal session's diff is an append to
the current week's file plus, occasionally, a small edit to an older
shard where something was superseded. The bucket used to be the calendar
month, until one month's observations reached 51 MB — past GitHub's 50 MB
push warning and heading for the hard 100 MB per-file limit.

Two things to expect at the diff:

- **A normal export diff is small.** If you see the entire directory
  churn, stop and find out why before pushing. The usual cause is a
  checkout running older code that re-shards the export back to monthly
  filenames; `write_export` clears every `*.jsonl` before writing, so
  every operator of the reviews repo must be on current code before
  their next export.
- **A deliberate re-shard is its own commit.** It is full-ledger churn
  that cannot be reviewed line by line, so never mix real verdicts into
  it — they would be invisible.

`import` is the inverse and is CI's business, not yours: it rebuilds a
throwaway sqlite from the committed export. `bundle` builds the
publishable bundle locally, which is useful for a dry run but is also
what CI does for you.

## What is automated, and when

Two scheduled workflows touch this pipeline. Both times are UTC, and both
are verifiable in one line of YAML:

| Workflow | Schedule | What it does |
| --- | --- | --- |
| `.github/workflows/build-classification.yml` | `cron: '31 4 * * *'` — daily, 04:31 | Clones `shakenfist/divergulent-reviews`, imports the committed JSONL export, builds the lean classification bundle, signs it keyless with Sigstore, and publishes it to the rolling `classification` release. |
| `.github/workflows/build-bts.yml` | `cron: '17 4 * * 1'` — Mondays, 04:17 | Runs one UDD query, builds the gzipped BTS bug index (`bug → source, status`), and publishes it to the rolling `bts` prerelease — which is what your `bts` snapshot pull downloads. |

A **scheduled** run of either publishes; a **manual** `workflow_dispatch`
is a build-only dry run unless you set its `publish` input. Automating
the publish does not weaken the review gate, because the only thing the
job can publish is what your export commit already approved — and the
measured cost of leaving it manual was the published bundle going three
weeks stale while 23 approved exports queued behind a dispatch nobody
sent.

The practical consequence for your session: **an export you push today
reaches clients in the next 04:31 build.** There is nothing else to
trigger.

Nothing else about this pipeline is automated. No cron pulls a snapshot,
runs `record`, spends LLM budget, opens a pull request, or exports the
ledger. (`.github/workflows/build-cache.yml` is also on a daily
schedule, but it builds the *divergence* cache — a different bundle, a
pure function of the archive, no ledger involved.)

## What costs money, and what does not

| Step | Cost |
| --- | --- |
| `status`, `record`, `export`, `report`, `bundle`, `import` | free — deterministic, no model call |
| `popcon`, `security-tracker`, `bts` | free — one polite HTTP request each |
| `review`, `web`, `requeue`, `history` | free of LLM budget; costs human attention |
| `risk` | **LLM budget** — one call per un-culled patch |
| `triage` | **LLM budget** — two calls per fingerprint (draft + verify) |

Only two verbs spend, and the shape of the spend is the thing to
remember: measured at roughly **$0.02 per Opus call**, and dominated by
**call count, not diff size**. The rubric is a few hundred tokens and the
diff is capped, so a 40,000-character patch and a five-line patch cost
about the same. That is why every lever that matters is a lever on how
many calls you make — `--limit`, the deterministic cull, the oversized
skip, choosing Sonnet (roughly 5× cheaper) for the pass that tolerates
it — and why the whole-corpus risk sweep is budgeted as a one-time
~$1.0–1.2k of subscription quota rather than a per-session expense.
[patch classification](patch-classification.md) has the full
cost-and-cache accounting; the `Cost & cache` section each run prints is
the live number.

## A session, end to end

A representative evening, assuming the snapshots are recent:

```
divergulent-classify status                  # where am I? is record due?
divergulent-classify record                  # if it said so (free)
divergulent-classify risk --limit 200        # extend the scored frontier
divergulent-classify triage --limit 100      # draft + verify the residue
divergulent-classify web                     # review, signed, until tired
divergulent-classify status                  # what moved?
divergulent-classify export                  # then commit and push, as above
```

Skip freely. `record` is only due when `status` says so, and a session
that is purely human review — no `risk`, no `triage` — is a perfectly
normal session and the cheapest kind.

## When the published bundle looks wrong

- **A verdict you recorded is not in the published bundle.** Did the
  export get committed *and pushed*? CI reads the committed JSONL in
  `divergulent-reviews`, never your sqlite, so an un-pushed verdict is
  invisible to it. Then check that a daily build has run since the push.
- **The bundle is stale by days.** Look at the last
  `build-classification.yml` run. The publish step only fires on a
  schedule or an explicit `publish: true` dispatch.
- **A CLI warning about a stale cache.** That is about the published
  bundle *this machine* has stored, not about the published
  classification bundle — run `divergulent cache pull`, or pass
  `--no-pull` if you do not care this run.
- **The bundle covers fewer patches than you expect.** It is supposed to.
  The bundle carries settled verdicts and *grows* as review proceeds;
  clients re-pull to see more of their patches explained. A patch still
  in the residue simply has no entry yet.

## Related documents

- [The processing workflow](workflow.md) — the ten stages, and which tier
  is allowed to decide what.
- [Patch classification](patch-classification.md) — the modules behind
  each verb above, and the design decisions inside them.
- [The deterministic rules](deterministic-rules.md) — every rule and
  axis `record` applies, with corpus hit counts.
- [Status](status.md) — where the pipeline stands, including how much
  residue is left.
- [Development](development.md) — the CI workflows in general, and how
  releases are published.
