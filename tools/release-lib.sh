# shellcheck shell=bash
#
# Shared helpers for the release-asset scripts: retrying flaky network calls,
# installing the pinned gh CLI, and confirming that what we published is
# actually downloadable. Sourced, never executed:
#
#     . "$(dirname "${BASH_SOURCE[0]}")/release-lib.sh"
#
# Callers are expected to `set -euo pipefail` themselves.

# Pinned gh version to install if the runner does not already provide it. We
# upload with `gh release upload --clobber`, which overwrites an existing asset
# of the same name in place -- exactly what a rolling release needs.
GH_VERSION="2.62.0"

# Retry a command with exponential backoff. Every call these scripts make is a
# network call against github.com, and a brief blip should not fail the whole
# build -- still less leave a rolling release half-updated (see publish_assets).
# Locals are underscore-prefixed because bash scopes them dynamically: a plain
# `local n` here would shadow an `n` inside a retried shell function.
retry() {
    local _retry_attempts="$1" _retry_delay="$2"
    shift 2
    local _retry_n=1
    until "$@"; do
        if [ "$_retry_n" -ge "$_retry_attempts" ]; then
            echo "ERROR: still failing after $_retry_n attempts: $*" >&2
            return 1
        fi
        echo "Attempt $_retry_n/$_retry_attempts failed; retrying in ${_retry_delay}s: $*" >&2
        sleep "$_retry_delay"
        _retry_n=$((_retry_n + 1))
        _retry_delay=$((_retry_delay * 2))
    done
}

# Ensure the gh CLI is available, downloading the pinned release and verifying
# it against GitHub's published checksums if absent (the same pattern
# release.yml uses for gitsign).
ensure_gh() {
    if command -v gh >/dev/null 2>&1; then
        return 0
    fi
    echo "gh not found; installing gh ${GH_VERSION}..." >&2
    local dir="gh_${GH_VERSION}_linux_amd64"
    retry 4 10 curl -sSLO "https://github.com/cli/cli/releases/download/v${GH_VERSION}/${dir}.tar.gz"
    retry 4 10 curl -sSLO "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_checksums.txt"
    sha256sum --ignore-missing -c "gh_${GH_VERSION}_checksums.txt"
    tar -xzf "${dir}.tar.gz"
    sudo install -m 0755 "${dir}/bin/gh" /usr/local/bin/gh
    rm -rf "${dir}.tar.gz" "${dir}" "gh_${GH_VERSION}_checksums.txt"
}

# Create the rolling prerelease if it does not exist yet.
#
# These tags are deliberately prereleases -- they are auto-updated data, and
# must never shadow the repository's "latest" release, which is reserved for
# software versions.
ensure_rolling_release() {
    local tag="$1" title="$2" notes="$3"
    if gh release view "$tag" >/dev/null 2>&1; then
        return 0
    fi
    retry 3 5 gh release create "$tag" --prerelease --title "$title" --notes "$notes"
}

# The owner/name slug for the repository we are publishing to.
release_repo() {
    if [ -n "${GITHUB_REPOSITORY:-}" ]; then
        echo "$GITHUB_REPOSITORY"
        return 0
    fi
    gh repo view --json nameWithOwner --jq .nameWithOwner
}

# Confirm every named asset is actually downloadable from its public URL.
#
# `gh release upload` exiting 0 is not proof that clients can fetch the asset,
# and these rolling releases have exactly one job: serve a stable URL. Checking
# the URL a client would really use turns a broken publish into a red build
# instead of a 404 discovered by a user the next morning.
verify_published() {
    local tag="$1"
    shift
    local repo name url
    repo="$(release_repo)"
    for name in "$@"; do
        name="$(basename "$name")"
        url="https://github.com/${repo}/releases/download/${tag}/${name}"
        # A just-uploaded asset can take a moment to become servable, so allow
        # a few attempts before calling it broken.
        if ! retry 4 5 curl -fsSL --head -o /dev/null "$url"; then
            echo "ERROR: uploaded ${name} but ${url} is not downloadable." >&2
            return 1
        fi
        echo "  verified ${url}"
    done
}

# Upload assets to a rolling release, then prove they are fetchable.
#
# `gh release upload --clobber` deletes the existing asset before writing its
# replacement, so a single failed API call can leave the release with the old
# asset deleted and the new one never uploaded. That is precisely how the
# 'cache' release lost cache-trixie.json.gz on 2026-09-06 -- a 502 on the
# delete, after which every `divergulent cache pull` 404ed until the next day's
# build. Retrying closes that window (the upload is idempotent, so a retry
# after a partial failure simply completes it), and verifying afterwards means
# we never report success over a release that is actually broken.
publish_assets() {
    local tag="$1"
    shift
    retry 4 10 gh release upload "$tag" "$@" --clobber
    verify_published "$tag" "$@"
}
