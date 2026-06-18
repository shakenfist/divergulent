# Phase 3 — `cache pull`: download, validate, store, auto-use

Part of [PLAN-published-cache.md](PLAN-published-cache.md).
Medium effort: a new download path, on-disk storage, automatic
discovery, and the freshness contract deferred from phase 2.

**Status: implemented.** `HttpClient.get_bytes` and `bundle.loads`/
`bundle.stored_path` are added; `divergulent cache pull [--cache-url]`
downloads, validates (parse + schema + release) and stores the bytes
verbatim; `cli._select_bundle` auto-discovers the stored bundle when
`--bundle` is absent, and `cli._resolve_sources` applies the freshness
contract (divergence always, staleness within
`BUNDLE_STALENESS_TTL_SECONDS` via the injectable `cli._utc_now`, else
live). Tests are offline (fake `get_bytes`, pinned clock, temp cache dir);
the phase-2 bundle tests now pin the clock too. Suite green;
`pre-commit run --all-files` clean.

## Prompt

Read, and reuse rather than reinvent:

- `divergulent/bundle.py` — `load(path)`; phase 3 adds a bytes loader so
  a download can be validated before it is stored.
- `divergulent/http.py` — `HttpClient` (throttle, size cap, User-Agent,
  graceful `None`). Its private `_fetch` already returns raw bytes; phase
  3 exposes a public raw-bytes fetch for the (gzipped, binary) bundle,
  which must **not** go through the JSON/text value cache.
- `divergulent/cache.py` — `default_cache_dir()` for where to store the
  bundle, and the unique-temp-file + `os.replace` pattern for an atomic
  write.
- `divergulent/cli.py` — `_usable_bundle` (schema + release validation),
  `_resolve_sources` (where the stored bundle is auto-discovered),
  `_detect_release`, `_utc_now_iso`, and the `cache` subcommand group
  (`build` today, `pull` here).

## Objective

`divergulent cache pull [--cache-url URL]` downloads the release's bundle,
validates it (recognised schema, matching release), and stores it under
the cache directory. The whole-machine commands then use that stored
bundle automatically (no `--bundle` needed), with `--bundle` remaining an
explicit override. A **freshness contract** governs use: divergence is
immutable and always usable; staleness has a TTL beyond which the client
queries Repology live rather than trust an aged map.

Phase 3 still has no publisher (phase 5), so the default URL points at the
intended GitHub Releases `latest` asset and `--cache-url` lets a human
test against a manually-hosted copy of the phase-1 artifact. Signature
verification is phase 4.

## Design decisions

- **Raw-bytes fetch.** Add `HttpClient.get_bytes(url) -> bytes | None`
  that returns `self._fetch(url)` — throttle, size cap and graceful
  failure, but no value cache (the bundle is stored as a file, not cached
  as a parsed value). The store keeps the **downloaded bytes verbatim**
  (not a re-serialisation), so a future signature (phase 4) verifies
  against exactly what was published.
- **Validate before store.** Add `bundle.loads(data: bytes) -> Bundle`
  (gunzip + JSON + `from_dict`). `cache pull` downloads, `loads()` to
  confirm it parses, checks `schema`/`cache_schema` are recognised and
  `release` matches the system, and only then writes the bytes
  atomically (unique temp file + `os.replace`). A download that fails to
  parse, is unrecognised, or is for another release is refused with a
  clear message and nothing is stored.
- **Storage path.** `cache-<release>.json.gz` under `default_cache_dir()`,
  keyed on the Debian release (the bundle's correctness partition). A
  helper resolves it so the builder/pull/consumer agree.
- **Default URL (provisional).** `--cache-url` overrides; otherwise
  `https://github.com/shakenfist/divergulent/releases/latest/download/cache-<release>.json.gz`.
  This is the client-defined canonical asset name that **phase 5 must
  publish under**; until phase 5 exists, use `--cache-url` to point at a
  hand-uploaded bundle. Make the host configurable so mirrors work.
- **Automatic discovery.** In `_resolve_sources`, when `--bundle` is not
  given, fall back to the stored `cache-<release>.json.gz` if it exists —
  silently (an absent store is the normal pre-pull state, not a warning).
  An explicit `--bundle` still warns when unusable.
- **Freshness contract (the deferred open question).**
  - **Divergence** from the bundle is always used (a fixed `(source,
    version)` patch set is immutable).
  - **Staleness** from the bundle is used only when the bundle's
    `generated_at` is within `BUNDLE_STALENESS_TTL_SECONDS`; past that the
    command queries Repology live for staleness (a printed notice
    explains why). This is safe and conservative: a stale "newest" can
    only *under*-report BEHIND (newest versions only increase), never cry
    wolf — so the TTL is generous (default 7 days), and beyond it we go
    live to catch newly-behind packages rather than silently miss them.
  - The freshness clock is injectable (`cli._utc_now`) so tests are
    deterministic; `_utc_now_iso` is refactored onto it.

## Steps

| Step | Effort | Model | Brief for sub-agent |
|------|--------|-------|---------------------|
| 3a | low | sonnet | Add `HttpClient.get_bytes(url)` (raw `_fetch`, no value cache) and `bundle.loads(data: bytes) -> Bundle`. Tests: bytes round-trip through `write`→read→`loads`; `get_bytes` returns the body and `None` on failure, and goes through the throttle. |
| 3b | medium | sonnet | Add `divergulent cache pull [--cache-url URL]`: resolve the URL (override or default-from-release), `get_bytes`, validate with `loads()` + schema/release checks, and store the bytes atomically as `cache-<release>.json.gz` under the cache dir (a `stored_bundle_path(cache_dir, release)` helper). Refuse (no store) on download/parse/schema/release failure with a clear message; print the stored path and size on success. Tests with a fake HTTP returning a fixture bundle's bytes: success stores the exact bytes; wrong-release and unparseable downloads store nothing and error. |
| 3c | medium | sonnet | Auto-discovery + freshness. Add `cli._utc_now` (refactor `_utc_now_iso` onto it) and `BUNDLE_STALENESS_TTL_SECONDS`. In `_resolve_sources`: use `--bundle` if given else the stored path if it exists; build divergence bundle-backed always, staleness bundle-backed only when `generated_at` is within the TTL (else live, with a notice). Update the phase-2 CLI bundle tests to patch `cli._utc_now` so freshness is deterministic. Tests: fresh bundle → bundle-backed staleness (no network); aged bundle → live staleness + bundle divergence; stored bundle auto-used without `--bundle`; absent store → silent live. |
| 3d | low | sonnet | Update `README.md` (the `pull`/`--cache-url` workflow and auto-use), `ARCHITECTURE.md` (the download/store/freshness flow), `AGENTS.md` (raw-bytes fetch, storage path, freshness contract), and the phase/master/index plan statuses. |

## Testing requirements

- Offline: fake HTTP returns a fixture bundle's bytes; the freshness
  clock is injected; storage uses a temp cache dir
  (`DIVERGULENT_CACHE_DIR`). No live network.
- A pulled-then-used run resolves covered packages from the stored bundle
  with no network (as in phase 2), and an aged bundle still serves
  divergence while staleness goes live.
- `pre-commit run --all-files` green.

## Success criteria

- `cache pull` downloads, validates and stores the release bundle; a
  later `score`/`staleness`/`divergence` uses it automatically with no
  `--bundle` and no network for covered packages.
- A download that is unparseable, unrecognised, or for another release is
  refused and nothing is stored; behaviour stays fully live.
- Divergence is always served from a stored bundle; staleness is served
  only while fresh, else live with a clear notice.
- The stored file is the downloaded bytes verbatim (ready for phase-4
  signature verification).
- The machine's inventory never leaves the host (only the bundle is
  fetched; per-miss live lookups behave as today).

## Out of scope (later phases)

- Signature creation and verification / spot-verify (phase 4).
- Scheduled publishing to GitHub Releases (phase 5) — phase 3 consumes a
  URL; it does not produce one.
- Multiple-mirror failover (Future work).

## Back brief

Before executing, back brief the operator on your understanding of this
phase.
