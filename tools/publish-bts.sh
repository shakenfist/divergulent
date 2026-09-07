#!/bin/bash
#
# Publish the BTS bug index to the rolling 'bts' GitHub prerelease, in place, so
# clients pull it from a stable URL
# (.../releases/download/bts/bts-index.tsv.gz). Run in CI with a GITHUB_TOKEN that
# has contents: write. Unsigned public data -- a pure function of UDD, regenerable
# at will -- so unlike the cache/classification bundles there is no signature.
set -euo pipefail

# shellcheck source=tools/release-lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/release-lib.sh"

asset="${1:?usage: publish-bts.sh <bts-index.tsv.gz>}"
tag="bts"

# Refuse to publish a suspiciously small asset (never overwrite good data with a bad
# pull). ~1.1M rows gzip to a few MB, so a ~1MB floor catches a truncation.
MIN_BYTES="${MIN_BYTES:-1000000}"

if [ ! -f "$asset" ]; then
    echo "ERROR: asset $asset not found; build the index before publishing." >&2
    exit 1
fi

size="$(stat -c%s "$asset")"
if [ "$size" -lt "$MIN_BYTES" ]; then
    echo "ERROR: asset is ${size} bytes (< ${MIN_BYTES}); refusing to publish a likely-truncated index." >&2
    exit 1
fi

ensure_gh

ensure_rolling_release "$tag" \
    "BTS bug index" \
    "Rolling, auto-updated Debian BTS bug index (bug -> source, status) for divergulent's phase-6 patch cross-reference. Not a software release."

echo "Publishing to release '$tag':"
publish_assets "$tag" "$asset"
