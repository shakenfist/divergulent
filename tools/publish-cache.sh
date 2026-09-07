#!/bin/bash
#
# Publish a signed cache bundle to the rolling 'cache' GitHub prerelease, in
# place, so clients can pull it from a stable URL
# (.../releases/download/cache/cache-<release>.json.gz). Run in CI with a
# GITHUB_TOKEN that has contents: write. Uploads both the bundle and its
# Sigstore signature, overwriting the previous day's assets, and confirms both
# are downloadable before reporting success.
set -euo pipefail

# shellcheck source=tools/release-lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/release-lib.sh"

bundle="${1:?usage: publish-cache.sh <bundle-file>}"
signature="${bundle}.sigstore.json"
tag="cache"

if [ ! -f "$signature" ]; then
    echo "ERROR: signature $signature not found; sign the bundle before publishing." >&2
    exit 1
fi

ensure_gh

ensure_rolling_release "$tag" \
    "Precomputed cache bundles" \
    "Rolling, auto-updated signed divergulent cache bundles. Not a software release."

echo "Publishing to release '$tag':"
publish_assets "$tag" "$bundle" "$signature"
