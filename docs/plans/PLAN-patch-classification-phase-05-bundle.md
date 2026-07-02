# Phase 5 — Classification bundle & client display

The curation work so far produces a local append-only **ledger** of
fingerprint→verdict decisions (deterministic rules, verified LLM triage, human
review) with security-risk, reach, and reviewability axes. None of it is visible
to a `divergulent` client yet. Phase 5 closes that loop: **publish a signed,
fingerprint-keyed classification bundle** built from the ledger, and have the
client render per-package category breakdowns with a per-patch "why" — the client
running *no* classifier and *no* LLM, exactly as it consumes the divergence cache
today.

Crucially, this phase delivers value from the classification work **as it stands**,
not when review is "done." The bundle is explicitly a *growing* artifact: it ships
whatever verdicts exist (the ~29% deterministic settle + the `test-only` peel +
however much LLM/human residue is decided so far), and each future review session
just enriches a bundle that already ships. This decouples the shippable product
from the completeness of the human-review grind.

**Status: implemented (P1–P5).** The ledger JSONL export/import (`export.py`,
round-trip-tested), the lean signed-able bundle (`classification_bundle.py`,
mirroring `bundle.py`, no raw evidence), the client consumption (`cache
pull-classification` + `show` rendering by hashing the patch body; the classify
import chain the client pulls in is stdlib-only), and the CI publish path
(`tools/build-classification.sh` + `publish-classification.sh` +
`build-classification.yml`, reusing the keyless `sign-bundle.sh`) are built and
offline-tested. As with the cache-publish phase, a real signed publish run is
validated after merge (it needs the ledger data repo + a read token wired). The
open questions below (evidence inline vs split, per-source rollup, data-repo home)
remain deferred follow-ups.

## The mechanics question this phase must answer

The divergence cache and the classification bundle look similar but have
**fundamentally different provenance**, and that difference drives the whole
pipeline design:

- The **divergence cache** is a *pure function of the Debian archive*. CI
  regenerates it from scratch nightly (`tools/build-cache.sh` →
  `divergulent cache build`). Nothing about it needs to be preserved — it is
  recomputed, not remembered.
- The **classification ledger** embeds *irreproducible human + LLM work*: signed
  human verdicts, verified LLM triage, stored raw model responses. CI **cannot**
  regenerate it. It is the **source of truth** and must be durably preserved,
  reviewable, and recoverable — the operator's laptop is not a backup.

So "how does my local ledger reach CI to be published?" is not an afterthought; it
is the load-bearing design decision of this phase. The answer below — **commit a
text export, never the sqlite** — falls directly out of that provenance split.

## Design decisions

### Two artifacts, two lifecycles, two sizes

| | Source of truth (Artifact A) | Published bundle (Artifact B) |
|---|---|---|
| **What** | The full append-only ledger | Derived current verdicts, lean |
| **Format** | Canonical **JSONL** (text) | Gzipped sorted **JSON** (like `bundle.py`) |
| **Contains** | Every `decision`/`observation`/`review`/`note`/`rule` row, incl. raw LLM evidence | Per-fingerprint category + risk/reach/reviewability + short reason + deciding rule/version. **No raw LLM dumps.** |
| **Home** | A git data repo (committed, reviewable) | A GitHub Release tag (`classification`), Sigstore-signed |
| **Built by** | The operator's `export` verb | CI, from Artifact A |
| **Consumed by** | CI (rebuilds sqlite, derives verdicts) | Clients (`cache pull` + `report`/`show`) |
| **Size** | Grows unbounded; text → git delta-compresses | Few MB gzipped; capped by dropping evidence |

### We do **not** commit the sqlite ledger — we commit a JSONL export

The sqlite file is a *derived working copy* on both ends, gitignored. Committing it
would be wrong on every axis:

- **Binary**: unreviewable diffs, unmergeable, no "what changed" in PR review.
- **Bloat**: sqlite pages don't delta-compress; the code (or data) repo's history
  would balloon with every full-file snapshot.
- **Off-pattern**: the project already publishes its bundle as gzipped *JSON*
  (`bundle.py`), not sqlite — sqlite-as-artifact is foreign here.

Instead, `export` serialises the ledger to **canonical JSONL** — one row per line,
stable field order, sorted by `(table, fingerprint, id)` so the same ledger always
yields byte-identical output. The ledger is an append-only *log*; JSONL is its
natural on-disk form. Properties we get for free:

- **The commit diff is the human-in-the-loop publish gate.** "Operator added 47
  human verdicts to `security`" is *visible in review* before anything goes public
  — the no-cry-wolf discipline applied to the classifier itself, and a fit for the
  operator's standing rule that they are involved in every publish.
- **git stays lean** (append-ordered text appends, clean deltas).
- **Round-trip is testable**: `import(export(L))` must reproduce `L` exactly, and
  re-import must be idempotent. This is the trust anchor for the whole pipeline.

### The pipeline, end to end

```
 operator (local)                          git data repo            CI (GitHub Actions)            client
 ───────────────                           ─────────────            ───────────────────            ──────
 review → ledger.sqlite  ──export──▶  classification-ledger.jsonl
 (gitignored, working)                       │  commit + push  ──▶  import → temp sqlite
                                             │  (reviewable diff)    rebuild_current_verdict
                                             │                       + join risk/reach/review axes
                                             │                       build lean JSON bundle
                                             │                       sign (keyless Sigstore)
                                             │                       gh release upload `classification`
                                             ▼                              │
                                       (durable, recoverable)               ▼
                                                                     cache pull → verify → report/show
```

The operator's `export → commit → push` is the *only* human step and the *only*
publish gate. CI is mechanical from there, reusing the divergence-cache publish
path verbatim (`tools/sign-bundle.sh` keyless OIDC, `gh release upload` to a rolling
tag). No PR is auto-created — the operator opens it, per house rule.

### CI rebuilds sqlite from the export rather than building the bundle from JSONL directly

The verdict-precedence logic (`verdict.py:current_verdict` / `rebuild_current_verdict`,
the `human > verified-LLM > heuristic > unverified-LLM` ranking) and the axis
read-backs (`reach_by_fingerprint`, `risk_*_by_fingerprint`,
`reviewability_by_fingerprint`) all operate on a sqlite `Connection`. So CI
`import`s the JSONL into a throwaway sqlite, then reuses that *already-tested*
derivation code unchanged. No second implementation of precedence to keep in sync.

### The bundle is keyed by fingerprint; the client hashes, it does not classify

The bundle maps `fingerprint → verdict`. The client already fetches patch *bodies*
on the deep-dive path (tier-2 `--classify`/`show` via `apt_patches`). To look a
patch up it computes the same `fingerprint.fingerprint()` (a pure stdlib hash,
shared from `divergulent/classify/fingerprint.py`) over the body it already has,
then indexes the bundle. **Hashing is not classifying** — this preserves the
"client runs no classifier/LLM" invariant while letting `show` render the verdict
and its "why." The polite tier-1 *count* overview stays as-is for now; a cheap
per-package category rollup (no body fetch) is a deferred enrichment (Open
questions).

### Schema-versioned and self-describing

The bundle carries `CLASSIFICATION_SCHEMA_VERSION`, `category_enum_version`, and
the rule/prompt versions behind each verdict, so consumers can detect and migrate
across changes rather than drift silently (a master-plan requirement: version the
enum *and* the bundle schema). The deciding `rule_id`+`version` travels with each
verdict so "why" is auditable client-side.

### Where the data repo lives

Recommendation: a **dedicated GitHub data repo** (e.g. `shakenfist/divergulent-ledger`),
private-able to start. Rationale: keeps the code repo's history clean of an
unbounded dataset; readable by the GitHub Actions publish workflow without
cross-host auth; and a dedicated ledger repo is the natural unit to later open as
the *shared community classification ledger* the master plan anticipates ("private
repo first; a shared ledger later"). A private GitLab home (the operator's usual
pattern) is possible but adds cross-host CI friction, since signing/publish run on
GitHub Actions — noted as an Open question, not the default.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| P1 | med | opus | none | **Ledger export/import (source-of-truth text format).** New `divergulent/classify/export.py`: `export_ledger(conn) -> Iterator[str]` and `write_export(conn, path)` emitting canonical, stably-sorted JSONL over `rule`/`decision`/`observation`/`review_queue`/`note`/`meta` (full fidelity, incl. evidence); `import_ledger(lines, dest_conn)` / `load_export(path) -> conn` rebuilding the sqlite. Add `export` + `import` verbs to the `classify/cli.py` dispatcher (forward like `report`). Gitignore `ledger.sqlite`/`fingerprints.sqlite`. Tests: round-trip `import(export(L)) == L` (row-for-row), determinism (byte-identical across two exports), idempotent re-import, empty-ledger. One commit. |
| P2 | med | opus | none | **Classification bundle builder.** New `divergulent/classify/classification_bundle.py` mirroring `bundle.py`: `ClassificationBundle` dataclass (`schema`, `category_enum_version`, `generated_at`, `source_release`, and `verdicts: {fingerprint: {category, confidence, risk, reach, reviewability, reason, rule_id, rule_version}}`), `to_dict`/`from_dict`/`write` (gzipped sorted JSON)/`load`. `build_classification_bundle(conn, *, generated_at, source_release)` → `rebuild_current_verdict` + join the three axis `*_by_fingerprint` read-backs + a short reason (NOT raw evidence). A `bundle` verb (export-or-sqlite in, bundle out). Tests: build from a seeded ledger, assert lean (no `raw_response` leaks), axes present, schema/version fields, deterministic bytes. One commit. |
| P3 | med | opus | none | **Client consumption + display.** Share `fingerprint.fingerprint()` to the client; add classification `cache pull` (new tag/URL template + freshness, mirroring `_cache_pull_command`); optional Sigstore `verify` (new `EXPECTED_SIGNER_IDENTITY` for the classification workflow, lazy/optional like today). Render in `_render_show`/`_show_command`: a per-package category breakdown and, per patch, `category` + `reason` + deciding `rule_id@version`. Tests: a fixture bundle, `show` renders categories + why; verify SKIP when `sigstore` absent; client computes no verdicts. One commit. |
| P4 | med | opus | none | **CI publish workflow + tools scripts.** `.github/workflows/build-classification.yml` (trigger: push/tag in the data repo + `workflow_dispatch`): checkout code + ledger export → `tools/build-classification.sh` (venv install, `classify import` → `classify bundle`) → reuse `tools/sign-bundle.sh` (keyless) → `tools/publish-classification.sh` (`gh release upload classification <bundle> <sig> --clobber`). Wire the `verify.py` expected identity to this workflow path. Scripts ≤5 lines call out to `tools/` per house rule. Document the data-repo split + secrets/checkout. (Validated by a real run *after* merge, like the cache-publish phase.) One commit. |
| P5 | low | opus | none | **Docs + runbook + plan index.** README/AGENTS/ARCHITECTURE: the two-artifact model, the `export → commit → push → CI → publish` pipeline, the data repo, and "client hashes, never classifies." Runbook: the operator's new steps (`divergulent-classify export`, review the diff, commit, push). Update `PLAN-patch-classification.md` Execution row + `docs/plans/index.md` to mark phase 5 in progress. One commit. |

P1 → P2 → P3/P4 (P3 and P4 both depend on P2's bundle format but not each other) → P5.
Everything offline-tested; the bundle reuses the divergence-cache trust model whole.

## Testing requirements

- **Round-trip integrity** is the trust anchor: `import(export(L))` reproduces `L`
  row-for-row; two exports of the same ledger are byte-identical; re-import is
  idempotent. Without this, the published bundle's provenance is unsound.
- The bundle builder is tested against a seeded ledger (deterministic + a verified
  LLM + a human verdict + all three axes) and asserts: correct precedence winner
  per fingerprint, axes joined, **no raw LLM evidence leaks**, schema/version fields
  present, deterministic bytes.
- The client renders categories + "why" from a fixture bundle, computes no verdicts
  itself, and treats a missing `sigstore` as SKIP (not FAIL).
- `pre-commit run --all-files` passes; house style (single quotes, 120 cols, no
  trailing whitespace).

## Success criteria

- A user running `divergulent show <pkg>` (with the classification bundle pulled)
  sees "*N patches — 30 feature, 10 security, …*", and for any patch its category,
  a short reason, and the rule/version that decided it.
- The bundle is built, signed, and published by CI from a **committed text export**;
  the sqlite ledger is never committed.
- The operator's publish action is `export → review the diff → commit → push`; the
  diff shows exactly which verdicts changed.
- The client runs **no** classifier and **no** LLM — it hashes patch bodies and
  looks them up, consuming the signed bundle under the existing trust model.
- The bundle is a *growing* artifact: republishing after more review enriches it
  with zero pipeline changes.

## Open questions

- **Evidence in the export: full vs split.** Committing full raw LLM responses
  inline keeps Artifact A self-contained but grows the JSONL. Alternative: a
  compact verdict JSONL for the reviewable diff + a content-addressed evidence
  store (like patch bodies) committed alongside. Lean: full-inline for the first
  cut (git compresses it); split when size warrants.
- **Cheap per-package overview.** Should the bundle also ship a
  `(source, version) → category counts` rollup so the *polite* tier-1 overview can
  show categories without fetching bodies? Cheaper client UX, larger + version-
  coupled bundle. Defer; key-by-fingerprint first.
- **Data-repo home & visibility.** Dedicated GitHub repo (default, CI-friendly) vs
  the operator's private GitLab (cross-host CI friction) vs an in-repo `ledger/`
  directory (simplest, but bloats the code repo). Private-first then open as a
  community ledger.
- **Bundle size budget.** ~60k fingerprints × lean verdict ≈ a few MB gzipped —
  confirm against the 0.73 MB divergence-cache norm and trim `reason` length if
  needed.
- **Republish cadence / trigger.** On every push to the data repo, on a tag, or
  manual `workflow_dispatch`? Lean: push-to-main of the data repo, with manual
  dispatch as the escape hatch.

## Out of scope

- Auto-creating the publish PR (operator opens it).
- Client-side classification or any LLM on the client.
- Changing the divergence-cache lifecycle or its bundle format.
- The BTS/upstream `external` rules (phase 6).
- A polished community-contribution workflow for the shared ledger (later).

## Back brief

Before executing: the bundle is **derived** from a **committed JSONL export** of the
ledger — the sqlite is never committed (binary, bloating, unmergeable, off-pattern);
the export round-trip must be lossless + deterministic (the trust anchor); CI
rebuilds sqlite from the export and reuses the *existing* verdict-derivation and
publish/sign code unchanged; the published bundle is lean (no raw LLM evidence) and
schema-versioned; the client hashes patch bodies and looks them up (hashing ≠
classifying), running no classifier; and the operator's `export → commit → push` is
the sole human-in-the-loop publish gate, with no auto-created PR.
