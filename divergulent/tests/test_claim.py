"""Tests for divergulent.classify.claim — author-controlled claim extraction.

All tests are offline (no I/O, no network).  Fixture patches cover every
``claimed_category`` path, CVE extraction, bug-ref pass-through, forwarded-ness
pass-through, and keyword-precedence edge cases.
"""
import testtools

from divergulent.classify.claim import Claim, extract_claim, CLAIM_RULE_VERSION
from divergulent.dep3 import PatchClass


# ---------------------------------------------------------------------------
# Shared diff body so fixtures are realistic patches.
# ---------------------------------------------------------------------------

_DIFF = '--- a/foo.c\n+++ b/foo.c\n@@ -1 +1 @@\n-old\n+new\n'


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _patch(header: str, diff: str = _DIFF) -> str:
    return header + '\n' + diff


# ---------------------------------------------------------------------------
# claimed_category — one test per category (and unknown)
# ---------------------------------------------------------------------------

class SecurityCategoryTestCase(testtools.TestCase):

    def test_cve_in_description_gives_security(self):
        text = _patch('Description: fix CVE-2024-1234 heap overflow\nForwarded: no\n')
        claim = extract_claim('fix-cve.patch', text)
        self.assertEqual('security', claim.claimed_category)

    def test_cve_in_header_only_no_description_gives_security(self):
        # CVE in a standalone comment line before the diff.
        text = '# CVE-2023-99999\n' + _DIFF
        claim = extract_claim('patch.patch', text)
        self.assertEqual('security', claim.claimed_category)

    def test_security_keyword_in_description(self):
        text = _patch('Description: address security vulnerability\nForwarded: no\n')
        claim = extract_claim('hardening.patch', text)
        self.assertEqual('security', claim.claimed_category)

    def test_overflow_keyword_in_description(self):
        text = _patch('Description: prevent buffer overflow in parser\nForwarded: no\n')
        claim = extract_claim('overflow.patch', text)
        self.assertEqual('security', claim.claimed_category)

    def test_cve_wins_over_fix_keyword(self):
        """A description with both 'fix' and a CVE must resolve to security."""
        text = _patch('Description: fix CVE-2025-5678 regression\nForwarded: no\n')
        claim = extract_claim('patch.patch', text)
        self.assertEqual('security', claim.claimed_category)

    def test_cve_captured_in_cves_field(self):
        text = _patch('Description: patch for CVE-2024-1234\nForwarded: no\n')
        claim = extract_claim('fix.patch', text)
        self.assertIn('CVE-2024-1234', claim.cves)

    def test_injection_keyword_gives_security(self):
        text = _patch('Description: prevent SQL injection attack\nForwarded: no\n')
        claim = extract_claim('sql.patch', text)
        self.assertEqual('security', claim.claimed_category)


class PackagingCategoryTestCase(testtools.TestCase):

    def test_deb_filename_prefix_gives_packaging(self):
        """deb-*.patch → packaging regardless of description."""
        text = _patch('Description: adjust config for Debian\nForwarded: no\n')
        claim = extract_claim('deb-config.patch', text)
        self.assertEqual('packaging', claim.claimed_category)

    def test_debian_filename_prefix_gives_packaging(self):
        text = _patch('Description: Debian-specific tweak\nForwarded: no\n')
        claim = extract_claim('debian-changes.diff', text)
        self.assertEqual('packaging', claim.claimed_category)

    def test_debian_subdir_gives_packaging(self):
        """A patch under a debian/ subdir is a packaging claim."""
        text = _patch('Description: update build rules\nForwarded: no\n')
        claim = extract_claim('debian/rules.patch', text)
        self.assertEqual('packaging', claim.claimed_category)

    def test_packaging_keyword_in_description(self):
        text = _patch('Description: packaging tweak for autotools\nForwarded: no\n')
        claim = extract_claim('build.patch', text)
        self.assertEqual('packaging', claim.claimed_category)

    def test_makefile_keyword_in_description(self):
        text = _patch('Description: fix makefile target\nForwarded: no\n')
        claim = extract_claim('build.patch', text)
        self.assertEqual('packaging', claim.claimed_category)

    def test_reproducib_keyword(self):
        text = _patch('Description: make build reproducible\nForwarded: no\n')
        claim = extract_claim('repro.patch', text)
        self.assertEqual('packaging', claim.claimed_category)


class DocumentationCategoryTestCase(testtools.TestCase):

    def test_fix_typo_gives_documentation(self):
        """'fix typo' → documentation even though 'fix' is also a bugfix keyword."""
        text = _patch('Description: fix typo in error message\nForwarded: no\n')
        claim = extract_claim('typo.patch', text)
        self.assertEqual('documentation', claim.claimed_category)

    def test_spelling_keyword_gives_documentation(self):
        text = _patch('Description: correct spelling in man page\nForwarded: no\n')
        claim = extract_claim('spelling.patch', text)
        self.assertEqual('documentation', claim.claimed_category)

    def test_manpage_keyword_gives_documentation(self):
        text = _patch('Description: update man page for new option\nForwarded: no\n')
        claim = extract_claim('manpage.patch', text)
        self.assertEqual('documentation', claim.claimed_category)

    def test_docs_subdir_gives_documentation(self):
        """A patch under docs/ subdir is a documentation claim."""
        text = _patch('Description: update changelog text\nForwarded: no\n')
        claim = extract_claim('docs/changelog.patch', text)
        self.assertEqual('documentation', claim.claimed_category)

    def test_doc_subdir_gives_documentation(self):
        text = _patch('Description: clarify option description\nForwarded: no\n')
        claim = extract_claim('doc/options.patch', text)
        self.assertEqual('documentation', claim.claimed_category)

    def test_readme_keyword_gives_documentation(self):
        text = _patch('Description: update README with new instructions\nForwarded: no\n')
        claim = extract_claim('readme.patch', text)
        self.assertEqual('documentation', claim.claimed_category)

    def test_documentation_keyword_gives_documentation(self):
        text = _patch('Description: improve documentation for API\nForwarded: no\n')
        claim = extract_claim('docs.patch', text)
        self.assertEqual('documentation', claim.claimed_category)


class FeatureCategoryTestCase(testtools.TestCase):

    def test_add_support_gives_feature(self):
        text = _patch('Description: add support for TLS 1.3\nForwarded: yes\n')
        claim = extract_claim('tls.patch', text)
        self.assertEqual('feature', claim.claimed_category)

    def test_feature_keyword_in_description(self):
        text = _patch('Description: new feature: parallel builds\nForwarded: yes\n')
        claim = extract_claim('parallel.patch', text)
        self.assertEqual('feature', claim.claimed_category)

    def test_implement_keyword_gives_feature(self):
        text = _patch('Description: implement rate limiting\nForwarded: yes\n')
        claim = extract_claim('ratelimit.patch', text)
        self.assertEqual('feature', claim.claimed_category)

    def test_new_option_gives_feature(self):
        text = _patch('Description: add new option --verbose-errors\nForwarded: yes\n')
        claim = extract_claim('verbose.patch', text)
        self.assertEqual('feature', claim.claimed_category)


class BugfixCategoryTestCase(testtools.TestCase):

    def test_fix_segfault_gives_bugfix(self):
        text = _patch('Description: fix segfault on empty input\nForwarded: yes\n')
        claim = extract_claim('segfault.patch', text)
        self.assertEqual('bugfix', claim.claimed_category)

    def test_crash_keyword_gives_bugfix(self):
        text = _patch('Description: prevent crash in parser\nForwarded: yes\n')
        claim = extract_claim('crash.patch', text)
        self.assertEqual('bugfix', claim.claimed_category)

    def test_regression_keyword_gives_bugfix(self):
        text = _patch('Description: address regression introduced in 2.1\nForwarded: yes\n')
        claim = extract_claim('regression.patch', text)
        self.assertEqual('bugfix', claim.claimed_category)

    def test_fix_keyword_gives_bugfix(self):
        text = _patch('Description: fix incorrect return value\nForwarded: yes\n')
        claim = extract_claim('fix.patch', text)
        self.assertEqual('bugfix', claim.claimed_category)

    def test_workaround_keyword_gives_bugfix(self):
        text = _patch('Description: workaround for upstream race condition\nForwarded: yes\n')
        claim = extract_claim('workaround.patch', text)
        self.assertEqual('bugfix', claim.claimed_category)


class UnknownCategoryTestCase(testtools.TestCase):

    def test_empty_diff_only_gives_unknown(self):
        """A bare diff with no header → unknown."""
        claim = extract_claim('patch.patch', _DIFF)
        self.assertEqual('unknown', claim.claimed_category)

    def test_no_header_gives_unknown(self):
        """No DEP-3 header and no keywords → unknown."""
        text = '# some comment\n' + _DIFF
        claim = extract_claim('patch.patch', text)
        self.assertEqual('unknown', claim.claimed_category)

    def test_generic_description_gives_unknown(self):
        """A description with no matched keywords stays unknown."""
        text = _patch('Description: adjust threshold value\nForwarded: no\n')
        claim = extract_claim('tweak.patch', text)
        self.assertEqual('unknown', claim.claimed_category)


# ---------------------------------------------------------------------------
# CVE extraction
# ---------------------------------------------------------------------------

class CVEExtractionTestCase(testtools.TestCase):

    def test_single_cve_extracted(self):
        text = _patch('Description: fix CVE-2024-12345\nForwarded: no\n')
        claim = extract_claim('fix.patch', text)
        self.assertEqual(['CVE-2024-12345'], claim.cves)

    def test_multiple_cves_extracted_in_order(self):
        text = _patch('Description: fixes CVE-2024-0001 and CVE-2024-0002\nForwarded: no\n')
        claim = extract_claim('fix.patch', text)
        self.assertEqual(['CVE-2024-0001', 'CVE-2024-0002'], claim.cves)

    def test_duplicate_cves_deduped(self):
        text = _patch(
            'Description: CVE-2024-9999\n'
            'CVE: CVE-2024-9999\n'
            'Forwarded: no\n',
        )
        claim = extract_claim('fix.patch', text)
        self.assertEqual(['CVE-2024-9999'], claim.cves)

    def test_cves_uppercased(self):
        text = '# cve-2023-00123\n' + _DIFF
        claim = extract_claim('fix.patch', text)
        self.assertEqual(['CVE-2023-00123'], claim.cves)

    def test_no_cves_when_absent(self):
        text = _patch('Description: fix segfault\nForwarded: yes\n')
        claim = extract_claim('fix.patch', text)
        self.assertEqual([], claim.cves)

    def test_cve_with_long_number(self):
        """CVE ids with >4 digits in the sequence number are valid."""
        text = _patch('Description: fix CVE-2021-123456\nForwarded: no\n')
        claim = extract_claim('fix.patch', text)
        self.assertEqual(['CVE-2021-123456'], claim.cves)

    def test_cves_not_extracted_from_diff_body(self):
        """CVE mentions in the diff body (after the header) are not captured."""
        diff_with_cve = (
            '--- a/changelog\n+++ b/changelog\n'
            '@@ -1 +1 @@\n-old\n+fix CVE-2022-0001\n'
        )
        text = 'Description: update changelog\nForwarded: no\n\n' + diff_with_cve
        claim = extract_claim('changelog.patch', text)
        self.assertEqual([], claim.cves)


# ---------------------------------------------------------------------------
# Bug references pass-through from dep3
# ---------------------------------------------------------------------------

class BugReferencesTestCase(testtools.TestCase):

    def test_debian_bug_ref_passed_through(self):
        text = _patch(
            'Description: fix regression\n'
            'Bug-Debian: https://bugs.debian.org/123\n'
            'Forwarded: no\n',
        )
        claim = extract_claim('fix.patch', text)
        self.assertEqual(1, len(claim.bugs))
        self.assertEqual('debian', claim.bugs[0].tracker)
        self.assertEqual('https://bugs.debian.org/123', claim.bugs[0].ref)

    def test_upstream_bug_ref_passed_through(self):
        text = _patch(
            'Description: fix upstream bug\n'
            'Bug: https://github.com/proj/issues/42\n'
            'Forwarded: yes\n',
        )
        claim = extract_claim('fix.patch', text)
        self.assertEqual(1, len(claim.bugs))
        self.assertEqual('upstream', claim.bugs[0].tracker)

    def test_no_bugs_gives_empty_list(self):
        claim = extract_claim('fix.patch', _DIFF)
        self.assertEqual([], claim.bugs)

    def test_multiple_bug_refs(self):
        text = _patch(
            'Description: multi-tracker fix\n'
            'Bug-Debian: https://bugs.debian.org/456\n'
            'Bug-Ubuntu: https://launchpad.net/bugs/789\n'
            'Forwarded: no\n',
        )
        claim = extract_claim('fix.patch', text)
        trackers = [b.tracker for b in claim.bugs]
        self.assertIn('debian', trackers)
        self.assertIn('ubuntu', trackers)


# ---------------------------------------------------------------------------
# Forwarded-ness pass-through from dep3
# ---------------------------------------------------------------------------

class ForwardednessTestCase(testtools.TestCase):

    def test_forwarded_no_gives_debian_only(self):
        text = _patch('Description: Debian tweak\nForwarded: no\n')
        claim = extract_claim('fix.patch', text)
        self.assertEqual(PatchClass.DEBIAN_ONLY.value, claim.forwarded)

    def test_forwarded_yes_gives_forwarded(self):
        text = _patch('Description: upstream fix\nForwarded: yes\n')
        claim = extract_claim('fix.patch', text)
        self.assertEqual(PatchClass.FORWARDED.value, claim.forwarded)

    def test_no_dep3_gives_unknown(self):
        claim = extract_claim('fix.patch', _DIFF)
        self.assertEqual(PatchClass.UNKNOWN.value, claim.forwarded)

    def test_deb_filename_gives_debian_only(self):
        """Heuristic: deb-* filename → debian-only even without DEP-3."""
        claim = extract_claim('deb-configure.diff', _DIFF)
        self.assertEqual(PatchClass.DEBIAN_ONLY.value, claim.forwarded)


# ---------------------------------------------------------------------------
# Description extraction
# ---------------------------------------------------------------------------

class DescriptionTestCase(testtools.TestCase):

    def test_description_field_extracted(self):
        text = _patch('Description: fix the thing\nForwarded: no\n')
        claim = extract_claim('fix.patch', text)
        self.assertEqual('fix the thing', claim.description)

    def test_subject_fallback(self):
        """git-format-patch uses Subject; we fall back to it."""
        text = (
            'From: Joe <joe@example.org>\n'
            'Subject: [PATCH] add support for new protocol\n\n' + _DIFF
        )
        claim = extract_claim('proto.patch', text)
        self.assertEqual('[PATCH] add support for new protocol', claim.description)

    def test_description_over_subject(self):
        """Description wins over Subject when both present."""
        text = _patch(
            'Description: proper description\n'
            'Subject: subject line\n'
            'Forwarded: no\n',
        )
        claim = extract_claim('fix.patch', text)
        self.assertEqual('proper description', claim.description)

    def test_no_description_is_none(self):
        claim = extract_claim('fix.patch', _DIFF)
        self.assertIsNone(claim.description)

    def test_multiline_description_folded(self):
        text = _patch(
            'Description: line one\n'
            ' line two continues here\n'
            'Forwarded: no\n',
        )
        claim = extract_claim('fix.patch', text)
        self.assertEqual('line one line two continues here', claim.description)


# ---------------------------------------------------------------------------
# Structural / provenance
# ---------------------------------------------------------------------------

class ClaimStructureTestCase(testtools.TestCase):

    def test_rule_version_is_constant(self):
        claim = extract_claim('fix.patch', _DIFF)
        self.assertEqual(CLAIM_RULE_VERSION, claim.rule_version)
        self.assertEqual(1, claim.rule_version)

    def test_claim_is_frozen(self):
        """Claim dataclass must be immutable (frozen=True)."""
        claim = extract_claim('fix.patch', _DIFF)
        with self.assertRaises(Exception):
            claim.claimed_category = 'security'  # type: ignore[misc]

    def test_returns_claim_instance(self):
        claim = extract_claim('fix.patch', _DIFF)
        self.assertIsInstance(claim, Claim)


# ---------------------------------------------------------------------------
# Keyword precedence edge cases
# ---------------------------------------------------------------------------

class PrecedenceTestCase(testtools.TestCase):

    def test_cve_beats_bugfix(self):
        """'fix CVE-...' — security beats bugfix."""
        text = _patch('Description: fix CVE-2024-9999 crash\nForwarded: no\n')
        claim = extract_claim('fix.patch', text)
        self.assertEqual('security', claim.claimed_category)

    def test_security_keyword_beats_documentation(self):
        """'security vulnerability in documentation' — security beats docs."""
        text = _patch('Description: fix security vulnerability in documentation\nForwarded: no\n')
        claim = extract_claim('fix.patch', text)
        self.assertEqual('security', claim.claimed_category)

    def test_packaging_beats_bugfix(self):
        """A deb-* filename wins over a 'fix' keyword in the description."""
        text = _patch('Description: fix configure detection\nForwarded: no\n')
        claim = extract_claim('deb-configure.patch', text)
        self.assertEqual('packaging', claim.claimed_category)

    def test_documentation_beats_feature(self):
        """'add documentation for new feature' — documentation beats feature."""
        text = _patch('Description: add documentation for new feature\nForwarded: no\n')
        claim = extract_claim('fix.patch', text)
        self.assertEqual('documentation', claim.claimed_category)

    def test_feature_beats_bugfix(self):
        """'add support for error recovery' — feature beats bugfix."""
        text = _patch('Description: add support for error recovery\nForwarded: yes\n')
        claim = extract_claim('feature.patch', text)
        self.assertEqual('feature', claim.claimed_category)

    def test_security_beats_packaging_for_deb_named_cve(self):
        """A deb-* filename plus a CVE → security (CVE is a stronger claim)."""
        text = _patch('Description: fix CVE-2024-0001 in Debian build\nForwarded: no\n')
        claim = extract_claim('deb-fix.patch', text)
        self.assertEqual('security', claim.claimed_category)
