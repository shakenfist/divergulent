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
"behind") when it cannot. Results are cached locally and Repology is
queried politely (≤1 request/second).

Report how many patches each package carries (the divergence axis, via
[sources.debian.org](https://sources.debian.org/)):

```bash
divergulent divergence            # packages carrying patches, most first
divergulent divergence --all      # also show clean / native / unknown
divergulent divergence --limit 50 # cap how many source packages are queried
divergulent divergence --json     # machine-readable
```

The whole-machine view reports a patch *count* per package, using one
request per source so a full run stays fast and polite. For the
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
the other commands populate, and supports `--limit`. The score only
*ranks*; both axes are always shown. Note that being behind pure
upstream is expected on a stable Debian release and is weighted lightly
— carried patches are the stronger signal. (Use `show` for the per-patch
Debian-only/forwarded classification of any package.)

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

## Development

Tests and linting run through `tox`:

```bash
tox -epy3      # unit tests (stestr + testtools)
tox -eflake8   # style checks on the current change
```

CI runs the same checks on push and pull requests
(`.github/workflows/unit-tests.yml`). Releases are tag-driven
(`v*`) and publish to PyPI via Sigstore-signed tags and PyPI trusted
publishing — see [RELEASE-SETUP.md](RELEASE-SETUP.md) for the one-time
configuration.

Planning and pre-push workflow templates live at the repository root:
[PLAN-TEMPLATE.md](PLAN-TEMPLATE.md) and
[PUSH-TEMPLATE.md](PUSH-TEMPLATE.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
