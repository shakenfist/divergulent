#!/bin/bash
#
# Build a precomputed divergulent cache bundle for the whole Debian archive on
# the release this runs on. Used by CI on a Debian 13 runner to measure the real
# bundle size and build time; runnable locally too. Honours DIVERGULENT_CACHE_DIR
# so a restored cache makes the divergence half incremental (immutable patch
# sets are fetched once, then only new versions on later runs).
set -euo pipefail

output="${1:-cache-bundle/cache-debian13.json.gz}"
mkdir -p "$(dirname "$output")"

# Enable deb-src so the builder can enumerate every source package. Debian 13
# ships the deb822-format debian.sources; add deb-src to its Types if absent.
if [ -f /etc/apt/sources.list.d/debian.sources ]; then
    sudo sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources
fi
sudo apt-get update

# Build and install divergulent into a throwaway venv.
python3 -m venv build-venv
build-venv/bin/pip install --quiet --upgrade pip
build-venv/bin/pip install --quiet .
div="build-venv/bin/divergulent"

# The whole point of this phase: measured build time and bundle size.
start=$(date +%s)
"$div" cache build --output "$output"
end=$(date +%s)

echo "Cache build took $((end - start))s"
echo "Bundle size (gzipped):"
ls -l "$output"
