#!/bin/bash
#
# Publish a signed cache bundle to the rolling 'cache' GitHub prerelease, in
# place, so clients can pull it from a stable URL
# (.../releases/download/cache/cache-<release>.json.gz). Run in CI with a
# GITHUB_TOKEN that has contents: write. Uploads both the bundle and its
# Sigstore signature, overwriting the previous day's assets.
set -euo pipefail

bundle="${1:?usage: publish-cache.sh <bundle-file>}"
signature="${bundle}.sigstore.json"
tag="cache"

if [ ! -f "$signature" ]; then
    echo "ERROR: signature $signature not found; sign the bundle before publishing." >&2
    exit 1
fi

# The 'cache' release is a rolling, auto-updated prerelease -- it must never be
# the repository's "latest" release (that is reserved for software versions).
if ! gh release view "$tag" >/dev/null 2>&1; then
    gh release create "$tag" --prerelease \
        --title "Precomputed cache bundles" \
        --notes "Rolling, auto-updated signed divergulent cache bundles. Not a software release."
fi

gh release upload "$tag" "$bundle" "$signature" --clobber

echo "Published to release '$tag':"
echo "  $bundle"
echo "  $signature"
