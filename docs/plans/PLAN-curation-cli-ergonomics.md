# Curation CLI ergonomics — a discoverable data root + one dispatcher

The curation-side commands grew one argparse per module, so they disagree on call
shape (`triage <ledger> <corpus>` vs `review_web --ledger X --corpus Y` vs
`ledger report <ledger>`) and make the operator re-type the same paths every time.
This consolidates them behind a **data-root convention** and **one dispatcher**,
and adds guardrails so a forgetful operator is protected from sharp edges (a stale
cache, the wrong directory) rather than silently working on bad data.

This is curation-side ergonomics, NOT a phase: phase 5 is the *client* displaying
the published bundle. There is exactly one curation user (the cache builder), so
the bar is **consistent + low-friction + protective**, not a polished public CLI.

**Status: first cut implemented (C1–C4).** Data root (`workspace.py`), the
`divergulent-classify` dispatcher (`cli.py`), `status`, and the cache-freshness
guardrail are built, tested, and documented; the old `python -m
divergulent.classify.<x>` forms keep working. Deferred follow-ups: true cache
auto-pull (needs the cache location reconciled with the data root), a guided
`next` verb, and a corpus-staleness signal — see Open questions.

## Prompt

Explore before changing:
- divergulent/classify/{triage,risk,review,review_web,ledger}.py — each has its own
  ``main(argv)`` and its own positional/flag convention. The dispatcher REUSES
  these mains (builds their argv from the resolved root); it does not reimplement
  them.
- divergulent/cli.py — the CLIENT CLI (scan/report/cache). Curation stays separate
  from it (different audience), but ``cache pull`` / freshness logic is the model
  for the guardrail.
- The existing data layout: a corpus directory holds ``bodies/`` +
  ``fingerprints.sqlite`` + ``ledger.sqlite``; the published cache is elsewhere.

## Objective

- **One way to run curation**, with consistent arguments: `divergulent-classify
  <verb>` (also `python -m divergulent.classify <verb>`), discovering the data
  root so no ledger/corpus paths are typed.
- **A data root** with a known layout and `git`-style upward discovery.
- **Guardrails for the forgetful operator**: detect a stale published cache and
  offer/auto-pull it; refuse to run (with a clear message) when not in a data root.
- **A `status` command** that orients the operator before a session.
- **Non-breaking**: the old `python -m divergulent.classify.<x>` entry points keep
  working, so nothing in the runbook or muscle memory breaks mid-transition.

## Design decisions

### The data root and its layout
A directory marked by a ``.divergulent`` file, holding:
```
<root>/.divergulent        # marker (discovery anchor; may hold config later)
<root>/corpus/             # bodies/ + fingerprints.sqlite + ledger.sqlite
<root>/cache/              # the published bundle(s) for this root
```
This matches the existing data (the ledger already lives inside the corpus dir),
so an existing setup becomes a root by dropping a ``.divergulent`` marker beside
its ``corpus/`` — no data move. A ``Workspace`` resolves
``ledger``/``corpus_dir``/``index``/``cache_dir`` from the root by convention.

### Discovery: explicit → env → walk-up → helpful error
Resolution order, like `git`:
1. ``--data <root>`` flag,
2. ``DIVERGULENT_DATA`` environment variable,
3. walk up from the cwd looking for ``.divergulent``,
4. lenient fallback: a cwd that directly contains ``corpus/ledger.sqlite`` is
   treated as a root,
5. otherwise a clear error: "not inside a divergulent data root — run
   `divergulent-classify init` here, or pass --data".

### One dispatcher that reuses the existing mains
``divergulent/classify/cli.py:main`` parses ``--data`` + a verb + the rest, then
forwards to the underlying module's ``main`` with the resolved paths spliced in
(e.g. ``triage`` → ``triage.main([ledger, corpus, *rest])``; ``web`` →
``review_web.main(['--ledger', ledger, '--corpus', corpus, *rest])``). A thin,
low-risk shim — the real logic stays in the existing, tested modules. Verbs:
``triage``, ``risk``, ``review``, ``web``, ``report``, ``status``, ``init``.

### The cache guardrail (the "don't let me forget" part)
Before a data-consuming verb, and in ``status``, check the stored published
bundle's freshness against what is available, reusing the client's freshness
logic. If a newer bundle exists: **auto-pull it** (cheap) and say so, unless
``--no-pull``. The corpus REBUILD (heavy) stays the operator's explicit call, but
``status`` flags loudly when the corpus looks older than the available bundle, so
"I forgot to re-pull" becomes a visible warning, not silent stale work.

### `status`: orient before a session
One screen: data-root path; corpus + ledger presence; **residue size**; counts by
category; the **security-risk distribution** (how many elevated/high still
un-reviewed); pending human-review count; cache freshness + any staleness warning.
The thing you read first each session instead of stitching ``ledger report`` + the
web UI together.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| C1 | med | opus | none | **Workspace discovery.** `workspace.py`: `Workspace` (root + resolved paths), `find(explicit=None)` (flag → env → walk-up → lenient → clear error), `init(root)` (write marker, mkdir corpus/cache). Offline unit tests (tmp dirs, env, walk-up). One commit. |
| C2 | med | opus | none | **The dispatcher + verbs.** `classify/cli.py:main` + `classify/__main__.py` + a `divergulent-classify` console script. Verbs forward to the existing mains with resolved paths; `init` calls `workspace.init`. Keep the old module mains. Tests drive `main([...])` with a fake data root, asserting it forwards the right argv (monkeypatch the underlying mains). One commit. |
| C3 | med | opus | none | **`status` + the cache guardrail.** `status` prints the orientation screen (residue, by-category, risk distribution, pending review, cache freshness). A freshness check + auto-pull-if-stale (reusing `cache`/`bundle` logic) wired into `status` and ahead of data-consuming verbs, with `--no-pull`. Tests with a seeded ledger + a faked cache state. One commit. |
| C4 | low | opus | none | **Docs + runbook.** Rewrite the runbook commands to the new form; note the old forms still work; update README/AGENTS/ARCHITECTURE. One commit. |

C1 → C2 → C3 → C4. Everything offline-tested; the dispatcher is a thin shim over
already-tested modules.

## Testing requirements

- Discovery is unit-tested across all four resolution paths + the error.
- The dispatcher forwards the correct argv per verb (underlying mains monkeypatched
  — no real triage/LLM/web).
- `status` renders against a seeded in-memory/temp ledger; the cache guardrail is
  tested with a faked stored-bundle state (no network).
- `pre-commit run --all-files` passes; house style.

## Success criteria

- From inside a data root, every curation action is `divergulent-classify <verb>`
  with no path arguments; `--data`/`DIVERGULENT_DATA` work from elsewhere.
- Running outside a root fails with an actionable message, never on the wrong db.
- A stale published cache is detected and pulled (or loudly flagged) before work —
  the forgetful-operator guardrail.
- `status` answers "where am I?" in one command.
- The old `python -m divergulent.classify.<x>` forms still work.

## Open questions

- **Corpus-staleness signal.** Best way to know the corpus is older than the
  available bundle — record the source bundle's `built_on` in the corpus/ledger at
  build time, or compare mtimes? Lean: stamp the bundle id at corpus-build and
  compare; mtime as a rough fallback for the first cut.
- **One binary or two.** A separate `divergulent-classify` script (chosen, keeps
  curation out of the client CLI) vs a `divergulent classify` subgroup. Revisit if
  one-binary is preferred.
- **`next`.** A guided "do the next sensible thing" verb (pull → risk → triage →
  review) — deferred; `status` first.

## Out of scope

- Config beyond the marker file, plugins, multi-root management — single operator.
- Changing the client (`divergulent`) CLI surface.
- Auto-rebuilding the corpus (heavy; stays an explicit operator action, only
  flagged).

## Back brief

Before executing: the dispatcher REUSES the existing module mains (no reimplemented
logic); the data root matches today's layout (marker beside the existing
``corpus/``) so there is no data move; the old ``python -m`` forms keep working;
and the cache guardrail protects the operator (detect + auto-pull or loudly warn),
never silently working on stale data, but never auto-rebuilding the corpus.
