# Phase 2 — Repology adapter & staleness axis

Part of [PLAN-initial.md](PLAN-initial.md). Plan this phase at
**high effort**: it introduces the first network-backed source,
the politeness layer the whole project depends on, and the
first axis of drift — and the version-comparison and
honest-uncertainty details are where correctness lives.

**Status: complete.** All steps (2a HTTP/politeness client, 2b
Repology adapter + staleness model, 2c `staleness` CLI, 2d
docs) are implemented and committed; `divergulent staleness`
works and the suite passes via `tox -epy3`.

## Prompt

Phase 1 is merged; explore the existing code before adding to
it: `divergulent/cache.py` (the on-disk TTL cache this phase
fetches through), `divergulent/sources/base.py` (the `Source`
protocol to implement), `divergulent/debversion.py` (the only
correct way to compare versions), and `divergulent/inventory.py`
(the installed packages to score). Ground the Repology details
in its API rather than guessing:

- **Per-project API:** `GET /api/v1/project/<name>` returns a
  JSON array of package entries, each with `repo`, `version`
  (sanitised, upstream-only — epoch/revision stripped),
  `origversion` (raw), `status`, `visiblename`, `srcname`,
  `binname`. `status` is one of: `newest`, `devel`, `unique`,
  `outdated`, `legacy`, `rolling`, `noscheme`, `incorrect`,
  `untrusted`, `ignored`.
- **Name resolution:** Repology's project name is not always
  the Debian source name. Resolve via
  `GET /tools/project-by?repo=debian_unstable&name_type=srcname&target_page=api_v1_project&name=<srcname>`,
  which 302-redirects to the project's API JSON. This is far
  more robust than assuming `project == srcname`.
- **Politeness (mandatory):** "no more than 1 request per
  second"; bulk clients (>1000 requests/day) MUST send a
  custom User-Agent containing a link to their source repo
  with an accessible issue tracker, or risk being blocked.

Flag uncertainty explicitly: where Repology cannot resolve a
package, or has only heuristic name matches, that must surface
as *unknown*, never as confirmed staleness.

## Objective

Add the staleness axis: for each installed package, determine
whether its version is behind pure upstream, using Repology as
the upstream-version source. Deliver a `divergulent staleness`
command that lists the packages that are behind, worst first,
with installed vs newest versions — and the reusable HTTP/
politeness layer that Phase 3 will also use.

End state: `divergulent staleness` produces a ranked report on
a real Debian box; the test suite passes offline with all HTTP
mocked; Repology is queried politely (identifying User-Agent,
≤1 req/s, cached, degrades gracefully).

## Design decisions

- **HTTP client: stdlib `urllib.request`**, not `requests` or
  `httpx`. Consistent with the dependency-minimalism principle
  (and the argparse decision) — a divergence auditor keeps its
  own dependency surface minimal. `urllib` handles GET, custom
  headers, timeouts, and follows the `project-by` redirect.
  This is a deliberate deviation from the occystrap pattern
  (which uses httpx); record it.
- **New module `divergulent/http.py`** — an `HttpClient` that
  wraps fetch + politeness + caching in one place:
  - **User-Agent:** `divergulent/<version>
    (+https://github.com/shakenfist/divergulent)` — repo link
    with issue tracker, per Repology's requirement.
  - **Timeout** on every request (default ~10s).
  - **Rate limiting:** enforce ≥1s between *network* requests
    (cache hits are free). Implemented with an injectable
    clock + sleep so it is testable without real time.
  - **Graceful degradation:** network error, timeout, non-200,
    or unparseable JSON → return `None` (caller treats as
    *unknown*), never raise out of the client.
  - **Caching:** every fetch goes through the Phase 1 `Cache`
    (default TTL ~24h) keyed by the request URL; a cache hit
    skips both the network and the rate limiter.
  - **Injectables for tests:** `urlopen`, `clock`, `sleep`.
  - **Host pinning:** only ever construct URLs for
    `repology.org`; the `project-by` redirect stays on-host.
    Do not follow redirects to other hosts.
- **URL building:** the source package name goes into a query
  parameter and MUST be URL-encoded (`urllib.parse.urlencode`
  / `quote`). Never string-concatenate it in raw.
- **Repology adapter `divergulent/sources/repology.py`**
  implements `Source`:
  - `lookup(source_package)` resolves srcname→project via
    `project-by` (resolver repo `debian_unstable`) and returns
    the parsed project entries, or `None`.
  - **Newest version selection:** prefer the entry whose
    `status == 'newest'` (latest *stable*) as the upstream
    bar; do NOT treat `devel`/`rolling` as the bar (avoid
    flagging everyone behind a pre-release). Ignore entries
    with status in `{ignored, incorrect, untrusted, noscheme}`.
    If no `newest` entry exists, fall back to the max valid
    `version` via `debversion`.
  - **Comparison (correctness landmine):** Repology `version`
    is upstream-only, while the installed Debian version
    carries epoch/revision. Compare the **upstream portion**
    of the installed version
    (`debversion.parse(...).upstream_version`) against the
    Repology newest version, using `debversion`. Comparing the
    full Debian version against an upstream-only version would
    produce false "behind" results.
- **Staleness model:** a `StalenessState` enum
  (`CURRENT` / `BEHIND` / `UNKNOWN`) and a `StalenessResult`
  dataclass (source package, installed version, newest version
  or `None`, state, resolved Repology project or `None`).
  `UNKNOWN` covers: project-by 404, no usable upstream
  version, or the source being absent from Repology — never
  reported as `BEHIND`.
- **Dedup by source package:** the inventory yields binary
  packages, many sharing one source. Collapse to unique source
  packages before querying Repology — one query per source,
  both for correctness and to minimise API calls.
- **CLI:** add `divergulent staleness` (leave `inventory` as
  is). Default output: only packages that are `BEHIND`, worst
  first (largest version gap, then name), as an aligned table
  (source, installed upstream, newest, state). `--json` mirrors
  it. `--all` also shows `CURRENT`/`UNKNOWN`. Progress/counts
  go to stderr so stdout stays clean for `--json`.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 2a | medium | sonnet | none | Implement `divergulent/http.py`: an `HttpClient(cache, *, user_agent=DEFAULT_UA, timeout=10.0, min_interval=1.0, urlopen=urllib.request.urlopen, clock=time.monotonic, sleep=time.sleep)` with `get_json(url, *, cache_namespace, cache_key, ttl_seconds)`. Behaviour: return cached value if present (no network, no rate-limit); otherwise enforce ≥`min_interval` since the last network call via `clock`/`sleep`, issue a GET with the `User-Agent` header and `timeout`, parse JSON, store it in the cache, and return it. On any `URLError`/`HTTPError`/timeout/JSON error, return `None` (never raise). DEFAULT_UA = `divergulent/<__version__> (+https://github.com/shakenfist/divergulent)`. Tests (offline, injecting `urlopen`/`clock`/`sleep`): cache hit avoids urlopen; rate limiter sleeps the right amount between two network calls; the User-Agent and timeout are passed; HTTP/URL errors and bad JSON yield `None`. |
| 2b | high | opus | none | Implement `divergulent/sources/repology.py`. Define `StalenessState` (Enum: CURRENT/BEHIND/UNKNOWN) and `@dataclass(frozen=True) StalenessResult(source_package, installed_version: DebianVersion, newest_version: str | None, state: StalenessState, project: str | None)`. `RepologySource(http_client, resolver_repo='debian_unstable')` implements `Source` (`name = 'repology'`). `lookup(source_package)` builds the URL-encoded `project-by` URL (`name_type=srcname`, `target_page=api_v1_project`), fetches via the http client (cache namespace `repology`, key = source package + resolver repo, TTL ~24h), and returns the parsed entries or `None`. Add `staleness(source_package, installed_version: DebianVersion) -> StalenessResult`: select the newest *stable* upstream version (prefer `status == 'newest'`, ignore `{ignored,incorrect,untrusted,noscheme}`, else max valid `version` via debversion), compare it against `installed_version.upstream_version` with debversion, and return CURRENT/BEHIND, or UNKNOWN when the project does not resolve or no usable version is found. This is high effort: the name resolution, the stable-vs-devel newest selection, the upstream-vs-upstream comparison, and the honest UNKNOWN handling are all correctness-critical. Tests use recorded Repology project JSON fixtures (in `divergulent/tests/fixtures/`) covering: behind, current, a `devel` newer than `newest` (must not count as behind), an unresolved package (UNKNOWN), and an epoch/revision installed version compared correctly against an upstream-only Repology version. |
| 2c | medium | sonnet | none | Add the `staleness` CLI command and aggregation. In `cli.py` add a `staleness` sub-parser (`--json`, `--all`). Build the inventory, dedup to unique source packages (source name + source version), construct a `RepologySource` over an `HttpClient` over the default cache, call `staleness()` per source, and render: by default only `BEHIND` rows, sorted worst-first (version gap then name), as an aligned table (source, installed, newest, state); `--json` mirrors it; `--all` includes CURRENT/UNKNOWN. Counts/progress to stderr. Tests: mock the source (and/or http client) so no network is touched; assert dedup (one query per source across multiple binaries), default-vs-`--all` filtering, sort order, and both output modes. |
| 2d | low | sonnet | none | Documentation. Update `ARCHITECTURE.md` (add `http.py` and the Repology source; note the staleness data flow inventory→dedup→Repology→ranked report), `AGENTS.md` (the politeness rules — User-Agent, ≤1 req/s, cache TTL, graceful degradation — and that HTTP uses stdlib urllib), and `README.md` (a `divergulent staleness` usage example and a note that staleness is heuristic: it relies on Repology name matching and reports UNKNOWN when it cannot resolve). |

Steps run in order: 2b depends on 2a, 2c on 2b, 2d on all.
Commit per step per the master plan.

## Testing requirements

- The whole suite stays **offline**: all HTTP is mocked
  (inject `urlopen` into `HttpClient`, or mock the source in
  CLI tests). No test hits repology.org.
- Repology parsing is tested against recorded fixture JSON
  covering behind / current / devel-newer-than-stable /
  unresolved / epoch-and-revision comparison.
- The rate limiter and cache-hit-skips-network behaviour are
  tested with injected clock/sleep.
- The CLI dedups to one query per source package.

## Success criteria for this phase

- `divergulent staleness` returns a correct ranked report on a
  real Debian machine (operator smoke check).
- Versions compared upstream-to-upstream via `debversion`;
  epoch/revision never causes a false "behind".
- Repology is queried politely: identifying User-Agent,
  ≥1s spacing, 24h cache, graceful degradation to UNKNOWN.
- Unresolved/heuristic cases are reported as UNKNOWN, not
  BEHIND — the tool does not cry wolf.
- `tox -epy3` and `tox -eflake8` pass; docs updated.

## Open questions for this phase

- **Resolver repo / suite.** Defaulting to `debian_unstable`
  for srcname→project resolution (broadest coverage, source
  names stable across suites). Should this be configurable
  (`--suite`/`--repo`) now, or deferred?
- **Newest = stable only?** Preferring `status == 'newest'`
  (stable) over `devel`. Confirm we do not want to flag
  packages as behind a development release by default (a
  `--include-devel` could come later).
- **Cache TTL.** 24h proposed; confirm acceptable freshness
  vs politeness trade-off.

## Out of scope (later phases)

- The divergence axis — `debian/patches` / DEP-3 (Phase 3).
- Combining staleness and divergence into one score and a
  whole-machine summary (Phase 4).
- A server/aggregator, UDD/DEHS/Wikidata sources, and bulk
  `/api/v1/projects/` range queries (Future work).

## Back brief

Before executing, back brief the operator on your
understanding of this phase and how the intended work aligns
with it.
