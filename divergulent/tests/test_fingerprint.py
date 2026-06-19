import testtools

from divergulent.classify import fingerprint as fp


# A plain quilt-style diff body with no DEP-3 header.
DIFF_BODY = (
    '--- a/foo.c\n'
    '+++ b/foo.c\n'
    '@@ -1,3 +1,3 @@ int main(void)\n'
    ' before\n'
    '-old line\n'
    '+new line\n'
    ' after\n'
)

DEP3_HEADER = (
    'Description: fix the thing\n'
    'Origin: vendor\n'
    'Forwarded: no\n'
    '\n'
)

OTHER_DEP3_HEADER = (
    'Description: a completely different explanation entirely\n'
    'Author: Someone Else <else@example.org>\n'
    'Bug-Debian: https://bugs.debian.org/999\n'
    '\n'
)


class NormaliseHeaderStrippedTestCase(testtools.TestCase):

    def test_identical_diff_different_descriptions_share_fingerprint(self):
        one = fp.fingerprint(DEP3_HEADER + DIFF_BODY)
        two = fp.fingerprint(OTHER_DEP3_HEADER + DIFF_BODY)
        bare = fp.fingerprint(DIFF_BODY)
        self.assertEqual(one, two)
        self.assertEqual(one, bare)

    def test_diff_git_decoration_starts_the_body(self):
        with_git = (
            'Description: something\n'
            '\n'
            'diff --git a/foo.c b/foo.c\n'
            'index 1234567..89abcde 100644\n'
            '--- a/foo.c\n'
            '+++ b/foo.c\n'
            '@@ -1,3 +1,3 @@ int main(void)\n'
            ' before\n'
            '-old line\n'
            '+new line\n'
            ' after\n'
        )
        self.assertEqual(fp.fingerprint(DIFF_BODY), fp.fingerprint(with_git))

    def test_bare_hunk_starts_the_body(self):
        with_preamble = 'Description: noise\nnoise\n\n@@ -1 +1 @@\n-a\n+b\n'
        bare = '@@ -1 +1 @@\n-a\n+b\n'
        self.assertEqual(fp.fingerprint(bare), fp.fingerprint(with_preamble))


class TriviallyDifferentMergeTestCase(testtools.TestCase):

    def test_line_number_offsets_share_fingerprint(self):
        shifted = DIFF_BODY.replace('@@ -1,3 +1,3 @@', '@@ -842,3 +839,3 @@')
        self.assertEqual(fp.fingerprint(DIFF_BODY), fp.fingerprint(shifted))

    def test_function_context_tail_share_fingerprint(self):
        other_context = DIFF_BODY.replace('int main(void)', 'static void helper(int x)')
        self.assertEqual(fp.fingerprint(DIFF_BODY), fp.fingerprint(other_context))

    def test_file_header_timestamps_share_fingerprint(self):
        timestamped = (
            '--- a/foo.c\t2024-01-01 00:00:00.000000000 +0000\n'
            '+++ b/foo.c\t2024-06-14 12:34:56.000000000 +0000\n'
            '@@ -1,3 +1,3 @@ int main(void)\n'
            ' before\n'
            '-old line\n'
            '+new line\n'
            ' after\n'
        )
        self.assertEqual(fp.fingerprint(DIFF_BODY), fp.fingerprint(timestamped))

    def test_trailing_whitespace_share_fingerprint(self):
        trailing = (
            '--- a/foo.c   \n'
            '+++ b/foo.c\n'
            '@@ -1,3 +1,3 @@ int main(void)  \n'
            ' before\t\n'
            '-old line \n'
            '+new line\n'
            ' after  \n'
        )
        self.assertEqual(fp.fingerprint(DIFF_BODY), fp.fingerprint(trailing))

    def test_crlf_line_endings_share_fingerprint(self):
        crlf = DIFF_BODY.replace('\n', '\r\n')
        self.assertEqual(fp.fingerprint(DIFF_BODY), fp.fingerprint(crlf))

    def test_ab_prefix_variants_share_fingerprint(self):
        no_prefix = DIFF_BODY.replace('a/foo.c', 'foo.c').replace('b/foo.c', 'foo.c')
        self.assertEqual(fp.fingerprint(DIFF_BODY), fp.fingerprint(no_prefix))


class GenuinelyDifferentTestCase(testtools.TestCase):

    def test_different_change_content_differs(self):
        other = DIFF_BODY.replace('+new line', '+a totally different replacement')
        self.assertNotEqual(fp.fingerprint(DIFF_BODY), fp.fingerprint(other))

    def test_added_change_line_differs(self):
        extra = DIFF_BODY.replace('+new line\n', '+new line\n+and another\n')
        self.assertNotEqual(fp.fingerprint(DIFF_BODY), fp.fingerprint(extra))


class StripPathKnobTestCase(testtools.TestCase):

    def _same_edit_on(self, path: str) -> str:
        return (
            f'--- a/{path}\n'
            f'+++ b/{path}\n'
            '@@ -1,3 +1,3 @@\n'
            ' before\n'
            '-old line\n'
            '+new line\n'
            ' after\n'
        )

    def test_strip_path_merges_different_files(self):
        one = fp.fingerprint(self._same_edit_on('src/foo.c'), strip_path=True)
        two = fp.fingerprint(self._same_edit_on('lib/bar.c'), strip_path=True)
        self.assertEqual(one, two)

    def test_keep_path_separates_different_files(self):
        one = fp.fingerprint(self._same_edit_on('src/foo.c'), strip_path=False)
        two = fp.fingerprint(self._same_edit_on('lib/bar.c'), strip_path=False)
        self.assertNotEqual(one, two)

    def test_keep_path_ignores_ab_prefix_and_timestamp(self):
        plain = self._same_edit_on('src/foo.c')
        decorated = (
            '--- a/src/foo.c\t2024-01-01 00:00:00 +0000\n'
            '+++ b/src/foo.c\t2024-06-14 00:00:00 +0000\n'
            '@@ -10,3 +12,3 @@\n'
            ' before\n'
            '-old line\n'
            '+new line\n'
            ' after\n'
        )
        self.assertEqual(
            fp.fingerprint(plain, strip_path=False),
            fp.fingerprint(decorated, strip_path=False))


class DropContextKnobTestCase(testtools.TestCase):

    def _edit_with_context(self, ctx: str) -> str:
        return (
            '--- a/foo.c\n'
            '+++ b/foo.c\n'
            '@@ -1,3 +1,3 @@\n'
            f' {ctx}\n'
            '-old line\n'
            '+new line\n'
            f' {ctx}-trailing\n'
        )

    def test_drop_context_merges_differing_surroundings(self):
        one = fp.fingerprint(self._edit_with_context('alpha'), drop_context=True)
        two = fp.fingerprint(self._edit_with_context('omega'), drop_context=True)
        self.assertEqual(one, two)

    def test_keep_context_separates_differing_surroundings(self):
        one = fp.fingerprint(self._edit_with_context('alpha'), drop_context=False)
        two = fp.fingerprint(self._edit_with_context('omega'), drop_context=False)
        self.assertNotEqual(one, two)


class FingerprintShapeTestCase(testtools.TestCase):

    def test_returns_version_and_64_hex(self):
        version, digest = fp.fingerprint(DIFF_BODY)
        self.assertEqual(1, version)
        self.assertEqual(64, len(digest))
        self.assertTrue(all(c in '0123456789abcdef' for c in digest))

    def test_stable_across_calls(self):
        self.assertEqual(fp.fingerprint(DIFF_BODY), fp.fingerprint(DIFF_BODY))

    def test_version_two_raises(self):
        self.assertRaises(ValueError, fp.fingerprint, DIFF_BODY, version=2)

    def test_normalise_version_two_raises(self):
        self.assertRaises(ValueError, fp.normalise, DIFF_BODY, version=2)
