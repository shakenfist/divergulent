#!/bin/bash
#
# Hermetic tests for tools/release-lib.sh. No network: `gh`, `curl` and `sleep`
# are stubbed on PATH, so this obeys the same offline rule as the Python tests.
#
# The shell half of the publish path is where the 2026-09-06 incident actually
# happened, and it carries the fiddly logic -- backoff arithmetic, 404 versus
# transient discrimination, header scraping, size comparison. Every case below
# corresponds to a way one of those has already been got wrong.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=tools/release-lib.sh
. "${HERE}/release-lib.sh"

STUB_WORK="$(mktemp -d)"
export STUB_WORK
trap 'rm -rf "${STUB_WORK}"' EXIT
BIN="${STUB_WORK}/bin"
mkdir -p "${BIN}"
PATH="${BIN}:${PATH}"
export GITHUB_REPOSITORY="example/repo"

failures=0
check() {
    if [ "$1" = "$2" ]; then
        echo "  PASS: $3"
    else
        echo "  FAIL: $3 (got '$1', want '$2')"
        failures=$((failures + 1))
    fi
}

contains() {
    case "$1" in
        *"$2"*) echo "  PASS: $3" ;;
        *) echo "  FAIL: $3 (in: $1)"; failures=$((failures + 1)) ;;
    esac
}

# Install a stub for $1, its body read from stdin. Stubs count invocations in
# ${STUB_WORK}/<name>.calls so a test can assert how many attempts happened.
# The body is a quoted heredoc: it is the stub's own runtime code, and nothing
# in it is meant to expand while this script is writing it.
stub() {
    {
        printf '#!/bin/bash\n'
        # SC2016 is the point: ${STUB_WORK} is written into the stub for the
        # stub to expand when it runs, not by us while writing it.
        # shellcheck disable=SC2016
        printf 'echo call >> "${STUB_WORK}/%s.calls"\n' "$1"
        cat
    } > "${BIN}/$1"
    chmod +x "${BIN}/$1"
    rm -f "${STUB_WORK}/$1.calls" "${STUB_WORK}/$1.n"
}

calls() { wc -l < "${STUB_WORK}/$1.calls" 2>/dev/null | tr -d ' ' || echo 0; }

# Backoff is stubbed away so the suite stays fast.
stub sleep <<'STUB'
exit 0
STUB

echo "== retry =="
retry 3 1 true >/dev/null 2>&1
check "$?" 0 "succeeds immediately"

stub flaky <<'STUB'
n=$(cat "${STUB_WORK}/flaky.n" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "${STUB_WORK}/flaky.n"
[ "$n" -ge 3 ]
STUB
retry 4 1 flaky >/dev/null 2>&1
check "$?" 0 "succeeds on a later attempt"
check "$(calls flaky)" 3 "stopped as soon as it succeeded"

stub always_bad <<'STUB'
exit 1
STUB
retry 3 1 always_bad >/dev/null 2>&1
check "$?" 1 "gives up after the attempt limit"
check "$(calls always_bad)" 3 "made exactly the attempts asked for"

# The HEAD dumps below mirror the real shape: github.com 302s to the CDN, and
# that redirect carries a Content-Length of its own for the short redirect body.
printf '12345' > "${STUB_WORK}/asset"

echo "== _verify_one_asset: the size comparison =="
stub curl <<'STUB'
printf 'HTTP/2 302 \r\nContent-Length: 20\r\n\r\nHTTP/2 200 \r\nContent-Length: 5\r\n'
STUB
_verify_one_asset "https://example/asset" "${STUB_WORK}/asset" 2>/dev/null
check "$?" 0 "passes when the final response length matches"

stub curl <<'STUB'
printf 'HTTP/2 302 \r\nContent-Length: 20\r\n\r\nHTTP/2 200 \r\nContent-Length: 99\r\n'
STUB
msg=$(_verify_one_asset "https://example/asset" "${STUB_WORK}/asset" 2>&1)
check "$?" 1 "fails when the final response length differs"
contains "${msg}" "is 99 bytes, expected 5" "names both sizes"

# The regression that motivated this file: only the 302 carries a length. The
# accumulator must not inherit it, or a chunked final hop fails a good asset.
stub curl <<'STUB'
printf 'HTTP/2 302 \r\nContent-Length: 20\r\n\r\nHTTP/2 200 \r\nTransfer-Encoding: chunked\r\n'
STUB
_verify_one_asset "https://example/asset" "${STUB_WORK}/asset" 2>/dev/null
check "$?" 0 "does NOT inherit the redirect hop's Content-Length"

stub curl <<'STUB'
exit 22
STUB
_verify_one_asset "https://example/asset" "${STUB_WORK}/asset" 2>/dev/null
check "$?" 1 "fails when the fetch itself fails"

echo "== verify_published: retries cover the size check too =="
# First attempt serves the *previous* asset's length, the second the right one:
# ordinary eventual consistency after a clobber, not a broken publish.
stub curl <<'STUB'
n=$(cat "${STUB_WORK}/curl.n" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "${STUB_WORK}/curl.n"
if [ "$n" -eq 1 ]; then
    printf 'HTTP/2 200 \r\nContent-Length: 999\r\n'
else
    printf 'HTTP/2 200 \r\nContent-Length: 5\r\n'
fi
STUB
verify_published tag "${STUB_WORK}/asset" >/dev/null 2>&1
check "$?" 0 "a transient stale length is retried, not failed"

stub curl <<'STUB'
printf 'HTTP/2 200 \r\nContent-Length: 999\r\n'
STUB
out=$(verify_published tag "${STUB_WORK}/asset" 2>&1)
check "$?" 1 "a persistent mismatch still fails"
contains "${out}" "never settled" "reports that the asset never settled"

echo "== ensure_rolling_release: a 404 versus a blip =="
stub gh <<'STUB'
printf 'HTTP/2.0 200 OK\n'
exit 0
STUB
ensure_rolling_release tag "title" "notes" >/dev/null 2>&1
check "$?" 0 "an existing release is left alone"
check "$(calls gh)" 1 "did not try to create it"

stub gh <<'STUB'
case "$*" in
    *"-i "*) printf 'HTTP/2.0 404 Not Found\n'; exit 1 ;;
    *) exit 0 ;;
esac
STUB
ensure_rolling_release tag "title" "notes" >/dev/null 2>&1
check "$?" 0 "a genuine 404 creates the release"
check "$(calls gh)" 2 "looked once, then created"

stub gh <<'STUB'
printf 'HTTP/2.0 502 Bad Gateway\n'
exit 1
STUB
ensure_rolling_release tag "title" "notes" >/dev/null 2>&1
check "$?" 1 "a 5xx fails rather than guessing"
check "$(calls gh)" 3 "retried the probe and never called create"

# Item 1 of the round-three review: create is not idempotent, so an attempt
# whose response was lost leaves the release made and every retry answering
# 422. The re-probe must notice that and succeed.
stub gh <<'STUB'
n=$(cat "${STUB_WORK}/gh.n" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "${STUB_WORK}/gh.n"
case "$*" in
    *"-i "*)
        # Missing on the first look, present once create has (silently) run.
        if [ "$n" -eq 1 ]; then
            printf 'HTTP/2.0 404 Not Found\n'
            exit 1
        fi
        printf 'HTTP/2.0 200 OK\n'
        exit 0
        ;;
    *)
        exit 1  # create always reports failure
        ;;
esac
STUB
ensure_rolling_release tag "title" "notes" >/dev/null 2>&1
check "$?" 0 "a create whose response was lost is not treated as a failure"

echo "== publish_assets: the path every real publish takes =="
stub gh <<'STUB'
exit 0
STUB
stub curl <<'STUB'
printf 'HTTP/2 200 \r\nContent-Length: 5\r\n'
STUB
out=$(publish_assets tag "${STUB_WORK}/asset" 2>&1)
check "$?" 0 "a good upload verifies and succeeds"
contains "${out}" "verified" "verification actually ran, rather than being skipped"

echo "== publish_assets: the operator warning =="
stub gh <<'STUB'
exit 1
STUB
out=$(publish_assets tag "${STUB_WORK}/asset" 2>&1)
check "$?" 1 "an exhausted upload fails"
contains "${out}" "may be receiving 404s right now" "warns that clients are broken now"

echo
if [ "${failures}" -eq 0 ]; then
    echo "release-lib: all checks passed"
    exit 0
fi
echo "release-lib: ${failures} check(s) failed"
exit 1
