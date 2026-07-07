#!/bin/sh
#
# A simple wrapper around flake8 which makes it possible
# to ask it to only verify files changed in the current
# git HEAD patch.
#
# Intended to be invoked via tox:
#
#   tox -eflake8 -- -HEAD
#
# Originally from the OpenStack project.

FLAKE_COMMAND="flake8 --max-line-length=120"

if test "$1" = "-HEAD" ; then
    shift
    # Only the Python files changed since HEAD~1 that still exist. Filtering to
    # *.py keeps flake8 from trying to parse markdown/yaml/shell as Python, and
    # skipping deleted paths avoids "file not found" noise.
    files=""
    for f in $(git diff --name-only HEAD~1); do
        case "$f" in
            *.py) test -f "$f" && files="${files} ${f}" ;;
        esac
    done
    if test -z "${files}" ; then
        echo "No changed Python files to check"
        exit 0
    fi
    echo "Running flake8 on${files}"
    # Word splitting of the file list is intentional (multiple filenames).
    # shellcheck disable=SC2086
    exec $FLAKE_COMMAND "$@" ${files}
else
    echo "Running flake8 on all files"
    exec $FLAKE_COMMAND "$@"
fi
