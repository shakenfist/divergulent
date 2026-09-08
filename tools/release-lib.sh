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
    # -f matters as much as the retry: without it curl exits 0 on a 404 or 502
    # and writes the error body into the file, so retry() sees success and the
    # run dies later at sha256sum instead of retrying the blip.
    retry 4 10 curl -fsSLO --connect-timeout 10 --max-time 300 \
        "https://github.com/cli/cli/releases/download/v${GH_VERSION}/${dir}.tar.gz"
    retry 4 10 curl -fsSLO --connect-timeout 10 --max-time 300 \
        "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_checksums.txt"
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
#
# A bare `gh release view` cannot tell "no such release" from "the API is
# having a moment", and answering the second as the first sends us to `gh
# release create` on a tag that already exists -- which fails deterministically
# and aborts the run before a single asset is uploaded. So ask for the release
# by tag and treat only a genuine 404 as missing.
ensure_rolling_release() {
    local tag="$1" title="$2" notes="$3"
    local repo status attempt
    repo="$(release_repo)"

    for attempt in 1 2 3; do
        # -i so the decision rests on the protocol status line rather than on
        # gh's human-readable error text, which carries no compatibility promise
        # across version bumps -- a rephrasing there would otherwise turn every
        # genuine 404 into a hard failure, and the first publish to a new tag
        # would never create it. The `|| true` matters under `set -e`: gh exits
        # non-zero on a 404, which is a case we handle rather than an error.
        status=$(gh api -i "repos/${repo}/releases/tags/${tag}" 2>/dev/null) || true
        status=${status%%$'\n'*}

        case "$status" in
            HTTP/*\ 2*)
                return 0
                ;;
            HTTP/*\ 404*)
                if retry 3 5 gh release create "$tag" --prerelease \
                        --title "$title" --notes "$notes"; then
                    return 0
                fi

                # Unlike `upload --clobber`, create is not idempotent. An attempt
                # that reached GitHub but whose response was lost leaves the
                # release made and every subsequent retry answering 422
                # already_exists -- so ask again before calling this a failure,
                # rather than aborting a publish whose release now exists.
                status=$(gh api -i "repos/${repo}/releases/tags/${tag}" 2>/dev/null) || true
                status=${status%%$'\n'*}
                case "$status" in
                    HTTP/*\ 2*)
                        return 0
                        ;;
                esac

                # The API also answers 404 when the token cannot see the
                # repository at all, so a failed create should say so.
                echo "ERROR: could not create release '${tag}'. Note that the 404 which" >&2
                echo "       sent us here also means a token without read access to" >&2
                echo "       ${repo}, not only a missing release." >&2
                return 1
                ;;
        esac

        echo "Attempt ${attempt}/3: cannot tell whether release '${tag}' exists (${status:-no response})" >&2
        if [ "$attempt" -lt 3 ]; then
            sleep 5
        fi
    done

    echo "ERROR: could not determine whether release '${tag}' exists; not publishing." >&2
    return 1
}

# The owner/name slug for the repository we are publishing to.
release_repo() {
    if [ -n "${GITHUB_REPOSITORY:-}" ]; then
        echo "$GITHUB_REPOSITORY"
        return 0
    fi
    retry 3 5 gh repo view --json nameWithOwner --jq .nameWithOwner
}

# Confirm every named asset is actually downloadable from its public URL, and
# is the size of the file we uploaded.
#
# `gh release upload` exiting 0 is not proof that clients can fetch the asset,
# and these rolling releases have exactly one job: serve a stable URL. Checking
# the URL a client would really use turns a broken publish into a red build
# instead of a 404 discovered by a user the next morning.
#
# A 200 alone only proves *something* answers to that name, so compare the
# advertised length against the local file: that also catches a truncated
# upload, and a stale asset left behind when a retry replaced only part of the
# set. A response carrying no content-length is not treated as a mismatch --
# a missing header is not evidence of a bad asset.
# One asset: is it there, and is it the size we uploaded? Both halves in one
# function so retry() can cover them together -- see verify_published.
_verify_one_asset() {
    local _va_url="$1" _va_path="$2"
    local _va_head _va_remote _va_local

    # Timeouts, not just -f: retry() can only re-run something that finishes.
    # A hung connection would otherwise stall the job past the upload.
    if ! _va_head=$(curl -fsSL --connect-timeout 10 --max-time 60 --head "$_va_url"); then
        return 1
    fi

    # -L means the redirect to the CDN is in this dump too, and github.com's 302
    # carries a content-length of its own for the short redirect body. Reset at
    # every status line so the value can only come from the response that
    # actually carried the asset, and is empty when that response had none --
    # otherwise a chunked final hop would silently inherit the redirect's length
    # and fail a publish that was fine.
    _va_remote=$(tr -d '\r' <<<"$_va_head" |
        awk '/^HTTP\// { n = "" } tolower($1) == "content-length:" { n = $2 } END { print n }')

    # Nothing local to compare against (a caller checking an asset it did not
    # just upload), or no content-length: presence is all we can assert, and a
    # missing header is not evidence of a bad asset.
    if [ ! -f "$_va_path" ] || [ -z "$_va_remote" ]; then
        return 0
    fi

    _va_local=$(stat -c%s "$_va_path")
    if [ "$_va_remote" != "$_va_local" ]; then
        echo "  ${_va_url} is ${_va_remote} bytes, expected ${_va_local}" >&2
        return 1
    fi
    return 0
}

verify_published() {
    local tag="$1"
    shift
    local repo path name url local_size
    repo="$(release_repo)"
    for path in "$@"; do
        name="$(basename "$path")"
        url="https://github.com/${repo}/releases/download/${tag}/${name}"

        # Retry the presence check and the size check together. A just-clobbered
        # asset can briefly serve a 404 *or* the previous asset's length, and
        # giving only the first of those any tolerance would turn ordinary
        # eventual consistency into a red build over a publish that worked --
        # false reds on the one trustworthy signal spend it fast.
        if ! retry 4 5 _verify_one_asset "$url" "$path"; then
            echo "ERROR: ${url} never settled: not downloadable, or never the size of ${path}." >&2
            return 1
        fi

        local_size=""
        if [ -f "$path" ]; then
            local_size="$(stat -c%s "$path")"
        fi
        echo "  verified ${url}${local_size:+ (${local_size} bytes)}"
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
    if ! retry 4 10 gh release upload "$tag" "$@" --clobber; then
        # Retrying narrows the delete-then-upload window; it does not close it.
        # Whoever reads this log needs to know clients are 404ing *now*, not at
        # the next scheduled build.
        echo "ERROR: upload to release '${tag}' failed. --clobber deletes an asset before" >&2
        echo "       replacing it, so the previous assets may already be gone and clients" >&2
        echo "       may be receiving 404s right now. Re-publish before relying on this" >&2
        echo "       release -- see the recovery steps in docs/classification-runbook.md." >&2
        return 1
    fi
    verify_published "$tag" "$@"
}
