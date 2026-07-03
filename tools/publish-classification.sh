#!/bin/bash
#
# Publish a signed classification bundle to the rolling 'classification' GitHub
# prerelease, in place, so clients can pull it from a stable URL
# (.../releases/download/classification/classification-<release>.json.gz). Run in
# CI with a GITHUB_TOKEN that has contents: write. Uploads both the bundle and
# its Sigstore signature, overwriting the previous assets.
set -euo pipefail

# Pinned gh version to install if the runner does not already provide it. We
# upload with `gh release upload --clobber`, which overwrites the previous
# assets in place -- exactly what a rolling release needs.
GH_VERSION="2.62.0"

bundle="${1:?usage: publish-classification.sh <bundle-file>}"
signature="${bundle}.sigstore.json"
tag="classification"

# Ensure the gh CLI is available, downloading the pinned release and verifying
# it against GitHub's published checksums if absent.
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

if [ ! -f "$signature" ]; then
    echo "ERROR: signature $signature not found; sign the bundle before publishing." >&2
    exit 1
fi

ensure_gh

# The 'classification' release is a rolling, auto-updated prerelease -- it must
# never be the repository's "latest" release (reserved for software versions).
if ! gh release view "$tag" >/dev/null 2>&1; then
    gh release create "$tag" --prerelease \
        --title "Patch classification bundles" \
        --notes "Rolling, auto-updated signed divergulent classification bundles. Not a software release."
fi

gh release upload "$tag" "$bundle" "$signature" --clobber

echo "Published to release '$tag':"
echo "  $bundle"
echo "  $signature"
