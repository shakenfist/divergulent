# Phase 5 — Per-package detail view (`show`)

Part of [PLAN-initial.md](PLAN-initial.md). Plan this phase at
**high effort** for the design, though the implementation is
modest: it is the first command that surfaces *per-patch*
detail, and the motivating concern (does a carried patch
reference the Debian bug that justified it?) lives here.

This phase extends beyond the original first swing (phases
1–4).

**Status: complete.** All steps (5a per-patch detail +
`dep3.bug_references`, 5b the `show` CLI, 5c docs) are
implemented and committed; `divergulent show <package>` works
on real data and the suite passes via `tox -epy3`. The
`divergence()` refactor onto `details()` left its behaviour
and tests unchanged. The header also shows the combined drift
score (the open question resolved in favour of a per-package
dashboard).

## Prompt

The first swing is merged; explore the existing code before
adding to it: `divergulent/inventory.py`,
`divergulent/dep3.py` (already parses `Bug`/`Bug-Debian` and
`Description`/`Subject`), `divergulent/sources/debian_patches.py`
(fetches and classifies patches but currently keeps only
counts), `divergulent/sources/repology.py`, and
`divergulent/cli.py` (the command/render patterns).

The motivating idea: a carried patch *should* reference the
Debian bug that prompted it, but often does not. So this view
surfaces, per patch, what the patch itself declares — its
classification, description, and any bug references — and is
honest when there is nothing to show ("no bug declared" is not
"no bug exists"). Querying the BTS for bugs the patch does
*not* reference is explicitly deferred (Future work).

## Objective

Add a `divergulent show <package>` command: a per-package
drill-down that prints the package's staleness, and a list of
each carried patch with its DEP-3 classification, description,
and declared bug references (with Debian BTS links). It
operates on one installed package, so it is light on the APIs
and benefits from the caches the other commands populate.

End state: `divergulent show bash` prints a readable per-patch
report on a real Debian box; the suite passes offline; a
`--json` form is available.

## Design decisions

- **Argument resolution.** `show <name>` resolves `<name>`
  against the inventory: first as a binary package name, then
  as a source package name; it reports on the *installed*
  source package and version. If `<name>` is not installed,
  exit with a clear error. (Showing arbitrary non-installed
  packages/versions is deferred.)
- **Per-patch detail, reusing the divergence machinery.** Add
  a `PatchDetail` (name, classification, description,
  forwarded raw value, bug references) and a `details(source,
  version) -> PackagePatches` method on `DebianPatchesSource`.
  Refactor the patch-series/format/base resolution that
  `divergence()` already does into a shared private helper so
  `details()` and `divergence()` do not duplicate it — and so
  `divergence()`'s existing behaviour and tests stay green
  (ideally `divergence()` becomes a tally over `details()`).
- **Bug references via DEP-3.** Add `dep3.bug_references(text)
  -> list[BugRef]` where `BugRef(tracker, ref)` comes from the
  `Bug` and `Bug-<vendor>` fields (`Bug` → upstream/generic,
  `Bug-Debian` → debian, `Bug-Ubuntu` → ubuntu, …). The CLI
  linkifies Debian refs: a bare number or `#number` becomes
  `https://bugs.debian.org/<n>`; an existing URL passes
  through. Description comes from the `Description`/`Subject`
  field.
- **Honesty.** Patches with no declared bug show "none
  declared", not a guess. Classification still follows the
  Phase 3 rules (DEP-3 plus the `# DP:` / `deb-*` heuristics);
  the detail view also shows *why* where it cheaply can (e.g.
  the forwarded value), so the user can judge.
- **The header.** `show` leads with the package, installed
  source version, and its staleness (current / behind →
  newest / unknown), then a one-line patch summary (N patches:
  X Debian-only, Y forwarded, Z unknown), then the per-patch
  detail. This makes `show` the per-package counterpart to the
  whole-machine `score`.
- **Output.** Human-readable by default; `--json` emits the
  same structure (staleness + per-patch list with bug refs).
- **Politeness.** One package only — no `--limit` needed; it
  reuses the shared cache. Still goes through the polite
  `HttpClient`.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 5a | medium | sonnet | none | In `divergulent/dep3.py` add `@dataclass(frozen=True) BugRef(tracker, ref)` and `bug_references(text) -> list[BugRef]`, deriving tracker from the field name (`bug` → 'upstream', `bug-debian` → 'debian', `bug-<x>` → '<x>'). In `divergulent/sources/debian_patches.py` add `@dataclass(frozen=True) PatchDetail(name, patch_class, description, forwarded, bugs)` and `@dataclass(frozen=True) PackagePatches(source_package, version, source_format, state, patches)`, plus `details(source_package, version) -> PackagePatches`. Refactor the series/format/base/effective-version resolution shared with `divergence()` into a private helper used by both; keep `divergence()`'s public behaviour and tests unchanged (prefer making it a tally over `details()`). Build each `PatchDetail` from the patch text via `dep3` (classification, `Description`/`Subject`, `Forwarded`, `bug_references`). Tests: bug_references parses Bug/Bug-Debian/Bug-Ubuntu and returns empty for none; details() yields per-patch records for a quilt package (using fake-http fixtures), and NATIVE/CLEAN/UNKNOWN states carry an empty patch list; divergence() counts still match. |
| 5b | medium | sonnet | none | Add the `show` CLI command. In `cli.py` add a `show` sub-parser taking a positional `package` and `--json`. Resolve the name against the inventory (binary then source); error clearly to stderr with exit code 1 if not installed. Query `RepologySource.staleness()` and `DebianPatchesSource.details()` (one shared `HttpClient`). Render a header (package, installed version, staleness/newest, patch summary counts) and then each patch: name, classification, description, forwarded value, and bug references — linkifying Debian refs to `https://bugs.debian.org/<n>` and showing "none declared" when empty. `--json` mirrors the structure. Tests: mock inventory + both sources; assert name resolution (binary and source), the not-installed error path, the rendered detail (including a Debian bug link and a "none declared" patch), and the JSON shape. |
| 5c | low | sonnet | none | Documentation. Update `ARCHITECTURE.md` (the `details()`/`PatchDetail` path and `dep3.bug_references`), `AGENTS.md` (that `show` is the per-package detail view and that declared bugs are surfaced but absence is not "no bug"), and `README.md` (a `divergulent show <package>` usage example noting the Debian BTS links and the honesty caveat). Mark phase 5 complete in the master plan, the index, and this plan. |

Steps run in order: 5b depends on 5a, 5c on both. Commit per
step per the master plan.

## Testing requirements

- The whole suite stays **offline**: inventory, Repology and
  sources.debian.org all mocked.
- `dep3.bug_references` is unit-tested across Bug/Bug-Debian/
  Bug-Ubuntu/none.
- `details()` is tested against fake-http fixtures for
  quilt/native/clean/unknown, and the existing `divergence()`
  counts are confirmed unchanged by the refactor.
- The `show` command is tested for binary- and source-name
  resolution, the not-installed error, the rendered detail
  (Debian bug link + "none declared"), and `--json`.

## Success criteria for this phase

- `divergulent show <package>` prints a correct per-patch
  report (with Debian bug links where declared) for an
  installed package on a real Debian machine (operator smoke
  check).
- Declared bug references are surfaced and linkified; patches
  without them say so plainly — no guessing.
- `divergence()` behaviour and tests are unchanged by the
  shared-resolution refactor.
- `tox -epy3` and `tox -eflake8` pass; docs updated.

## Open questions for this phase

- **Header scope.** Should `show` also display the package's
  combined drift score (from `score.combine`), making it a
  true per-package dashboard? Proposed: show staleness +
  patch summary now; fold in the score if it reads well.
- **Non-installed packages.** Deferred — `show` is
  installed-only for this phase. Confirm.
- **Bug-ref normalisation.** Confirm the Debian linkification
  rule (bare number / `#number` → `bugs.debian.org/<n>`; URL
  passes through).

## Out of scope (future work)

- **BTS cross-referencing** — querying the Debian BTS (via UDD
  or the BTS API) for open bugs against the source package that
  the patches do *not* reference. Recorded in the master plan's
  Future work.
- The "patch hygiene & justification" work (separate future
  master plan), including LLM categorization.

## Back brief

Before executing, back brief the operator on your
understanding of this phase and how the intended work aligns
with it.
