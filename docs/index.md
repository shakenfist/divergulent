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
- [The processing workflow](workflow.md) — how a carried patch travels
  from discovery through fingerprinting, deterministic rules, LLM
  triage, and human review to a published, signed verdict; and which
  stages are deterministic, model-driven, or human.
- [The deterministic rules](deterministic-rules.md) — every
  deterministic rule and axis, one by one: what it matches, what it
  decides, its precedence, and — just as importantly — what it
  deliberately refuses to decide.
- [Plans](plans/index.md) — the planning documents that drove each
  phase of the work, kept for the historical record. The documents
  above describe the system as it is; the plans describe how it got
  there.

For contributor-facing material (build, test, and style conventions)
see [AGENTS.md](../AGENTS.md) at the repository root; for a
module-by-module tour of the code see
[ARCHITECTURE.md](../ARCHITECTURE.md); for CLI usage see
[README.md](../README.md).
