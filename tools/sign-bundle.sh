#!/bin/bash
#
# Sign a cache bundle with Sigstore keyless OIDC (sigstore-python), emitting
# <bundle>.sigstore.json beside it. Intended to run in CI, where an ambient
# GitHub Actions OIDC token is available (the job needs id-token: write);
# sigstore-python detects that token automatically. The client verifies the
# resulting signature against the workflow's identity.
set -euo pipefail

bundle="${1:?usage: sign-bundle.sh <bundle-file>}"

python3 -m venv sign-venv
sign-venv/bin/pip install --quiet --upgrade pip
sign-venv/bin/pip install --quiet 'sigstore>=4.3,<5'

# Keyless: no --identity-token, so sigstore-python uses the ambient CI OIDC.
sign-venv/bin/python -m sigstore sign --overwrite "$bundle"

echo "Signed bundle:"
ls -l "${bundle}.sigstore.json"
