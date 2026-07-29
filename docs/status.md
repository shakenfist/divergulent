# Status

Where the project is today, and the major pieces in flight. The plans
under [plans/](plans/index.md) carry the full history.

## What works now

Five commands work against real data: `divergulent inventory` (installed
packages → source packages), `divergulent staleness` (behind pure
upstream, via Repology), `divergulent divergence` (carried distro-only
patches, via sources.debian.org), `divergulent score` (both axes
combined into a ranked, whole-machine drift report), and `divergulent
show` (per-package patch detail with Debian bug references). The plan
lives in [plans/PLAN-initial.md](plans/PLAN-initial.md); see
[plans/index.md](plans/index.md) for the plan index, including
planned next steps (Debian BTS cross-referencing, and a patch-hygiene
assessment).

## The published precomputed cache

A **published precomputed cache** is in progress
([plans/PLAN-published-cache.md](plans/PLAN-published-cache.md)):
the slow half of a cold run (staleness + divergence) is a function of the
Debian release, not of your machine, so it can be computed once centrally
and downloaded as a small signed bundle. Two pieces exist now: a central
builder (`divergulent cache build`, run in CI) that sweeps the whole
archive into a ~0.73 MB gzipped bundle; client consumption — the
`--bundle PATH` flag and `cache pull` resolve covered packages from a
bundle (downloaded and stored locally, used automatically, with a live
fallback); trust — the bundle is Sigstore-signed in CI, verified on the
client (with the optional `verify` extra) and always spot-checked against
live origins; and publishing — a scheduled CI job builds, signs and
publishes the bundle daily to a stable URL, so `cache pull` just works.
Growing the published cache to a Debian 11/12/13/testing/unstable matrix
is tracked in the road-to-1.0 plan.

## The patch-classification pipeline

A **patch-classification** pipeline (curation-side, for whoever builds the
published cache — not something end users run) turns the carried-patch
residue into an explainable, signed classification, layered by cost:
free deterministic rules settle roughly a third of the ~60k distinct
carried patches; a claim-blind, adversarially-verified LLM tier triages
the substantive residue, prioritised by a security-risk gate; and a
Sigstore-signed human tier (a CLI and a local web UI recording
byte-identical verdicts against one append-only ledger) tops the
precedence. Cheap deterministic axes — reviewability (patch size), reach
(install base via Debian [popcon](https://popcon.debian.org/)), and an
external CVE/bug cross-reference against pinned Debian snapshots — keep
the expensive tiers pointed at the patches that matter most. The
pipeline is documented end to end in [workflow.md](workflow.md), and
every deterministic rule — what it matches, its precedence, and its
measured corpus hit rate — in
[deterministic-rules.md](deterministic-rules.md); the plan history is in
[plans/PLAN-patch-classification.md](plans/PLAN-patch-classification.md).
