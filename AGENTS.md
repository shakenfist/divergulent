# Agent and contributor notes

## Build, test, lint

- Run the unit tests: `tox -epy3` (stestr + testtools).
- Run lint: `tox -eflake8` (flake8, max line length 120; checks the
  current change).
- Tests must pass **offline** — no calls to live external services.
  Mock external effects or use recorded fixtures
  (`divergulent/tests/fixtures/`). The `dpkg-query` call in
  `inventory.py` is injectable for exactly this reason.

## Conventions

- Python ≥ 3.11. Single quotes for strings, double quotes for
  docstrings, lines wrapped at 120 characters, no trailing whitespace.
- Compare versions only via `divergulent.debversion` (Debian ordering:
  epochs, `~`, revisions) — never with naive string comparison.
- The installed-package inventory is sensitive; do not transmit it
  off-box without an explicit, opt-in, documented path.

## Dependencies

divergulent audits dependency/patch divergence, so it keeps its own
footprint small. The sole runtime dependency is `python-debian` (Debian
version comparison), wrapped in `debversion.py`. Add new runtime
dependencies only with clear, case-by-case justification, and prefer the
standard library (e.g. `argparse` over `click`).

## Releases

Releases are tag-driven (`v*`): Sigstore-signed tags plus PyPI trusted
publishing, running on self-hosted runners. The package version is
derived from the git tag by `setuptools_scm`. One-time configuration is
documented in [RELEASE-SETUP.md](RELEASE-SETUP.md).

## Planning workflow

Plans live in `docs/plans/` — a master plan plus one file per phase,
created from [PLAN-TEMPLATE.md](PLAN-TEMPLATE.md). Pre-push checks are in
[PUSH-TEMPLATE.md](PUSH-TEMPLATE.md).
