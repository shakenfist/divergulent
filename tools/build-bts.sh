#!/bin/bash
#
# Build the hosted BTS bug index: query UDD (the Ultimate Debian Database) for
# every bug's id/source/status across the live and archived tables, write a
# tab-separated file, and gzip it. This is the weekly CENTRAL pull that feeds the
# rolling 'bts' artifact, so every operator's `divergulent-classify bts` is one
# polite HTTP GET instead of a UDD query. Pure public data (no signing): a bug is
# a bug, regenerable at will.
set -euo pipefail

output="${1:?usage: build-bts.sh <output.tsv.gz>}"
mkdir -p "$(dirname "$output")"

# The public UDD read-only mirror (Debian wiki: UltimateDebianDatabase). Overridable
# via env for a different mirror or a local UDD instance.
UDD_HOST="${UDD_HOST:-udd-mirror.debian.net}"
UDD_DB="${UDD_DB:-udd}"
UDD_USER="${UDD_USER:-udd-mirror}"
export PGPASSWORD="${PGPASSWORD:-udd-mirror}"

# A floor well below the real row count (~1.1M): a UDD hiccup or a truncated result
# must never ship as the index. Overridable for testing.
MIN_ROWS="${MIN_ROWS:-500000}"

# bugs = open + recently-closed; archived_bugs = closed-and-archived. Their union is
# essentially every bug still in the tracker, so an old closed bug a patch cites is
# still found (not a false "not-found" contradiction). UNION ALL avoids the dedup
# cost -- a bug lives in one table or the other.
query="SELECT id, source, status FROM bugs WHERE source IS NOT NULL
UNION ALL
SELECT id, source, status FROM archived_bugs WHERE source IS NOT NULL"

tab="$(printf '\t')"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

echo "Querying UDD (${UDD_USER}@${UDD_HOST}/${UDD_DB})..." >&2
psql -h "$UDD_HOST" -U "$UDD_USER" -d "$UDD_DB" --no-align --tuples-only \
    --field-separator="$tab" -c "$query" > "$tmp"

rows="$(wc -l < "$tmp")"
echo "  ${rows} rows" >&2
if [ "$rows" -lt "$MIN_ROWS" ]; then
    echo "ERROR: only ${rows} rows (< ${MIN_ROWS}); refusing to build a truncated index." >&2
    exit 1
fi

gzip -c "$tmp" > "$output"
echo "Built BTS index:" >&2
ls -l "$output" >&2
