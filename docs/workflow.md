# The processing workflow

This document describes how divergulent turns raw data into answers,
end to end. There are two halves with very different postures:

- The **client side** (`divergulent <command>`) runs on a user's
  machine, is local-only by default, and never runs a classifier or
  an LLM.
- The **curation side** (`divergulent-classify <verb>`) runs
  centrally, once, on whoever maintains the published caches. It is
  where all the expensive work happens.

## The client side in one paragraph

The client reads the installed-package set from `dpkg`, maps each
binary package to its Debian source package, and asks two questions
per source: is it behind upstream (staleness, via Repology), and does
it carry distro-only patches (divergence, via sources.debian.org)?
`divergulent score` combines both into a ranked whole-machine report;
`divergulent show <package>` drills into one package's patches. A
cold run is slow (thousands of polite HTTP requests), so the project
publishes a precomputed, Sigstore-signed **cache bundle** per Debian
release — `divergulent cache pull` downloads and verifies it, after
which most lookups resolve instantly from disk. A second published
bundle (`cache pull-classification`) annotates each patch with its
curated classification; the client joins against it by hashing the
patch body it already fetched. Hashing a diff is not classifying it —
the client never classifies anything.

## The curation pipeline

The interesting machinery is curation-side: turning ~60,000 distinct
carried patches into explainable, provenance-carrying classifications
without pretending to more certainty than the evidence supports. The
pipeline is layered by cost: free deterministic passes first, then
metered LLM passes over what remains, then scarce human attention on
the highest-priority residue.

Stages, in order, with who decides what:

```
 1. corpus build      crawl the archive's carried patches      deterministic
 2. fingerprint       normalise + hash each patch body         deterministic
 3. claim + content   what the author SAYS vs what diff DOES   deterministic
 4. category rules    settle structural categories             deterministic
 5. ledger record     append decisions + observations          deterministic
      (+ reviewability, reach, CVE/bug cross-reference axes)
 6. risk gate         benign cull, then LLM risk score         mixed
 7. LLM triage        classify the residue, verified drafts    LLM
 8. human review      signed verdicts on the priority queue    human
 9. derived verdict   precedence over the live decisions       deterministic
10. export + bundle   committed JSONL + published signed map   deterministic
```

### 1. Corpus build

`divergulent-classify` builds a resumable, content-addressed corpus
of every carried patch in the archive, by fetching each source
package's `.debian.tar.*` from the mirror network (never the upstream
tarball). A measurement pass deduplicates the bodies into a sqlite
fingerprint index. On Debian trixie this found ≈61.5k carried patch
occurrences deduplicating to 60,640 distinct patches — a dedup ratio
of only 1.02x. Carried patches are overwhelmingly bespoke, which
kills any hope of classifying the archive by sharing verdicts across
packages: nearly every patch must be judged on its own.

### 2. Fingerprint

Every patch body is normalised (decoration lines, file-header paths
and timestamps, hunk line numbers, trailing whitespace and line
endings all stripped; the DEP-3 author header is skipped entirely)
and hashed. The pair `(version, sha256(normalised body))` is the
**fingerprint** — the identity every later stage keys on, and the
join key clients use against the published classification bundle.
Normalisation is versioned so it can evolve without silently
orphaning old verdicts.

### 3. Claim and content extraction

Two independent, deterministic extractions per patch:

- The **claim**: what the author *says* the patch is, parsed from its
  DEP-3 header — claimed category, CVE identifiers, bug references,
  forwarded status. The claim is provenance, never trusted: 58% of
  patches carry no usable claim at all, and a claim that disagrees
  with the content is a signal, not an error.
- The **content profile**: what the diff *does* — every touched file
  typed as test / build / doc / code / data, line counts, and a set
  of conservative "trivial change" detections.

Keeping these separate is load-bearing: the content verdict must
depend on the diff alone, so that claim/content disagreement means
something.

### 4. Deterministic category rules

Eight precedence-ordered rules settle the categories that are
structurally determined — a change touching only build files *is*
packaging; a change touching only the test suite *is* test — and
deliberately refuse to guess the ones that are about intent
(bugfix / feature / security). Roughly a third of the corpus settles
here for free; the rest becomes the "substantive residue". A separate
code-aware scan flags dangerous constructs (shell-outs, fetch-piped-
to-shell, embedded private keys...) as review candidates — never as
verdicts. [The deterministic rules](deterministic-rules.md) documents
every rule, with corpus hit counts.

### 5. The ledger

All decisions land in an **append-only provenance ledger** (sqlite
locally; committed to git as a deterministic JSONL export). Nothing
is ever updated in place: a decision is only ever *superseded* by a
newer one, and the "current verdict" is always derived, never stored,
so it cannot drift from its evidence. Every decision records who
decided it (`rule id + version`, `llm-triage:<model> + prompt
version`, or a human identity) — so retiring a bad rule re-queues
exactly the fingerprints it had settled, and nothing else.

The same recording pass also attaches three cheap deterministic
**axes** that ride alongside the category: reviewability (size),
reach (install base), and the phase-6 CVE/bug cross-reference. These
are described with the rules in
[deterministic-rules.md](deterministic-rules.md).

### 6. The security-risk gate

Before spending the expensive triage pass, a cheap, claim-blind LLM
pass scores *every* patch's security risk on a coarse ordinal
(`none / low / elevated / high`) — the whole corpus, not just the
residue, because a patch the rules settled as `packaging` can still
be security-relevant (a `debian/rules` change can flip a hardening
flag). A deterministic cull first scores provably-benign patches
(empty, whitespace-only, comment-only, doc-only, translation/
changelog-only) as `none` with no LLM call. The gate is **advisory**:
it feeds the priority order so triage and humans reach the scariest
patches first, but it never sets a category and its failures degrade
to `elevated` (recall-safe — a scoring failure makes a patch *more*
visible, not less).

### 7. LLM triage

The substantive residue is triaged by a claim-blind LLM pass: the
model sees the diff, never the author's claim. Every draft verdict is
**adversarially verified** by a second pass before it counts; an
unverified draft is recorded for the audit trail but ranks *below*
the deterministic rules in verdict precedence, so an unreviewed guess
can never win. Triage runs over a bounded, prioritised slice (risk
first, then dangerous-construct flags, then high-occurrence), never
the whole queue by accident. Model and prompt version are part of
each decision's identity, so a model swap or prompt bump is cleanly
supersedable. When triage notices clusters of identical verified
verdicts it surfaces them as *candidate* deterministic rules — for
human approval, never auto-applied.

### 8. Human review

The top of the precedence order. A reviewer works the priority queue
through a CLI or a local web UI (both record byte-identical verdicts
against the same ledger), seeing each diff in its original source
context beside the LLM draft. Human verdicts are signed with Sigstore
and are authoritative. The web UI adds review-by-category, an
audit/spot-check view for confirming the deterministic rules are
classifying correctly, and signed append-only reviewer notes.

### 9. The derived verdict

The current verdict for a fingerprint is computed, on demand, from
the live decisions by a strict precedence:

> **human > verified-LLM > deterministic heuristic > unverified-LLM**

with recency, confidence, and insertion order as tie-breaks. This
ordering is the pipeline's honesty guarantee in one line: a human
always wins, a machine draft counts only once verified, and an
unverified guess never outranks an explainable rule.

### 10. Export and publish

The ledger's committed source of truth is a deterministic sharded
JSONL export (reviewable diffs, no binary sqlite in git). From it, CI
builds the lean **classification bundle** — a gzipped
fingerprint → verdict map carrying category, the three axes, a short
reason, and the deciding rule, with no raw LLM evidence — signs it
with Sigstore, and publishes it to a rolling release. The bundle
*grows* as review settles more of the residue; clients simply re-pull
to see more of their patches explained.

## Where the boundaries are

| Tier | Decides | Never does |
| --- | --- | --- |
| Deterministic | structural categories, size, reach, provable benignity, CVE-confirmed `security` | guess intent; pronounce malice |
| LLM | draft categories for the residue; advisory risk scores | count unverified; see the author's claim |
| Human | final verdicts, rule approvals, notes | be bypassed: nothing outranks a signed human verdict |

The published bundle preserves these boundaries: every entry says
which tier decided it and why, so a reader of `divergulent show` can
weigh a rule-settled `packaging` differently from a human-signed
`security` if they choose.
