# Phase 4 (sub-plan) — local web UI for human review

Part of [PLAN-patch-classification.md](PLAN-patch-classification.md), extending
the phase-4 [LLM triage tier](PLAN-patch-classification-phase-04-llm-triage.md)
step 4e (the signed human-review tool). That step shipped as a CLI and
explicitly left the door open: *"start as a CLI, with the option to grow into a
local web UI"*, and the phase's open question
*"Review-tool form"* (CLI vs web) was deferred until the CLI had been operated.
It now has — across several sessions of real reviews — and the operator's
finding is that a GUI would make the grind materially easier, **specifically**
for two workflows the linear CLI queue does not serve:

1. **review by category** — sit down and clear all the `documentation` drafts,
   or scrutinise only the `security` ones;
2. **cherry-pick by fingerprint** — when hunting a theory ("are these autotools
   re-generations all benign?"), pull *those* patches, not the next-in-queue.

A third workflow surfaced during planning and is folded in: **spot-check
patches that are *not* in the review queue** — the deterministically-ruled and
already-settled patches — to confirm a rule is classifying correctly, and
re-queue any misfire for human review. The CLI only ever showed the residue
queue; a browser is the natural place to audit the settled view.

**Status: implemented.** Shipped as `divergulent/classify/review_web.py` behind
the `review` extra, in commits across W1–W5 (the keystone `review_one` split, the
read-only server, the signed verdict POST, the audit/spot-check view + re-queue,
and keyboard-shortcut/doc polish). The CLI is unchanged; web and CLI verdicts are
byte-identical. This document is kept as the design record.

## Prompt

Explore before changing:
- divergulent/classify/review.py — the existing CLI this grows from. The whole
  read path is already factored into pure, reusable functions:
  `build_context_view` / `render_in_context` (diff in upstream context),
  `_representative_patch`, `_carrying_packages`, `_llm_draft` / `_draft_reasoning`,
  `_format_package_lines`, `_assignable_categories`, `resolve_fingerprint`
  (prefix → fingerprint — *already* the cherry-pick primitive), `requeue_one`
  (fingerprint-keyed — *already* the "send a rule misfire back to a human"
  primitive the audit view re-uses). Crucially, the render primitives
  (`_representative_patch`, `_carrying_packages`, `_llm_draft`,
  `build_context_view`) are **keyed by fingerprint, not by a queue item**, so the
  full diff-in-context artefact can be rendered for *any* fingerprint, not just
  queued ones — this is what makes the audit/spot-check view cheap.
  The verdict path — `canonical_record`, `sign_decision`, `build_sigstore_signer`
  + `_sign_with_refresh` (lazy auth, refresh-on-expiry) — is reused **verbatim**.
  Note `review_one` (≈line 594) currently *builds context AND acts on the
  choice* in one function; the seam at the `ask(context)` call is where this plan
  splits it.
- divergulent/classify/ledger.py — `open_ledger` (existence + schema guard,
  `Row` factory), `pending_review_items`, `append_review_item`, `mark_reviewed`,
  `resolve_settled_review_items`, `reopen_review_items`, `append_decision`. The
  web layer issues **no new SQL of its own** beyond one worklist query that joins
  the queue to the LLM draft for the category filter — add that as a named helper
  in ledger.py, not an inline string in the server. That helper carries the
  **same `ORDER BY priority DESC, id`** as `pending_review_items`, so a
  category-filtered list is still highest-priority-first *within* the category
  (the filter scopes the queue; it does not change the ordering).
- divergulent/classify/verdict.py — `current_verdict` / `rebuild_current_verdict`
  (the derived view; the web tool never stores a verdict, it appends a decision
  and lets the cache rebuild, exactly as the CLI does). `current_verdict` is also
  the **audit view's data source**: it returns each settled fingerprint's
  `Verdict` with its `category` *and provenance* (`kind` ∈ human/llm/heuristic,
  `decided_by` rule id, `rule_version`). For a deterministically-ruled patch the
  category here *is the rule's category* — which is exactly why the audit view
  reads category from `current_verdict` (the rule's verdict) while the review
  queue reads it from the LLM draft (what the human is about to accept/override).
- divergulent/verify.py and pyproject.toml — the **optional-extra** pattern.
  `sigstore` is the `verify` extra. The web UI follows the *same* pattern with a
  new `review` extra (`divergulent[review]` → Flask + Jinja2). This adds **no new
  dependency to the default install** — the scan/report path every user walks
  stays stdlib + `python-debian`. Review is a tiny, opted-in population (they
  already need the `verify` extra to sign), so carrying Flask + Jinja2 *behind an
  extra* is consistent with [[dependency-minimalism]], not a breach of it: the
  minimalism that matters is on the default path, and the extras are the
  sanctioned escape valve. Signing stays behind the `verify` extra exactly as
  today; a reviewer installs `divergulent[review,verify]`.

The design constraint is **no new default dependency**; review-only deps live
behind the `review` extra. Use Flask + Jinja2 (server-rendered, autoescaping);
do not reach for FastAPI/async — it is overkill for a handful of localhost routes.

## Objective

A local, single-user, localhost-bound web UI over the *existing* review
machinery that:

- presents the same trustworthy artefact the CLI does — the diff **in the
  context of the original upstream source**, the LLM draft + reasoning, the
  author's claim, the queue reason, and the carrying-package blast radius;
- offers three ways to slice the same queue: **next most important** (priority
  order — `pending_review_items`' existing `ORDER BY priority DESC, id`, where
  stored priority is the fingerprint's `n_occurrences`/blast radius; this is the
  CLI's default and the web default too, surfaced as an explicit "review next"
  affordance with the priority shown per row), **filter by category** (the LLM
  draft's category), and **cherry-pick an arbitrary patch by fingerprint/prefix**
  (reusing `resolve_fingerprint`);
- records the human verdict as the **same signed `kind='human'` decision** the
  CLI records — byte-identical `canonical_record`, same Sigstore signature, same
  `mark_reviewed` + dequeue — so the two front-ends are fully interchangeable and
  a reviewer can use either against the same ledger;
- adds an **audit / spot-check view** over patches *not* in the review queue —
  the settled `current_verdict`, filterable by category **and provenance**
  (`kind` = rule/llm/human, or a specific `decided_by` rule), rendering the same
  diff-in-context artefact for any fingerprint — so the operator can confirm a
  deterministic rule is classifying correctly, and **re-queue a misfire** for
  human review via the existing `requeue_one` when it is not;
- adds **no new default dependency** and **no new trust surface**: Flask + Jinja2
  behind the `review` extra (off the scan/report path), bound to `127.0.0.1`, no
  auth (single-user local tool), never a client feature, never CI.

Non-goal: multi-user, remote access, accounts, or any networked deployment. If
this ever wants to be more than a localhost convenience, that is a separate
plan with a real threat model.

## Design decisions

### The refactor is the keystone: split `review_one` into build + record
`review_one` already has a clean seam at the `ask(context)` call. Split it:

- `build_review_context(conn, corpus_dir, index_path, *, fingerprint, item=None,
  fetch) -> ReviewContext | None` — keyed by **fingerprint**, not by a queue
  `item` (the queue row is now optional, supplied only when reviewing from the
  queue; the audit view passes a fingerprint with `item=None`). Gathers the
  representative patch, body, claim, diff, upstream context, draft, and packages —
  all from fingerprint-keyed primitives. Returns `None` when there is no
  representative row (the current defer-pending case). **`patch_name` must travel
  with the context** (the evidence blob needs it) — add it to `ReviewContext`
  rather than returning a side tuple. Keying off the fingerprint here is what lets
  one render path serve both the queue review page and the audit spot-check page.
- `record_review_verdict(conn, item, context, choice, *, signer, now)
  -> ReviewOutcome` — the sign + `append_decision` + `mark_reviewed` path,
  unchanged, driven by a `choice` that today comes from `ask()` and tomorrow
  comes from an HTTP POST.

`review_one` becomes a three-line composition of the two (build → `ask` →
record), so the CLI keeps its exact behaviour and the web server calls the same
two functions around an HTTP round-trip. This refactor stands on its own merits
and lands as its own commit **before** any web code — the CLI's existing tests
must pass unchanged.

### Flask + Jinja2 behind the `review` extra, server-rendered HTML, no JS framework
A small Flask app with Jinja2 templates, server-rendered. **Jinja2 autoescaping
is the load-bearing reason to take the dependency**: for a tool whose whole job
is trustworthy review, "a patch/path/package containing `<` or `&` cannot break
the page" being structural (autoescape on) beats my-discipline-dependent
`html.escape` calls scattered through string templates. Flask also buys clean
routing and form parsing, and — crucially for testing — a **test client that
exercises handlers with no live socket** (a *better* offline-test story than
hand-rolling on `http.server`, not a regression). The diff renders **nicer** than
the pager: `add`/`del`/`context` spans with a small inline stylesheet, the
upstream-context lines distinguishable from the patch hunk. A *small* sprinkle of
vanilla JS for keyboard shortcuts (`j`/`k` next/prev, number keys to pick a
category, `d` defer) is acceptable; no build step, no bundler, no front-end
framework. FastAPI/async is explicitly rejected — overkill for a handful of
localhost routes.

### Routes are thin; they call the same functions the CLI calls
- `GET /` — the worklist. Renders `pending_review_items(conn)` in its existing
  **priority order** (highest `priority`/`n_occurrences` first), so the top row is
  always "the next most important." Each row links to its review page and shows
  **priority/occurrence count**, package count, and draft category at a glance, so
  the reviewer sees *why* it ranks where it does. An optional `?category=<cat>`
  filter (served by the new ledger helper) narrows to that category **but keeps
  the same priority order within it** — so "review next most important" composes
  with the category slice (next-most-important *documentation* patch, say). A
  fingerprint search box
  (`?fingerprint=<prefix>` → `resolve_fingerprint`) narrow the list; a prominent
  **"Review next most important"** action jumps straight to the top pending item
  (the priority slice as a one-click entry, mirroring the CLI's default pull).
- `GET /review/<fingerprint>` — one item. Calls `build_review_context(...,
  fingerprint=...)` and renders the diff-in-context, draft, claim, reason,
  packages, and a verdict form whose options are exactly `_assignable_categories()`
  (so `test` and the triage categories stay in lockstep with the CLI) plus
  accept-draft and defer. Because the context is fingerprint-keyed, the *same*
  page renders for a queued item (with the verdict form) and for an audited
  settled item (reached from `/audit`, showing its current verdict + a re-queue
  action instead of the verdict form).
- `POST /review/<fingerprint>` — the verdict. Captures `now` **once** server-side,
  calls `record_review_verdict` (which signs via the shared
  `build_sigstore_signer` factory — same lazy-auth + refresh-on-expiry, so a
  token that expired while the human read is refreshed at submit), then
  `rebuild_current_verdict` + `resolve_settled_review_items`, and redirects back
  to the worklist. Signing failures render an error page, not a 500.
- `GET /audit` — the spot-check view (settled patches *not* in the queue). Lists
  `current_verdict(conn)` entries, with `?category=<cat>` and `?source=<kind|rule>`
  filters (provenance from `Verdict.kind` / `Verdict.decided_by`), each row
  showing fingerprint, category, and *who decided it under which rule version*.
  Rows link to `GET /review/<fingerprint>` for the full diff. No signing — this is
  a read view plus one write action below.
- `POST /requeue/<fingerprint>` — re-queue a misfire. Captures `now` once, calls
  `requeue_one(conn, fingerprint, now=...)` (the existing CLI primitive), and
  redirects to `/audit`. This is an *un-signed* queue mutation (it records no
  verdict; it just re-opens the item for human review), exactly as the CLI's
  requeue does — the eventual human verdict is what gets signed.
- `GET /healthz` or similar is unnecessary; keep the surface minimal.

### Signing is unchanged — the GUI neither fixes nor worsens Sigstore
The OIDC browser flow, the short-lived token, and `_sign_with_refresh` carry
over verbatim. The web UI's *only* incidental win: the human reads in the
browser and the token must be valid at **submit**, which the refresh path already
guarantees — so the "token expired while I read the diff" failure the CLI hit is
structurally avoided, not by new code but by where the read-time sits. The
broader "is Sigstore the right choice" question stays open and out of scope here.

### One process, injected boundaries, offline tests
The app is constructed via a `create_app(conn, corpus_dir, index_path, *, fetch,
signer)` factory carrying the same injected `fetch` and `signer` the CLI uses, so
handler logic is tested **offline** with Flask's test client (`app.test_client()`
— no socket bound, no network, no real signer): drive a `GET`/`POST`, assert the
rendered HTML / the recorded decision. The `_real_fetch` and `build_sigstore_signer`
wiring lives only in the `__main__`/CLI entry, never in the factory the tests call.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| W1 | med | opus | none | **Refactor only, no web code.** Split `review_one` into `build_review_context(...) -> ReviewContext \| None` and `record_review_verdict(conn, item, context, choice, *, signer, now) -> ReviewOutcome`; add `patch_name` to `ReviewContext`; rewrite `review_one` as their composition. Add the ledger worklist-by-category helper (`pending_review_items` filtered by the joined LLM-draft category) as a named function in ledger.py, carrying the **same `ORDER BY priority DESC, id`** so a category-filtered list stays priority-ordered within the category. All existing review/ledger tests pass unchanged; add a test that `build_review_context` returns `None` on a missing representative row and that `record_review_verdict` is byte-identical in its `canonical_record`/decision to the old path. One commit. |
| W2 | high | opus | none | **The server, read-only half.** Add the `review` extra to pyproject.toml (Flask + Jinja2). Add `divergulent/classify/review_web.py`: a `create_app(...)` Flask factory with `GET /` (worklist in `pending_review_items` priority order, priority/occurrence + package count + draft category shown per row, a "Review next most important" link to the top pending item, `?category=` filter, `?fingerprint=` search via `resolve_fingerprint`) and `GET /review/<fingerprint>` (diff-in-context + draft + claim + packages, Jinja2 template with diff coloring). No POST, no signing yet. The factory takes injected `fetch`/`signer` so it tests offline via `app.test_client()` against a fake fetch and an in-memory ledger. Bind `127.0.0.1` only. One commit. |
| W3 | high | opus | none | **The verdict path.** Add `POST /review/<fingerprint>`: capture `now` once, call `record_review_verdict` with the shared `build_sigstore_signer`, then `rebuild_current_verdict` + `resolve_settled_review_items`, redirect to `/`. Verdict form options = `_assignable_categories()`. Signing/auth errors render an error template. A `python -m divergulent.classify.review_web --ledger ... --corpus ... [--port]` entry (`app.run(host='127.0.0.1', ...)`; print the URL). Offline tests (test client): a POSTed verdict records the same signed `kind='human'` decision the CLI would, marks the item reviewed, and dequeues settled items; a signer failure renders the error template, not a 500. One commit. |
| W5 | med | opus | none | **The audit / spot-check view.** Add `GET /audit`: list `current_verdict(conn)` with `?category=` and `?source=<kind\|rule>` provenance filters, each row showing fingerprint + category + `decided_by`/`rule_version`, linking to `GET /review/<fingerprint>` (which already renders any fingerprint after W1). Add `POST /requeue/<fingerprint>` calling the existing `requeue_one` (capture `now` once; redirect to `/audit`). The `/review/<fingerprint>` page, when reached for a settled non-queued item, shows the current verdict + a re-queue button instead of the sign-a-verdict form. Offline tests (test client): the audit list filters correctly by category and by provenance; a re-queue re-opens the item (asserted via `pending_review_items` now including it) and records **no** decision; a hostile fixture stays escaped. One commit. |
| W4 | low | opus | none | **Polish + docs.** Keyboard shortcuts (vanilla JS: `j`/`k`, number keys → category, `d` defer); the carrying-package blast-radius line and draft-reasoning rendering matching the CLI's `_format_package_lines` / `_draft_reasoning`; update README.md, ARCHITECTURE.md, AGENTS.md (a `review_web` module alongside `review`, the three queue slices + the audit/spot-check view, and the `pip install divergulent[review]` / `[review,verify]` invocation), and the Obsidian runbook with the web-UI invocation. Note in the phase-4 plan + findings that the "Review-tool form" open question is resolved (both forms ship; same ledger). One commit. |

W1 → W2 → W3 → W5 → W4. W1 is a pure refactor that must not change CLI behaviour
and is **framework-agnostic** (it stands on its own merits regardless of the web
choice); W2 is usable read-only (the prototype the operator can feel before W3
commits the write path); W3 makes it record verdicts; W5 adds the audit/spot-check
view + re-queue (it builds on W1's fingerprint-keyed context and W3's write
plumbing); W4 is ergonomics + docs last, once the full surface exists. Every step
is fully offline-tested (injected fetch/signer, Flask test client — no real
socket).

## Testing requirements

- Handlers are tested **without a live socket** — via Flask's `app.test_client()`
  against a `create_app(...)` factory with a temp/in-memory ledger, an injected
  fake `fetch`, and a fake `signer`. No real network, no real Sigstore, ever, in
  tests.
- The web verdict path records a decision **byte-identical** to the CLI path for
  the same inputs (same `canonical_record`, same `kind='human'`, same dequeue) —
  asserted directly, so the two front-ends cannot silently diverge.
- The category filter and fingerprint search are tested against a seeded queue.
- The audit view filters settled verdicts correctly by category and by provenance
  (`kind`/`decided_by`); a re-queue **re-opens** the item (it reappears in
  `pending_review_items`) and records **no decision** — asserted directly.
- HTML output is escaped (Jinja2 autoescape) — a patch/package/path containing
  `<`/`&` does not break the page; assert on a hostile fixture.
- The server binds `127.0.0.1` only (assert the bind address in the entry).
- `pre-commit run --all-files` passes; house style (single quotes, 120 cols, no
  trailing whitespace, `from __future__ import annotations`).

## Success criteria for this phase

- A `python -m divergulent.classify.review_web` local tool that drives the human
  review queue from a browser, **reusing every existing read and write function**
  — no duplicated context-building, no second signing path, no new SQL beyond one
  named worklist helper.
- The operator-requested workflows work: **next most important** (priority order),
  **review filtered by category**, and **cherry-pick an arbitrary patch by
  fingerprint/prefix**.
- The **audit / spot-check view** lets the operator browse settled patches *not*
  in the queue, filter by category and by which rule/kind decided them, read the
  full diff, and **re-queue a misfire** for human review — all reusing
  `current_verdict` + `requeue_one`, no new classification path.
- A web verdict and a CLI verdict are **interchangeable**: same signed
  `kind='human'` decision, same precedence, same dequeue; a reviewer can switch
  front-ends mid-grind against one ledger.
- **No new default dependency** — Flask + Jinja2 live behind `divergulent[review]`,
  off the scan/report path; localhost-only, no auth, never CI, never a client
  feature. Signing stays behind the `verify` extra (`[review,verify]` to do both).
- The CLI is untouched in behaviour; its tests pass unchanged after the W1 split.

## Open questions for this phase

- **Worklist ordering** — *resolved*: priority order always (`pending_review_items`'
  existing `ORDER BY priority DESC, id`), so the web "next most important" is the
  same item the CLI's `review` loop would pull next. When a category is selected,
  it is **priority order *within* that category** (the category helper carries the
  same `ORDER BY`); with no category, plain priority order as today. Surfaced as an
  explicit slice (default order + a "Review next" button + the priority shown per
  row), not new ordering logic. *Subtlety to document, not change:* the stored queue `priority` is the
  fingerprint's `n_occurrences` only — the dangerous-construct flag forces an item
  into the queue and shapes triage order but is **not** folded into the stored
  priority, so "most important" here means "most widespread," not "most
  dangerous." If a dangerous-first slice is ever wanted it is a *separate* filter
  joining live observations (out of scope for this sub-plan; note it, don't build
  it).
- **Category-filter source** — *resolved*: the **two views use two sources, by
  design**. The review-queue worklist filters on the **LLM draft** category (what
  the human is about to accept/override; deterministically-ruled patches are not
  in the queue, so there is no conflict). The audit/spot-check view filters on the
  **derived verdict** (`current_verdict`), which for a rule-classified fingerprint
  *is* the rule's category — so "the rule defines the category when a patch never
  reached the LLM" falls out for free.
- **Concurrent edits** — single user, but two browser tabs could both POST a
  verdict for the same fingerprint. The ledger is append-only and
  `mark_reviewed` is idempotent-ish; decide whether a second POST on an
  already-reviewed item is a no-op + friendly message or a soft error.
- **Defer semantics in a browser** — the CLI `defer` leaves the item pending and
  records nothing; the web `defer` should do the same and just navigate on. Keep
  identical.
- **Keyboard-shortcut scope (W4)** — how far to go (`j`/`k`/number-pick is
  plenty; resist building a SPA). Hard line: no framework, no build step.

## Out of scope (later / never)

- Multi-user, remote access, authentication, or any non-localhost deployment —
  a separate plan with a real threat model if ever wanted.
- Any client-side use — the review tool is curation-side, exactly like the CLI.
- Re-litigating the Sigstore-vs-alternatives question — tracked in the parent
  phase-4 plan's open questions; this sub-plan reuses whatever signing the CLI
  uses, unchanged.
- Syntax highlighting beyond add/del/context coloring — nice, but it would want a
  *further* highlighter dependency on top of Flask/Jinja2, for marginal review
  value; out, to keep the `review` extra to what the job actually needs.

## Back brief

Before executing any step, back brief the operator on your understanding of this
sub-plan and how the intended work aligns with it — in particular: W1 is a
behaviour-preserving refactor that must leave the CLI identical and key
`build_review_context` off a fingerprint (queue item optional); the web and CLI
verdict paths must remain byte-identical in what they record; the audit view's
re-queue records **no decision** (only the eventual human verdict is signed); and
the two category sources are deliberate — queue worklist from the LLM draft, audit
view from the derived `current_verdict`.
