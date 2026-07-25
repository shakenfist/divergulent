"""Tests for divergulent.classify.generated -- the generated-output scanner.

All offline (the scanner is pure: no I/O, no network).  Coverage follows the two
independent signals and the arithmetic phase 3 will route on:

* the NAME signal at each edge -- any path depth, exact basename never substring, case
  sensitivity, and backup suffixes in both directions (``Makefile.in.ori`` is marked,
  ``Makefile.am.ori`` is not);
* the BANNER signal standalone -- every shipped pattern with a positive, a negative and
  its captured version, a banner arriving as a context line, and a banner in a file the
  name set does not know;
* the added-lines-preferred banner selection -- added beats context beats removed, and a
  removed-only banner still fires when it is the only group present;
* the corroboration tier -- ``Makefile.in`` marks only alongside a banner; every other
  name still marks on the name signal alone;
* the ``config.guess``/``config.sub`` datestamp as version evidence that never becomes a
  banner signal; and
* a gatos-shaped fixture asserting the marked set, the per-file signals, the identity
  ``residue_changed + generated_changed == total_changed``, and coverage.

The observation helpers are covered too: ``detail_for``'s dominant family (including the
alphabetical tie break and the empty-scan refusal), ``evidence_for``'s canonical, stable,
uniformly-keyed JSON, and ``generated_marks`` reading live rows back out of a temporary
ledger -- ignoring superseded rows, other kinds, and malformed evidence.
"""
import json
import os
import tempfile

import testtools

from divergulent.classify import generated
from divergulent.classify import ledger as ledger_mod


# ---------------------------------------------------------------------------
# Diff fixture builders
# ---------------------------------------------------------------------------

def _file_diff(path, body_lines):
    """A one-hunk diff on ``path`` whose hunk body is ``body_lines`` verbatim.

    Each entry already carries its diff prefix (``' '``, ``'+'`` or ``'-'``); the hunk
    header's counts are derived so the fixture is a well-formed unified diff.
    """
    added = sum(1 for line in body_lines if line.startswith('+'))
    removed = sum(1 for line in body_lines if line.startswith('-'))
    context = len(body_lines) - added - removed
    body = ''.join('%s\n' % line for line in body_lines)
    return ('--- a/%s\n+++ b/%s\n@@ -1,%d +1,%d @@\n%s'
            % (path, path, context + removed, context + added, body))


def _edit(path):
    """A one-line edit on ``path`` with no generator evidence of any kind."""
    return _file_diff(path, [' context', '-old', '+new'])


def _banner_diff(text, path='src/notes.txt'):
    """A diff on a file the name set does NOT know, carrying ``text`` as context.

    Context-line placement is the realistic case (a banner arrives in the untouched top
    of a regenerated file) and it isolates the banner signal from the name signal.
    """
    return _file_diff(path, [' %s' % text, '-old', '+new'])


def _marked(text):
    """``{path: GeneratedFile}`` for the marked files of ``text``."""
    return {entry.path: entry for entry in generated.scan(text).files}


# ---------------------------------------------------------------------------
# The name signal
# ---------------------------------------------------------------------------

class NameSignalTestCase(testtools.TestCase):

    def test_top_level_configure_matches(self):
        self.assertEqual('autotools', generated.name_family('configure'))

    def test_name_matches_at_depth(self):
        # A subdirectory's generated file is as generated as a top-level one.
        self.assertEqual('autotools', generated.name_family('m4/libtool.m4'))
        self.assertEqual('autotools', generated.name_family('sub/dir/configure'))

    def test_tilde_name_matches(self):
        # ``lt~obsolete.m4``'s interior tilde is part of the name, not a backup suffix.
        self.assertEqual('autotools', generated.name_family('m4/lt~obsolete.m4'))

    def test_exact_basename_not_substring(self):
        # Substring matching would swallow hand-written neighbours wholesale.
        self.assertIsNone(generated.name_family('myconfigure'))
        self.assertIsNone(generated.name_family('preconfigure'))
        self.assertIsNone(generated.name_family('reconfigure'))
        self.assertIsNone(generated.name_family('configure.ac'))
        self.assertIsNone(generated.name_family('configure.in'))

    def test_matching_is_case_sensitive(self):
        # These names are case-sensitive on disk; ``Configure`` is somebody else's file.
        self.assertIsNone(generated.name_family('Configure'))
        self.assertIsNone(generated.name_family('MAKEFILE.IN'))

    def test_acinclude_is_deliberately_absent(self):
        # A maintainer-written aclocal input, excluded pending the phase-1 measurement.
        self.assertIsNone(generated.name_family('acinclude.m4'))

    def test_hand_written_build_inputs_are_not_marked(self):
        self.assertIsNone(generated.name_family('Makefile.am'))
        self.assertIsNone(generated.name_family('src/Makefile.am'))

    def test_name_only_match_has_the_name_signal_alone(self):
        # ``configure`` is not in the corroboration tier (unlike ``Makefile.in`` -- see
        # CorroborationTestCase), so the name signal alone marks it.
        entry = _marked(_edit('configure'))['configure']
        self.assertEqual(('name',), entry.signals)
        self.assertEqual('autotools', entry.family)
        self.assertIsNone(entry.generator)
        self.assertIsNone(entry.version)


# ---------------------------------------------------------------------------
# Backup suffixes: classified as what they are a copy of, both directions
# ---------------------------------------------------------------------------

class BackupSuffixTestCase(testtools.TestCase):

    def test_backup_of_a_generated_file_is_marked(self):
        self.assertEqual('autotools', generated.name_family('Makefile.in.ori'))
        # ``Makefile.in`` needs a banner to mark at all (corroboration tier -- see
        # CorroborationTestCase), so a non-corroboration name proves the stripping here.
        self.assertEqual('autotools', generated.name_family('configure.ori'))
        self.assertIn('configure.ori', _marked(_edit('configure.ori')))

    def test_backup_of_a_hand_written_file_is_not_marked(self):
        # The gatos case: ``Makefile.am.ori`` is a copy of a hand-written input.
        self.assertIsNone(generated.name_family('Makefile.am.ori'))
        self.assertEqual({}, _marked(_edit('Makefile.am.ori')))

    def test_every_backup_suffix_strips(self):
        for suffix in ('.ori', '.orig', '.bak', '.rej', '~'):
            self.assertEqual('autotools', generated.name_family('configure%s' % suffix))

    def test_repeated_suffixes_strip(self):
        self.assertEqual('autotools', generated.name_family('Makefile.in.orig~'))
        self.assertEqual('autotools', generated.name_family('configure~.bak.rej'))

    def test_a_bare_suffix_is_left_alone(self):
        # Nothing underneath to classify it as; stripping to '' would match nothing
        # anyway, but the guard keeps the helper total.
        self.assertEqual('~', generated.strip_backup_suffixes('~'))
        self.assertEqual('.bak', generated.strip_backup_suffixes('.bak'))


# ---------------------------------------------------------------------------
# The banner signal: standalone, version-capturing, whole-region
# ---------------------------------------------------------------------------

class BannerSignalTestCase(testtools.TestCase):

    def _banner(self, text, path='src/notes.txt'):
        entry = _marked(_banner_diff(text, path=path)).get(path)
        self.assertIsNotNone(entry, 'expected a banner mark for %r' % text)
        return entry

    def _quiet(self, text):
        self.assertEqual({}, _marked(_banner_diff(text)))

    def test_autoconf_banner_captures_version(self):
        entry = self._banner('# Generated by GNU Autoconf 2.59.')
        self.assertEqual('autoconf', entry.generator)
        self.assertEqual('2.59', entry.version)
        self.assertEqual('autotools', entry.family)

    def test_autoconf_banner_without_version(self):
        # Ancient autoconf omits the version; the claim still stands, unversioned.
        entry = self._banner('# Generated by GNU Autoconf.')
        self.assertEqual('autoconf', entry.generator)
        self.assertIsNone(entry.version)

    def test_autoconf_negative_is_case_sensitive(self):
        self._quiet('# generated by gnu autoconf 2.59')

    def test_automake_banner_captures_version(self):
        entry = self._banner('# Makefile.in generated by automake 1.9.6 from Makefile.am.')
        self.assertEqual('automake', entry.generator)
        self.assertEqual('1.9.6', entry.version)

    def test_automake_negative_without_a_version(self):
        # The format always carries a version; prose about automake does not.
        self._quiet('# this file was not generated by automake at all')

    def test_automake_pre_1_5_banner_captures_version(self):
        # The automake <=1.4 form (measured: 57 regions / 7 fingerprints, versions
        # 1.4-p5, 1.4, 1.4-p4, 1.4-p6, 1.5 -- findings doc, "Pattern tuning").
        entry = self._banner('# Makefile.in generated automatically by automake 1.4-p6 from Makefile.am')
        self.assertEqual('automake', entry.generator)
        self.assertEqual('1.4-p6', entry.version)

    def test_automake_pre_1_5_negative(self):
        self._quiet('# this file was not generated automatically by anything at all')

    def test_aclocal_banner_captures_version(self):
        entry = self._banner('# aclocal.m4 generated automatically by aclocal 1.9.6 -*- Autoconf -*-')
        self.assertEqual('aclocal', entry.generator)
        self.assertEqual('1.9.6', entry.version)

    def test_aclocal_negative(self):
        self._quiet('# generated automatically by aclocal-1.9 in a previous life')

    def test_autoheader_banner_has_no_version(self):
        entry = self._banner('/* config.h.in.  Generated from configure.ac by autoheader.  */')
        self.assertEqual('autoheader', entry.generator)
        self.assertIsNone(entry.version)

    def test_autoheader_negative(self):
        self._quiet('/* Generated from configure.ac by autoconf.  */')

    def test_libtool_ltmain_banner_captures_version(self):
        entry = self._banner('# ltmain.sh (GNU libtool) 1.5.22')
        self.assertEqual('libtool', entry.generator)
        self.assertEqual('1.5.22', entry.version)

    def test_libtool_product_name_banner_without_version(self):
        entry = self._banner('# This file is part of GNU Libtool.')
        self.assertEqual('libtool', entry.generator)
        self.assertIsNone(entry.version)

    def test_libtool_negative(self):
        self._quiet('# built with ltmain.sh from GNU libtool somewhere')

    def test_bare_gnu_libtool_prose_is_not_a_banner(self):
        # Narrowed from the bare product name (``\bGNU Libtool\b``), which fired on
        # prose: a fortune cookie and libtool.texi's own manual text (findings doc, gap
        # list). The boilerplate line above keeps all 10 real hits; this kills both FPs.
        self._quiet('-- "GNU Libtool documentation"')
        self._quiet('This manual is for GNU Libtool (version @value{VERSION}).')

    def test_libtool_paste_header_fires_on_acinclude_m4(self):
        # The libtool-paste header: acinclude.m4 is maintainer-written, but pasted
        # libtool.m4 self-identifies -- marks it banner-only, on the strength of its
        # content (findings doc, "acinclude.m4 -- the deliberate-absence question").
        text = _file_diff('acinclude.m4', [
            ' ## libtool.m4 - Configure libtool for the target system. -*-Shell-script-*-',
            '+AC_SOMETHING'])
        entry = _marked(text)['acinclude.m4']
        self.assertEqual(('banner',), entry.signals)
        self.assertEqual('autotools', entry.family)
        self.assertEqual('libtool', entry.generator)

    def test_libtool_paste_header_matches_host_system_too(self):
        entry = self._banner('## libtool.m4 - Configure libtool for the host system.')
        self.assertEqual('libtool', entry.generator)

    def test_generated_marker_is_its_own_family(self):
        entry = self._banner('// @generated by the thing that generates things')
        self.assertEqual('@generated', entry.generator)
        self.assertEqual('marker', entry.family)
        self.assertIsNone(entry.version)

    def test_generated_marker_negative(self):
        self._quiet('// @generatedxyz is not the marker')

    def test_banner_outside_the_name_set_marks_on_the_banner_alone(self):
        # Exactly how the name set's gaps get found: no name match, banner only.
        entry = self._banner('# Generated by GNU Autoconf 2.59.', path='acinclude.m4')
        self.assertEqual(('banner',), entry.signals)

    def test_banner_in_a_context_line_fires(self):
        # The realistic case -- the banner is untouched context above the hunk.
        text = _file_diff('acinclude.m4', [' # Generated by GNU Autoconf 2.13', '+AC_SOMETHING'])
        self.assertEqual(('banner',), _marked(text)['acinclude.m4'].signals)

    def test_banner_in_a_removed_line_fires(self):
        # No added- or context-line banner is present, so the removed-only fallback
        # still fires -- the preference chain loses nothing when it is the only group.
        text = _file_diff('acinclude.m4', ['-# Generated by GNU Autoconf 2.13', '+AC_SOMETHING'])
        self.assertEqual('2.13', _marked(text)['acinclude.m4'].version)

    def test_added_banner_preferred_over_removed(self):
        # The version reported is evidence about the file AS THE PATCH LEAVES IT: a
        # version bump must report the NEW (added) version, not the old (removed) one.
        text = _file_diff('configure', [
            '-# Generated by GNU Autoconf 2.59.',
            '+# Generated by GNU Autoconf 2.68.'])
        entry = _marked(text)['configure']
        self.assertEqual('2.68', entry.version)

    def test_context_banner_preferred_over_removed(self):
        text = _file_diff('acinclude.m4', [
            '-# Generated by GNU Autoconf 2.59.',
            ' # Generated by GNU Autoconf 2.13',
            '+AC_SOMETHING'])
        entry = _marked(text)['acinclude.m4']
        self.assertEqual('2.13', entry.version)

    def test_both_signals_are_reported_sorted(self):
        text = _file_diff('configure', [' # Generated by GNU Autoconf 2.59', '+echo hi'])
        entry = _marked(text)['configure']
        self.assertEqual(('banner', 'name'), entry.signals)
        self.assertEqual('autotools', entry.family)
        self.assertEqual('autoconf', entry.generator)

    def test_first_banner_per_file_wins(self):
        # One generator per file, never an accumulation of every banner present -- and
        # the winner is the first hit WITHIN the preferred group (both banners here are
        # context lines, so it comes down to line order, same as before the added-lines
        # preference was added).
        text = _file_diff('acinclude.m4', [
            ' # Generated by GNU Autoconf 2.59',
            ' # Makefile.in generated by automake 1.9.6 from Makefile.am.',
            '+AC_SOMETHING'])
        entry = _marked(text)['acinclude.m4']
        self.assertEqual('autoconf', entry.generator)
        self.assertEqual('2.59', entry.version)


# ---------------------------------------------------------------------------
# The corroboration tier: ``Makefile.in`` needs a banner to mark at all
# ---------------------------------------------------------------------------

class CorroborationTestCase(testtools.TestCase):

    def test_bare_makefile_in_is_not_marked(self):
        # ~880 of 1,280 corpus Makefile.in matches are hand-written AC_OUTPUT templates
        # in plain-autoconf projects (findings doc, "The Makefile.in problem"); with no
        # banner corroborating the name, the file carries no signals at all.
        self.assertEqual({}, _marked(_edit('Makefile.in')))
        # ``name_family`` itself is unaffected -- corroboration is a scan()-level rule,
        # not a change to what basenames the name set recognises.
        self.assertEqual('autotools', generated.name_family('Makefile.in'))

    def test_makefile_in_with_banner_is_marked_with_both_signals(self):
        text = _file_diff('Makefile.in', [
            ' # Makefile.in generated by automake 1.9.6 from Makefile.am.',
            '-CFLAGS = -O1',
            '+CFLAGS = -O2'])
        entry = _marked(text)['Makefile.in']
        self.assertEqual(('banner', 'name'), entry.signals)
        self.assertEqual('automake', entry.generator)
        self.assertEqual('1.9.6', entry.version)

    def test_gnumakefile_in_no_longer_matches(self):
        # 7 of 7 distinct corpus paths were hand-written (GNUstep / plain-autoconf
        # templates); 0 of 10 carried a banner. Dropped from the name set entirely.
        self.assertIsNone(generated.name_family('GNUmakefile.in'))
        self.assertEqual({}, _marked(_edit('GNUmakefile.in')))

    def test_config_status_and_configure_lineno_no_longer_match(self):
        # 0 corpus hits each -- build-time products that never appear in a carried
        # patch. Dropped from the name set.
        self.assertIsNone(generated.name_family('config.status'))
        self.assertIsNone(generated.name_family('configure.lineno'))
        self.assertEqual({}, _marked(_edit('config.status')))
        self.assertEqual({}, _marked(_edit('configure.lineno')))


# ---------------------------------------------------------------------------
# The config.guess / config.sub datestamp: version evidence, not a signal
# ---------------------------------------------------------------------------

class TimestampVersionTestCase(testtools.TestCase):

    def test_timestamp_on_config_guess_is_version_evidence(self):
        text = _file_diff('config.guess', [" timestamp='2005-07-08'", '-old', '+new'])
        entry = _marked(text)['config.guess']
        self.assertEqual(('name',), entry.signals)
        self.assertEqual('config.guess', entry.generator)
        self.assertEqual('2005-07-08', entry.version)

    def test_timestamp_on_config_sub_is_version_evidence(self):
        text = _file_diff('config.sub', ["+timestamp='2005-07-08'"])
        entry = _marked(text)['config.sub']
        self.assertEqual(('name',), entry.signals)
        self.assertEqual('config.sub', entry.generator)

    def test_timestamp_elsewhere_does_nothing(self):
        text = _file_diff('src/foo.c', ["+static const char *stamp = \"timestamp='2005-07-08'\";"])
        self.assertEqual({}, _marked(text))

    def test_timestamp_on_another_generated_name_does_nothing(self):
        # The datestamp is only meaningful for the two names that carry one. ``configure``
        # (not ``Makefile.in``, which needs a banner to mark at all -- corroboration tier).
        text = _file_diff('configure', [" timestamp='2005-07-08'", '+all:'])
        entry = _marked(text)['configure']
        self.assertIsNone(entry.generator)
        self.assertIsNone(entry.version)


# ---------------------------------------------------------------------------
# A gatos-shaped patch: the arithmetic phase 3 routes on
# ---------------------------------------------------------------------------

GATOS_SHAPED = ''.join([
    # Regenerated configure, with the banner visible as context (2 removed, 4 added).
    _file_diff('configure', [
        ' # Generated by GNU Autoconf 2.59.',
        '-ac_old_one',
        '-ac_old_two',
        '+ac_new_one',
        '+ac_new_two',
        '+ac_new_three',
        '+ac_new_four']),
    # Regenerated Makefile.in, WITH its automake banner as context -- mirrors the real
    # gatos, where all twelve Makefile.in regions carry an automake 1.9.6 banner (1
    # removed, 1 added; the context line adds neither). A bare Makefile.in with no
    # banner would not be marked at all (corroboration tier).
    _file_diff('src/Makefile.in', [
        ' # Makefile.in generated by automake 1.9.6 from Makefile.am.',
        '-CFLAGS = -O1',
        '+CFLAGS = -O2']),
    # A backup of a hand-written input: NOT generated (1 added).
    _file_diff('Makefile.am.ori', ['+EXTRA_DIST = notes']),
    # The hand-written residue (1 added).
    _file_diff('src/foo.c', ['+    int fixed = 1;']),
])


class GatosShapedPatchTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.scan = generated.scan(GATOS_SHAPED)
        self.by_path = {entry.path: entry for entry in self.scan.files}

    def test_only_the_generated_files_are_marked(self):
        self.assertEqual(['configure', 'src/Makefile.in'], sorted(self.by_path))

    def test_marked_files_keep_diff_order(self):
        self.assertEqual(('configure', 'src/Makefile.in'),
                         tuple(entry.path for entry in self.scan.files))

    def test_configure_carries_both_signals_and_the_version(self):
        entry = self.by_path['configure']
        self.assertEqual(('banner', 'name'), entry.signals)
        self.assertEqual('autoconf', entry.generator)
        self.assertEqual('2.59', entry.version)
        self.assertEqual(4, entry.added)
        self.assertEqual(2, entry.removed)

    def test_makefile_in_is_marked_with_both_signals(self):
        entry = self.by_path['src/Makefile.in']
        self.assertEqual(('banner', 'name'), entry.signals)
        self.assertEqual('automake', entry.generator)
        self.assertEqual('1.9.6', entry.version)

    def test_the_backup_of_a_hand_written_input_is_not_marked(self):
        self.assertNotIn('Makefile.am.ori', self.by_path)

    def test_hand_written_source_is_not_marked(self):
        self.assertNotIn('src/foo.c', self.by_path)

    def test_residue_and_generated_sum_to_the_total(self):
        # The identity phase 3's routing depends on.
        self.assertEqual(self.scan.total_changed,
                         self.scan.residue_changed + self.scan.generated_changed)

    def test_the_counts_are_the_hand_analysis(self):
        self.assertEqual(10, self.scan.total_changed)
        self.assertEqual(8, self.scan.generated_changed)
        self.assertEqual(2, self.scan.residue_changed)

    def test_coverage_is_the_generated_fraction(self):
        self.assertAlmostEqual(0.8, self.scan.coverage)

    def test_rule_version_is_recorded(self):
        self.assertEqual(generated.GENERATED_RULES_VERSION, self.scan.rule_version)


# ---------------------------------------------------------------------------
# Nothing to mark
# ---------------------------------------------------------------------------

class NoMatchTestCase(testtools.TestCase):

    def test_empty_diff(self):
        scan = generated.scan('')
        self.assertEqual((), scan.files)
        self.assertEqual(0, scan.total_changed)
        self.assertEqual(0, scan.residue_changed)
        self.assertEqual(0.0, scan.coverage)

    def test_hand_written_diff_marks_nothing(self):
        text = _edit('src/foo.c') + _edit('doc/foo.1')
        scan = generated.scan(text)
        self.assertEqual((), scan.files)
        self.assertEqual(0, scan.generated_changed)
        self.assertEqual(scan.total_changed, scan.residue_changed)
        self.assertEqual(0.0, scan.coverage)

    def test_a_dep3_header_is_skipped(self):
        # Header text is an author claim, not diff content: a banner-shaped line there
        # must not mark anything.
        text = 'Description: Generated by GNU Autoconf 2.59\n\n%s' % _edit('src/foo.c')
        self.assertEqual((), generated.scan(text).files)


# ---------------------------------------------------------------------------
# The candidate family: measured, never marking
# ---------------------------------------------------------------------------

class CandidateBannerTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.text = _file_diff('src/table.h', [
            ' /* DO NOT EDIT: this file is machine-made. */',
            '+static const int table[] = { 1, 2, 3 };'])

    def test_candidate_hit_is_reported(self):
        hits = generated.candidate_banner_hits(self.text)
        self.assertEqual(1, len(hits))
        path, snippet = hits[0]
        self.assertEqual('src/table.h', path)
        self.assertIn('DO NOT EDIT', snippet)

    def test_candidate_hit_does_not_mark(self):
        # The whole point of "candidate": measured in phase 1, marking nothing.
        self.assertEqual((), generated.scan(self.text).files)

    def test_other_candidate_patterns(self):
        for text in ('# Do not edit this file, edit foo.in instead',
                     '# Automatically generated, changes will be lost'):
            hits = generated.candidate_banner_hits(_banner_diff(text))
            self.assertEqual(1, len(hits))

    def test_clean_diff_has_no_candidate_hits(self):
        self.assertEqual([], generated.candidate_banner_hits(_edit('src/foo.c')))


# ---------------------------------------------------------------------------
# The observation detail: dominant family and coverage percent
# ---------------------------------------------------------------------------

# A ``marker``-dominated patch: 6 changed lines of ``@generated`` output against 1 line
# of an autotools name match, plus 2 lines of hand-written residue (7 of 9 generated).
MARKER_DOMINANT = ''.join([
    _file_diff('src/table.h', [
        ' /* @generated by mktable -- do not hand-edit */',
        '+static const int a = 1;',
        '+static const int b = 2;',
        '+static const int c = 3;',
        '+static const int d = 4;',
        '+static const int e = 5;',
        '+static const int f = 6;']),
    _file_diff('configure', ['+ac_new_one']),
    _edit('src/foo.c'),
])

# Two families with EXACTLY the same generated changed-line count (2 apiece), with the
# ``marker`` file first in diff order -- so an ordering-dependent implementation would
# answer ``marker`` and the alphabetical tie break answers ``autotools``.
FAMILY_TIE = ''.join([
    _file_diff('src/table.h', [' /* @generated */', '-old_row', '+new_row']),
    _file_diff('configure', ['-ac_old_one', '+ac_new_one']),
])


class DetailForTestCase(testtools.TestCase):

    def test_gatos_shaped_patch_is_autotools(self):
        # 8 of 10 changed lines generated, all of them autotools.
        self.assertEqual('autotools/80', generated.detail_for(generated.scan(GATOS_SHAPED)))

    def test_marker_family_can_dominate(self):
        # The dominant family is the one with the most generated changed lines (6 marker
        # vs 1 autotools), not the first one seen; 7 of 9 changed lines rounds to 78.
        self.assertEqual('marker/78', generated.detail_for(generated.scan(MARKER_DOMINANT)))

    def test_ties_are_broken_alphabetically(self):
        self.assertEqual('autotools/100', generated.detail_for(generated.scan(FAMILY_TIE)))

    def test_percent_is_the_rounded_coverage(self):
        scan = generated.scan(MARKER_DOMINANT)
        self.assertEqual(str(round(scan.coverage * 100)), generated.detail_for(scan).split('/')[1])

    def test_stable_across_calls(self):
        scan = generated.scan(GATOS_SHAPED)
        self.assertEqual(generated.detail_for(scan), generated.detail_for(scan))

    def test_empty_scan_is_refused(self):
        # The recorder never asks: a scan that marks nothing records no observation.
        self.assertRaises(ValueError, generated.detail_for, generated.scan(''))
        self.assertRaises(ValueError, generated.detail_for, generated.scan(_edit('src/foo.c')))


# ---------------------------------------------------------------------------
# The observation evidence: canonical, stable, uniformly keyed
# ---------------------------------------------------------------------------

class EvidenceForTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.scan = generated.scan(GATOS_SHAPED)
        self.evidence = generated.evidence_for(self.scan)
        self.payload = json.loads(self.evidence)

    def test_two_calls_are_byte_identical(self):
        # The recorder's idempotency skip is a string comparison.
        self.assertEqual(self.evidence, generated.evidence_for(self.scan))
        self.assertEqual(self.evidence, generated.evidence_for(generated.scan(GATOS_SHAPED)))

    def test_keys_are_sorted_in_the_serialised_form(self):
        self.assertEqual(json.dumps(self.payload, sort_keys=True), self.evidence)

    def test_top_level_keys(self):
        self.assertEqual(['files', 'generated_changed', 'residue_changed', 'total_changed'],
                         sorted(self.payload))

    def test_per_file_keys(self):
        for entry in self.payload['files']:
            self.assertEqual(['added', 'family', 'generator', 'path', 'removed', 'signals',
                              'version'], sorted(entry))

    def test_files_are_in_diff_order(self):
        self.assertEqual(['configure', 'src/Makefile.in'],
                         [entry['path'] for entry in self.payload['files']])

    def test_signals_serialise_as_a_list(self):
        entry = self.payload['files'][0]
        self.assertIsInstance(entry['signals'], list)
        self.assertEqual(['banner', 'name'], entry['signals'])

    def test_generator_and_version_carry_through(self):
        entry = self.payload['files'][0]
        self.assertEqual('autoconf', entry['generator'])
        self.assertEqual('2.59', entry['version'])
        self.assertEqual(4, entry['added'])
        self.assertEqual(2, entry['removed'])

    def test_absent_generator_and_version_are_explicit_nulls(self):
        # Uniform schema: the keys are present and null, never omitted.
        payload = json.loads(generated.evidence_for(generated.scan(
            _file_diff('configure', ['+ac_new_one']))))
        entry = payload['files'][0]
        self.assertIn('generator', entry)
        self.assertIn('version', entry)
        self.assertIsNone(entry['generator'])
        self.assertIsNone(entry['version'])
        self.assertEqual(['name'], entry['signals'])

    def test_arithmetic_matches_the_scan(self):
        self.assertEqual(self.scan.generated_changed, self.payload['generated_changed'])
        self.assertEqual(self.scan.residue_changed, self.payload['residue_changed'])
        self.assertEqual(self.scan.total_changed, self.payload['total_changed'])

    def test_unmarked_scan_still_serialises(self):
        # Not what the recorder does with it (it records nothing), but the helper is
        # total: an empty file list with honest arithmetic.
        payload = json.loads(generated.evidence_for(generated.scan(_edit('src/foo.c'))))
        self.assertEqual([], payload['files'])
        self.assertEqual(0, payload['generated_changed'])
        self.assertEqual(2, payload['residue_changed'])


# ---------------------------------------------------------------------------
# The consumer side: live observations parsed back out of a ledger
# ---------------------------------------------------------------------------

WHEN = '2026-07-25T00:00:00+00:00'

FP_LIVE = 'a' * 64
FP_SUPERSEDED = 'b' * 64
FP_OTHER_KIND = 'c' * 64
FP_MALFORMED = 'd' * 64


class GeneratedMarksTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = ledger_mod.create_ledger(os.path.join(self.tmp.name, 'ledger.sqlite'))
        self.addCleanup(self.conn.close)

        self.scan = generated.scan(GATOS_SHAPED)
        self.detail = generated.detail_for(self.scan)
        self.evidence = generated.evidence_for(self.scan)

        self._append(FP_LIVE, generated.GENERATED_KIND, self.detail, self.evidence)

        # A prior row for another fingerprint, superseded: never returned.
        self._append(FP_SUPERSEDED, generated.GENERATED_KIND, 'autotools/50', self.evidence)
        ledger_mod.supersede_observations_for_fingerprint(
            self.conn, fingerprint=FP_SUPERSEDED, kind=generated.GENERATED_KIND,
            superseded_at=WHEN)

        # A live observation of an unrelated kind: never returned.
        self._append(FP_OTHER_KIND, 'reviewability', 'oversized',
                     json.dumps({'changed_lines': 9000}, sort_keys=True))

    def _append(self, fingerprint, kind, detail, evidence, observed_by=None):
        ledger_mod.append_observation(
            self.conn, fingerprint=fingerprint, kind=kind, detail=detail, evidence=evidence,
            observed_by=observed_by or generated.GENERATED_OBSERVED_BY,
            rule_version=generated.GENERATED_RULES_VERSION, observed_at=WHEN)

    def test_only_the_live_generated_row_comes_back(self):
        self.assertEqual([FP_LIVE], sorted(generated.generated_marks(self.conn)))

    def test_detail_is_carried_through(self):
        self.assertEqual(self.detail, generated.generated_marks(self.conn)[FP_LIVE]['detail'])

    def test_evidence_is_parsed(self):
        mark = generated.generated_marks(self.conn)[FP_LIVE]
        self.assertEqual(['configure', 'src/Makefile.in'],
                         [entry['path'] for entry in mark['files']])
        self.assertEqual(['banner', 'name'], mark['files'][0]['signals'])
        self.assertEqual('2.59', mark['files'][0]['version'])

    def test_the_arithmetic_round_trips(self):
        mark = generated.generated_marks(self.conn)[FP_LIVE]
        self.assertEqual(self.scan.generated_changed, mark['generated_changed'])
        self.assertEqual(self.scan.residue_changed, mark['residue_changed'])
        self.assertEqual(self.scan.total_changed, mark['total_changed'])

    def test_record_keys(self):
        self.assertEqual(['detail', 'files', 'generated_changed', 'residue_changed',
                          'total_changed'],
                         sorted(generated.generated_marks(self.conn)[FP_LIVE]))

    def test_malformed_evidence_is_skipped_not_raised(self):
        # The ledger is append-only operator data: one bad row must not brick the axis.
        for evidence in (None, 'not json at all {', '"a bare string"', '[]',
                         json.dumps({'files': 'not a list'}),
                         json.dumps({'files': []})):
            self._append(FP_MALFORMED, generated.GENERATED_KIND, 'autotools/99', evidence)
            marks = generated.generated_marks(self.conn)
            self.assertNotIn(FP_MALFORMED, marks)
            self.assertIn(FP_LIVE, marks)
            ledger_mod.supersede_observations_for_fingerprint(
                self.conn, fingerprint=FP_MALFORMED, kind=generated.GENERATED_KIND,
                superseded_at=WHEN)

    def test_empty_ledger_is_an_empty_map(self):
        conn = ledger_mod.create_ledger(os.path.join(self.tmp.name, 'empty.sqlite'))
        self.addCleanup(conn.close)
        self.assertEqual({}, generated.generated_marks(conn))
