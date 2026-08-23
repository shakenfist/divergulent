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
from a clone that never ran `pre-commit install`, is still checked. A separate workflow
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
