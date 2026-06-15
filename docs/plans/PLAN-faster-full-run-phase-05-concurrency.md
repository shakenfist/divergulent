# Phase 5 — Bounded-concurrency fetching

Part of [PLAN-faster-full-run.md](PLAN-faster-full-run.md).
High effort: touches the shared HTTP client and cache for thread
safety and adds a concurrent gather. This is the "Future work"
item the original plan deferred, now needed to reach the cold-run
target.

**Status: complete.** `HttpClient` has a thread-safe ticket
throttle; `Cache.set` writes via a unique temp file;
`cli._concurrent_map` gathers `divergence`/`score` over a thread
pool sized by `--workers` (default 8); sources.debian.org's
interval is 0, bounded by concurrency. Tests cover throttle
spacing under threads, concurrent cache writes, and order/serial
behaviour of the gather. Suite green.

## Why

After phase 4, a cold `score` is bounded by two serial halves:
Repology (~570 req at the mandated 1 req/s ≈ 9.5 min) and
sources.debian.org (~570 req at ~0.6 s ≈ 6 min). sources.debian.org
has **no documented rate limit**, so its half is needlessly serial.
Running it concurrently both shrinks it and lets it hide under the
Repology wait, taking a cold `score` to roughly the Repology floor
(~10 min) and a `divergence`-only run to well under a minute.

Repology stays serial: its 1 req/s is mandated and is the floor we
cannot beat without being a bad citizen.

## Design

- **Thread-safe throttle (ticket model).** `HttpClient._throttle`
  takes a lock, computes this host's next allowed start as
  `max(now, last + interval)`, records it, releases the lock, then
  sleeps to that time *outside* the lock. Per-host spacing is
  preserved across threads (Repology still fires 1/s in aggregate);
  different hosts and a zero-interval host overlap freely. Existing
  single-threaded timing is unchanged.
- **Thread-safe cache writes.** `Cache.set` writes via a unique
  temp file (`tempfile.mkstemp` in the cache dir) before
  `os.replace`, so concurrent writers cannot clobber a shared
  `.tmp`. Distinct keys already hash to distinct files.
- **Concurrent gather.** A `_concurrent_map(items, fn, workers,
  progress)` helper runs `fn` over deduped sources on a
  `ThreadPoolExecutor`, preserving input order and stepping the
  progress reporter as each completes. Used by the `staleness`,
  `divergence`, and `score` gather loops.
- **Politeness knob.** sources.debian.org's per-request interval
  drops to 0 (concurrency, not spacing, now bounds it); a
  `DEFAULT_WORKERS` constant (proposed 8) caps concurrent
  connections and is the single politeness control. A `--workers N`
  flag overrides it. Repology requests still self-limit to 1/s via
  the throttle regardless of worker count.

## Steps

| Step | Brief |
|------|-------|
| 5a | Make `HttpClient._throttle` thread-safe via the ticket model + a `threading.Lock`; add tests that single-threaded timing is unchanged and that concurrent same-host calls are spaced while different hosts overlap. |
| 5b | Make `Cache.set` use a unique temp file; add a concurrent-write test (many threads, distinct + identical keys, no corruption). |
| 5c | Add `_concurrent_map` and route the `staleness`/`divergence`/`score` gathers through it with ordered results and live progress; add `--workers`; set sources.debian.org interval to 0 with `DEFAULT_WORKERS` the bound. Tests: order preserved, progress counts, `--workers 1` == serial. |
| 5d | Update `ARCHITECTURE.md` / `README.md` / `AGENTS.md` for concurrency and the `--workers` knob; refresh CI sample timing expectations. |

## Success criteria

- A cold `divergence` over a real machine completes in well under a
  minute; a cold `score` lands near the ~10 min Repology floor
  (down from 34 min), with no `--limit`.
- Repology is still queried at <=1 req/s in aggregate even under
  concurrency; sources.debian.org concurrency is bounded by
  `--workers` (default 8).
- Output (table/JSON/summary) and staleness/divergence meaning are
  unchanged; `--json` stays uncorrupted.
- `tox -epy3` / `tox -eflake8` / `pre-commit` pass; tests offline.

## Out of scope

- Concurrent Repology fetching (forbidden by its rate limit).
- Async/await rewrite — a thread pool is sufficient and keeps the
  stdlib-only, low-dependency posture.
