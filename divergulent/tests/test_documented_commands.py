"""Every command spelled in docs/ must be one the CLI actually accepts.

This exists because a documented migration step was wrong in a way no test
could see: `divergulent-classify ledger supersede llm-triage:<model> 1`, the
ONLY published remedy for a stale generation of decisions, named a verb the
front CLI does not have. Prose is not executed, so the error survived review
of the code it described.

The guard is derived rather than enumerated: the verb list comes from
`cli._ALL_VERBS` and the subcommand list from the ledger module's own
argparse, so adding or renaming a verb cannot leave this test asserting a
stale vocabulary. Doc text is normalised across newlines first -- the
original defect had `divergulent-classify ledger` split over a line break,
which is exactly where a same-line grep stops looking.
"""
import io
import os
import re

import testtools

from divergulent.classify import cli as cli_mod
from divergulent.classify import ledger as ledger_mod


_DOCS_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'docs')

# The tracked prose. `docs/plans/` is deliberately excluded: a plan records what
# was true when it was written and is never edited afterwards, so holding it to
# the current vocabulary would fail on history rather than on a defect.
_TRACKED = ('deterministic-rules.md', 'development.md', 'index.md', 'patch-classification.md',
            'status.md', 'usage.md', 'what-is-divergulent.md', 'workflow.md')

_FRONT_CLI = re.compile(r'divergulent-classify\s+([a-z][a-z-]*)')
_LEDGER_MODULE = re.compile(r'python -m divergulent\.classify\.ledger\s+([a-z][a-z-]*)')


def _prose(name):
    """Read one doc with newlines flattened, so a wrapped command still reads as one."""
    with io.open(os.path.join(_DOCS_ROOT, name), encoding='utf-8') as f:
        return re.sub(r'\s+', ' ', f.read())


def _ledger_subcommands():
    """The ledger module's real subcommands, read off its own parser."""
    parser = ledger_mod._build_parser()
    for action in parser._subparsers._group_actions:
        if action.choices is not None:
            return set(action.choices)
    raise AssertionError('the ledger parser grew no subcommands')


class DocumentedCommandsTestCase(testtools.TestCase):
    def test_every_documented_front_cli_verb_exists(self):
        verbs = set(cli_mod._ALL_VERBS)
        for name in _TRACKED:
            for verb in _FRONT_CLI.findall(_prose(name)):
                self.assertIn(
                    verb, verbs,
                    'docs/%s spells `divergulent-classify %s`, which is not a verb the CLI '
                    'accepts (it has: %s)' % (name, verb, ', '.join(sorted(verbs))))

    def test_every_documented_ledger_subcommand_exists(self):
        # The doc form is `... ledger build|record|report|supersede`, so split the
        # alternation before checking each branch.
        subcommands = _ledger_subcommands()
        for name in _TRACKED:
            prose = _prose(name)
            for match in re.finditer(r'python -m divergulent\.classify\.ledger\s+([a-z|-]+)', prose):
                for sub in match.group(1).split('|'):
                    self.assertIn(
                        sub, subcommands,
                        'docs/%s spells `python -m divergulent.classify.ledger %s`, which is '
                        'not a subcommand (it has: %s)'
                        % (name, sub, ', '.join(sorted(subcommands))))

    def test_the_documented_supersede_invocation_parses(self):
        """The migration step from the security-routing change, run through the real parser.

        Asserting the verb exists is not enough: the published form also has to put
        the ledger path before the rule id, which is the half the original text got
        wrong the second time.
        """
        parser = ledger_mod._build_parser()
        args = parser.parse_args(['supersede', '/tmp/ledger.sqlite', 'llm-triage:some-model', '1'])
        self.assertEqual('/tmp/ledger.sqlite', args.ledger)
        self.assertEqual('llm-triage:some-model', args.rule_id)
        self.assertEqual(1, args.version)
