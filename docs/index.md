# divergulent documentation

divergulent answers one question about a Debian machine: **how divergent
from pure upstream is this system?** These documents explain what the
tool is, how it processes data, and — in detail — the deterministic
rules at the heart of its patch classification.

Honest framing of the audience: the most likely reader is someone who
stumbled onto the project and is curious how it works, or the author
six months from now, trying to remember which deterministic rules
already exist. The documents are written for that reader — an
interested outsider, not a contributor — so they favour explanation
and worked examples over API reference.

## Contents

- [What is divergulent?](what-is-divergulent.md) — the motivation, the
  two axes of drift (staleness and divergence), and the design
  principles the whole project follows.
- [Usage](usage.md) — the complete command reference, including cache
  bundles, bundle verification, and the optional patch-classification
  bundle.
- [Status](status.md) — what works today, the published precomputed
  cache, and how much of the carried-patch residue the classification
  pipeline has actually settled.
- [Development](development.md) — tests, the CI workflows, and how
  releases are published.
- [The processing workflow](workflow.md) — how a carried patch travels
  from discovery through fingerprinting, deterministic rules, LLM
  triage, and human review to a published, signed verdict; and which
  stages are deterministic, model-driven, or human.
- [The deterministic rules](deterministic-rules.md) — every
  deterministic rule and axis, one by one: what it matches, what it
  decides, its precedence, and — just as importantly — what it
  deliberately refuses to decide.
- [Patch classification](patch-classification.md) — the curation-side
  pipeline: the corpus crawl, deterministic classification, the
  append-only decision ledger, LLM and human triage, and publishing.
- [The classification runbook](classification-runbook.md) — for whoever
  operates that pipeline: the recurring command sequence, what it
  costs, what CI already does on a schedule, and what to check when the
  published bundle looks wrong.
- [Plans](plans/index.md) — the planning documents that drove each
  phase of the work, kept for the historical record. The documents
  above describe the system as it is; the plans describe how it got
  there.

For contributor-facing material (build, test, and style conventions)
see [AGENTS.md](https://github.com/shakenfist/divergulent/blob/develop/AGENTS.md)
at the repository root; for a module-by-module tour of the code see
[ARCHITECTURE.md](https://github.com/shakenfist/divergulent/blob/develop/ARCHITECTURE.md);
for CLI usage see [usage.md](usage.md).
