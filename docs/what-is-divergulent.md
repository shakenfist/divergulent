# What is divergulent?

divergulent answers a question that is surprisingly hard to ask of a
Linux distribution: **how divergent from pure upstream is this
machine?** It looks at the packages actually installed on a Debian
system and reports, per package and as a whole-machine summary, how
far the software you are running has drifted from what upstream
authors actually released.

## Why the question matters

It is hard, as a user of a Linux distribution, to tell how stale or
how divergent the software packaged for you is compared to pure
upstream. That gap is also a supply-chain concern: a malicious change
does not have to be introduced at the upstream author layer — it can
just as easily be introduced at the distribution layer, as a carried
patch. The xz-utils backdoor travelled partly this way: the malicious
payload activated in the *distribution build environment*, not in the
upstream git tree most eyes were on.

Today there is no easy way for a user to ask "how much of my machine
is *not* what upstream released?" and get a reasonable answer.
divergulent exists to make that question answerable.

## The two axes of drift

Drift has two distinct causes with different data sources, so
divergulent measures them separately:

1. **Staleness** — the packaged version is *behind* pure upstream
   (version lag). "Am I running something old?" Staleness is mostly a
   bug-and-CVE-exposure concern, and on a stable Debian release it is
   expected and largely benign.
2. **Divergence** — the distribution ships code that is *not in any
   upstream release*: carried patches, grafted onto the same upstream
   version number. This axis is almost invisible to users today, and
   it is the one most relevant to the supply-chain question. A Debian
   machine typically carries tens of thousands of distinct patches
   across its installed packages.

A large, trusted patch set (the kernel, say) is normal. divergulent
therefore aims to provide *visibility and ranking*, not a verdict:
its job is to show you where the drift is and let you judge it.

## Beyond counting: what *are* all these patches?

Counting carried patches is only the first step. The more interesting
question is what each patch actually *is*: a build fix? a
documentation typo? a backported security fix? something that
deserves a closer look? divergulent's patch-classification pipeline
answers this for the whole Debian archive — roughly 60,000 distinct
carried patches — and publishes the result as a small signed bundle
any client can download.

Classification is expensive, so it is layered by cost:

1. **Deterministic rules** settle everything that is structurally
   determined (about a third of the corpus) for free — see
   [the deterministic rules](deterministic-rules.md).
2. **An LLM tier** triages the substantive residue, with every draft
   adversarially verified before it counts.
3. **A human tier** reviews the highest-priority remainder and signs
   its verdicts.

The full journey is described in [the processing
workflow](workflow.md).

## Design principles

A few principles run through every part of the project, and are worth
knowing because they explain many otherwise-odd choices:

- **Never cry wolf.** A heuristic or unverified signal is surfaced as
  uncertainty, never presented as fact. `unknown` always means
  "genuinely could not determine", never "probably fine". A tool in
  this space that confidently says "you're fine" on weak evidence is
  itself a hazard. The same principle caps ambition: divergulent
  flags constructs *for review*; it never pronounces a patch
  malicious.
- **The package inventory is sensitive.** The list of what is
  installed on your machine fingerprints it. divergulent's default
  posture is local-only: nothing about your machine leaves it. The
  published caches invert the flow — precomputed centrally, then
  downloaded — precisely so that clients never need to upload
  anything.
- **Lean on data the ecosystem already publishes.** Repology for
  upstream versions, sources.debian.org and the apt mirror network
  for patches, Debian popcon for install base, the Debian Security
  Tracker and BTS for cross-referencing claims. divergulent does not
  crawl upstream version control.
- **Be polite to shared infrastructure.** All network access goes
  through one rate-limited, cached, identifying HTTP client.
- **Trust is earned, then verified.** Published bundles are signed in
  CI with Sigstore, verified on the client, and additionally
  spot-checked against live sources — a downloaded bundle is treated
  as untrusted until both checks pass.
- **Provenance over authority.** Every classification decision
  records who or what decided it (which rule at which version, which
  model with which prompt, which human with what signature), and
  verdicts are derived from an append-only ledger rather than stored
  — so any decision can be revisited and any rule retired cleanly.

## What divergulent is not

- It is not a scanner that tells you whether a patch is malicious. It
  ranks and explains; humans judge.
- It is not an upstream-tracking service. Staleness comes from
  Repology's existing matching, reported honestly as heuristic.
- The classification pipeline is not something end users run. Users
  only ever download its published, signed output; the client runs no
  classifier and no LLM.
