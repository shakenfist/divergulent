#!/bin/bash
#
# Build the published patch-classification bundle from a committed ledger JSONL
# export. Unlike the divergence cache (which sweeps the whole archive and needs
# deb-src), this is pure Python over a text file: import the export into a
# throwaway sqlite ledger, then project it down to the lean, signable bundle.
# The export is the irreproducible source of truth (human + verified-LLM
# verdicts); this script never regenerates it, only republishes from it.
set -euo pipefail

output="${1:?usage: build-classification.sh <output.json.gz> <ledger-export-dir> <release>}"
export_dir="${2:?usage: build-classification.sh <output.json.gz> <ledger-export-dir> <release>}"
release="${3:-unknown}"
mkdir -p "$(dirname "$output")"

if [ ! -f "$export_dir/manifest.json" ]; then
    echo "ERROR: ledger export dir '$export_dir' has no manifest.json." >&2
    exit 1
fi

# Build and install divergulent into a throwaway venv.
python3 -m venv build-venv
build-venv/bin/pip install --quiet --upgrade pip
build-venv/bin/pip install --quiet .

workdir="$(mktemp -d)"
ledger="$workdir/ledger.sqlite"

# Import (sharded JSONL dir -> sqlite) then build (sqlite -> lean gzipped-JSON
# bundle). Both reuse the tested module mains directly, so no data-root discovery.
build-venv/bin/python -m divergulent.classify.export import "$export_dir" --ledger "$ledger"
build-venv/bin/python -m divergulent.classify.classification_bundle "$ledger" \
    --release "$release" --output "$output"

echo "Built classification bundle:"
ls -l "$output"
