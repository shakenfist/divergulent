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

# apt-get indextargets exposes a Sources target only when deb-src is enabled;
# this is exactly what divergulent's deb_src_available() checks. $(CREATED_BY) is
# an apt format placeholder, not a shell expansion.
# shellcheck disable=SC2016
sources_available() {
    apt-get indextargets --format '$(CREATED_BY)' 2>/dev/null | grep -qx Sources
}

# Print every apt source file, for diagnostics when enabling deb-src fails.
dump_apt_sources() {
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
        [ -f "$f" ] || continue
        echo "--- $f ---" >&2
        cat "$f" >&2 2>/dev/null || true
    done
}

# Enable deb-src for the Debian base repositories so the builder can enumerate
# every source package. Handles both apt source formats and the classic layout
# whether the Debian repos live in sources.list or sources.list.d/*.list, and
# only touches config when deb-src is not already available, so it is safe to
# re-run.
ensure_deb_src() {
    if sources_available; then
        echo "deb-src already enabled."
        return 0
    fi

    # deb822 (.sources): append deb-src to any Types: line that lacks it.
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then
        sudo sed -i '/^Types:/ { /deb-src/! s/$/ deb-src/ }' /etc/apt/sources.list.d/debian.sources
    fi

    # Classic layout: derive a deb-src line from each Debian-archive deb line in
    # sources.list and sources.list.d/*.list. Restrict to debian.org URIs so
    # third-party repos (which carry no source packages) do not get a deb-src
    # entry that would 404 and fail apt-get update.
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
        [ -f "$f" ] || continue
        sed -n '/debian\.org/ s/^deb \(.*\)/deb-src \1/p' "$f"
    done | sort -u | sudo tee /etc/apt/sources.list.d/divergulent-deb-src.list >/dev/null

    sudo apt-get update

    if ! sources_available; then
        echo "ERROR: deb-src is still not available after attempting to enable it." >&2
        dump_apt_sources
        exit 1
    fi
    echo "deb-src enabled."
}

ensure_deb_src

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
