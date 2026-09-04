# Status

Where the project is today: what is finished and shipping, what is still
being built, and — for the classification pipeline, which is finished but
far from done — how much of the work remains. The plans under
[plans/](plans/index.md) carry the full history.

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

A **published precomputed cache** works today
([plans/PLAN-published-cache.md](plans/PLAN-published-cache.md), complete):
the slow half of a cold run (staleness + divergence) is a function of the
Debian release, not of your machine, so it is computed once centrally and
downloaded as a small signed bundle. Install divergulent, run `cache pull`
with no arguments, and runs are fast from then on — re-run it weekly, or
before a run you care about: divergence from a stored bundle is always
used (a fixed version's patches never change), but staleness only while
the bundle is under a week old, after which staleness is queried live
([usage.md](usage.md)). The pieces: a central builder (`divergulent cache
build`) that sweeps the whole archive into a
~0.73 MB gzipped bundle; client consumption — the `--bundle PATH` flag and
`cache pull` resolve covered packages from a bundle (downloaded and stored
locally, used automatically, with a live fallback); trust — the bundle is
Sigstore-signed in CI, verified on the client (with the optional `verify`
extra) and always spot-checked against live origins; and publishing — a
scheduled CI job builds, signs and publishes the bundle daily to a stable
URL. The cache currently covers **trixie only**; growing it to a Debian
11/12/13/testing/unstable matrix is the remaining work, tracked in the
road-to-1.0 plan.

## The patch-classification pipeline

The **patch-classification** pipeline (curation-side, for whoever builds
the published cache — not something end users run) turns the
carried-patch residue into an explainable, signed classification, layered
by cost: free deterministic rules settle roughly a third of the ~60k
distinct carried patches; a claim-blind, adversarially-verified LLM tier
triages the substantive residue, prioritised by a security-risk gate; and
a Sigstore-signed human tier (a CLI and a local web UI recording
byte-identical verdicts against one append-only ledger) tops the
precedence. Cheap deterministic axes — reviewability (patch size), reach
(install base via Debian [popcon](https://popcon.debian.org/)), and an
external CVE/bug cross-reference against pinned Debian snapshots — keep
the expensive tiers pointed at the patches that matter most.

**The architecture is complete and shipping.** All of the above exists,
runs against the real trixie corpus, and publishes: CI rebuilds the
signed classification bundle from the committed ledger export every day
at 04:31 UTC, and `divergulent cache pull-classification` fetches it.
What continues is not construction but *review* — and it is a grind. Of
the ~60,640 distinct carried patches, the deterministic tier settles the
structurally-determined ones (29.2% at the first corpus-wide
measurement, plus a further ~15% once the `test-only` rule landed) and
leaves roughly 43k fingerprints of substantive residue whose category is
a question about intent. That residue is overwhelmingly still
unreviewed. So the honest claim is that Debian's carried patches have
been made *classifiable*, not that they are classified: the bundle
carries only settled verdicts, and it **grows** as review proceeds, so
clients re-pull to see more of their patches explained.

The pipeline is documented end to end in [workflow.md](workflow.md); the
recurring operator loop, its cadence and its costs are in
[classification-runbook.md](classification-runbook.md); every
deterministic rule — what it matches, its precedence, and its measured
corpus hit rate — is in
[deterministic-rules.md](deterministic-rules.md); and the plan history is in
[plans/PLAN-patch-classification.md](plans/PLAN-patch-classification.md).
