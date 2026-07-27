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

divergulent leans on data Debian and the ecosystem already publish
([Repology](https://repology.org/api),
[sources.debian.org](https://sources.debian.org)) rather than crawling
upstream version control itself, and treats heuristic signals as
uncertainty, not fact. The local-package inventory is sensitive (it
fingerprints the host), so the default posture is local-only — nothing
leaves the machine.

## Installation

```bash
pip install divergulent
```

Add the optional Sigstore verifier for signed cache bundles with
`pip install divergulent[verify]`.

## Usage

```bash
divergulent inventory     # installed packages mapped to source packages
divergulent staleness     # packages behind upstream, worst first
divergulent divergence    # packages carrying distro-only patches, most first
divergulent score         # both axes, ranked, with a whole-machine summary
divergulent show bash     # per-patch detail for one package
divergulent cache pull    # fetch the published cache bundle for fast runs
```

See [docs/usage.md](https://github.com/shakenfist/divergulent/blob/main/docs/usage.md)
for the complete command reference, including cache bundles, bundle
verification, and the optional patch-classification bundle.

## Documentation

In the [docs/](https://github.com/shakenfist/divergulent/blob/main/docs/index.md)
directory:

- [Documentation Index](https://github.com/shakenfist/divergulent/blob/main/docs/index.md) - Where to start
- [What is divergulent?](https://github.com/shakenfist/divergulent/blob/main/docs/what-is-divergulent.md) - Motivation, the two axes, and design principles
- [Usage](https://github.com/shakenfist/divergulent/blob/main/docs/usage.md) - The complete command reference
- [The processing workflow](https://github.com/shakenfist/divergulent/blob/main/docs/workflow.md) - How a carried patch travels from discovery to a signed verdict
- [The deterministic rules](https://github.com/shakenfist/divergulent/blob/main/docs/deterministic-rules.md) - Every classification rule, its precedence, and its hit rate
- [Status](https://github.com/shakenfist/divergulent/blob/main/docs/status.md) - What works today and the pieces in flight
- [Development](https://github.com/shakenfist/divergulent/blob/main/docs/development.md) - Tests, CI workflows, and releasing

Project reference files:

- [ARCHITECTURE.md](https://github.com/shakenfist/divergulent/blob/main/ARCHITECTURE.md) - A module-by-module tour of the code
- [AGENTS.md](https://github.com/shakenfist/divergulent/blob/main/AGENTS.md) - Build, test, and style conventions

## License

Apache License 2.0. See [LICENSE](https://github.com/shakenfist/divergulent/blob/main/LICENSE).
