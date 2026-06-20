"""Tests for divergulent.classify.content — ground-truth content profiling.

All tests are offline (no I/O, no network).  Coverage: file typing for every
type (with explicit emphasis on the code-vs-prose split — a ``*.1`` manpage is
doc, a ``*.c`` is code), multi-file mixed-type diffs, ``touches_code``, and each
trivial-only flag — including the phase-1 recurring-tail real examples (a
mode-only patch is ``is_empty``; a ``.gitignore`` adding ``.pc`` is
``ignore_file_only``; a whitespace reindent is ``whitespace_only``).
"""
import testtools

from divergulent.classify.content import (
    ContentProfile, CONTENT_RULE_VERSION, FILE_TYPES, code_added_lines, profile)
from divergulent.classify.content import _classify_file


# ---------------------------------------------------------------------------
# Diff fixture builders
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


# ---------------------------------------------------------------------------
# File typing — the code-vs-prose split is the load-bearing case.
# ---------------------------------------------------------------------------

class FileTypingTestCase(testtools.TestCase):

    def test_c_source_is_code(self):
        self.assertEqual('code', _classify_file('src/foo.c'))

    def test_header_is_code(self):
        self.assertEqual('code', _classify_file('include/foo.h'))

    def test_python_is_code(self):
        self.assertEqual('code', _classify_file('pkg/module.py'))

    def test_manpage_one_is_doc(self):
        # The motivating case: a *.1 manpage must type as doc, never code.
        self.assertEqual('doc', _classify_file('man/foo.1'))

    def test_manpage_section_three_with_suffix_is_doc(self):
        self.assertEqual('doc', _classify_file('foo.3pm'))

    def test_markdown_is_doc(self):
        self.assertEqual('doc', _classify_file('README.md'))

    def test_rst_is_doc(self):
        self.assertEqual('doc', _classify_file('guide.rst'))

    def test_doc_tree_txt_is_doc(self):
        self.assertEqual('doc', _classify_file('docs/install.txt'))

    def test_bare_txt_outside_doc_tree_is_data(self):
        # .txt is too generic to call doc on its own.
        self.assertEqual('data', _classify_file('strings.txt'))

    def test_readme_basename_is_doc(self):
        self.assertEqual('doc', _classify_file('README'))

    def test_changelog_is_doc(self):
        self.assertEqual('doc', _classify_file('ChangeLog'))

    def test_configure_ac_is_build(self):
        self.assertEqual('build', _classify_file('configure.ac'))

    def test_makefile_is_build(self):
        self.assertEqual('build', _classify_file('Makefile'))

    def test_makefile_am_is_build(self):
        self.assertEqual('build', _classify_file('src/Makefile.am'))

    def test_cmakelists_is_build(self):
        self.assertEqual('build', _classify_file('CMakeLists.txt'))

    def test_m4_macro_is_build(self):
        self.assertEqual('build', _classify_file('m4/ax_check.m4'))

    def test_debian_tree_is_build(self):
        # Debian packaging is build, even a .py under debian/.
        self.assertEqual('build', _classify_file('debian/rules'))

    def test_debian_tree_overrides_code_extension(self):
        self.assertEqual('build', _classify_file('debian/helper.py'))

    def test_json_is_data(self):
        self.assertEqual('data', _classify_file('config.json'))

    def test_po_is_data(self):
        self.assertEqual('data', _classify_file('po/de.po'))

    def test_tests_dir_is_test(self):
        self.assertEqual('test', _classify_file('tests/foo.c'))

    def test_test_prefix_basename_is_test(self):
        self.assertEqual('test', _classify_file('pkg/test_module.py'))

    def test_test_suffix_basename_is_test(self):
        self.assertEqual('test', _classify_file('pkg/module_test.go'))

    def test_dot_t_is_test(self):
        self.assertEqual('test', _classify_file('t/basic.t'))

    def test_spec_dir_is_test(self):
        self.assertEqual('test', _classify_file('spec/widget_spec.rb'))

    def test_contests_is_not_test_dir(self):
        # Whole-component match: 'contests' must not be read as 'test'.
        self.assertEqual('code', _classify_file('contests/foo.c'))

    def test_test_dir_beats_code_extension(self):
        # A test source file is test, not code: touching it is not a code change.
        self.assertEqual('test', _classify_file('tests/helpers.py'))


# ---------------------------------------------------------------------------
# Line / hunk counting and per-type counts
# ---------------------------------------------------------------------------

class CountingTestCase(testtools.TestCase):

    def test_single_edit_counts(self):
        prof = profile(_edit('src/foo.c'))
        self.assertEqual(1, prof.added_lines)
        self.assertEqual(1, prof.removed_lines)
        self.assertEqual(1, prof.hunks)
        self.assertEqual([('src/foo.c', 'code')], prof.files)
        self.assertEqual({'code': 1}, prof.file_types)

    def test_headers_are_not_counted_as_changes(self):
        # The +++/--- header lines must not inflate added/removed counts.
        prof = profile(_edit('src/foo.c'))
        self.assertEqual(1, prof.added_lines)
        self.assertEqual(1, prof.removed_lines)

    def test_per_type_line_counts(self):
        diff = _edit('src/foo.c') + _edit('README.md', removed='a', added='b')
        prof = profile(diff)
        self.assertEqual({'code': 1, 'doc': 1}, prof.added_by_type)
        self.assertEqual({'code': 1, 'doc': 1}, prof.removed_by_type)

    def test_multi_hunk_counts(self):
        diff = (
            '--- a/src/foo.c\n'
            '+++ b/src/foo.c\n'
            '@@ -1,2 +1,2 @@\n'
            '-a\n'
            '+b\n'
            '@@ -10,2 +10,3 @@\n'
            ' ctx\n'
            '+c\n'
            '+d\n'
        )
        prof = profile(diff)
        self.assertEqual(2, prof.hunks)
        self.assertEqual(3, prof.added_lines)
        self.assertEqual(1, prof.removed_lines)


# ---------------------------------------------------------------------------
# Multi-file mixed types and touches_code
# ---------------------------------------------------------------------------

class MultiFileTestCase(testtools.TestCase):

    def test_mixed_types_counted(self):
        diff = (
            _edit('src/foo.c')
            + _edit('README.md', removed='a', added='b')
            + _edit('debian/rules', removed='c', added='d')
            + _edit('data.json', removed='1', added='2')
        )
        prof = profile(diff)
        self.assertEqual(
            {'code': 1, 'doc': 1, 'build': 1, 'data': 1}, prof.file_types)
        self.assertTrue(prof.touches_code)

    def test_touches_code_false_for_doc_only(self):
        diff = _edit('README.md', removed='a', added='b')
        prof = profile(diff)
        self.assertFalse(prof.touches_code)
        self.assertEqual({'doc': 1}, prof.file_types)

    def test_manpage_and_c_file_both_present_and_touches_code(self):
        # The explicit requirement: a manpage + a .c file; both types present
        # and touches_code is True (the prose mention must not mask the code).
        diff = (
            _edit('man/foo.1', removed='.B old', added='.B new')
            + _edit('src/foo.c', removed='int x;', added='int y;')
        )
        prof = profile(diff)
        self.assertTrue(prof.touches_code)
        self.assertIn('doc', prof.file_types)
        self.assertIn('code', prof.file_types)
        self.assertEqual(
            {('man/foo.1', 'doc'), ('src/foo.c', 'code')}, set(prof.files))


# ---------------------------------------------------------------------------
# is_empty — phase-1 recurring tail: permission-/mode-only patches.
# ---------------------------------------------------------------------------

class IsEmptyTestCase(testtools.TestCase):

    def test_mode_only_change_is_empty(self):
        # A git mode change with no hunks normalises to empty.
        diff = (
            'diff --git a/script.sh b/script.sh\n'
            'old mode 100644\n'
            'new mode 100755\n'
        )
        prof = profile(diff)
        self.assertTrue(prof.is_empty)
        self.assertEqual(0, prof.added_lines)
        self.assertEqual(0, prof.removed_lines)

    def test_real_change_is_not_empty(self):
        prof = profile(_edit('src/foo.c'))
        self.assertFalse(prof.is_empty)


# ---------------------------------------------------------------------------
# ignore_file_only — phase-1 recurring tail: .gitignore adding .pc / _build.
# ---------------------------------------------------------------------------

class IgnoreFileOnlyTestCase(testtools.TestCase):

    def test_gitignore_adding_pc_is_ignore_file_only(self):
        diff = (
            '--- a/.gitignore\n'
            '+++ b/.gitignore\n'
            '@@ -1,2 +1,4 @@\n'
            ' *.o\n'
            ' *.lo\n'
            '+.pc\n'
            '+_build\n'
        )
        prof = profile(diff)
        self.assertTrue(prof.ignore_file_only)
        self.assertEqual({'data': 1}, prof.file_types)

    def test_gitignore_plus_code_is_not_ignore_file_only(self):
        diff = (
            '--- a/.gitignore\n'
            '+++ b/.gitignore\n'
            '@@ -1 +1,2 @@\n'
            ' *.o\n'
            '+.pc\n'
        ) + _edit('src/foo.c')
        prof = profile(diff)
        self.assertFalse(prof.ignore_file_only)

    def test_ignore_line_with_whitespace_is_not_ignore_file_only(self):
        # An interior-whitespace line is not a typical ignore pattern.
        diff = (
            '--- a/.gitignore\n'
            '+++ b/.gitignore\n'
            '@@ -1 +1,2 @@\n'
            ' *.o\n'
            '+rm -rf /\n'
        )
        prof = profile(diff)
        self.assertFalse(prof.ignore_file_only)


# ---------------------------------------------------------------------------
# whitespace_only — phase-1 recurring tail: a reindent.
# ---------------------------------------------------------------------------

class WhitespaceOnlyTestCase(testtools.TestCase):

    def test_reindent_is_whitespace_only(self):
        diff = (
            '--- a/src/foo.c\n'
            '+++ b/src/foo.c\n'
            '@@ -1,2 +1,2 @@\n'
            '-    return x;\n'
            '+\treturn x;\n'
            '-  if (y) {\n'
            '+    if (y) {\n'
        )
        prof = profile(diff)
        self.assertTrue(prof.whitespace_only)

    def test_real_content_change_is_not_whitespace_only(self):
        prof = profile(_edit('src/foo.c'))
        self.assertFalse(prof.whitespace_only)

    def test_pure_addition_is_not_whitespace_only(self):
        diff = (
            '--- a/src/foo.c\n'
            '+++ b/src/foo.c\n'
            '@@ -1 +1,2 @@\n'
            ' ctx\n'
            '+new code line\n'
        )
        prof = profile(diff)
        self.assertFalse(prof.whitespace_only)

    def test_identical_lines_are_not_whitespace_only(self):
        # A no-op (same lines removed and added) is not a whitespace change.
        diff = (
            '--- a/src/foo.c\n'
            '+++ b/src/foo.c\n'
            '@@ -1 +1 @@\n'
            '-same\n'
            '+same\n'
        )
        prof = profile(diff)
        self.assertFalse(prof.whitespace_only)


# ---------------------------------------------------------------------------
# comment_only
# ---------------------------------------------------------------------------

class CommentOnlyTestCase(testtools.TestCase):

    def test_python_comment_change_is_comment_only(self):
        diff = (
            '--- a/pkg/foo.py\n'
            '+++ b/pkg/foo.py\n'
            '@@ -1,2 +1,2 @@\n'
            '-# old explanation\n'
            '+# new explanation\n'
        )
        prof = profile(diff)
        self.assertTrue(prof.comment_only)

    def test_c_line_comment_is_comment_only(self):
        diff = (
            '--- a/src/foo.c\n'
            '+++ b/src/foo.c\n'
            '@@ -1 +1,2 @@\n'
            ' ctx\n'
            '+// a note\n'
        )
        prof = profile(diff)
        self.assertTrue(prof.comment_only)

    def test_c_single_line_block_comment_is_comment_only(self):
        diff = (
            '--- a/src/foo.c\n'
            '+++ b/src/foo.c\n'
            '@@ -1 +1,2 @@\n'
            ' ctx\n'
            '+/* a note */\n'
        )
        prof = profile(diff)
        self.assertTrue(prof.comment_only)

    def test_multiline_block_comment_open_is_not_comment_only(self):
        # We do not track block state; an unterminated open forces False.
        diff = (
            '--- a/src/foo.c\n'
            '+++ b/src/foo.c\n'
            '@@ -1 +1,3 @@\n'
            ' ctx\n'
            '+/* start\n'
            '+   keep going */\n'
        )
        prof = profile(diff)
        self.assertFalse(prof.comment_only)

    def test_code_change_is_not_comment_only(self):
        prof = profile(_edit('src/foo.c'))
        self.assertFalse(prof.comment_only)

    def test_unknown_language_is_not_comment_only(self):
        # A .json file: no comment syntax known -> conservative False.
        diff = (
            '--- a/data.json\n'
            '+++ b/data.json\n'
            '@@ -1 +1 @@\n'
            '-# x\n'
            '+# y\n'
        )
        prof = profile(diff)
        self.assertFalse(prof.comment_only)


# ---------------------------------------------------------------------------
# code_added_lines — the semantic level the dangerous-construct scan runs at.
# ---------------------------------------------------------------------------

class CodeAddedLinesTestCase(testtools.TestCase):

    def test_returns_only_code_added_lines(self):
        prof_diff = (
            '--- a/src/foo.c\n'
            '+++ b/src/foo.c\n'
            '@@ -1,2 +1,3 @@\n'
            ' ctx\n'
            '-removed code\n'
            '+added code line\n'
            '+second added line\n'
        )
        self.assertEqual(
            ['added code line', 'second added line'], code_added_lines(prof_diff))

    def test_excludes_doc_added_lines(self):
        # A line added to a manpage must NOT appear: prose is not code.
        diff = (
            _edit('man/foo.1', removed='.B old', added='system("/bin/sh")')
            + _edit('src/foo.c', removed='int x;', added='int y;')
        )
        self.assertEqual(['int y;'], code_added_lines(diff))

    def test_excludes_removed_and_context_lines(self):
        diff = (
            '--- a/src/foo.c\n'
            '+++ b/src/foo.c\n'
            '@@ -1,2 +1,2 @@\n'
            ' context line\n'
            '-removed line\n'
            '+added line\n'
        )
        self.assertEqual(['added line'], code_added_lines(diff))

    def test_empty_input_returns_empty(self):
        self.assertEqual([], code_added_lines(''))

    def test_doc_only_diff_returns_empty(self):
        diff = _edit('README.md', removed='a', added='b')
        self.assertEqual([], code_added_lines(diff))


# ---------------------------------------------------------------------------
# Header-skipping and shape
# ---------------------------------------------------------------------------

class ShapeTestCase(testtools.TestCase):

    def test_dep3_header_is_skipped(self):
        header = 'Description: fix the thing\nForwarded: no\n\n'
        bare = profile(_edit('src/foo.c'))
        with_header = profile(header + _edit('src/foo.c'))
        self.assertEqual(bare.files, with_header.files)
        self.assertEqual(bare.added_lines, with_header.added_lines)

    def test_dev_null_target_uses_source_path(self):
        # A deletion: target is /dev/null, so the source path names the file.
        diff = (
            '--- a/src/gone.c\n'
            '+++ /dev/null\n'
            '@@ -1,2 +0,0 @@\n'
            '-int x;\n'
            '-int y;\n'
        )
        prof = profile(diff)
        self.assertEqual([('src/gone.c', 'code')], prof.files)
        self.assertEqual(2, prof.removed_lines)

    def test_returns_content_profile_with_rule_version(self):
        prof = profile(_edit('src/foo.c'))
        self.assertIsInstance(prof, ContentProfile)
        self.assertEqual(CONTENT_RULE_VERSION, prof.rule_version)

    def test_file_types_are_in_vocabulary(self):
        diff = (
            _edit('src/foo.c') + _edit('README.md', removed='a', added='b')
            + _edit('debian/rules', removed='c', added='d')
            + _edit('data.json', removed='1', added='2')
            + _edit('tests/foo.c', removed='e', added='f')
        )
        prof = profile(diff)
        for _, file_type in prof.files:
            self.assertIn(file_type, FILE_TYPES)

    def test_empty_input(self):
        prof = profile('')
        self.assertEqual([], prof.files)
        self.assertTrue(prof.is_empty)
        self.assertFalse(prof.touches_code)
        self.assertFalse(prof.whitespace_only)
        self.assertFalse(prof.comment_only)
        self.assertFalse(prof.ignore_file_only)
