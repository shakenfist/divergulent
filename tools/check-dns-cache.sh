#!/bin/bash
#
# Report whether the runner's local DNS cache (systemd-resolved) is active and
# actually serving cache hits. The cache is stood up by the CI runner's
# cloud-init (see shakenfist/private-ci) to stop bulk apt / source-archive work
# re-resolving the same hosts thousands of times and rate-limiting the upstream
# resolver. cloud-init output is not visible in the Actions log, so this runs as
# an early workflow step to surface the cache status where a human looking at a
# CI run will actually see it.
#
# This is a performance optimisation, not a hard dependency: a missing or
# ineffective cache warns loudly but never fails the job (exit 0 throughout), so
# the only thing that changes is whether the warning is visible.
set -uo pipefail

probe_host="${1:-deb.debian.org}"

if ! command -v resolvectl >/dev/null 2>&1; then
    echo "WARNING: resolvectl is not present, so there is no systemd-resolved"
    echo "         cache; DNS is resolving directly against the upstream resolver."
    exit 0
fi

if ! systemctl is-active --quiet systemd-resolved; then
    echo "WARNING: systemd-resolved is not active; DNS is falling back to the"
    echo "         upstream resolver and bulk lookups will not be cached. The"
    echo "         cloud-init resolver setup may have failed on this runner."
    exit 0
fi

echo "systemd-resolved is active. Resolver status:"
resolvectl status | sed -n '1,12p'
echo

# Two lookups of the same host within its TTL: the second must be a cache hit if
# caching is working. Compare the hit counter before and after to prove it,
# rather than merely confirming the service is up.
read_hits() {
    resolvectl statistics 2>/dev/null \
        | awk -F: '/Cache Hits/ { gsub(/[^0-9]/, "", $2); print $2; exit }'
}

hits_before="$(read_hits)"
hits_before="${hits_before:-0}"
resolvectl query "${probe_host}" >/dev/null 2>&1 || true
resolvectl query "${probe_host}" >/dev/null 2>&1 || true
hits_after="$(read_hits)"
hits_after="${hits_after:-0}"

echo "Cache hits: ${hits_before} -> ${hits_after} after two lookups of ${probe_host}."
if [ "${hits_after}" -gt "${hits_before}" ]; then
    echo "The local DNS cache is serving hits — caching is working correctly."
else
    echo "WARNING: the cache-hit count did not increase. The local cache may not"
    echo "         be effective; bulk lookups could still be hitting the upstream"
    echo "         resolver. (A failed lookup of ${probe_host} would also show this.)"
fi
echo
resolvectl statistics 2>/dev/null | grep -iE -A4 'cache' || true
exit 0
