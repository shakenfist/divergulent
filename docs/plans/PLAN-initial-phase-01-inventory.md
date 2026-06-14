# Phase 1 — Project skeleton & dpkg inventory

Part of [PLAN-initial.md](PLAN-initial.md). Plan this phase at
**high effort**: it sets the project's shape and the Debian
version-comparison foundation that every later correctness
claim rests on.

**Status: complete.** All steps (1a bootstrap, 1b debversion,
1c inventory, 1d cache/source-interface, 1e CLI, 1f docs) are
implemented and committed; `divergulent inventory` works and
the suite passes via `tox -epy3`.

## Prompt

This phase has no existing code to explore — it creates the
skeleton. Ground decisions in the master plan and the house
conventions (single quotes, double quotes for docstrings,
120-char lines, no trailing whitespace; sub-agents implement,
the management session reviews and commits). Research the
Debian-specific details rather than guessing:

- `dpkg-query` output fields, especially the `${source:Package}`
  and `${source:Version}` virtual fields and when the source
  name/version differ from the binary's.
- Debian version syntax and ordering (`deb-version(7)`):
  epoch, upstream version, Debian revision, and the special
  ordering of `~`.
- The `python-debian` library's `debian.debian_support.Version`
  / `version_compare` API.

## Objective

Deliver a working, installable Python CLI that enumerates the
installed packages on a Debian machine, maps each to its
source package and source version, and prints them — plus the
shared foundations (Debian version handling, the data-source
adapter interface, and an on-disk cache) that Phases 2–4 plug
into. No network access in this phase.

Concrete end state: `divergulent inventory` prints the
installed packages with binary name, binary version, source
name, and source version; `pre-commit run --all-files` passes;
the test suite passes offline with `dpkg-query` mocked.

## Bootstrap status

The project skeleton, packaging, test/lint tooling, and CI/
release pipeline were bootstrapped ahead of phase execution
(mirroring shakenfist/occystrap and shakenfist/clingwrap):

- `pyproject.toml` with `setuptools_scm` (version from git
  tags, written to `divergulent/_version.py`), the
  `divergulent` console-script entry point, the `python-debian`
  runtime dep, and the `test` extra (coverage, testtools,
  mock, stestr, flake8).
- `tox.ini` (`py3`, `flake8` envs), `.stestr.conf`,
  `tools/flake8wrap.sh`, `.pre-commit-config.yaml`.
- Minimal importable package: `divergulent/__init__.py`,
  `cli.py` (argparse skeleton with an `inventory` placeholder),
  `sources/__init__.py`, and a passing smoke test.
- `.github/workflows/unit-tests.yml` (Sanity checks on
  `self-hosted, vm, debian-12`, triggered on push/PR to
  `main`) and `.github/workflows/release.yml` (the
  Sigstore-signed-tag + PyPI-trusted-publishing release
  pipeline), plus `RELEASE-SETUP.md` and `.github/
  actionlint.yaml`.

Verified locally: `tox -epy3` builds the package and the smoke
tests pass. Remaining one-time setup (PyPI trusted publisher,
the `release` GitHub environment, protected tags) is the
operator's, per `RELEASE-SETUP.md`.

So Phase 1 execution effectively begins at step **1b**.

## Design decisions

These are resolved here so the implementing sub-agents do not
have to make them. Flagged items under *Open questions* are
not yet resolved.

- **Package layout (flat, matches house style).** Items
  marked *(done)* were bootstrapped already (see Bootstrap
  status below); the rest are this phase's work.
  ```
  divergulent/
    __init__.py          # (done) version via setuptools_scm
    cli.py               # (done) argparse skeleton; inventory wiring in 1e
    debversion.py        # 1b — Debian version parse/compare wrapper
    inventory.py         # 1c
    cache.py             # 1d
    sources/
      __init__.py        # (done)
      base.py            # 1d — Source adapter interface
    tests/
      __init__.py        # (done)
      test_smoke.py      # (done)
      fixtures/dpkg-query-sample.txt   # 1c
      test_debversion.py # 1b
      test_inventory.py  # 1c
      test_cache.py      # 1d
      test_cli.py        # 1e
  pyproject.toml                       # (done) setuptools_scm packaging
  tox.ini, .stestr.conf                # (done)
  tools/flake8wrap.sh                  # (done)
  .pre-commit-config.yaml              # (done)
  .github/workflows/{unit-tests,release}.yml  # (done)
  RELEASE-SETUP.md                     # (done)
  ARCHITECTURE.md      # 1f
  AGENTS.md            # 1f
  ```
- **CLI framework: stdlib `argparse`.** (Skeleton done.) No
  third-party CLI dependency. The project's whole thesis is
  dependency/supply-chain hygiene, so we keep the runtime
  dependency surface minimal and justified. Sub-command
  structure (`divergulent <command>`) so Phases 2–4 can add
  commands.
- **Debian version module is named `debversion.py`**, NOT
  `version.py` — `setuptools_scm` writes the package version
  to `divergulent/_version.py`, and a `version.py` alongside
  it invites confusion. `debversion.py` is unambiguously the
  Debian-version-comparison wrapper.
- **Reading dpkg: shell out to `dpkg-query`** via
  `subprocess.run` with arguments passed as a **list** (never
  a shell string; no `shell=True`). Use the format string:
  ```
  dpkg-query -W -f '${db:Status-Abbrev}\t${Package}\t${Version}\t${source:Package}\t${source:Version}\t${Architecture}\n'
  ```
  `${source:Package}` / `${source:Version}` make dpkg resolve
  the `Source:` field (which is empty when it equals the
  binary, and is `name (version)` when the source version
  differs). Filter to installed packages using the status
  abbrev (installed rows start with `ii`). This avoids a
  hand-rolled parser of `Source: foo (1.2-3)`.
- **Debian version comparison: `python-debian`'s
  `debian.debian_support.Version`.** This is the one runtime
  dependency we add in Phase 1. Rationale: Debian version
  ordering (epochs, `~`, revisions) is subtle and
  reimplementing it is exactly the kind of correctness risk
  the project must not take; `python-debian` is
  Debian-maintained, reputable, pip-installable, and does
  precisely this. It is **wrapped** behind `divergulent/
  version.py` so the rest of the code depends on our
  interface, the library can be swapped, and the (likely
  missing) type stubs are contained to one module.
- **Data model: stdlib `@dataclass(frozen=True)`.** No
  Pydantic in Phase 1 — nothing here needs validation/serial
  -isation beyond a trivial `--json`. Revisit if a later
  phase needs schema validation.
- **Cache: a minimal filesystem cache** under
  `$XDG_CACHE_HOME/divergulent` (default `~/.cache/
  divergulent`), with an environment override. Entries are
  JSON `{stored_at, ttl, value}`. The clock is injectable
  (`clock: Callable[[], float] = time.time`) so TTL is
  testable. **Cache keys are hashed (sha256) to form the
  filename** — never interpolate a source-derived key into a
  path (path-traversal guard, per the PUSH template). There
  is no cache *consumer* until Phase 2; we build only the
  minimal get/set/TTL surface and accept that its shape may
  be refined when the first real source lands.
- **Source adapter interface: a `Source` `Protocol` (or ABC)
  in `divergulent/sources/base.py`** defining the contract
  Phases 2 (Repology) and 3 (sources.debian.org) implement.
  Keep it thin: a `name` and a `lookup(...)` returning a
  source-specific result. The HTTP client and politeness
  layer (User-Agent, timeouts, rate limiting, graceful
  degradation) are **deferred to Phase 2**, when there is an
  actual HTTP caller to shape them.
- **Testing: `stestr` + `testtools`, run via `tox`** (matches
  the occystrap/clingwrap house pattern; `.stestr.conf` points
  at `divergulent/tests`). Tests live inside the package
  (`divergulent.tests`). External effects (`dpkg-query`) are
  abstracted behind a single injectable call site and driven
  from recorded fixtures so tests are deterministic and
  host-independent. Run with `tox -epy3`.
- **Lint: `flake8 --max-line-length=120`** via
  `tools/flake8wrap.sh` (run with `tox -eflake8`; lints files
  changed since `HEAD~1`, matching occystrap). Explicitly
  **not `black`** — its forced double quotes conflict with the
  house single-quote style. The single-quote convention is a
  convention, not CI-enforced (as in the other repos). `mypy`
  is not in the CI gate today (occystrap/clingwrap do not gate
  on it); type hints are still used throughout and mypy can be
  added later if desired.
- **Python target: 3.11+** (bookworm ships 3.11, trixie 3.13).

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 1a | — | — | — | **Done (bootstrapped).** Project skeleton, `setuptools_scm` packaging, tox/stestr/flake8 tooling, importable package + smoke test, CI (`unit-tests.yml`) and the release pipeline (`release.yml` + `RELEASE-SETUP.md`). See Bootstrap status. |
| 1b | medium | sonnet | none | Implement `divergulent/debversion.py`: wrap `debian.debian_support.Version`. Expose a small typed interface — at minimum `parse(version_str) -> DebianVersion` (carrying `epoch`, `upstream_version`, `debian_revision`, and the raw string) and `compare(a, b) -> int` plus `is_older(a, b) -> bool`. Contain any `# type: ignore` for the untyped library to this module. Write `divergulent/tests/test_debversion.py` (testtools `TestCase`) covering tricky ordering: `1.0` < `1.1`; epochs (`1:1.0` > `2.0`); `~` pre-release (`1.0~rc1` < `1.0`); `+dfsg` and native-vs-non-native revisions; equal versions differing only by revision. Use a table of known pairs from `deb-version(7)`. |
| 1c | high | opus | none | Implement `divergulent/inventory.py`. Define `@dataclass(frozen=True) InstalledPackage` with `binary_name, binary_version, source_name, source_version, architecture` (versions as `DebianVersion` from 1b). Implement `list_installed(run=...)` that invokes `dpkg-query -W -f '...'` (the exact format string in Design decisions) via `subprocess.run` with an argument **list**, captures stdout, and parses tab-separated rows. Keep only rows whose status-abbrev indicates installed (`ii`). Handle: empty `${source:Package}` (falls back to binary name — though the virtual field should populate it), source version differing from binary version, and multi-arch duplicates. The `subprocess` call must be injectable (`run` parameter defaulting to a thin wrapper) so tests feed fixture output. This is high effort because the field semantics and edge cases (status filtering, source/binary version skew, arch) are where silent inventory bugs hide. Add `divergulent/tests/fixtures/dpkg-query-sample.txt` (hand-authored, covering: a normal package, one where source≠binary name, one where source version≠binary version, a not-installed `rc`/`un` row that must be excluded, and a multi-arch case) and `divergulent/tests/test_inventory.py` asserting the parsed result. |
| 1d | medium | sonnet | none | Implement `divergulent/cache.py` and `divergulent/sources/base.py`. `cache.py`: a `Cache(root: Path, clock=time.time)` with `get(namespace, key) -> dict | None` (respecting TTL) and `set(namespace, key, value, ttl_seconds)`, storing JSON `{stored_at, ttl, value}` in files named by `sha256(namespace + key)` under `root/` (path-traversal-safe). Provide `default_cache_dir()` resolving `$XDG_CACHE_HOME/divergulent` with env override. `base.py`: a thin `Source` `Protocol`/ABC with a `name: str` and `lookup(...)` contract documented for Phases 2–3 to implement; no HTTP yet. Write `divergulent/tests/test_cache.py` using a fake clock to prove TTL expiry, a tmp dir for `root`, and a test that a malicious key (e.g. containing `../`) cannot escape `root`. |
| 1e | low | sonnet | none | Wire the CLI. Make `divergulent inventory` call `inventory.list_installed()` and print a readable, aligned table (binary, binary version, source, source version, arch), sorted by source then binary name (replacing the current placeholder). Keep the `--json` flag emitting the same data as JSON. Update `cli.py` and add `divergulent/tests/test_cli.py` that runs `main(['inventory'])` against a mocked inventory and checks the output. |
| 1f | low | sonnet | none | Documentation. Create `ARCHITECTURE.md` (module map: cli, inventory, debversion, cache, sources/base; data flow; the planned-but-empty source/HTTP layer) and `AGENTS.md` (build/test commands — `tox -epy3` / `tox -eflake8`, the single-quote/120-col conventions, the dependency-minimalism policy and why `python-debian` is the one runtime dep, the offline-tests rule, and a pointer to `RELEASE-SETUP.md`). Update `README.md` with a short Usage section showing `divergulent inventory`. Keep all three consistent with what 1b–1e actually built. |

Steps run roughly in order. 1b precedes 1c (inventory stores
`DebianVersion`). 1d is independent of 1b/1c and may run in
parallel. 1e depends on 1c; 1f depends on everything. Commit
per step (or per logical group) per the master plan.

## Testing requirements

- Tests are `testtools` test cases under `divergulent/tests/`,
  run via `tox -epy3` (stestr).
- The full suite must pass **offline** and **without a real
  `dpkg`** — every external effect is mocked or fixture-fed.
- Version comparison is tested against a table of known-tricky
  pairs (epochs, `~`, `+dfsg`, native vs non-native).
- Inventory parsing is tested against a fixture covering
  source≠binary name, source≠binary version, excluded
  not-installed rows, and multi-arch.
- Cache tests prove TTL expiry (fake clock) and path-traversal
  safety.

## Success criteria for this phase

- `pre-commit run --all-files` passes (flake8 + flake8-quotes,
  mypy, pytest).
- `divergulent inventory` produces a correct ranked listing on
  a real Debian box (manual smoke check by the operator).
- Debian version ordering is correct per the test table; no
  naive string comparison anywhere.
- The one runtime dependency is `python-debian`, wrapped in
  `version.py`; the dev tooling is flake8/mypy/pytest.
- `README.md`, `ARCHITECTURE.md`, `AGENTS.md` reflect what was
  built.

## Open questions for this phase

- **stestr vs pytest** — resolved: **stestr** + testtools via
  tox, matching the operator's other repos.
- **CI** — resolved: CI is bootstrapped now (Sanity checks +
  release pipeline), not deferred.
- **Cache now vs Phase 2** — building the cache (1d) with no
  consumer risks premature shaping. Recommended to build the
  minimal surface now (per the master plan) but accept it may
  be refined in Phase 2; the operator may still prefer to
  defer 1d entirely until Repology needs it.
- **Branch model** — divergulent uses a single `main` branch
  and CI triggers on `main` (the occystrap example uses a
  `develop`/`master` split). Confirm `main`-only is the
  intent, or adopt the split.

## Out of scope (later phases)

- Any network access, HTTP client, or politeness layer
  (Phase 2).
- The Repology adapter and the staleness axis (Phase 2).
- `debian/patches` / DEP-3 divergence (Phase 3).
- Scoring and the ranked combined report (Phase 4).

## Back brief

Before executing, back brief the operator on your
understanding of this phase and how the intended work aligns
with it.
