# Phase 1 — Per-host rate-limit tuning

Part of [PLAN-faster-full-run.md](PLAN-faster-full-run.md).
Medium effort: small and well-understood, but it changes how
politely we hit a real service, so pick the interval carefully.

## Prompt

Read `divergulent/http.py` (the `HttpClient` per-host throttle:
`min_interval`, `_last_request` per host, `_throttle`) and
`divergulent/cli.py` (where `HttpClient` is constructed for the
`divergence` and `score` commands). Repology mandates ≤1 req/s;
sources.debian.org has no documented limit but is a single
service, so stay moderate.

## Objective

Let each host have its own request interval, so the
divergence-count half (sources.debian.org) can run a few
requests/second while Repology stays at the mandated ≤1 req/s —
cutting the divergence half of a cold run several-fold.

## Design decisions

- **Per-host interval overrides.** `HttpClient` keeps a default
  `min_interval` (1.0 s) plus an optional `host_intervals:
  dict[str, float]` mapping hosts to their interval; `_throttle`
  uses `host_intervals.get(host, min_interval)`.
- **sources.debian.org interval ~0.34 s (~3 req/s).** A named
  constant, applied where the client is built for the
  divergence/score commands. Repology stays at the 1.0 s
  default (mandated). Conservative and easy to tune.
- **No behaviour change otherwise** — caching, graceful
  degradation, and the per-host accounting are unchanged.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 1a | medium | sonnet | none | Add `host_intervals: dict[str, float] | None = None` to `HttpClient.__init__`; in `_throttle`, use `self._host_intervals.get(host, self._min_interval)` for the wait. Update `divergulent/tests/test_http.py`: a host with a smaller override waits less than the default; an unspecified host still uses the default; cross-host independence still holds. |
| 1b | low | sonnet | none | Define a constant (e.g. `SOURCES_DEBIAN_INTERVAL = 0.34` near the debian-patches source or in cli) and pass `host_intervals={'sources.debian.org': SOURCES_DEBIAN_INTERVAL}` wherever the `HttpClient` used by `divergence`/`score` is constructed in `cli.py`. Repology keeps the default. Update README/AGENTS to note Repology is ≤1 req/s (mandated) while sources.debian.org runs a bit faster. Add a CLI test (or extend one) confirming the constructed client carries the override, or assert via a small unit check. |

## Testing requirements

- Offline; `HttpClient` clock/sleep injected as today.
- Per-host override (faster host waits less), default fallback,
  and cross-host independence are covered.

## Success criteria

- sources.debian.org requests are spaced at the configured
  (faster) interval; Repology remains at ≤1 req/s.
- `tox -epy3` / `tox -eflake8` pass.

## Open questions

- The exact sources.debian.org interval (0.34 s proposed).

## Out of scope

- Repology bulk (phase 2); concurrency (Future work).

## Back brief

Before executing, back brief the operator on your understanding
of this phase.
