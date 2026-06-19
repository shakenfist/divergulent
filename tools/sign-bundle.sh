#!/bin/bash
#
# Sign a cache bundle with Sigstore keyless OIDC (sigstore-python), emitting
# <bundle>.sigstore.json beside it. Intended to run in CI, where an ambient
# GitHub Actions OIDC token is available (the job needs id-token: write);
# sigstore-python detects that token automatically. The client verifies the
# resulting signature against the workflow's identity.
set -euo pipefail

bundle="${1:?usage: sign-bundle.sh <bundle-file>}"

# Retry a command with exponential backoff. Keyless signing makes several
# network calls (PyPI for the install, then Fulcio for the certificate and
# Rekor for the transparency log); a brief network blip should not fail the
# whole build, so each network step is retried.
retry() {
    local attempts="$1" delay="$2"
    shift 2
    local n=1
    until "$@"; do
        if [ "$n" -ge "$attempts" ]; then
            echo "ERROR: still failing after $n attempts: $*" >&2
            return 1
        fi
        echo "Attempt $n/$attempts failed; retrying in ${delay}s: $*" >&2
        sleep "$delay"
        n=$((n + 1))
        delay=$((delay * 2))
    done
}

python3 -m venv sign-venv
retry 4 10 sign-venv/bin/pip install --quiet --upgrade pip
retry 4 10 sign-venv/bin/pip install --quiet 'sigstore>=4.3,<5'

# Keyless: no --identity-token, so sigstore-python uses the ambient CI OIDC.
# --overwrite lets a retry replace a partial signature from a failed attempt.
retry 4 15 sign-venv/bin/python -m sigstore sign --overwrite "$bundle"

echo "Signed bundle:"
ls -l "${bundle}.sigstore.json"
