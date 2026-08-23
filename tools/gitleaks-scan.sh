#!/bin/bash

# Scan this repository's git history for leaked credentials.
#
# Two things happen here, and the second is the more important one:
#
# 1. gitleaks scans every commit reachable from HEAD -- which on a pull
#    request means the whole of develop plus the branch under test --
#    against .gitleaks.toml, and the script fails if anything is found.
#
# 2. A positive control proves the scanner can still fire. A detector
#    which reports nothing is indistinguishable from a detector which is
#    broken, and .gitleaks.toml carries an allowlist which could in
#    principle grow until it forgives everything. That allowlist is
#    aimed squarely at the private-key rule, so a real private key is
#    planted in a scratch directory and the scan fails if gitleaks does
#    not report it. Green here means "scanned and found nothing", not
#    "did nothing".
#
# Reachability from HEAD, rather than gitleaks' default of every ref, is
# deliberate. Scanning every ref is not what anyone means by "scan this
# project's history", and 8.16 misattributes the extra findings to
# unrelated merge commits, so they cannot be triaged by commit either.
#
# Usage:
#   tools/gitleaks-scan.sh [--gitleaks PATH]
#
# Runs from anywhere inside the working tree -- it changes to the top
# itself -- but the clone must be a full one, not shallow.

set -e

GITLEAKS=gitleaks
while [ $# -gt 0 ]; do
    case "$1" in
        --gitleaks)
            if [ -z "$2" ]; then
                echo "--gitleaks needs a path."
                exit 1
            fi
            GITLEAKS="$2"
            shift 2
            ;;
        *)
            # Refuse rather than ignore. A silently discarded flag would
            # leave the caller believing they had changed the scan.
            echo "Unrecognised argument: $1"
            echo "Usage: tools/gitleaks-scan.sh [--gitleaks PATH]"
            exit 1
            ;;
    esac
done

if ! command -v "$GITLEAKS" >/dev/null 2>&1 && [ ! -x "$GITLEAKS" ]; then
    echo "gitleaks not found. Install it, or pass --gitleaks PATH."
    exit 1
fi

# .gitleaks.toml is named relative to the working directory. Run from a
# subdirectory -- the obvious thing to do when reproducing a CI failure
# -- it would simply not be found, and the scan would silently run
# against the stock rules with none of the accepted findings recorded.
cd "$(git rev-parse --show-toplevel)"

if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
    echo "This is a shallow clone, so most of history cannot be scanned."
    echo "Check out with fetch-depth: 0."
    exit 1
fi

# The positive control. The key is generated here rather than written
# into this file, because a literal one would be found by the real scan
# below -- correctly, since a credential in a committed file is exactly
# what we are looking for.
CONTROL=$(mktemp -d)
trap 'rm -rf "$CONTROL"' EXIT

ssh-keygen -q -t rsa -b 2048 -N '' -C control@example.com \
    -f "$CONTROL/id_rsa"

echo "Positive control: a private key planted in a scratch directory."
set +e
"$GITLEAKS" detect --source "$CONTROL" --config .gitleaks.toml --no-git \
    --redact --no-banner --report-path "$CONTROL/report.json" \
    --report-format json
control_status=$?
set -e

found=$(python3 -c "
import json

with open('$CONTROL/report.json') as f:
    print(' '.join(sorted({x['RuleID'] for x in json.load(f)})), end='')
")

case " $found " in
    *" private-key "*) ;;
    *)
        echo
        echo "The positive control failed: gitleaks did not report the"
        echo "private-key rule against a key planted for it to find."
        echo "Rules which did fire: ${found:-none}."
        echo
        echo "Do not trust a clean scan until this passes. Check the"
        echo "allowlist in .gitleaks.toml -- one wide enough to swallow"
        echo "the control is wide enough to swallow a real credential."
        exit 1
        ;;
esac

if [ $control_status -eq 0 ]; then
    echo "The positive control did not set a failure exit code."
    exit 1
fi

echo "Positive control passed: the planted key was reported."
echo

# The real scan.
echo "Scanning every commit reachable from HEAD."
"$GITLEAKS" detect --source . --config .gitleaks.toml --log-opts="HEAD" \
    --redact --verbose --no-banner
