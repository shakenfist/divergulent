# divergulent

*(Working name — I am bad at naming things.)*

**How divergent from pure upstream is this machine?**

divergulent is a tool for answering that question about a Debian system.
It looks at the packages you actually have installed and reports, per
package and as a whole-machine summary, how far your distribution has
drifted from what upstream actually ships.

## Why

It is hard, as a user of a Linux distribution, to tell how stale or how
divergent the software being packaged for you is compared to pure
upstream. That gap is also a supply-chain concern: malicious change does
not have to be introduced at the upstream author layer — it can just as
easily be introduced at the distribution layer, as a carried patch.
Today there is no easy way for a user to ask "how much of my machine is
*not* what upstream released?" and get a reasonable answer.

## Two axes of drift

divergulent measures drift along two distinct axes, because they have
different causes and different data sources:

1. **Staleness** — the packaged version is *behind* pure upstream
   (version lag). "Am I running something old?"
2. **Divergence** — the distribution ships code that is *not in any
   upstream release* (carried patches). Same version number, distro-only
   changes grafted on. This is the axis that is almost invisible to users
   today, and the one most relevant to the supply-chain question.

A large, trusted patch set (e.g. the kernel) is normal — so divergulent
aims to provide *visibility and ranking*, not a verdict.

## Approach (early days)

divergulent is written in Python and, for its first swing, runs entirely
on the local machine:

- It reads the installed-package set from `dpkg`.
- It leans on data Debian and the ecosystem already publish rather than
  crawling upstream version control itself:
  - [Repology](https://repology.org/api) for the staleness axis.
  - [sources.debian.org](https://sources.debian.org) `debian/patches/`
    plus [DEP-3](https://dep-team.pages.debian.net/deps/dep3/) headers
    for the divergence axis.
  - (Later) [UDD](https://udd.debian.org/), uscan/DEHS, and Wikidata.
- It treats sources honestly: heuristic or editable signals are surfaced
  as uncertainty, not presented as fact.

The local-package inventory is sensitive (it fingerprints the host), so
the default posture is local-only — nothing leaves the machine.

## Usage

List the installed packages mapped to their source packages:

```bash
divergulent inventory          # aligned table
divergulent inventory --json   # machine-readable
```

Report packages that are behind upstream (the staleness axis, via
[Repology](https://repology.org/)):

```bash
divergulent staleness          # packages behind upstream, worst first
divergulent staleness --all    # also show current and unknown
divergulent staleness --json   # machine-readable
```

Staleness is heuristic: it relies on Repology resolving your Debian
source package to an upstream project, and reports `unknown` (never
"behind") when it cannot. Each source is looked up individually and
cached locally (~24h), and Repology is queried politely (≤1
request/second).

Report how many patches each package carries (the divergence axis, via
[sources.debian.org](https://sources.debian.org/)):

```bash
divergulent divergence            # packages carrying patches, most first
divergulent divergence --all      # also show clean / native / unknown
divergulent divergence --limit 50 # cap how many source packages are queried
divergulent divergence --workers 4 # fewer concurrent requests (default 8)
divergulent divergence --json     # machine-readable
```

The whole-machine view reports a patch *count* per package, using one
request per source so a full run stays fast and polite. sources.debian.org
has no documented rate limit, so requests run concurrently — `--workers`
(default 8) bounds how many are in flight at once and is the politeness
control; `--workers 1` is fully serial. For the
per-patch [DEP-3](https://dep-team.pages.debian.net/deps/dep3/)
classification (forwarded-upstream vs Debian-only vs unknown), either
drill into one package with `divergulent show <package>` (see below), or
classify the whole machine with `--classify`:

```bash
divergulent divergence --classify   # Debian-only/forwarded/unknown per package
divergulent score --classify        # ranked, weighting Debian-only patches
```

`--classify` fetches each source package's packaging (the `.debian.tar.*`,
not the upstream source) from your configured apt mirror — so it needs
`deb-src` indices enabled (`apt-get update` after adding them). Without
them it prints a notice and falls back to patch counts.

Combine both axes into one ranked, whole-machine answer:

```bash
divergulent score                 # ranked drift report + whole-machine summary
divergulent score --all           # include packages with no detected drift
divergulent score --limit 50      # cap how many source packages are queried
divergulent score --json          # machine-readable
```

`score` is the heaviest command (it queries both axes for every source
package), so it shares one rate-limited HTTP client, reuses the caches
the other commands populate, and supports `--limit` and `--workers`.
Repology stays at ≤1 request/second whatever the worker count, and the
sources.debian.org fetches overlap under that wait, so a cold `score` is
bounded by the Repology half. The score only
*ranks*; both axes are always shown. Note that being behind pure
upstream is expected on a stable Debian release and is weighted lightly
— carried patches are the stronger signal. (Use `show` for the per-patch
Debian-only/forwarded classification of any package.)

The long whole-machine commands (`staleness`, `divergence`, `score`)
show live progress on a terminal (and periodic lines in logs); pass
`--quiet` to suppress it. The first run is slower while it builds a
~24h-cached snapshot of upstream versions; later runs reuse it and are
near-instant.

If you have a precomputed cache **bundle** (the gzipped artifact the
builder produces — see Status), point any of these commands at it to
resolve covered packages instantly from disk instead of querying the
network, falling back to live lookups only for what the bundle does not
cover:

```bash
divergulent score --bundle cache-debian13.json.gz       # both axes from the bundle
divergulent staleness --bundle cache-debian13.json.gz
divergulent divergence --bundle cache-debian13.json.gz
```

The bundle is read locally (your package list never leaves the machine)
and is used only if its schema is recognised and it describes the Debian
release you are running; otherwise the command prints a notice and runs
fully live. A package present in the bundle but installed at a different
version, or absent entirely, falls back to a live lookup — so results
never regress, and `unknown` still means genuinely unresolved.

Rather than pass `--bundle` every time, download the bundle once and let
the commands find it automatically:

```bash
divergulent cache pull                       # download + store this release's bundle
divergulent cache pull --cache-url URL       # ... from a specific URL or mirror
divergulent score                            # now uses the stored bundle, no flag needed
```

`cache pull` (no arguments) downloads the bundle the project publishes for
your release, checks it is recognised, **verifies it**, and stores it
under the cache directory; later runs use it automatically (an explicit
`--bundle` still overrides). The bundle is rebuilt and re-published
daily (with a weekly full rebuild) to a stable URL, so `cache pull`
refreshes it on demand; pass `--cache-url` to use a mirror or a
hand-hosted bundle. Divergence from a stored bundle is always used (a
fixed version's patches never change); **staleness** is used only while
the bundle is fresh (within a week) — past that, staleness is queried
live so newly-behind packages are not missed, while divergence still
comes from the bundle.

A downloaded bundle is untrusted, so two independent checks run before it
is stored:

- **Signature** — the bundle is signed in CI with Sigstore. Install the
  optional verifier (`pip install divergulent[verify]`) and `cache pull`
  checks the signature against the publishing workflow's identity,
  refusing a bundle that fails. Without the extra the check is skipped
  with a notice (use `--require-signature` to make a missing/failed
  signature fatal).
- **Spot-check** — always on, needs no extra: a random sample of the
  bundle's entries is compared against the live origin, and a bundle whose
  data demonstrably disagrees is refused (a transient live failure never
  causes a false refusal). Tune with `--spot-check N` (0 disables).

`--insecure` skips both. Re-check a stored bundle anytime with
`divergulent cache verify`.

Drill into a single installed package:

```bash
divergulent show bash          # per-patch detail with Debian bug links
divergulent show bash --json   # machine-readable
```

`show` lists each carried patch with its classification, description,
and any bug references the patch declares (Debian references are linked
to bugs.debian.org). A patch that declares no bug shows "none declared"
— it means the patch does not reference one, not that none exists.

## Status

Five commands work against real data: `divergulent inventory` (installed
packages → source packages), `divergulent staleness` (behind pure
upstream, via Repology), `divergulent divergence` (carried distro-only
patches, via sources.debian.org), `divergulent score` (both axes
combined into a ranked, whole-machine drift report), and `divergulent
show` (per-package patch detail with Debian bug references). The plan
lives in [docs/plans/PLAN-initial.md](docs/plans/PLAN-initial.md); see
[docs/plans/index.md](docs/plans/index.md) for the plan index, including
planned next steps (Debian BTS cross-referencing, and a patch-hygiene
assessment).

A **published precomputed cache** is in progress
([docs/plans/PLAN-published-cache.md](docs/plans/PLAN-published-cache.md)):
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

A **patch-classification** pipeline (curation-side, for whoever builds the
published cache — not something end users run) turns the carried-patch
residue into an explainable, signed classification: deterministic rules
first, a verified LLM triage tier, then a Sigstore-signed human-review
tier. A cheap, claim-blind **security-risk gate**
(`python -m divergulent.classify.risk`) scores each patch's security risk
so the triage and human tiers reach the riskiest carried patches first.
A deterministic **reviewability axis** scores each patch's size
(`normal`/`large`/`oversized` by changed-line count) for free; an
`oversized` diff is not line-reviewable, so the LLM passes skip it and the
web UI gives it its own bucket.
The human tier is both a CLI (`python -m divergulent.classify.review`)
and a **local web UI** (`python -m divergulent.classify.review_web`, behind
the optional `review` extra — `pip install divergulent[review]`, or
`[review,verify]` to sign) that adds review-by-category, fingerprint
cherry-picking, and an audit view for spot-checking that the deterministic
rules classify correctly. The web UI also lets a reviewer attach **signed,
append-only notes** to a patch — ad hoc observations ("introduces
`sprintf()` near a privilege boundary") that don't fit a verdict — shown
with their signer identity and signature, and surfaces **age** signals
(the patch's DEP-3 `Last-Update` and the package's last-upload date) so a
scary construct in ancient, unloved code reads differently from a recent
one. Both front-ends record byte-identical verdicts against one ledger.
See
[docs/plans/PLAN-patch-classification.md](docs/plans/PLAN-patch-classification.md).

## Development

Tests and linting run through `tox`:

```bash
tox -epy3      # unit tests (stestr + testtools)
tox -eflake8   # style checks on the current change
```

CI runs the same checks on push and pull requests
(`.github/workflows/unit-tests.yml`). A separate workflow
(`.github/workflows/sample-output.yml`) runs a full `divergulent score`
on a Debian 13 runner and uploads the rendered report as a build
artifact, so reviewers can see how the output looks on a real machine
(and as a live end-to-end check). A scheduled workflow
(`.github/workflows/build-cache.yml`) builds the whole-archive bundle on
a Debian 13 runner (`tools/build-cache.sh`), signs it
(`tools/sign-bundle.sh`), and publishes it (`tools/publish-cache.sh`) to
the rolling `cache` prerelease daily — incremental each day, a full
rebuild weekly — so `divergulent cache pull` serves a fresh, signed
bundle. Software releases are tag-driven
(`v*`) and publish to PyPI via Sigstore-signed tags and PyPI trusted
publishing — see [RELEASE-SETUP.md](RELEASE-SETUP.md) for the one-time
configuration.

Planning and pre-push workflow templates live at the repository root:
[PLAN-TEMPLATE.md](PLAN-TEMPLATE.md) and
[PUSH-TEMPLATE.md](PUSH-TEMPLATE.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
