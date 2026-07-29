# Usage

The complete command reference for divergulent. For what the tool is
and why it exists, see [what-is-divergulent.md](what-is-divergulent.md).

## Inventory

List the installed packages mapped to their source packages:

```bash
divergulent inventory          # aligned table
divergulent inventory --json   # machine-readable
```

## Staleness

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

## Divergence

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

## Score

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

## Using a precomputed cache bundle

If you have a precomputed cache **bundle** (the gzipped artifact the
builder produces — see [status.md](status.md)), point any of these
commands at it to resolve covered packages instantly from disk instead
of querying the network, falling back to live lookups only for what the
bundle does not cover:

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

### Bundle verification

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

## Show

Drill into a single installed package:

```bash
divergulent show bash          # per-patch detail with Debian bug links
divergulent show bash --json   # machine-readable
```

`show` lists each carried patch with its classification, description,
and any bug references the patch declares (Debian references are linked
to bugs.debian.org). A patch that declares no bug shows "none declared"
— it means the patch does not reference one, not that none exists.

## Patch classification (an optional second bundle)

Beyond the DEP-3 forwarded/Debian-only class, there is a richer,
curated **classification** of what each patch actually *is* — its
category (security, feature, bugfix, packaging, documentation, test…),
plus security-risk, install-base *reach*, and reviewability axes. That
curation is expensive (deterministic rules, a verified LLM tier, and
human review), so like the divergence cache it is done **once, centrally**
and published as a small signed bundle keyed by patch fingerprint. Pull
it and `show` annotates each patch with its category and *why*:

```bash
divergulent cache pull-classification   # download + verify + store this release's bundle
divergulent show bash                    # patches now carry: class, axes, and the deciding rule
```

The client runs **no** classifier and **no** LLM — it hashes the patch
body it already fetched and looks the verdict up in the bundle (hashing a
diff is not classifying it). Without the bundle, `show` behaves exactly
as before. The bundle is signed and verified the same way as the
divergence cache; it *grows* as review settles more of the residue, so
re-pulling simply enriches what you see.
