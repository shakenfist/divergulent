import testtools

from divergulent import dep3
from divergulent.dep3 import PatchClass


DIFF_BODY = '--- a/foo.c\n+++ b/foo.c\n@@ -1 +1 @@\n-old\n+new\n'


class ParseHeaderTestCase(testtools.TestCase):

    def test_parses_fields_lowercased(self):
        text = 'Description: fix a thing\nForwarded: no\n\n' + DIFF_BODY
        fields = dep3.parse_header(text)
        self.assertEqual('fix a thing', fields['description'])
        self.assertEqual('no', fields['forwarded'])

    def test_folds_continuation_lines(self):
        text = 'Description: line one\n line two\nForwarded: no\n\n' + DIFF_BODY
        self.assertEqual('line one line two', dep3.parse_header(text)['description'])

    def test_stops_at_triple_dash(self):
        text = 'Subject: x\n---\nForwarded: no\n'
        self.assertNotIn('forwarded', dep3.parse_header(text))

    def test_stops_at_diff(self):
        self.assertEqual({}, dep3.parse_header(DIFF_BODY))


class ClassifyTestCase(testtools.TestCase):

    def test_vendor_not_needed_is_debian_only(self):
        text = 'Description: distro tweak\nOrigin: vendor\nForwarded: not-needed\n\n' + DIFF_BODY
        self.assertEqual(PatchClass.DEBIAN_ONLY, dep3.classify(text))

    def test_forwarded_no_is_debian_only(self):
        text = 'Description: x\nForwarded: no\n\n' + DIFF_BODY
        self.assertEqual(PatchClass.DEBIAN_ONLY, dep3.classify(text))

    def test_origin_vendor_alone_is_debian_only(self):
        text = 'Description: x\nOrigin: vendor\n\n' + DIFF_BODY
        self.assertEqual(PatchClass.DEBIAN_ONLY, dep3.classify(text))

    def test_forwarded_yes_is_forwarded(self):
        text = 'Description: x\nForwarded: yes\n\n' + DIFF_BODY
        self.assertEqual(PatchClass.FORWARDED, dep3.classify(text))

    def test_forwarded_url_is_forwarded(self):
        text = 'Description: x\nForwarded: https://lists.example.org/123\n\n' + DIFF_BODY
        self.assertEqual(PatchClass.FORWARDED, dep3.classify(text))

    def test_origin_upstream_is_forwarded(self):
        text = 'Description: x\nOrigin: upstream, https://git.example/abc\n\n' + DIFF_BODY
        self.assertEqual(PatchClass.FORWARDED, dep3.classify(text))

    def test_origin_backport_is_forwarded(self):
        text = 'Description: x\nOrigin: backport, https://git.example/abc\n\n' + DIFF_BODY
        self.assertEqual(PatchClass.FORWARDED, dep3.classify(text))

    def test_applied_upstream_is_forwarded(self):
        text = 'Description: x\nApplied-Upstream: 1.2.3\nForwarded: no\n\n' + DIFF_BODY
        # Applied-Upstream wins even alongside a stale "Forwarded: no".
        self.assertEqual(PatchClass.FORWARDED, dep3.classify(text))

    def test_implicit_yes_via_bug(self):
        text = 'Description: x\nBug-Debian: https://bugs.debian.org/1\n\n' + DIFF_BODY
        self.assertEqual(PatchClass.FORWARDED, dep3.classify(text))

    def test_bare_diff_is_unknown(self):
        self.assertEqual(PatchClass.UNKNOWN, dep3.classify(DIFF_BODY))

    def test_description_only_is_unknown(self):
        text = 'Description: just a fix\nAuthor: Joe <joe@example.org>\n\n' + DIFF_BODY
        self.assertEqual(PatchClass.UNKNOWN, dep3.classify(text))

    def test_git_format_patch_without_dep3_is_unknown(self):
        text = (
            'From abc123 Mon Sep 17 00:00:00 2001\n'
            'From: Joe <joe@example.org>\n'
            'Date: Thu, 1 Jan 2026 00:00:00 +0000\n'
            'Subject: [PATCH] fix the foo\n\n'
            'Some explanation.\n---\n' + DIFF_BODY)
        self.assertEqual(PatchClass.UNKNOWN, dep3.classify(text))

    def test_non_standard_not_yet_is_debian_only(self):
        text = 'Description: x\nForwarded: not yet\n\n' + DIFF_BODY
        self.assertEqual(PatchClass.DEBIAN_ONLY, dep3.classify(text))


class HeuristicTestCase(testtools.TestCase):

    def test_dp_marker_is_debian_only(self):
        text = '# DP: tweak for Debian\n\n' + DIFF_BODY
        self.assertEqual(PatchClass.DEBIAN_ONLY, dep3.classify(text))

    def test_deb_filename_is_debian_only(self):
        self.assertEqual(PatchClass.DEBIAN_ONLY, dep3.classify(DIFF_BODY, name='deb-config.diff'))

    def test_debian_changes_filename_is_debian_only(self):
        self.assertEqual(PatchClass.DEBIAN_ONLY, dep3.classify(DIFF_BODY, name='debian-changes'))

    def test_plain_filename_stays_unknown(self):
        self.assertEqual(PatchClass.UNKNOWN, dep3.classify(DIFF_BODY, name='fix-upstream.patch'))

    def test_no_name_and_no_marker_is_unknown(self):
        self.assertEqual(PatchClass.UNKNOWN, dep3.classify(DIFF_BODY))

    def test_explicit_dep3_overrides_heuristic(self):
        # A deb-* filename with an explicit "Forwarded: yes" is still forwarded.
        text = 'Forwarded: yes\n\n' + DIFF_BODY
        self.assertEqual(PatchClass.FORWARDED, dep3.classify(text, name='deb-thing.diff'))


class BugReferencesTestCase(testtools.TestCase):

    def test_debian_and_upstream_bugs(self):
        text = 'Description: x\nBug-Debian: https://bugs.debian.org/123\nBug: https://up/2\n\n' + DIFF_BODY
        refs = dep3.bug_references(text)
        self.assertEqual([('debian', 'https://bugs.debian.org/123'), ('upstream', 'https://up/2')],
                         [(r.tracker, r.ref) for r in refs])

    def test_vendor_bug(self):
        text = 'Description: x\nBug-Ubuntu: https://launchpad.net/bugs/9\n\n' + DIFF_BODY
        refs = dep3.bug_references(text)
        self.assertEqual(1, len(refs))
        self.assertEqual('ubuntu', refs[0].tracker)

    def test_no_bugs(self):
        self.assertEqual([], dep3.bug_references(DIFF_BODY))
