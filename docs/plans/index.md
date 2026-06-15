# Plan index

This directory holds divergulent's planning documents. Each master plan
is created from [PLAN-TEMPLATE.md](../../PLAN-TEMPLATE.md); detailed
per-phase plans live alongside their master plan and are linked from the
master plan's Execution table.

## Plan status

| Plan | Phases | Status | Description |
|------|--------|--------|-------------|
| [PLAN-initial.md](PLAN-initial.md) | 1. Skeleton & dpkg inventory ✓ · 2. Repology staleness ✓ · 3. debian/patches divergence ✓ · 4. Scoring & ranked report ✓ · 5. Per-package detail (`show`) ✓ | Phases 1–5 complete | A local CLI that reports staleness and carried-patch divergence for installed packages, ranked, with a whole-machine summary and a per-package detail view. |
| [PLAN-full-machine-run.md](PLAN-full-machine-run.md) | 1. Tier 1 polite default overview · 2. Tier 2 `--classify` via apt · 3. CI full-run sample output | Not started | Make a full-machine run polite and viable via tiered patch data (cheap count overview, opt-in apt-source classification, per-package deep dive), proven by a CI full score of a Debian 13 runner. |
