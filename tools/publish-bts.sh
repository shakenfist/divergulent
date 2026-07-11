#!/bin/bash
#
# Publish the BTS bug index to the rolling 'bts' GitHub prerelease, in place, so
# clients pull it from a stable URL
# (.../releases/download/bts/bts-index.tsv.gz). Run in CI with a GITHUB_TOKEN that
# has contents: write. Unsigned public data -- a pure function of UDD, regenerable
# at will -- so unlike the cache/classification bundles there is no signature.
set -euo pipefail

# Pinned gh version to install if the runner does not already provide it. We upload
# with `gh release upload --clobber`, which overwrites the previous asset in place.
GH_VERSION="2.62.0"

asset="${1:?usage: publish-bts.sh <bts-index.tsv.gz>}"
tag="bts"

# Refuse to publish a suspiciously small asset (never overwrite good data with a bad
# pull). ~1.1M rows gzip to a few MB, so a ~1MB floor catches a truncation.
MIN_BYTES="${MIN_BYTES:-1000000}"

# Ensure the gh CLI is available, downloading the pinned release and verifying it
# against GitHub's published checksums if absent.
ensure_gh() {
    if command -v gh >/dev/null 2>&1; then
        return 0
    fi
    echo "gh not found; installing gh ${GH_VERSION}..." >&2
    local dir="gh_${GH_VERSION}_linux_amd64"
    curl -sSLO "https://github.com/cli/cli/releases/download/v${GH_VERSION}/${dir}.tar.gz"
    curl -sSLO "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_checksums.txt"
    sha256sum --ignore-missing -c "gh_${GH_VERSION}_checksums.txt"
    tar -xzf "${dir}.tar.gz"
    sudo install -m 0755 "${dir}/bin/gh" /usr/local/bin/gh
    rm -rf "${dir}.tar.gz" "${dir}" "gh_${GH_VERSION}_checksums.txt"
}

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

# The 'bts' release is a rolling, auto-updated prerelease -- it must never be the
# repository's "latest" release (reserved for software versions).
if ! gh release view "$tag" >/dev/null 2>&1; then
    gh release create "$tag" --prerelease \
        --title "BTS bug index" \
        --notes "Rolling, auto-updated Debian BTS bug index (bug -> source, status) for divergulent's phase-6 patch cross-reference. Not a software release."
fi

gh release upload "$tag" "$asset" --clobber

echo "Published to release '$tag':"
echo "  $asset"
