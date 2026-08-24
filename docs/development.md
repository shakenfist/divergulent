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

CI runs `pre-commit run --all-files` on push and pull requests
(`.github/workflows/unit-tests.yml`), which is what makes the hooks
enforced rather than advisory: a commit made with `--no-verify`, or
from a clone that never ran `pre-commit install`, is still checked.

Once CI passes on a pull request, `shakenfist-bot` posts an automated
review. The reviewer itself lives in
[shakenfist/actions](https://github.com/shakenfist/actions); this
repository only carries the calling job at the bottom of
`unit-tests.yml`, whose `needs:` list is the CI-passed gate. It reviews
a pull request once. Two bot commands are available to anyone with
write access, as comments on the pull request:

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
bundle. Software releases are tag-driven
(`v*`) and publish to PyPI via Sigstore-signed tags and PyPI trusted
publishing — see
[RELEASE-SETUP.md](https://github.com/shakenfist/divergulent/blob/develop/RELEASE-SETUP.md)
for the one-time configuration.

Process documents live at the repository root:
[PLAN-TEMPLATE.md](https://github.com/shakenfist/divergulent/blob/develop/PLAN-TEMPLATE.md)
(the starting point for new plan files) and
[PUSH-AUDIT.md](https://github.com/shakenfist/divergulent/blob/develop/PUSH-AUDIT.md)
(the pre-push audit runbook). For build, test, and style conventions
see
[AGENTS.md](https://github.com/shakenfist/divergulent/blob/develop/AGENTS.md);
for a module-by-module tour of the code see
[ARCHITECTURE.md](https://github.com/shakenfist/divergulent/blob/develop/ARCHITECTURE.md).
