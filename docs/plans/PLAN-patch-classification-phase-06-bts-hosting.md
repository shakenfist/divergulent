# Phase 6 follow-up — host the BTS bug index as a rolling artifact

Phase 6's E5 shipped the BTS bug cross-reference but left its data source as an
*operator-configured URL*: there is no canonical Debian flat file of
`bug → (source, status)`, so `divergulent-classify bts` needed a `--url` pointing
at whatever the operator mirrored. In practice that means the tier is never
exercised — an option nobody runs is an option that quietly rots.

This follow-up closes that gap the same way the divergence cache and the
classification bundle are distributed: **a central job builds the index once, on a
schedule, and publishes it to a rolling GitHub prerelease tag**, so the client
pulls a stable URL with no configuration. Hosting it ourselves *is* the fix — it
turns "operator must supply a URL, probably skip it" into a first-class snapshot
that matches `popcon` and `security-tracker`.

**Status: implemented.**

## Why hosted, not per-operator

- **Politeness.** UDD (the Ultimate Debian Database) is the real source, and it is
  a shared community service. One central query per week is the "one polite
  crawler" thesis the caches already embody; every operator querying the mirror
  themselves is exactly what we avoid elsewhere.
- **No client dependency.** Querying UDD needs a Postgres client — which
  dependency-minimalism forbids in the tool. A hosted flat file needs only an HTTP
  GET (+ gunzip), which the tool already does for popcon/tracker.
- **A real default URL.** Once hosted, `BTS_URL` points at the asset and
  `divergulent-classify bts` works with no flags — the graduation E5 deferred.

## Size (measured shape, estimated magnitude)

`bugs` (open + recently-closed) **∪** `archived_bugs` (closed-and-archived) is
essentially every bug still in the tracker — on the order of **~1.1M rows** (Debian
bug numbers are past 1.1M). Each row is `id⇥source⇥status\n` ≈ 30 bytes, so:

- Uncompressed TSV: **~30–40 MB**
- Gzipped: **~4–6 MB** (`status` is a ~6-value enum and `source` repeats over a
  universe of ~35k packages ever, so it compresses ~6–8×)

That is the same order as the divergence cache and the Security Tracker JSON, so it
adds no new operational weight. The first CI run prints the exact count; the sanity
floor below keys off it thereafter.

## Design

### One artifact: gzipped TSV on a rolling `bts` tag
Distributed exactly like `cache` / `classification`: a rolling **prerelease** tag
(never the repo's "latest"), one asset (`bts-index.tsv.gz`), overwritten in place
each run via `gh release upload --clobber`. Release-**independent** (a bug is a bug
regardless of trixie/bookworm), so unlike the cache there is no per-release fan-out
— one asset serves everyone.

### Not signed (deliberately)
The cache and classification bundles are Sigstore-signed because they gate what a
client trusts about drift/verdicts. The BTS index is a **pure function of public
UDD data** with zero irreproducible human work — regenerable at will, low-stakes
(it only tells the cross-reference whether a bug number exists). So it ships
**unsigned**, matching the popcon/tracker snapshots (also unsigned public data).
Adding a signature later is a one-line parity change if wanted.

### The client learns to gunzip; the default URL points home
`bts.py::pull` gains transparent gzip handling — it detects the `\x1f\x8b` magic on
the downloaded bytes and decompresses before parsing, so it accepts both a plain
TSV (an operator's own `file://` export) and the hosted `.tsv.gz`. `BTS_URL`
defaults to the hosted asset, so `divergulent-classify bts` needs no `--url`.

### A build-time sanity floor
UDD hiccups must not ship a truncated index. `build-bts.sh` refuses to emit a file
with implausibly few rows (a floor well below the real count), and `publish-bts.sh`
refuses to publish a suspiciously small asset — the same "never overwrite good data
with a bad pull" discipline `publish-cache.sh` applies. (A compare-against-published
regression gate, as the cache does, is a possible future nicety; the floor is the
cheap first line.)

## Steps

| Step | Brief |
|------|-------|
| B1 | **`bts.py`: gunzip + default URL.** Transparent gzip decompression in `pull` (magic-byte detection), `BTS_URL` → the hosted rolling-tag asset. Unit-tested against a gzipped fixture download and a plain one. |
| B2 | **Build + publish scripts.** `tools/build-bts.sh` (psql query `bugs ∪ archived_bugs` against the UDD mirror → TSV → gzip, with a min-row floor; connection overridable via env) and `tools/publish-bts.sh` (rolling `bts` tag, unsigned, size floor), mirroring the classification scripts. |
| B3 | **Weekly workflow.** `.github/workflows/build-bts.yml`: a weekly `schedule` (+ `workflow_dispatch`), installs `postgresql-client`, runs build → upload artifact → publish. |
| B4 | **Docs.** This plan, the phase-6 plan/findings note, AGENTS/ARCHITECTURE/README, and the operator runbook (bts now works out of the box). |

## Testing requirements
- `pull` transparently decompresses a gzipped download and still parses a plain
  one; the pinned `bts.sqlite` is identical either way.
- The default `BTS_URL` is the hosted asset (asserted so a typo is caught).
- The build/publish scripts are shellcheck-clean; no live network in the test suite
  (the gzip path uses an injected download, as the existing bts tests do).
- `pre-commit run --all-files` green.

## Out of scope
- Signing the index (deliberately unsigned; see above).
- A compare-against-published regression gate (the min-row floor is the first line;
  the fuller gate can follow if a real regression is ever seen).
- Any change to the verification semantics — this is purely *distribution* of the
  same E5 bug index.

## Back brief
Before executing: this is a *distribution* change, not a semantics change. It hosts
the E5 bug index as a rolling, unsigned, gzipped TSV built weekly from UDD
(`bugs ∪ archived_bugs`), so `divergulent-classify bts` works with no operator URL —
mirroring the cache/classification publish plumbing and the popcon/tracker snapshot
model. One central polite UDD query per week; the client only HTTP-GETs + gunzips.
