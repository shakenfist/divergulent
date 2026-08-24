# Patch classification

How divergulent decides what a carried Debian patch actually *does*. The
pipeline runs **curation-side only** — centrally in the builder, never on
a client — and every stage is append-only, so a decision can be
superseded but never rewritten.

The design record is
[`PLAN-patch-classification.md`](plans/PLAN-patch-classification.md) and
its findings documents; this page describes what runs today.

## Corpus crawl

`divergulent/classify/` is **curation-side only** — it runs centrally in
the builder, never on a client. `corpus.py` crawls the
archive's patched packages into a resumable, content-addressed corpus of raw
patch bodies (reusing `apt_patches`' uncapped fetch with per-worker keep-alive
connection reuse so a bulk crawl resolves DNS ~once per worker, not per file; it
also records each package's `debian/changelog` last-upload date and its one-line
`debian/control` description synopsis from the same `.debian.tar.*`, surfaced as
package age and a description line in review); `fingerprint.py`/`measure.py`
deduplicate and count (the index gains a `package` table carrying both). The first crawl measured
≈61.5k carried patches → 60,640 distinct (dedup 1.02x): carried patches are
overwhelmingly bespoke, so classification leverage must come from category
rules, not deduplication. See
`docs/plans/PLAN-patch-classification-phase-01-findings.md`.

## Deterministic classification

`claim.py`/`content.py`/`rules.py`/`classify.py` classify each
fingerprint deterministically, keeping the author's **claim** (DEP-3 metadata,
untrusted) strictly separate from the **content** (the diff, ground truth) so
their disagreement is the signal. Content is typed code-vs-prose, and the
dangerous-construct scan runs only over added lines in code files — never
pronouncing malice, only surfacing candidate flags. It measured 29.2% of
patches as deterministically settled (packaging/documentation), ~43k
substantive residue for triage — of which the `test-only` rule (a patch
touching only test files → the structural `test` category, CATEGORY_ENUM v2)
deterministically settles a further ~15%, since test churn cannot change the
shipped artifact. The run surfaced (and the same phase then
fixed) a backtick false-positive source by making the dangerous-construct scan
language-aware (shell-only backtick), and showed 58% of patches carry no usable
claim. See
`docs/plans/PLAN-patch-classification-phase-02-findings.md`.

## The decision ledger

`ledger.py`/`record.py`/`verdict.py` wrap the verdicts in an
append-only decision ledger: a versioned rule registry, an immutable `decision`
table that is only ever *superseded* (never edited or deleted), and an
`observation` table for the dangerous-construct flags (so a flag never becomes
a category). The current verdict is **derived**, never stored — per fingerprint,
the highest-precedence live decision — so it cannot drift, and retiring a rule
re-queues exactly its fingerprints (a surgical redo).
`python -m divergulent.classify.ledger build|record|report|supersede` operates
it; the CLI is the only place that reads a clock. `build` creates from scratch
(and now confirms before WIPING a populated ledger — destroying appended
llm/human work — unless `--force`); `record` is the non-destructive counterpart
that applies current/new rules to an EXISTING ledger, superseding a fingerprint's
stale heuristic decision when its winning rule changed (how the `test-only` rule
is rolled out: it reclassified ~6.4k fingerprints to `test` while preserving all
llm/human decisions). The ledger reproduced the deterministic
distribution exactly with a 42,907-fingerprint derived queue. See
`docs/plans/PLAN-patch-classification-phase-03-findings.md`.

## LLM and human triage

The reserved llm/human seats are filled here. `triage.py` does the claim-blind LLM
draft + an independent adversarial verification, routing each patch to
`verified` or `needs_human`. The model-call boundary is
`call(system, user, *, model, schema=None) -> CallResult(text, usage)`: the
**static rubric is the cacheable `system` prompt** and the per-patch diff is the
`user` message, so the rubric is billed once per run and read from cache
thereafter (the rubric is relocated verbatim, so verdicts and the
`(model, prompt_version)` identity are unchanged). The default `claude_cli_call`
backend runs `claude -p --system-prompt <rubric> --tools "" --strict-mcp-config
--setting-sources "" --output-format json` (no new dependency,
subscription-billed) and parses the token-usage block + `total_cost_usd`. **Those
three flags are the cost lever**: a one-shot classification uses none of what
Claude Code injects by default — `--tools ""` drops the built-in tool definitions
(~17k tokens/call), `--strict-mcp-config` ignores local MCP servers, and
`--setting-sources ""` drops project/global `CLAUDE.md` + settings (~2.8k
tokens/call). Together they shrink each request from ~66k to **~640 tokens**
(rubric + diff as plain input, no wasteful cache writes) — API-level efficiency
on the subscription path, ~100× less input. `anthropic_call` sends the rubric as
a 1h-cached `cache_control` block. The driver sums each call's usage into a **Cost
& cache** report section (tokens, cache-hit ratio, reported + at-rates cost per
run and per patch). See
`docs/plans/PLAN-patch-classification-phase-04-triage-backend.md`. Step 4c bumped the ledger to **schema v2**: a
`verified` flag on `decision`, reserved `signature`/`signed_by` columns for
signed human ManualDecisions (4e), and a `review_queue` worklist table. The
precedence is now `human > verified-llm > heuristic > unverified-llm`
(`verdict.decision_rank`) — an **unverified LLM guess never outranks a
heuristic** (no cry wolf), and only the adversarial pass (or a human) promotes
it. `triage_record.record_triage_result` records a `TriageResult` idempotently:
an `llm` decision keyed `decided_by='llm-triage:<model>'` (a model swap is a new
rule identity) / `rule_version=<prompt_version>` (a prompt bump is a new
version), `verified` set from the routing, the draft+verification kept as JSON
evidence, and a pending `review_queue` item for every `needs_human` result.
`python -m divergulent.classify.triage` (in `triage_driver.py`) triages a
bounded, prioritised slice (never the whole queue by accident) and surfaces
candidate deterministic rules for human approval. `python -m
divergulent.classify.risk` (in `risk.py`) is a **security-risk gate**: a cheap,
claim-blind LLM scores **every** carried patch's security risk on a coarse ordinal
(`none/low/elevated/high`) — the whole corpus, not just the residue, since a
settled `packaging` patch (a `debian/rules` hardening change) can still be
security-relevant — recorded as a supersedable `security-risk`
**observation** (`observed_by='risk-gate:<model>'` / `rule_version=`, the same
`(model, prompt_version)` provenance as triage). It is **advisory** — it feeds
priority (risk is the top component of the work-list and `review_queue.priority`,
so the scariest patches are triaged/reviewed first) but never the verdict, so it
needs no verify. A **security-safe cull** scores provably-benign patches (empty/
whitespace/comment-only, doc-only, translation/changelog) `none` with no LLM call
— narrower than the packaging category (a `debian/rules` hardening-flag change is
NOT culled). Default model Opus (bake-off: 100% recall / 0% false-alarm at
≥elevated vs Sonnet 73%/3%); the cull fires ~7% of the full corpus (mostly
doc-only). Diffs are **capped** before the gate (`RISK_MAX_DIFF_CHARS`, head only,
truncation recorded) and `oversized` patches are skipped entirely, so neither the
context-overflow error nor the giant-diff cost spikes recur. Measured cost is
~$0.02/Opus call (the ~600-token rubric is re-sent uncached every call), so the
whole-corpus pass is **~$1.0–1.2k of subscription quota, one-time** — dominated by
call-count, NOT diff size; the model (Sonnet ≈5× cheaper) / scope / rubric-caching
levers are the real cost dial. A third, **deterministic** axis —
`reviewability.py` — records each fingerprint's size tier
(`normal`/`large`/`oversized`, by changed-line count) as a `reviewability`
observation (`observed_by='size-rule'`) at `ledger build`/`record`; an `oversized`
patch (>5,000 changed lines) is not line-reviewable, so both LLM passes skip it and
the review UI buckets it. A fourth, also **deterministic** axis — `reach.py` —
records each fingerprint's **install-base** as a t-shirt size (`XS`–`XL`, a
`reach` observation, `observed_by='popcon-rule'`) from a pinned Debian popcon
snapshot (`popcon.py`, `python -m divergulent.classify.popcon <corpus_dir>` →
`corpus/popcon.sqlite`) joined against the source's binary names (the `.dsc`
`Binary:` field, captured into `package.binaries` on the corpus rebuild). Reach is
the MAX install count over the binaries of every source carrying the fingerprint,
bucketed relative to the snapshot's `max(inst)` anchor (resilient to t64/soname
renames). It enters priority as a **secondary key WITHIN the security tier** —
`risk_rank * 1e9 + reach_rank * 1e6 + occurrence`, bands non-overlapping so reach
**never crosses a risk boundary** — surfacing "security-impacting AND widely-run"
first; it is opt-in on a pinned snapshot and recorded only when a bucket changes
(no churn on count drift). See
`docs/plans/PLAN-patch-classification-phase-04-risk-gate.md`,
`docs/plans/PLAN-patch-classification-phase-04-reviewability-axis.md` and
`docs/plans/PLAN-patch-classification-phase-04-reach-axis.md`. `cross_reference.py`
adds the phase-6 **external** tier (`purity='external'`): it verifies the CVE/bug
references a patch *claims* against Debian's own bulk-pinned records — the Security
Tracker (`security_tracker.py` → `corpus/security_tracker.sqlite`) and the BTS
(`bts.py` → `corpus/bts.sqlite`, a gzipped `bug→source,status` TSV built weekly from
UDD `bugs ∪ archived_bugs` and hosted on the rolling `bts` prerelease — `bts` works
with no operator URL, `pull` gunzips transparently). A code-touching confirmed
CVE over the `unknown` residue settles a `security` decision with an
`input_snapshot` + `input_fresh_until` freshness horizon (re-verified past the
horizon, retracted when the tracker stops supporting it); a contradicted claim only
records a `claim-unconfirmed` provenance observation (a priority nudge below risk +
a review badge), never a category or malice verdict, and defers to any
high-confidence pure-content verdict. Opt-in on the snapshots
(`divergulent-classify security-tracker` / `bts`); ~10% of real-corpus patches carry
a reference (1.44% a CVE) — a scalpel. See
`docs/plans/PLAN-patch-classification-phase-06-cross-reference.md` and its findings.
`python -m divergulent.classify.review` (in `review.py`) is the local, interactive,
Sigstore-signed human tier. It has three subcommands: `review <ledger>
<corpus_dir>` drains the queue (showing a files-changed summary — per-file
added/removed counts, largest first — then each diff in its sources.debian.org
original-source context — fetched per touched file by the file's real path, not
the patch filename, with epoch-stripped version fallback, alongside the source
package(s) carrying the fingerprint — and authenticating to
Sigstore ONCE per session) and records a non-repudiable `kind='human'`
ManualDecision that tops the precedence; `requeue <ledger> <fingerprint>` sends
one fingerprint back for re-review (superseding its settled human verdict, kept
in history, and re-opening its queue item); `history <ledger>` lists recent
verdicts (including superseded ones) so a reviewer can reconsider a past call.
The LLM backends (default `claude -p`, optional Anthropic API) and the signing
are curation-side and injected, so the whole suite is offline; the actual
triage/review pass is the operator's budgeted step. See
`docs/plans/PLAN-patch-classification-phase-04-llm-triage.md`.


## The curation CLI

`divergulent-classify` (in `cli.py`, also `python -m divergulent.classify`) is the
**one front** for all of the above: it resolves a **data root** (`workspace.py`:
a `.divergulent` marker beside `corpus/`+`cache/`, discovered git-style via
`--data`/`DIVERGULENT_DATA`/walk-up) and **forwards** to each command's existing
module main with the ledger/corpus paths spliced in — so verbs (`status`,
`record`, `triage`, `risk`, `review`, `web`, `report`, `requeue`, `history`,
`popcon`, `security-tracker`, `bts`, `export`, `import`, `bundle`, `init`) take no
paths (`record` re-applies the deterministic rules to the
existing ledger — the recurring "I changed a rule, re-apply it" pass; `popcon`
pulls the reach axis's install-base snapshot into `corpus/popcon.sqlite`,
`security-tracker` and `bts` pull the CVE/bug snapshots into
`corpus/security_tracker.sqlite` / `corpus/bts.sqlite`, all **corpus-only**,
needing no ledger; `export`/`import` serialise the ledger to/from
the committed JSONL source of truth and `bundle` builds the publishable bundle —
the publish path below; `import` **creates** the ledger, so it needs none
to pre-exist; the one-time corpus/`build` steps stay
longhand, as they create the root's contents). It guards the forgetful operator: a missing ledger or a not-a-root cwd is a
clear error not a crash, and a **stale published cache** is loudly flagged before
data-consuming verbs. `status` is the one-screen orientation (residue, categories,
risk distribution, pending review, cache age). The old `python -m
divergulent.classify.<x>` forms still work. See
`docs/plans/PLAN-curation-cli-ergonomics.md`.


## Publishing the bundle

`export.py`/`classification_bundle.py`, plus the client half, publish the
curation work. The load-bearing decision is **provenance**: the divergence cache
is a pure function of the archive (CI regenerates it), but the classification
ledger embeds irreproducible human + verified-LLM verdicts, so it is the **source
of truth** and reaches CI as a **committed JSONL export** — never the sqlite
(binary: unreviewable diffs, unmergeable, bloats git). The export is a *directory*
of compact JSONL (null columns omitted), with the two big append-only tables
(`decision`, `observation`) **sharded by calendar month** so no file crosses
GitHub's 100 MB limit as the ledger grows without bound (append-only: supersessions
keep old rows); the small tables are whole, and a `manifest.json` lists the shards.
`export.py` serialises every table verbatim (ids preserved, so verdict precedence —
which tie-breaks on `decision.id` — survives) and rebuilds a faithful sqlite via
`ledger.create_schema`; the round-trip (`import(export(L)) == L`, byte-deterministic,
idempotent) is the trust anchor. The operator's `export → commit → push` is the **sole human-in-the-
loop publish gate** — the diff is a reviewable "here are the verdicts I just added"
— with no auto-created PR. `classification_bundle.py` mirrors `bundle.py` (a single
gzipped, key-sorted JSON document, `schema`/`entry_schema`-versioned) and projects
the ledger down to a **lean** fingerprint→verdict map: category + risk/reach/
reviewability axes + a short provenance reason + the deciding rule, but **no raw
LLM evidence** (that stays auditable in the export). CI builds it from the export
(`tools/build-classification.sh`, pure Python — no archive/deb-src), signs it
keyless (reusing `tools/sign-bundle.sh`) and publishes to the rolling
`classification` release (`tools/publish-classification.sh`,
`.github/workflows/build-classification.yml` — a daily schedule that publishes,
like the cache; the human gate stays the export commit/push into the reviews
repo). The **client** pulls it (`cache
pull-classification`, signature-verified against `verify.CLASSIFICATION_SIGNER_IDENTITIES`,
no spot-check — a verdict has no live oracle), and `show` joins it by hashing each
patch body it already fetched (`PatchDetail.fingerprint`, the same normalised-diff
key; hashing ≠ classifying) to render the per-package breakdown + per-patch "why".
The classify import chain the client now pulls in is stdlib-only, so the minimal
install stays minimal. The bundle *grows* as review settles the residue —
re-publishing just enriches it. See
`docs/plans/PLAN-patch-classification-phase-05-bundle.md`.

## The review web app


`python -m divergulent.classify.review_web` (in `review_web.py`) is a **local
web UI over the same review machinery** — it reuses `build_review_context` and
`record_review_verdict` verbatim, so a web verdict is **byte-identical** to a CLI
verdict and the two front-ends are interchangeable against one ledger. It adds
the slices the linear queue cannot: review **by category**, **cherry-pick by
fingerprint or package**, and an **audit/spot-check view** over settled patches not in the
queue (the derived `current_verdict`, filtered by category and provenance) to
check a deterministic rule and **re-queue** a misfire via `requeue_one` (records
no decision). The queue worklist keys category off the **LLM draft**; the audit
view keys it off the **derived verdict** (the rule's category for rule-classified
patches). It also adds **signed reviewer notes** — append-only, free-text human
annotations on a fingerprint (a third ledger entry type, neither decision nor rule
observation, in an OPTIONAL `note` table that existing ledgers gain via
`ensure_note_table` with no rebuild). A note is signed with the SAME session signer
the verdicts use (`record_note` over `canonical_note`), shown WITH its signer
identity + signature, indicated by a worklist count badge, and never enters the
published bundle. Flask + Jinja2 (autoescaping) are behind the optional **`review`
extra** — `pip install divergulent[review]`, or `[review,verify]` to sign — off
the default scan/report install; it binds **loopback only**, has no auth, is
single-user, and is never run in CI or by clients. Handlers test offline through
Flask's test client (injected fake `fetch`/`signer`, temp ledger; no socket). See
`docs/plans/PLAN-patch-classification-phase-04-review-web.md`.

