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

Report packages carrying distro-only patches (the divergence axis, via
[sources.debian.org](https://sources.debian.org/)):

```bash
divergulent divergence            # packages carrying Debian-only patches
divergulent divergence --all      # also show clean / native / unknown
divergulent divergence --limit 50 # cap how many source packages are queried
divergulent divergence --json     # machine-readable
```

Each patch is classified from its [DEP-3](https://dep-team.pages.debian.net/deps/dep3/)
header as forwarded-upstream, Debian-only, or unknown. Because DEP-3 is
not universally used, patches without it are also checked for
Debian-authored signals (the old `# DP:` convention and `deb-*` /
`debian-*` filenames); anything still unattributed is reported as
unknown rather than assumed divergent. This axis makes one request per
patch, so it caches aggressively and supports `--limit`.

## Status

Early. Phases 1–3 are implemented: `divergulent inventory` reads the
installed-package set from `dpkg`, `divergulent staleness` reports which
packages are behind pure upstream, and `divergulent divergence` reports
which packages carry distro-only patches. A combined whole-machine score
is not built yet. The plan for the first implementation lives in
[docs/plans/PLAN-initial.md](docs/plans/PLAN-initial.md); see
[docs/plans/index.md](docs/plans/index.md) for the plan index.

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
