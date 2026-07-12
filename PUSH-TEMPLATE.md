Thanks for your work on this. I appreciate it. Some final
checks before I push.

## How to use this template

The pre-push audit splits into two waves:

**Wave 1 — mechanical.** Build verification, lint, unit and
end-to-end test suites, and the parts of style conformance
that grep can answer. Always run wave 1 first; wave 2 is
only worth spending on if wave 1 passes.

**Wave 2 — judgment.** Code-quality/correctness,
test-coverage, documentation, and security review. Some of
this is mechanical (TODO/FIXME grep, dead-code detection, new
dependencies) and the rest needs sub-agents to read code and
apply judgment. The four judgment agents are independent and
can be spawned in parallel.

The management session reviews all findings, fixes any
issues, and confirms the push.

These checks assume the default branch is `main`; adjust the
diff base below if the repository uses something else.

## Wave 1: Mechanical checks

Run the following, stopping on the first failure:

```
pre-commit run --all-files
tox          # or: pytest, if tox is not configured yet
```

`pre-commit` runs the configured lint, unit tests, and type
checking. `tox` runs the full test matrix.

A defining concern for this project is that **tests must not
hit live external services.** Confirm the suite passes with
the network disabled (responses mocked or served from
recorded fixtures):

```
# Should still pass — no Repology / UDD / sources.debian.org calls
tox          # or pytest, run in an environment with no network
```

Then a few grep-level style and hygiene checks on the diff
against `main`:

```
git diff main...HEAD -- '*.py' | grep -nE '^\+[^+].{120,}'  # lines > 120 chars
git diff main...HEAD -- '*.py' | grep -nE '^\+[^+].*\bprint\('  # stray print() in new code
git diff main...HEAD -- '*.py' | grep -nE '^\+[^+].*requests\.(get|post)\(' | grep -v 'timeout'  # network calls missing a timeout
git diff main...HEAD -- '*.py' | grep -nE '^\+[^+].*(subprocess\.|os\.system|shell=True)\b'  # shell-out review
```

Exit condition: wave 1 passes when pre-commit, the test
suite (including offline), and the style/hygiene greps all
come back clean. If anything fails, fix the cause and re-run
before spending on wave 2.

### Style conformance — judgment portion

The commands above cover what grep can prove. The remaining
style questions need a sub-agent to read code:

| Setting | Value |
|---------|-------|
| Model | sonnet |
| Effort | low |

**Brief for sub-agent (only if wave 1 passes):**

Check `git diff main...HEAD` for adherence to project
conventions in `CLAUDE.md` and `AGENTS.md`:

- Python conventions: import ordering, single quotes for
  strings and double quotes for docstrings, 120-char lines,
  no trailing whitespace.
- Data-source adapter conventions: does every new external
  source follow the shared fetch/normalise interface, go
  through the caching layer, and set a descriptive
  User-Agent? Direct ad-hoc network calls that bypass the
  cache are a finding.
- Politeness discipline (blocking): any new outbound request
  must respect documented rate limits / terms of use, carry
  a timeout, and degrade gracefully when the source is
  down. A new source with no caching or no backoff is a
  finding.
- Version-comparison discipline (blocking): version ordering
  must use Debian version semantics (e.g. `apt_pkg` /
  `dpkg --compare-versions`), never naive string comparison
  or `sorted()` on raw version strings. Epochs and Debian
  revisions must be handled correctly.

Report a short list of any violations found. If none, say
"Style checks passed."

## Wave 2: Deeper review

Only run wave 2 after wave 1 passes.

Start with the mechanical sweep on the diff:

```
# TODO / FIXME / HACK / XXX in changed files
git diff main...HEAD -- '*.py' | grep -nE '^\+.*\b(TODO|FIXME|HACK|XXX)\b'

# New `# noqa`, `# type: ignore`, or `pragma: no cover`
git diff main...HEAD -- '*.py' | grep -nE '^\+.*(# noqa|# type: ignore|pragma: no cover)'

# Unsafe parsing of remote data (this tool parses a lot of it)
git diff main...HEAD -- '*.py' | grep -nE '^\+.*\b(yaml\.load\b|pickle\.load|eval\(|exec\()'

# New test functions vs files changed (sanity ratio)
git diff main...HEAD --stat | tail -1
git diff main...HEAD -- '*.py' | grep -cE '^\+\s*def test_'

# Documentation files touched (warns if none — the diff may have merited doc updates)
git diff main...HEAD --name-only -- 'docs/*' '*.md'

# Classifier changed without the classifier docs being touched — if the
# first grep matches and the second is empty, the reader docs probably
# need an update (see 2c below)
git diff main...HEAD --name-only -- 'divergulent/classify/*' 'divergulent/dep3.py'
git diff main...HEAD --name-only -- 'docs/deterministic-rules.md' 'docs/workflow.md'

# New dependencies (supply-chain surface of the tool itself)
git diff main...HEAD --name-only | grep -E 'requirements.*\.txt|pyproject\.toml|setup\.(py|cfg)'
```

These report only — they do not block. Treat the output as
input to the judgment agents below.

Then spawn the judgment agents. They are independent and
can run in parallel.

### 2a. Code quality and correctness

| Setting | Value |
|---------|-------|
| Model | sonnet |
| Effort | medium |

**Brief for sub-agent:**

The mechanical sweep has already extracted TODO/FIXME
comments, new `# noqa` / `# type: ignore`, and unsafe-parse
patterns. Take that report as input.

Add the judgment-level review on the diff
(`git diff main...HEAD`):

- **Duplicated code:** Are there significant blocks of
  duplicated logic the mechanical scan can't see? Look
  especially for copy-paste across data-source adapters that
  should share a base class or helper.
- **Missed abstractions:** Should any new code be extracted
  into a shared module? Look for logic a second data source
  or output format would likely need.
- **Correctness — the heart of the tool (blocking):**
  - Version comparison uses Debian version ordering (epochs,
    `~` pre-release ordering, revisions), not string compare.
  - Staleness determination distinguishes "genuinely behind
    upstream" from "Repology hasn't matched the package" or
    "no upstream feed exists" — the latter must not be
    reported as confirmed staleness.
  - Patch/divergence classification reads DEP-3 headers
    correctly: `Forwarded: yes` / upstream origin is benign
    drift; `Forwarded: no` / vendor origin is real
    divergence; a missing header is *unknown*, not zero.
  - The scoring model does not let a single noisy signal
    dominate, and large trusted patch sets (e.g. the kernel)
    are not flagged as anomalous purely by patch count.
- **Cry-wolf avoidance (blocking):** any place that presents
  a heuristic or unverified signal as a definitive verdict
  is a finding. Uncertainty must remain visible in the
  output.
- **Triage the mechanical findings:** for each
  TODO / noqa / type:ignore the sweep flagged, say blocking
  or advisory and why. Skip ones inside test modules unless
  they disable coverage on production code.

Report findings as a bullet list. For each finding, state
the file, line, and whether it's blocking (must fix before
push) or advisory (can address later).

### 2b. Test review

| Setting | Value |
|---------|-------|
| Model | sonnet |
| Effort | medium |

**Brief for sub-agent:**

Review the diff (`git diff main...HEAD`) for test coverage:

- Does every new public function or significant code path
  have unit test coverage?
- Is all external access mocked or served from recorded
  fixtures, so the suite is deterministic and offline? Tests
  that hit live Repology / UDD / sources.debian.org are a
  finding.
- Do the tests include adversarial cases for the data the
  tool ingests: malformed version strings, missing epochs,
  empty or malformed `debian/patches/series`, patches with
  no DEP-3 header, truncated JSON, an unreachable source,
  and a source returning an unexpected schema?
- Is version-comparison logic tested against known-tricky
  pairs (epochs, `~rc`, `+dfsg`, native vs non-native)?
- Are there assertions that test implementation details
  rather than behaviour (fragile tests)?
- Are there new modules or functions with zero coverage that
  should have at least basic tests?

Report findings as a bullet list grouped by file.

### 2c. Documentation review

| Setting | Value |
|---------|-------|
| Model | sonnet |
| Effort | medium |

**Brief for sub-agent:**

Check that documentation matches the current code state.
Read the diff (`git diff main...HEAD`) and verify:

- `README.md` reflects any new features, changed CLI usage,
  new data sources, or updated project structure (and still
  mentions any `.claude/skills/` if present).
- `ARCHITECTURE.md` reflects any new or modified modules,
  data-source adapters, the cache, the scoring model, or the
  client/server split.
- `AGENTS.md` reflects any new dependencies, build commands,
  conventions, or the polite-API-usage rules.
- `docs/` content is in sync — in particular any description
  of how each data source is queried, what the staleness and
  divergence axes mean, and how the score is computed.
- The reader-facing docs in `docs/` match the classifier
  (blocking — this is how the rules stay documented):
  - Any added, removed, re-ordered, or re-tuned deterministic
    rule — a change to `rules.py`'s `_CATEGORY_RULES` or its
    dangerous-construct pattern tables, `content.py`'s file
    typing, the `reviewability.py`/`reach.py` thresholds,
    `risk.py`'s provably-benign cull, or `cross_reference.py`'s
    settle guards — is reflected in
    `docs/deterministic-rules.md`: the rule tables, the
    precedence rationale, and (for a new rule) a short section
    saying what it matches and why it is safe to settle
    deterministically.
  - Any bump to a `*_VERSION` / `*_RULE_VERSION` /
    `*_PROMPT_VERSION` constant is reflected there too.
  - Pipeline changes (a new tier or axis, a reordering, a new
    `divergulent-classify` verb) are reflected in
    `docs/workflow.md`.
  - New reader-facing documentation lands in `docs/` (not the
    repository root) and is linked from `docs/index.md`.
- Plan files in `docs/plans/` are up to date — completed
  phases marked complete, deferred items listed, and the
  *Plan Status* table in `docs/plans/index.md` reflects
  reality.
- If a new data source was added, the docs state its
  trust level (authoritative vs heuristic vs editable) and
  any rate-limit / terms-of-use obligations.

Report findings as a bullet list. "No documentation gaps
found" is a valid answer.

### 2d. Security and trust review

| Setting | Value |
|---------|-------|
| Model | opus |
| Effort | high |

**Brief for sub-agent:**

Security review of the diff (`git diff main...HEAD`). This
requires careful judgment — read the actual code, not just
the diff summary. Divergulent is a supply-chain *visibility*
tool, so both classic vulnerabilities and trust/integrity
failures matter.

Check for:

- **Untrusted-input parsing:** The tool consumes remote data
  it did not author — JSON from Repology/UDD, patch files and
  `series` files from sources.debian.org, version strings,
  `debian/watch` contents. Is all of it parsed defensively?
  Any `eval`/`exec`, `pickle.load`, or `yaml.load` without
  `SafeLoader` is a finding. Could a malicious/malformed
  response crash the client or the server, or cause
  unbounded memory growth (e.g. a huge patch or version
  list)?
- **Command / shell safety:** Calls to `dpkg-query`, `dpkg`,
  or other local tools must pass arguments as a list, never
  via a shell string with interpolated package names. Any
  `shell=True` with a non-constant command is a finding.
- **SSRF and URL handling:** If any code follows URLs derived
  from package metadata (upstream repos, `debian/watch`
  targets) or accepts a server endpoint from config, is it
  constrained? Could it be coerced into fetching internal /
  link-local addresses or arbitrary schemes (`file://`)?
- **Privacy of the package inventory:** The installed-package
  list is sensitive (it fingerprints the host). Is it ever
  transmitted off-box? If so, is that path opt-in,
  documented, and minimised (e.g. hashed, or only the
  packages being queried)? Silent telemetry is a finding.
- **Cache integrity:** Is the on-disk cache written/read
  safely (no path traversal from a source-derived key, no
  TOCTOU, sane permissions)? Could a poisoned cache entry
  cause the tool to under-report divergence?
- **Trust misrepresentation (project-specific):** Does the
  output ever present an *editable* source (Wikidata) or a
  *heuristic* match (Repology name-matching) as authoritative
  fact? A security tool that confidently says "you're fine"
  on weak evidence is itself a hazard. Mislabelled
  confidence is a finding here.
- **Tool supply chain:** Do new third-party dependencies
  pull their weight, come from a reputable source, and pin
  versions? (Ironic, but a divergence-auditing tool that is
  itself an unaudited dependency sprawl undermines its own
  thesis.)

Report findings with severity (critical / high / medium /
low / informational). For each finding, state the file,
line, the vulnerability or trust-failure class, and a
recommended fix.

## Management session checklist

After all agents complete, the management session should:

- [ ] Wave 1 passed (pre-commit, tests incl. offline, style
      and hygiene greps clean).
- [ ] Wave 2 findings reviewed.
- [ ] Any blocking findings from 2a/2b/2c have been fixed
      and re-verified.
- [ ] Any security/trust findings from 2d have been assessed
      — critical and high must be fixed before push.
- [ ] The commit history is clean (no fixup commits that
      should be squashed, no accidental files, no WIP
      messages).
- [ ] The branch is up to date with the target branch
      (rebase if needed).
- [ ] Ready to push.
