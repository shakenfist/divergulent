# Development

Tests and linting run through `tox`:

```bash
tox -epy3      # unit tests (stestr + testtools)
tox -eflake8   # style checks on the current change
```

Everything the project gates on is a pre-commit hook — actionlint over
the workflows, shellcheck over `tools/`, skillsaw over the agent
context files, and the two `tox` environments above — so the local
run is the same gate CI applies:

```bash
pre-commit install       # once, per clone
pre-commit run --all-files
```

CI runs `pre-commit run --all-files` on pull requests and merge queue
entries (`.github/workflows/unit-tests.yml`), which is what makes the
hooks enforced rather than advisory: a commit made with `--no-verify`,
or from a clone that never ran `pre-commit install`, is still checked.

### Merging

Pull requests merge through a merge queue on `develop`, not with the
merge button directly. A pull request can be queued once its "Can
enqueue" check passes. The queue then runs `unit-tests.yml` again on
the commit it is about to merge, and merges only when "Can merge"
passes. That second run is the one a release counts (see "Releasing"
below), so a change merged around the queue, by an administrator
bypassing it, cannot be released until a later commit has gone through
the queue.

The queue builds one entry at a time (a serial queue is what the
consistency audit's `merge-queue-config` criterion accepts, and
RELEASE-SETUP.md describes the ruleset). `unit-tests.yml` has no
`concurrency` groups today, so nothing depends on that yet. A group
added to a job that a queue run reaches must key a first attempt on
`github.event.merge_group.base_ref` and a re-run on `github.ref`, as
client-python-ova's `ci.yml` does, and it would then depend on the
queue being serial: raising the build concurrency would make each
entry cancel the one ahead of it. A re-run keeps its original event,
so the `github.run_attempt == '1'` test is what stops a "Re-run jobs"
on an old queue run from cancelling the live entry or being cancelled
by it.

The two checks are the `can-enqueue` and `can-merge` jobs at the bottom
of `unit-tests.yml`. A job added to that workflow goes in the `needs:`
of both, or the queue will not wait for it. The automated reviewer
stays out of them: it is advisory and must not block a merge.

### Automated review

Once CI passes on a pull request, `shakenfist-bot` posts an automated
review. The reviewer itself lives in
[shakenfist/actions](https://github.com/shakenfist/actions); this
repository only carries the calling job in `unit-tests.yml`, just
above `can-enqueue` and `can-merge`. Its `needs:` list is the
CI-passed gate. It reviews a pull request once. Two bot commands are
available to anyone with write access, as comments on the pull
request:

| Comment | Effect |
|---------|--------|
| `@shakenfist-bot please re-review` | Request a fresh review of a pull request the bot has already reviewed |
| `@shakenfist-bot please retest` | Re-run the CI checks without pushing a commit |

Neither runs on a pull request from a fork: the reviewer runs Claude
Code with a write-capable token over a diff it did not write, so fork
pull requests are reviewed only on an explicit human request.

`.github/workflows/secret-scan.yml` scans the history for leaked
credentials with `gitleaks`, on pull requests and on pushes to
`develop`. It is not a pre-commit hook and it carries no path filter: a
credential pasted into a documentation example is a credential, so the
scan deliberately reads the prose a filter would skip. Reproduce it locally with `tools/gitleaks-scan.sh`, which runs
the same scan the workflow does — including the positive control that
plants a key and fails if `gitleaks` does not report it, so a clean
result means the scanner ran rather than that it was broken. Accepted
findings go in `.gitleaks.toml` when the content recurs (a test
fixture, a documentation placeholder) or in `.gitleaksignore` as a
fingerprint when a specific historical commit is being forgiven; never
suppress a finding for a credential that still authorises something.

`.github/workflows/sample-output.yml` runs a full `divergulent score`
on a Debian 13 runner and uploads the rendered report as a build
artifact, so reviewers can see how the output looks on a real machine
(and as a live end-to-end check). A scheduled workflow
(`.github/workflows/build-cache.yml`) builds the whole-archive bundle on
a Debian 13 runner (`tools/build-cache.sh`), signs it
(`tools/sign-bundle.sh`), and publishes it (`tools/publish-cache.sh`) to
the rolling `cache` prerelease daily — incremental each day, a full
rebuild weekly — so `divergulent cache pull` serves a fresh, signed
bundle. All three publish scripts share `tools/release-lib.sh`, which
retries the GitHub API calls and then confirms each published asset is
reachable at its public download URL, at the size of the file just
uploaded, before reporting success — `gh release upload --clobber`
deletes the old asset before writing the new one, so an unretried
failure mid-call can leave a rolling release with neither. Software
releases are tag-driven (`v*`) and publish to PyPI via Sigstore-signed
tags and PyPI trusted publishing — see
[RELEASE-SETUP.md](https://github.com/shakenfist/divergulent/blob/develop/RELEASE-SETUP.md)
for the one-time configuration.

### Releasing

Pushing a `v*` tag triggers `release.yml`. Its jobs, in order:

1. `ci-gate` checks that `unit-tests.yml` succeeded on the tagged
   commit, in its merge queue run. A run started by hand, as
   `pr-retest.yml` does with `workflow_dispatch`, does not count. A
   queue run that fails, flaky or not, ejects its entry, so the commit
   it built never reaches `develop` and re-running that run cannot make
   any tag pass: re-queue the pull request and tag the commit that
   merges. Re-running a queue run helps only when the tagged commit's
   run was re-run after it merged and that later attempt failed,
   because the gate judges each run at its latest attempt; then use
   "Re-run jobs" on it. How the gate decides is documented with the
   action, as
   [`require-ci-success`](https://github.com/shakenfist/actions/blob/main/docs/actions.md#require-ci-success).
   It has no override.
2. `build` builds the sdist and wheel, then runs `twine check` and
   [`check-dist`](https://github.com/shakenfist/actions/blob/main/docs/actions.md#check-dist),
   which fails if the wheel holds a test directory. It runs only if the
   gate passed.
3. The publishing jobs wait for approval in the `release`
   environment, re-sign the tag with gitsign, and publish to PyPI with
   attestations.

So tag a commit that is on `develop` and went through the merge
queue, and wait for its `unit-tests.yml` run to finish first. A gate
failure means nothing was signed or published, so every failure is
recoverable: the template's
[recovery table](https://github.com/shakenfist/development/blob/main/templates/release-automation/README.md#recovering-from-a-gate-failure)
says what to do for each cause, which is usually moving the tag or
re-running CI and then the `ci-gate` job.

Running `release.yml` by hand with `workflow_dispatch` skips the gate
and the publishing jobs, and only builds and checks the packages.

The wheel ships the `divergulent` package and its subpackages but not
`divergulent.tests`, and ships no data files: nothing in the package
reads one at runtime. `[tool.setuptools] include-package-data` is
`false` in `pyproject.toml` because setuptools_scm would otherwise
offer every tracked file, tests and fixtures included, as package data.
A data file the code starts to load needs an explicit `package-data`
entry.

Process documents live at the repository root:
[PLAN-TEMPLATE.md](https://github.com/shakenfist/divergulent/blob/develop/PLAN-TEMPLATE.md)
(the starting point for new plan files) and
[PUSH-AUDIT.md](https://github.com/shakenfist/divergulent/blob/develop/PUSH-AUDIT.md)
(the pre-push audit runbook). For build, test, and style conventions
see
[AGENTS.md](https://github.com/shakenfist/divergulent/blob/develop/AGENTS.md);
for a module-by-module tour of the code see
[ARCHITECTURE.md](https://github.com/shakenfist/divergulent/blob/develop/ARCHITECTURE.md).
