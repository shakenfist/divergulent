# Plan index

This directory holds divergulent's planning documents. Each master plan
is created from [PLAN-TEMPLATE.md](../../PLAN-TEMPLATE.md); detailed
per-phase plans live alongside their master plan and are linked from the
master plan's Execution table.

## Plan status

| Plan | Phases | Status | Description |
|------|--------|--------|-------------|
| [PLAN-initial.md](PLAN-initial.md) | 1. Skeleton & dpkg inventory ✓ · 2. Repology staleness ✓ · 3. debian/patches divergence ✓ · 4. Scoring & ranked report ✓ · 5. Per-package detail (`show`) ✓ | Phases 1–5 complete | A local CLI that reports staleness and carried-patch divergence for installed packages, ranked, with a whole-machine summary and a per-package detail view. |
| [PLAN-full-machine-run.md](PLAN-full-machine-run.md) | 1. Tier 1 polite default overview ✓ · 2. Tier 2 `--classify` via apt ✓ · 3. CI full-run sample output ✓ | Complete | Make a full-machine run polite and viable via tiered patch data (cheap count overview, opt-in apt-source classification, per-package deep dive), proven by a CI full score of a Debian 13 runner. |
| [PLAN-faster-full-run.md](PLAN-faster-full-run.md) | 1. Per-host rate-limit tuning ✓ · 2. Repology bulk staleness ↩ reverted · 3. Progress reporting ✓ · 4. Revert bulk to per-package ✓ · 5. Bounded-concurrency fetching ✓ | Complete | Make a cold full-machine run fast and legible. Phase 2's whole-archive Repology sweep proved a cold-run regression (34 min); phases 4–5 revert it to per-package and add bounded concurrency on the unthrottled sources.debian.org half. A published precomputed cache (Future work) is the planned path to a seconds-long cold run. |
| [PLAN-published-cache.md](PLAN-published-cache.md) | 1. Central builder + measure ⋯ · 2. Bundle-backed sources + fallback ⋯ · 3. `cache pull` ⋯ · 4. Signing + verify ⋯ · 5. Publish to GitHub Releases ⋯ | Not started | Compute staleness + divergence for the whole Debian archive once, centrally, and publish a small signed bundle that clients download whole (private) and match locally — turning a cold run into seconds. Builder first to confirm size/timing before delivery. |
