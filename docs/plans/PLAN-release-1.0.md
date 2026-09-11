# Road to a 1.0 release

A living checklist of what divergulent must do before it earns a `1.0`
tag. This is a placeholder/tracking plan: each workstream graduates to its
own detailed `PLAN-…` (and per-phase files) when we pick it up. Created
from [PLAN-TEMPLATE.md](https://github.com/shakenfist/divergulent/blob/develop/PLAN-TEMPLATE.md) in spirit, but kept as a
checklist rather than a single linear phase list.

## What 1.0 means for divergulent

divergulent's promise is **honest, private drift measurement**: it tells
you how far a Debian machine has drifted from pure upstream (staleness)
and how many Debian-only patches it carries (divergence), never crying
wolf, without the machine's inventory leaving the host. A 1.0 is the point
where that promise holds **by default, for ordinary Debian users, with no
hand-holding** — fast, private, trustworthy, and documented.

## Must-do workstreams

### 1. Finish the published cache (publish — phase 5)

The default experience must be: install, `cache pull`, and runs are fast
and private. Tracked in
[PLAN-published-cache-phase-05-publish.md](PLAN-published-cache-phase-05-publish.md).
Blocking, because phases 1–4 are inert until something is published at the
default URL.

**Done (2026-09-04).** The published-cache plan is complete; a `cache pull`
with no arguments downloads, signature-verifies and spot-checks the daily
bundle.

- [x] Scheduled daily (incremental) + weekly (full `--refresh`) builds.
- [x] Signed bundle published to a stable URL; client constants reconciled.

**Merged:** reconstructed rather than recorded at the time. `52cb034e`
(#16), the merge that landed the scheduled publish and created this
plan file, plus the publish-path fixes that followed it — `01ee0ef4`
(#17), `0e55a386` (#18), `1323f00a` (#19) and `a87c22d8` (#21) — and
`5288f4a2` (#88), which ticked the boxes above. The code itself belongs to
[PLAN-published-cache.md](PLAN-published-cache.md)'s phase 5, which that
plan carries as `Complete`; this workstream tracked it rather than
landing it. They are in workstream 9's audit range all the same, and
deliberately so: this plan records them, and the published-cache plan
carries no push audit phase and — being `Complete` without one — is not
reopened to acquire one, so nothing else would ever read them. Its
earlier phases are a different matter; workstream 9 says where the
range starts and what that leaves outside it.

### 2. Multi-release build matrix (Debian 11, 12, 13, testing, unstable)

Today the builder targets one release (trixie). A 1.0 should serve the
**releases people actually run**: bullseye (11), bookworm (12), trixie
(13), plus **testing** and **unstable** for the developers who track them.
The bundle data is release-partitioned and architecture-independent, so
one bundle per release serves every architecture of that release — the
client already keys storage, the URL, and validation on the release.

The interesting part: **we have no CI runner images for testing and
unstable** (and maintaining self-hosted runners per release is a poor use
of effort). The challenge we're choosing to take on:

- [ ] **Build inside official Debian containers** on a single Docker-capable
      runner: `debian:bullseye`, `debian:bookworm`, `debian:trixie`,
      `debian:testing`, `debian:sid`. Because divergence is
      arch-independent, one amd64 container per release suffices — no
      per-release runners, no per-arch builds. `tools/build-cache.sh`
      already enables deb-src and builds in a venv, so it should run
      largely as-is inside each container.
- [ ] **Per-release Repology repo** for staleness: confirm the mapping
      (`debian_11`, `debian_12`, `debian_13`, `debian_unstable`, and the
      right id for testing) — `builder.build_staleness_map(repo=…)` and the
      bundle's `repology_repo` field already parameterise this.
- [ ] **Release detection for testing/unstable.** `/etc/os-release`
      `VERSION_CODENAME` is reliable for stable releases but slippery for
      testing (often reports the *next* stable codename) and unstable
      (`sid`). Decide and document how the client names and matches these
      so `cache pull` selects the right bundle (and how a testing/unstable
      user is told when no exact bundle exists → live fallback).
- [ ] **A CI matrix** that builds, signs, and publishes a bundle per
      release, reusing the phase-5 publish mechanism (assets already keyed
      on `<release>`). Stagger and stay incremental — five full crawls is a
      lot of upstream traffic, so daily-incremental + periodic-full per
      release, spread out, matters even more here.
- [ ] **Politeness at matrix scale** — one polite central crawler is the
      whole point; confirm aggregate Repology/sources.debian.org load stays
      reasonable across five releases.

This graduates to its own `PLAN-cache-matrix.md` when picked up.

**Merged:** nothing landed yet.

### 3. Trust hardening

- [x] **A real end-to-end VERIFIED.** Done 2026-09-04: a `cache pull`
      against the published bundle reported `signature verified`, and the
      certificate identity the real run carries is
      `build-cache.yml@refs/heads/develop`, which
      `EXPECTED_SIGNER_IDENTITIES` already accepts — so the phase-4 "the
      expected identity is a guess" risk is closed. See
      [PLAN-published-cache-phase-05-publish.md](PLAN-published-cache-phase-05-publish.md).
- [ ] **Decide the default trust level.** Today, with no `verify` extra, a
      bundle that passes the spot-check is stored even unsigned. Confirm
      spot-check-as-the-floor is the intended 1.0 default, or nudge harder
      toward signatures (docs, prompts, or making the extra a default for
      some install paths).
- [ ] **Drop the pre-rename signer identity.** `EXPECTED_SIGNER_IDENTITIES`
      and `CLASSIFICATION_SIGNER_IDENTITIES` (`divergulent/verify.py`) each
      still carry a `@refs/heads/main` entry from before the default-branch
      rename. Nothing *published* has needed it since both rolling
      prereleases were republished on 2026-09-03, but a client holding a
      *stored* pre-rename bundle would start failing `cache verify` the
      moment it goes. Drop both once a release cycle has passed, so no user
      can still be holding one.
- [ ] **Measure the spot-check at its default sample size.** The first real
      pull reported "3 checked, 5 inconclusive" at `DEFAULT_SPOT_CHECK = 8`
      (a re-run at 40 was 40/0, so it was small-sample luck rather than a
      bundle-quality signal). But the spot-check is the *only* integrity
      check a base install gets, so measure the typical definite share
      across several pulls and decide whether 8 is enough to catch a
      tampered bundle — against the politeness cost of raising it.
- [ ] **Commit to schema stability.** State that `schema`/`cache_schema` 1
      is stable, and that the publisher owns migrations while the client
      drops what it cannot read.

**Merged:** reconstructed rather than recorded at the time — no code
against the boxes above. The one item complete here was a verification
run against the already-published bundle, and what landed was the
record of it, in `5288f4a2` (#88). Trust-hardening code did land in
this window, though, and "no code" should not be read as "nothing
touched `verify.py`": `0e55a386` (#18) taught `spot_check()` that a
bundle-side `UNKNOWN` is the bundle declining to make a claim rather
than a mismatch — "no cry wolf" on both sides — which is this
workstream's subject matter. It is recorded under workstream 1's line,
beside the publish-path fixes it shipped with, so it is in the audit
range either way. The four open items land their own merges, recorded
here as they do.

### 4. "No cry wolf" validation on real machines

The product is trustworthiness, so validate it beyond the CI sample.

- [ ] Run `score`/`staleness`/`divergence` on several real, diverse Debian
      machines and confirm we are not producing **false BEHIND** (Repology
      name-matching, epoch/upstream-version handling) or **false clean/
      native** (source-format detection). Where we are unsure, we must say
      UNKNOWN, not guess.
- [ ] Sanity-check the provisional scoring weights against real data (the
      score only ranks and both axes are always shown, so this is tuning,
      not correctness — but worth a pass).

**Merged:** nothing landed yet.

### 5. Privacy model, stated plainly

- [ ] Document the two privacy regimes crisply: the **bundle path** sends
      nothing about your machine anywhere (whole-bundle download, local
      match); the **live path** (no bundle, or per-miss lookups) sends
      package names to Repology / sources.debian.org. Tell users how to
      stay fully private (pull a bundle).

**Merged:** nothing landed yet.

### 6. Robustness for a public release

- [ ] Behave gracefully off the beaten path: non-Debian systems, missing
      `dpkg`, unexpected versions, and network failures should produce a
      clear message, never a traceback.
- [ ] A clean `--help` and a short quickstart in the README.

**Merged:** nothing landed yet.

### 7. Release mechanics

- [ ] **Start at `v0.1`, not `v1.0`.** The package version is
      `setuptools_scm`-derived from `v*` tags; with no tag yet it already
      resolves to `0.1.dev…`, so we are on the 0.1 track by default. Tag
      the first release **`v0.1`** to signal pre-1.0 (room to increment
      through 0.2, 0.3… as this checklist closes) and reserve `v1.0` for
      when the gate is met. This **release version is independent of the
      bundle schema**: `schema`/`cache_schema` are integers starting at
      `1` (a client checks `schema == 1`) and only bump on a breaking data
      shape change — never renumber the schema to track the release.
- [ ] Confirm the existing tag-driven release pipeline
      (`release.yml`: Sigstore-signed tags + PyPI trusted publishing)
      produces a clean release, including the optional `verify` extra on
      PyPI.
- [ ] Release notes / changelog.

**Merged:** nothing landed yet.

### 8. Builder robustness and publish safety

Prompted by the first real publish: an incremental build reused stale
`UNKNOWN` divergence values from earlier dev runs, so the published bundle
carried entries that should have been `CLEAN`. Transient build-time
failures must not silently degrade the published cache, and a bad build
must never overwrite a good one. Two layers of defence, plus a paper
trail.

- [ ] **Retry within the build.** Transient sources.debian.org / Repology
      failures during the ~34k-package crawl currently collapse straight
      to `UNKNOWN`. Retry failed fetches (bounded, with backoff) before
      recording `UNKNOWN`, and distinguish a *genuine* format-`UNKNOWN` (a
      real non-quilt/non-native source) from a *fetch-failure* `UNKNOWN`,
      so a transient failure is never baked into the bundle nor cached as
      if authoritative.
- [ ] **Sanity-check a new bundle before publishing.** Compare the freshly
      built bundle against the currently published one and **refuse to
      overwrite on an obvious regression** — e.g. markedly fewer
      divergence/staleness entries, a much smaller file, or a sharply
      higher `UNKNOWN` fraction. Coverage should be roughly monotonic; a
      sudden shrink is a red flag, not something to publish silently. If
      the check trips, retry the build once; if it still looks wrong, keep
      the old published bundle (`publish-cache.sh` is gated on this check —
      we **never** automatically overwrite a published cache with a much
      smaller or worse one).
- [ ] **File a GitHub issue on any builder/publish error.** A build
      failure, a tripped sanity check, or a refused publish should open
      (or update) a GitHub issue with the details — entry counts, sizes,
      the failing run URL — so failures are debuggable rather than silent.
      Needs `issues: write` on the job; de-duplicate so a recurring
      failure updates one issue rather than spamming new ones.
- [x] **Accurate patch counts (fix the 60-patch cap).** The divergence
      bundle undercounted every heavily-patched package: the
      sources.debian.org patches API truncates its `patches` array at **60**
      (confirmed — it returns 60 for grub2, whose real `debian/patches/
      series` is **148**), and `summary()` counted whatever the API
      returned. **Done:** the same API response carries the full series
      length in a top-level `count` field, so `summary()` now trusts
      `count` (when present and ≥ the rendered list) and falls back to the
      rendered list otherwise — no extra requests, no deb-src crawl. The
      `count` field is also authoritative when the rendered list silently
      drops an entry it cannot display (bash: `count` 29, 27 rendered).
      The `total` in the published bundle is now correct for the heavy
      tail. **Residual, deferred to
      [PLAN-patch-classification.md](PLAN-patch-classification.md):** the
      *names* list (and therefore the per-patch `details()` bodies) is
      still capped at 60, so classification — which needs the complete set
      of patch names and bodies — must still read the full deb-src
      `series` (the path `--classify` already uses). The cap prerequisite
      there narrows from "fix the count" to "fetch the full names + bodies".

This graduates to its own `PLAN-builder-robustness.md` when picked up,
likely alongside the matrix since both touch `build-cache.yml`.

**Merged:** reconstructed rather than recorded at the time. `d7c030e4`
(#20) for the patch-count fix, the one item complete here — it rode in
with the patch-classification plan because that plan is what the
60-patch cap was blocking. Recording it here is not duplication:
[PLAN-patch-classification.md](PLAN-patch-classification.md)'s phase 7
audit was written as `d7c030e4..develop`, and two-dot notation excludes
the named commit, so that audit never read this merge's own diff — the
`_patch_count` change in `divergulent/sources/debian_patches.py`.
Workstream 9's union range does read it. The other three items have not
landed.

### 9. Push audit

The last workstream, and it runs before the `v1.0` tag rather than after
it: [PUSH-AUDIT.md](https://github.com/shakenfist/divergulent/blob/develop/PUSH-AUDIT.md)
over the accumulated diff of everything the workstreams above landed,
not over whichever one landed last. That is the whole-plan audit the
`plan-push-audit-phase` shared block in
[PLAN-TEMPLATE.md](https://github.com/shakenfist/divergulent/blob/develop/PLAN-TEMPLATE.md)
requires of every master plan, and it is not optional. divergulent has
the runbook at its repository root, so this phase runs it rather than
explaining its absence.

- [ ] Run the audit over the range assembled from the `Merged:` lines
      above plus the unattributed merges tabulated below, once
      workstreams 2–8 have landed and recorded theirs. **The range is a
      union of merge diffs, not a contiguous span:** every SHA recorded
      in this plan is a merge commit on `develop`'s first-parent chain,
      so the audit reads each one as `git diff <sha>^1 <sha>` and
      concatenates the results. Writing it as `52cb034e..develop`
      instead would be wrong twice over — two-dot notation *excludes*
      the named commit, silently dropping #16's own diff, which is the
      merge that landed the scheduled publish and the whole subject of
      workstream 1; and the span would sweep in all of
      [PLAN-patch-classification.md](PLAN-patch-classification.md),
      whose phase 7 push audit already read it as `d7c030e4..develop`
      across 85 files. Do not re-audit those merges here; they are
      recorded there. Workstreams 3 and 8 are in the wait list even
      though each has already recorded a line: both still carry open
      items whose merges are not in the range yet, and an audit that ran
      before they landed would miss exactly what auditing the whole plan
      at once is for.
- [ ] File the findings as their own pull request against `develop`. A
      `v1.0` tag waits on them being fixed, or declined in writing here.
- [ ] Record the result here in one sentence even when it is "nothing
      found" — a clean audit is a result worth writing down.

**What of the range was reconstructed.** This phase was appended on
2026-09-10, long after workstreams 1, 3 and 8 landed what they landed,
so their `Merged:` lines above were recovered rather than recorded at
the time: from `gh pr list --state merged` and `git rev-list
--first-parent origin/develop`, never from a path-filtered `git log`
alone. Every SHA they name is a merge commit — `git rev-list --merges -1
<sha>` returns the SHA itself — and sits on `develop`'s first-parent
chain, so each one's diff against its first parent is the whole of what
that pull request landed.

**What the reconstruction could not attribute.** Publish and release
work has continued since this plan was last edited, and the plan as
written attributes none of it to a workstream. Those merges are
recorded here rather than under a box, and every one of them is in the
audit range:

| SHA | PR | What it landed |
|---|---|---|
| `02e84a77` | #76 | `actions/cache` bump, `build-cache.yml` among the workflows it touches |
| `9fa2b5f5` | #77 | `actions/checkout` bump across the workflows, `build-cache.yml` and `release.yml` included |
| `e28d90a1` | #104 | release asset download fix in `release.yml` |
| `aa06a0b7` | #105 | release tag guards in `release.yml` |
| `a5bd504e` | #107 | publish retry and verification: `tools/release-lib.sh`, the three publish scripts and `tools/sign-bundle.sh`, plus `divergulent/cli.py` and the new `tools/test-release-lib.sh` harness |

Each is a merge commit on `develop`'s first-parent chain, so its diff
against its first parent is the whole of what it landed. Naming the
merges rather than the paths an audit should read is deliberate. The
shared block reserves the path fallback for a phase that accreted over
months of unrelated commits with *no* recoverable range, and this range
is plainly recoverable — the merges are right there. A path list would
also have been wrong in both directions at once: too narrow, because
the publish scripts do not cover the ~90 lines of `divergulent/cli.py`
or the 218-line shell test harness that rode in with #107; and too
wide, because any list broad enough to catch these five also selects
`4c655626` (#34) and `f5d0a7a8` (#43), which
[PLAN-patch-classification.md](PLAN-patch-classification.md) already
records and whose phase 7 push audit already read them.

**The orphans that are not publish work.** The table above scopes
itself to publish and release merges, which is this plan's subject, and
that leaves a second class unaccounted for. Cross-referencing every
merge on `develop` since #16 against every pull request number the
plans under `docs/plans/` name leaves fourteen that no plan mentions at
all: `771e6473` (#47) and `7b45c114` (#60), README work; `0297f4ee`
(#49), `a3ac97ec` (#78) and `6374bc08` (#72), fleet-consistency and
runner configuration; `4201839a` (#62) and `70b2a8f4` (#67),
plan-template blocks; and `8da3e564` (#74), `7e08f6c7` (#75),
`a6cb8a9b` (#83), `eea87799` (#84), `a792a65d` (#86), `f686c89c`
(#108) and `5fa9b746` (#111), Renovate bumps. A fifteenth, `a966dd22`
(#87), is mentioned in the prose of
[PLAN-patch-classification-phase-07-closeout.md](PLAN-patch-classification-phase-07-closeout.md)
but never recorded as a merge. None of them is in this plan's range and
no plan audits them, which is the same hole the pre-#16 paragraph
below states — just inside the window rather than before it. That
enumeration is true as of 2026-09-11 and the class keeps growing as
Renovate lands; closing it is its own piece of work, and it is named
here so that it is stated rather than silently omitted.

**Where the range starts, and what that leaves outside it.** The oldest
merge in the set is `52cb034e` (#16), the merge that created this plan
file. Everything earlier on `develop`'s first-parent chain is outside
the range, and that is fifteen merges rather than a handful: `fb31a07f`
(#1) through `55ecf094` (#15), plus `33308a2e`, the root commit that
scaffolded the project before there were pull requests at all. They
landed [PLAN-initial.md](PLAN-initial.md)'s five phases (#1–#5),
[PLAN-full-machine-run.md](PLAN-full-machine-run.md)'s three (#6–#8),
[PLAN-faster-full-run.md](PLAN-faster-full-run.md)'s work (#9–#10),
[PLAN-published-cache.md](PLAN-published-cache.md)'s phases 1–4 (#11,
#13–#15), and one `build-cache.yml` deb-src fix that belongs to no plan
(#12, `f1c5542a`). All four of those plans are `Complete`, none carries
a push audit phase, and none records a `Merged:` line anywhere — so
under the shared block none is reopened to acquire one, and nothing
audits any of the fifteen. The gap is fifteen merges wide and the whole
pre-#16 chain, not the three this paragraph named in an earlier
revision; closing it means auditing them deliberately, as its own piece
of work, rather than letting a path rule sweep them in while a reader
believes it is filling a small hole.

Whoever closes workstream 7 or 8 should fold `e28d90a1` (#104),
`aa06a0b7` (#105) and `a5bd504e` (#107) into that workstream's
`Merged:` line if they turn out to belong there.

**Merged:** nothing landed yet, and a closeout section cannot name the
merge that lands it — so this is filled in afterwards, as
[PLAN-patch-classification.md](PLAN-patch-classification.md)'s phase 7
was.

## Administration

- Registered in [docs/plans/index.md](index.md).
- Each workstream above becomes its own detailed plan when scheduled; this
  file tracks the overall 1.0 gate and is updated as items close.
