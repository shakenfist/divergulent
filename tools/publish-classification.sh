#!/bin/bash
#
# Publish a signed classification bundle to the rolling 'classification' GitHub
# prerelease, in place, so clients can pull it from a stable URL
# (.../releases/download/classification/classification-<release>.json.gz). Run in
# CI with a GITHUB_TOKEN that has contents: write. Uploads both the bundle and
# its Sigstore signature, overwriting the previous assets, and confirms both are
# downloadable before reporting success.
set -euo pipefail

# shellcheck source=tools/release-lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/release-lib.sh"

bundle="${1:?usage: publish-classification.sh <bundle-file>}"
signature="${bundle}.sigstore.json"
tag="classification"

if [ ! -f "$signature" ]; then
    echo "ERROR: signature $signature not found; sign the bundle before publishing." >&2
    exit 1
fi

ensure_gh

ensure_rolling_release "$tag" \
    "Patch classification bundles" \
    "Rolling, auto-updated signed divergulent classification bundles. Not a software release."

echo "Publishing to release '$tag':"
publish_assets "$tag" "$bundle" "$signature"
