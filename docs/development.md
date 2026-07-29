# Development

Tests and linting run through `tox`:

```bash
tox -epy3      # unit tests (stestr + testtools)
tox -eflake8   # style checks on the current change
```

CI runs the same checks on push and pull requests
(`.github/workflows/unit-tests.yml`). A separate workflow
(`.github/workflows/sample-output.yml`) runs a full `divergulent score`
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
[RELEASE-SETUP.md](https://github.com/shakenfist/divergulent/blob/main/RELEASE-SETUP.md)
for the one-time configuration.

Process documents live at the repository root:
[PLAN-TEMPLATE.md](https://github.com/shakenfist/divergulent/blob/main/PLAN-TEMPLATE.md)
(the starting point for new plan files) and
[PUSH-AUDIT.md](https://github.com/shakenfist/divergulent/blob/main/PUSH-AUDIT.md)
(the pre-push audit runbook). For build, test, and style conventions
see
[AGENTS.md](https://github.com/shakenfist/divergulent/blob/main/AGENTS.md);
for a module-by-module tour of the code see
[ARCHITECTURE.md](https://github.com/shakenfist/divergulent/blob/main/ARCHITECTURE.md).
