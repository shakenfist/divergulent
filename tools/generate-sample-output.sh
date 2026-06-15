#!/bin/bash
#
# Generate a sample divergulent report for the machine this runs on, so
# reviewers can see how the output renders on a real system. Used by CI on a
# Debian 13 runner, but runnable locally. Honours DIVERGULENT_CACHE_DIR so the
# expensive first run can be cached.
set -euo pipefail

outdir="${1:-sample-output}"
mkdir -p "$outdir"

# Build and install divergulent into a throwaway venv.
python3 -m venv sample-venv
sample-venv/bin/pip install --quiet --upgrade pip
sample-venv/bin/pip install --quiet .
div="sample-venv/bin/divergulent"

header() {
    echo "# $1"
    echo "# $(uname -srm) | $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo
}

# Tier 1: the full, polite whole-machine score (no --limit). This is the point
# of the exercise -- a full run must be feasible and polite.
{ header 'divergulent score --all'; "$div" score --all; } \
    >"$outdir/score.txt" 2>"$outdir/score-summary.txt" || true
"$div" score --all --json >"$outdir/score.json" 2>/dev/null || true

# Tier 2: a small classified sample, only when deb-src indices are present.
# --classify downloads source packages, so it is bounded with --limit here.
# $(CREATED_BY) is an apt-get format placeholder, not a shell expansion.
# shellcheck disable=SC2016
if apt-get indextargets --format '$(CREATED_BY)' 2>/dev/null | grep -qx 'Sources'; then
    { header 'divergulent score --classify --all --limit 25 (sample)'; \
        "$div" score --classify --all --limit 25; } \
        >"$outdir/score-classified.txt" 2>"$outdir/score-classified-summary.txt" || true
fi

echo "Sample output written to $outdir/"
ls -l "$outdir"
