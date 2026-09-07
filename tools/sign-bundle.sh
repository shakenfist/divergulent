#!/bin/bash
#
# Sign a cache bundle with Sigstore keyless OIDC (sigstore-python), emitting
# <bundle>.sigstore.json beside it. Intended to run in CI, where an ambient
# GitHub Actions OIDC token is available (the job needs id-token: write);
# sigstore-python detects that token automatically. The client verifies the
# resulting signature against the workflow's identity.
set -euo pipefail

# shellcheck source=tools/release-lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/release-lib.sh"

bundle="${1:?usage: sign-bundle.sh <bundle-file>}"

# Keyless signing makes several network calls (PyPI for the install, then Fulcio
# for the certificate and Rekor for the transparency log); a brief network blip
# should not fail the whole build, so each network step goes through the shared
# retry() helper.
python3 -m venv sign-venv
retry 4 10 sign-venv/bin/pip install --quiet --upgrade pip
retry 4 10 sign-venv/bin/pip install --quiet 'sigstore>=4.3,<5'

# Keyless: no --identity-token, so sigstore-python uses the ambient CI OIDC.
# --overwrite lets a retry replace a partial signature from a failed attempt.
retry 4 15 sign-venv/bin/python -m sigstore sign --overwrite "$bundle"

echo "Signed bundle:"
ls -l "${bundle}.sigstore.json"
