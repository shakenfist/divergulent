# Phase 3 — Progress reporting for long-running commands

Part of [PLAN-faster-full-run.md](PLAN-faster-full-run.md).
Medium effort: small, but the terminal/CI behaviour and output
hygiene need care.

## Prompt

Read `divergulent/cli.py` — the whole-machine gather loops
(`_gather_score`, `_gather_divergence`, `_gather_classified` /
`_score_classified`, and the staleness command's loop) all
iterate over deduped source packages, and today emit nothing
until the run finishes. The summary already goes to **stderr**;
the table/JSON go to **stdout**.

## Objective

Show live progress during the long whole-machine commands
(`staleness`, `divergence`, `score`, and their `--classify`
paths) so the user can see it is working — without corrupting
machine-readable output or making CI/log files ugly.

## Design decisions

- **Progress goes to stderr**, never stdout (stdout is reserved
  for the table / `--json`). So progress never corrupts piped or
  `--json` output.
- **Terminal-aware.** When stderr is a TTY, animate a single
  updating line (carriage return) like `[ 42/318] glibc`. When
  it is **not** a TTY (CI, pipe, redirect to the sample-output
  artifact), do **not** emit carriage-return spam — instead
  print a plain line periodically (e.g. every N packages or at
  ~10% steps) so logs show progress without control characters.
- **Counts + current package.** Show processed/total and the
  current source package name; finish with a newline (TTY) so
  the summary and table that follow render cleanly.
- **`--quiet` suppresses progress** (on the long commands). The
  end-of-run summary stays (it is a result, not chatter).
  Progress is auto-on when stderr is a TTY and `--quiet` is not
  given.
- **Reusable + testable.** A small progress helper takes the
  total, an output stream, and an explicit `tty`/enabled flag
  (so tests inject a `StringIO` and assert behaviour without a
  real terminal). `show` (single package, fast) gets no
  progress.

## Steps

| Step | Effort | Model | Isolation | Brief for sub-agent |
|------|--------|-------|-----------|---------------------|
| 3a | medium | sonnet | none | Add a small progress reporter (e.g. `divergulent/progress.py` or a helper in `cli.py`): constructed with `total`, `stream` (default `sys.stderr`), and an `enabled`/`tty` flag (default `stream.isatty()`); a `step(label)` that increments and, when enabled, writes `\r[ n/total] label` on a TTY or a plain periodic line otherwise; a `finish()` that writes a trailing newline on a TTY. No output when disabled. Tests inject a `StringIO` with tty True/False and `--quiet`-style disabling: assert it counts, animates on TTY, emits periodic plain lines off-TTY, and is silent when disabled. |
| 3b | medium | sonnet | none | Wire the reporter into the whole-machine gather loops in `cli.py` (`staleness`, `divergence`, `score`, and the `--classify` paths), constructed with the deduped source count and stepping per source with the package name. Add a `--quiet` flag to those commands to disable it. Keep stdout (table/JSON) and the stderr summary unchanged. Ensure `--json` runs still emit only JSON on stdout (progress stays on stderr). Tests: a run with a non-TTY stderr does not corrupt stdout/JSON; `--quiet` produces no progress; progress reflects the number of sources. |

## Testing requirements

- Offline; existing source mocks reused.
- Progress helper: TTY animation, non-TTY periodic lines,
  disabled silence, correct counts.
- CLI: stdout/JSON uncorrupted by progress; `--quiet` honoured.

## Success criteria

- Running a long command interactively shows a live, updating
  progress line; in CI/pipes it shows occasional plain progress
  lines (no `\r` noise) and never corrupts stdout/JSON.
- `--quiet` disables progress; the summary still prints.
- `tox -epy3` / `tox -eflake8` pass; docs updated.

## Open questions

- **Off-TTY cadence** — every N packages vs ~10% steps
  (proposed: a sensible fixed N, e.g. 25).
- **Scope of `--quiet`** — progress only (proposed) vs also the
  summary.

## Out of scope

- Per-host rate tuning (phase 1) and Repology bulk (phase 2).
- A full progress bar library / ETA estimation.

## Back brief

Before executing, back brief the operator on your understanding
of this phase.
