"""Tests for divergulent.classify.review -- the step-4e signed human-review tool.

All tests are OFFLINE: every external boundary is injected.  The source ``fetch``
returns a canned original file (no network), the ``signer`` returns a fixed
``('FAKE-SIG', 'reviewer@example.org')`` (no browser/OIDC, no real sigstore), the
``ask`` returns a scripted choice (no real stdin), and ``now`` is a fixed
timestamp (no clock).  A small synthetic corpus (a content-addressed body + a
phase-1 fingerprint index) and a ledger seeded with a pending review item and the
matching ``kind='llm'`` draft decision are built by hand.

Coverage:

  * an accept records a ``kind='human'`` decision with ``verified=1``, the
    signature/signed_by set, and the LLM draft's category; ``mark_reviewed``
    cleared the queue item; the human decision tops the verdict;
  * an override records the OVERRIDE category (not the draft's);
  * a defer leaves the item pending and records nothing;
  * ``fetch_source_file`` builds the right sources.debian.org URL (asserted
    against the injected fetch's recorded URL);
  * ``render_in_context`` shows the change against the original;
  * the canonical record is deterministic over (fingerprint, category, when);
  * the default sigstore signer raises the clear extra-absent error when
    ``sigstore`` is missing (skipped if it is installed).
"""
import os
import sqlite3
import tempfile

import testtools

from divergulent.classify import ledger as ledger_mod
from divergulent.classify import review
from divergulent.classify import verdict as verdict_mod
from divergulent.classify.corpus import body_sha256
from divergulent.classify.fingerprint import fingerprint


WHEN = '2026-06-14T00:00:00Z'

# A code patch with a DEP-3 header (the claim) and a diff body whose hunk lands
# in the middle of the original file below, so render-in-context can frame it.
PATCH = (
    'Description: enlarge the read buffer to avoid truncation\n'
    'Forwarded: no\n'
    '\n'
    '--- a/src/reader.c\n'
    '+++ b/src/reader.c\n'
    '@@ -3,3 +3,3 @@\n'
    ' int read_line(FILE *fp) {\n'
    '-    char buf[64];\n'
    '+    char buf[4096];\n'
    ' \n')

# The original (pre-patch) upstream file the fetch returns -- contains the
# original ``buf[64]`` line the diff replaces.
ORIGINAL = (
    '#include <stdio.h>\n'
    '\n'
    'int read_line(FILE *fp) {\n'
    '    char buf[64];\n'
    '\n'
    '    return fgets(buf, sizeof(buf), fp) ? 0 : -1;\n'
    '}\n')

SOURCE_PACKAGE = 'reader'
VERSION = '1.2-3'
PATCH_NAME = 'fix-buffer.patch'


def _fp(text):
    return fingerprint(text)[1]


def _build_corpus(corpus_dir):
    """Lay down the content-addressed body + a phase-1 fingerprint index.

    Returns ``(index_path, fingerprint_hex)`` for the single seeded patch.
    """
    sha = body_sha256(PATCH)
    directory = os.path.join(corpus_dir, 'bodies', sha[:2])
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, sha), 'w', encoding='utf-8') as handle:
        handle.write(PATCH)

    index_path = os.path.join(corpus_dir, 'fingerprints.sqlite')
    connection = sqlite3.connect(index_path)
    try:
        connection.execute(
            'CREATE TABLE patch ('
            'source_package TEXT NOT NULL, version TEXT NOT NULL, '
            'patch_name TEXT NOT NULL, raw_sha256 TEXT NOT NULL, '
            'normalisation_version INTEGER NOT NULL, fingerprint TEXT NOT NULL)')
        connection.execute(
            'INSERT INTO patch (source_package, version, patch_name, raw_sha256, '
            'normalisation_version, fingerprint) VALUES (?, ?, ?, ?, ?, ?)',
            (SOURCE_PACKAGE, VERSION, PATCH_NAME, sha, 1, _fp(PATCH)))
        connection.commit()
    finally:
        connection.close()
    return index_path, _fp(PATCH)


def _recording_fetch(original=ORIGINAL):
    """A fake ``fetch`` that records the URL it was asked for and returns ``original``."""
    seen = {}

    def fetch(url):
        seen['url'] = url
        return original

    return fetch, seen


def _fake_signer(signature='FAKE-SIG', signed_by='reviewer@example.org'):
    """A fake ``signer`` recording the bytes it signed; returns a fixed pair."""
    seen = {}

    def signer(record_bytes):
        seen['record_bytes'] = record_bytes
        return signature, signed_by

    return signer, seen


def _scripted_ask(choice):
    """A fake ``ask`` that records the context it was shown and returns ``choice``."""
    seen = {}

    def ask(context):
        seen['context'] = context
        return choice

    return ask, seen


class ReviewFixture:
    """Mixin: a synthetic corpus + a ledger with a pending item and an llm draft."""

    def _setup(self, *, draft_category='bugfix'):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        corpus_dir = tmp.name

        index_path, fp_hex = _build_corpus(corpus_dir)

        ledger_path = os.path.join(corpus_dir, 'ledger.sqlite')
        conn = ledger_mod.create_ledger(ledger_path)
        self.addCleanup(conn.close)
        ledger_mod.register_rules(conn, ledger_mod.default_registry())

        # A heuristic 'unknown' baseline so the fingerprint is genuinely residue.
        ledger_mod.append_decision(
            conn, fingerprint=fp_hex, category='unknown', confidence='low',
            decided_by='substantive', rule_version=1, kind='heuristic',
            evidence=None, decided_at=WHEN, commit=False)
        # The llm draft (kind='llm') the reviewer accepts or overrides.
        ledger_mod.append_decision(
            conn, fingerprint=fp_hex, category=draft_category, confidence='medium',
            decided_by='llm-triage:claude-sonnet-4-6', rule_version=1, kind='llm',
            verified=False, evidence='{"draft": {"reasoning": "enlarges a buffer"}}',
            decided_at=WHEN, commit=False)
        # The pending review item.
        ledger_mod.append_review_item(
            conn, fingerprint=fp_hex, reason='verifier refuted the drafted category',
            draft_category=draft_category, draft_confidence='medium',
            enqueued_at=WHEN, priority=5)
        conn.commit()

        return conn, corpus_dir, index_path, fp_hex

    def _item(self, conn):
        items = ledger_mod.pending_review_items(conn)
        self.assertEqual(1, len(items))
        return items[0]


class ReviewOneTestCase(ReviewFixture, testtools.TestCase):

    def test_accept_records_signed_human_decision_and_clears_item(self):
        conn, corpus_dir, index_path, fp_hex = self._setup(draft_category='bugfix')
        fetch, _seen_fetch = _recording_fetch()
        signer, _seen_signer = _fake_signer()
        ask, _seen_ask = _scripted_ask(review.CHOICE_ACCEPT)

        outcome = review.review_one(
            conn, corpus_dir, index_path, self._item(conn),
            fetch=fetch, signer=signer, ask=ask, now=WHEN)

        self.assertTrue(outcome.recorded)
        self.assertFalse(outcome.deferred)
        self.assertEqual('bugfix', outcome.category)

        rows = [r for r in ledger_mod.decisions_for(conn, fp_hex) if r['kind'] == 'human']
        self.assertEqual(1, len(rows))
        human = rows[0]
        self.assertEqual('bugfix', human['category'])      # accepted the draft
        self.assertEqual('human', human['kind'])
        self.assertEqual(1, human['verified'])
        self.assertEqual('FAKE-SIG', human['signature'])
        self.assertEqual('reviewer@example.org', human['signed_by'])
        self.assertEqual(review.DECIDED_BY, human['decided_by'])
        self.assertEqual(WHEN, human['decided_at'])

        # The queue item was cleared (no longer pending).
        self.assertEqual([], ledger_mod.pending_review_items(conn))

    def test_human_decision_tops_the_verdict(self):
        conn, corpus_dir, index_path, fp_hex = self._setup(draft_category='security')
        fetch, _ = _recording_fetch()
        signer, _ = _fake_signer()
        ask, _ = _scripted_ask(review.CHOICE_ACCEPT)

        review.review_one(
            conn, corpus_dir, index_path, self._item(conn),
            fetch=fetch, signer=signer, ask=ask, now=WHEN)

        verdicts = verdict_mod.current_verdict(conn)
        winner = verdicts[fp_hex]
        self.assertEqual('human', winner.kind)
        self.assertEqual('security', winner.category)
        self.assertTrue(winner.verified)

    def test_override_records_the_override_category(self):
        conn, corpus_dir, index_path, fp_hex = self._setup(draft_category='bugfix')
        fetch, _ = _recording_fetch()
        signer, _ = _fake_signer()
        ask, _ = _scripted_ask('security')  # override the draft

        outcome = review.review_one(
            conn, corpus_dir, index_path, self._item(conn),
            fetch=fetch, signer=signer, ask=ask, now=WHEN)

        self.assertEqual('security', outcome.category)
        human = [r for r in ledger_mod.decisions_for(conn, fp_hex) if r['kind'] == 'human'][0]
        self.assertEqual('security', human['category'])

    def test_defer_leaves_item_pending_and_records_nothing(self):
        conn, corpus_dir, index_path, fp_hex = self._setup()
        fetch, _ = _recording_fetch()
        signer, signer_seen = _fake_signer()
        ask, _ = _scripted_ask(review.CHOICE_DEFER)

        outcome = review.review_one(
            conn, corpus_dir, index_path, self._item(conn),
            fetch=fetch, signer=signer, ask=ask, now=WHEN)

        self.assertFalse(outcome.recorded)
        self.assertTrue(outcome.deferred)
        self.assertIsNone(outcome.category)
        # No human decision, the item is still pending, and the signer was not called.
        self.assertEqual(
            [], [r for r in ledger_mod.decisions_for(conn, fp_hex) if r['kind'] == 'human'])
        self.assertEqual(1, len(ledger_mod.pending_review_items(conn)))
        self.assertNotIn('record_bytes', signer_seen)

    def test_signed_bytes_are_the_canonical_record(self):
        conn, corpus_dir, index_path, fp_hex = self._setup(draft_category='bugfix')
        fetch, _ = _recording_fetch()
        signer, signer_seen = _fake_signer()
        ask, _ = _scripted_ask(review.CHOICE_ACCEPT)

        review.review_one(
            conn, corpus_dir, index_path, self._item(conn),
            fetch=fetch, signer=signer, ask=ask, now=WHEN)

        self.assertEqual(
            review.canonical_record(fp_hex, 'bugfix', WHEN),
            signer_seen['record_bytes'])

    def test_context_shown_to_ask_includes_draft_and_claim(self):
        conn, corpus_dir, index_path, fp_hex = self._setup(draft_category='bugfix')
        fetch, _ = _recording_fetch()
        signer, _ = _fake_signer()
        ask, ask_seen = _scripted_ask(review.CHOICE_DEFER)

        review.review_one(
            conn, corpus_dir, index_path, self._item(conn),
            fetch=fetch, signer=signer, ask=ask, now=WHEN)

        context = ask_seen['context']
        self.assertEqual('bugfix', context.draft_category)
        self.assertEqual('medium', context.draft_confidence)
        self.assertEqual('enlarges a buffer', context.draft_reasoning)
        # The claim category is derived from the DEP-3 header ('enlarge' -> feature
        # is not matched; 'buffer'/'truncation' are not security keywords) -- assert
        # it is a real category string, and that the original framed the change.
        self.assertIn(context.claim_category, (
            'bugfix', 'feature', 'documentation', 'packaging', 'security', 'unknown'))
        self.assertIn('char buf[64]', context.context_view)   # original context
        self.assertIn('+    char buf[4096];', context.context_view)  # the change

    def test_fetches_the_modified_source_file_not_the_patch_filename(self):
        # Regression: the original-context fetch must use the file the patch
        # MODIFIES (``+++ b/src/reader.c``), not the quilt patch filename
        # (``fix-buffer.patch``), which would 404 -> "no original available".
        conn, corpus_dir, index_path, fp_hex = self._setup(draft_category='bugfix')
        fetch, seen = _recording_fetch()
        signer, _ = _fake_signer()
        ask, _ = _scripted_ask(review.CHOICE_DEFER)

        review.review_one(
            conn, corpus_dir, index_path, self._item(conn),
            fetch=fetch, signer=signer, ask=ask, now=WHEN)

        self.assertEqual(
            'https://sources.debian.org/data/main/r/reader/1.2-3/src/reader.c',
            seen['url'])
        self.assertNotIn(PATCH_NAME, seen['url'])

    def test_context_carries_the_package_names(self):
        conn, corpus_dir, index_path, _fp = self._setup()
        ask, ask_seen = _scripted_ask(review.CHOICE_DEFER)
        review.review_one(
            conn, corpus_dir, index_path, self._item(conn),
            fetch=_recording_fetch()[0], signer=_fake_signer()[0], ask=ask, now=WHEN)
        context = ask_seen['context']
        self.assertEqual(SOURCE_PACKAGE, context.source_package)
        self.assertEqual(VERSION, context.version)
        self.assertEqual((SOURCE_PACKAGE,), context.packages)


class BuildReviewContextTestCase(ReviewFixture, testtools.TestCase):
    """The fingerprint-keyed context builder review_one and the web UI share."""

    def test_returns_none_for_missing_representative_row(self):
        # A fingerprint with no row in the phase-1 index has no body to show.
        conn, corpus_dir, index_path, _fp = self._setup()
        context = review.build_review_context(
            conn, corpus_dir, index_path, fingerprint='a' * 64,
            fetch=_recording_fetch()[0])
        self.assertIsNone(context)

    def test_carries_patch_name_and_no_reason_without_an_item(self):
        # Built straight from a fingerprint (the audit/spot-check path): patch_name
        # rides along for the evidence blob; reason is None (no queue item).
        conn, corpus_dir, index_path, fp_hex = self._setup()
        context = review.build_review_context(
            conn, corpus_dir, index_path, fingerprint=fp_hex,
            fetch=_recording_fetch()[0])
        self.assertEqual(PATCH_NAME, context.patch_name)
        self.assertIsNone(context.reason)

    def test_takes_the_reason_from_the_queue_item_when_given(self):
        conn, corpus_dir, index_path, fp_hex = self._setup()
        item = self._item(conn)
        context = review.build_review_context(
            conn, corpus_dir, index_path, fingerprint=fp_hex, item=item,
            fetch=_recording_fetch()[0])
        self.assertEqual(item['reason'], context.reason)


class RecordReviewVerdictTestCase(ReviewFixture, testtools.TestCase):
    """The record half of the split records the same signed decision as before."""

    def test_records_the_byte_identical_canonical_record_and_clears_item(self):
        conn, corpus_dir, index_path, fp_hex = self._setup(draft_category='bugfix')
        item = self._item(conn)
        signer, signer_seen = _fake_signer()
        context = review.build_review_context(
            conn, corpus_dir, index_path, fingerprint=fp_hex, item=item,
            fetch=_recording_fetch()[0])

        outcome = review.record_review_verdict(
            conn, item, context, review.CHOICE_ACCEPT, signer=signer, now=WHEN)

        self.assertTrue(outcome.recorded)
        self.assertEqual('bugfix', outcome.category)
        # The bytes handed to the signer are exactly the canonical record.
        self.assertEqual(
            review.canonical_record(fp_hex, 'bugfix', WHEN), signer_seen['record_bytes'])
        human = [r for r in ledger_mod.decisions_for(conn, fp_hex) if r['kind'] == 'human'][0]
        self.assertEqual('bugfix', human['category'])
        self.assertEqual('FAKE-SIG', human['signature'])
        self.assertEqual([], ledger_mod.pending_review_items(conn))

    def test_defer_records_nothing_and_leaves_the_item_pending(self):
        conn, corpus_dir, index_path, fp_hex = self._setup()
        item = self._item(conn)
        signer, signer_seen = _fake_signer()
        context = review.build_review_context(
            conn, corpus_dir, index_path, fingerprint=fp_hex, item=item,
            fetch=_recording_fetch()[0])

        outcome = review.record_review_verdict(
            conn, item, context, review.CHOICE_DEFER, signer=signer, now=WHEN)

        self.assertFalse(outcome.recorded)
        self.assertNotIn('record_bytes', signer_seen)
        self.assertEqual(1, len(ledger_mod.pending_review_items(conn)))


class FetchSourceFileTestCase(testtools.TestCase):

    def test_builds_sources_debian_org_url(self):
        fetch, seen = _recording_fetch()
        text = review.fetch_source_file(
            'reader', '1.2-3', 'src/reader.c', fetch=fetch)
        self.assertEqual(ORIGINAL, text)
        self.assertEqual(
            'https://sources.debian.org/data/main/r/reader/1.2-3/src/reader.c',
            seen['url'])

    def test_lib_prefix_uses_four_char_pool_prefix(self):
        self.assertEqual(
            'https://sources.debian.org/data/main/libf/libfoo/2-1/foo.c',
            review.source_file_url('libfoo', '2-1', 'foo.c'))

    def test_missing_original_returns_none(self):
        def fetch(url):
            return None
        self.assertIsNone(
            review.fetch_source_file('reader', '1.2-3', 'src/reader.c', fetch=fetch))

    def test_epoch_version_falls_back_to_stripped_path(self):
        # sources.debian.org strips the Debian epoch from its /data path, so the
        # with-epoch URL 404s (returns None) and the stripped form must be tried.
        seen = []

        def fetch(url):
            seen.append(url)
            return None if '%3A' in url else ORIGINAL  # the epoch colon, URL-quoted

        text = review.fetch_source_file('reader', '1:1.2-3', 'src/reader.c', fetch=fetch)
        self.assertEqual(ORIGINAL, text)
        self.assertEqual(
            ['https://sources.debian.org/data/main/r/reader/1%3A1.2-3/src/reader.c',
             'https://sources.debian.org/data/main/r/reader/1.2-3/src/reader.c'],
            seen)


class RenderInContextTestCase(testtools.TestCase):

    def test_renders_change_against_original(self):
        diff_body = (
            '--- a/src/reader.c\n'
            '+++ b/src/reader.c\n'
            '@@ -3,3 +3,3 @@\n'
            ' int read_line(FILE *fp) {\n'
            '-    char buf[64];\n'
            '+    char buf[4096];\n'
            ' \n')
        rendered = review.render_in_context(ORIGINAL, diff_body)
        # The removed and added lines are both visible, and surrounding original
        # context frames them.
        self.assertIn('-    char buf[64];', rendered)
        self.assertIn('+    char buf[4096];', rendered)
        self.assertIn('#include <stdio.h>', rendered)

    def test_no_original_falls_back_to_raw_diff(self):
        rendered = review.render_in_context(None, '--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n')
        self.assertIn('no original', rendered)
        self.assertIn('+y', rendered)


def _context(*, packages, source_package='reader', version='1.2-3'):
    """A minimal ReviewContext for exercising the package-line formatter."""
    return review.ReviewContext(
        fingerprint='f' * 64, diff_body='', context_view='',
        draft_category=None, draft_confidence=None, draft_reasoning=None,
        claim_category='unknown', reason=None,
        source_package=source_package, version=version, patch_name='fix.patch',
        packages=tuple(packages))


class _FakeExpired(Exception):
    """Stands in for sigstore.oidc.ExpiredIdentity in signer-refresh tests."""


class SignWithRefreshTestCase(testtools.TestCase):
    """The signer retries ONCE after re-auth when the identity token has expired,
    so a slow read never loses a verdict (and a fast one never re-auths)."""

    def test_success_does_not_refresh(self):
        calls = {'attempt': 0, 'refresh': 0}

        def attempt():
            calls['attempt'] += 1
            return 'sig'

        def refresh():
            calls['refresh'] += 1

        result = review._sign_with_refresh(attempt, refresh, (_FakeExpired,))
        self.assertEqual('sig', result)
        self.assertEqual(1, calls['attempt'])
        self.assertEqual(0, calls['refresh'])

    def test_expiry_refreshes_once_then_succeeds(self):
        calls = {'attempt': 0, 'refresh': 0}

        def attempt():
            calls['attempt'] += 1
            if calls['attempt'] == 1:
                raise _FakeExpired()
            return 'sig-after-reauth'

        def refresh():
            calls['refresh'] += 1

        result = review._sign_with_refresh(attempt, refresh, (_FakeExpired,))
        self.assertEqual('sig-after-reauth', result)
        self.assertEqual(2, calls['attempt'])   # retried exactly once
        self.assertEqual(1, calls['refresh'])   # re-authenticated once

    def test_second_consecutive_expiry_propagates(self):
        def attempt():
            raise _FakeExpired()

        self.assertRaises(
            _FakeExpired, review._sign_with_refresh, attempt, lambda: None, (_FakeExpired,))


class AssignableCategoriesTestCase(testtools.TestCase):
    """A human reviewer may assign the full category enum, including `test`."""

    def test_includes_test_and_all_llm_categories(self):
        from divergulent.classify.triage import TRIAGE_CATEGORIES
        cats = review._assignable_categories()
        self.assertIn('test', cats)
        for c in TRIAGE_CATEGORIES:
            self.assertIn(c, cats)

    def test_interactive_ask_accepts_test(self):
        import io
        import sys
        original = sys.stdin
        sys.stdin = io.StringIO('test\n')
        self.addCleanup(setattr, sys, 'stdin', original)
        choice = review._interactive_ask(_context(packages=['python3-stdlib-extensions']))
        self.assertEqual('test', choice)


class FormatPackageLinesTestCase(testtools.TestCase):
    """The review UI names the representative package and the full blast radius."""

    def test_single_package_shows_only_the_representative(self):
        lines = review._format_package_lines(_context(packages=['reader']))
        self.assertEqual(['package: reader (1.2-3)'], lines)

    def test_multiple_packages_listed(self):
        lines = review._format_package_lines(
            _context(packages=['alpha', 'beta', 'reader']))
        self.assertEqual('package: reader (1.2-3)', lines[0])
        self.assertEqual('carried by 3 packages: alpha, beta, reader', lines[1])

    def test_long_list_is_truncated_with_count(self):
        packages = ['p%02d' % i for i in range(20)]
        lines = review._format_package_lines(_context(packages=packages), limit=8)
        self.assertIn('carried by 20 packages: p00, p01, p02, p03, p04, p05, p06, p07', lines[1])
        self.assertIn('(+12 more)', lines[1])


class SplitDiffByFileTestCase(testtools.TestCase):

    def test_single_file_keyed_by_target_path(self):
        segments = review.split_diff_by_file(
            '--- a/src/reader.c\n+++ b/src/reader.c\n@@ -1 +1 @@\n-x\n+y\n')
        self.assertEqual(1, len(segments))
        self.assertEqual('src/reader.c', segments[0].path)
        self.assertIn('+y', segments[0].body)

    def test_multi_file_splits_into_one_segment_each(self):
        segments = review.split_diff_by_file(
            '--- a/src/foo.c\n+++ b/src/foo.c\n@@ -1 +1 @@\n-a\n+b\n'
            '--- a/src/bar.c\n+++ b/src/bar.c\n@@ -2 +2 @@\n-c\n+d\n')
        self.assertEqual(['src/foo.c', 'src/bar.c'], [s.path for s in segments])
        self.assertIn('+b', segments[0].body)
        self.assertNotIn('+d', segments[0].body)  # bar's hunk did not leak into foo
        self.assertIn('+d', segments[1].body)

    def test_deletion_uses_source_path_when_target_is_dev_null(self):
        segments = review.split_diff_by_file(
            '--- a/src/gone.c\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n')
        self.assertEqual('src/gone.c', segments[0].path)

    def test_no_file_header_yields_no_segments(self):
        self.assertEqual([], review.split_diff_by_file('not a diff at all\n'))


class SourceTreePathTestCase(testtools.TestCase):
    """The sources.debian.org fetch path: strip a tarball root, keep real paths."""

    def _seg(self, old, new):
        body = '--- %s\n+++ %s\n@@ -1 +1 @@\n-x\n+y\n' % (old, new)
        return review.split_diff_by_file(body)[0]

    def test_quilt_ab_prefix_is_already_root_relative(self):
        # The common case: a/ b/ stripped, nothing more to strip.
        seg = self._seg('a/src/reader.c', 'b/src/reader.c')
        self.assertEqual('src/reader.c', review._source_tree_path(seg))

    def test_two_tree_orig_suffix_strips_the_root(self):
        # diff -ruN <root>.orig/<path> <root>/<path> -> drop the root component.
        seg = self._seg(
            'llvm-snapshot_17~++20230517.orig/llvm/utils/lit/lit/ProgressBar.py',
            'llvm-snapshot_17~++20230517/llvm/utils/lit/lit/ProgressBar.py')
        self.assertEqual('llvm/utils/lit/lit/ProgressBar.py', review._source_tree_path(seg))

    def test_two_tree_non_versioned_root_with_orig_suffix_strips(self):
        # .orig suffix is definitive even when the root is not version-shaped.
        seg = self._seg('pgpainless.orig/build.gradle', 'pgpainless/build.gradle')
        self.assertEqual('build.gradle', review._source_tree_path(seg))

    def test_shared_versioned_root_without_suffix_strips(self):
        # Both sides name the same versioned tarball dir, no .orig suffix.
        seg = self._seg('botan-2.12.0/src/os/hurd.txt', 'botan-2.12.0/src/os/hurd.txt')
        self.assertEqual('src/os/hurd.txt', review._source_tree_path(seg))

    def test_bare_path_against_a_real_subdir_is_not_stripped(self):
        # No a/ b/, no version, no .orig: src/ is a real subdir -> leave it alone.
        seg = self._seg('src/reader.c', 'src/reader.c')
        self.assertEqual('src/reader.c', review._source_tree_path(seg))

    def test_single_component_path_is_unchanged(self):
        seg = self._seg('a/Makefile', 'b/Makefile')
        self.assertEqual('Makefile', review._source_tree_path(seg))


class BuildContextViewTestCase(testtools.TestCase):

    def test_fetches_each_file_by_its_real_path(self):
        seen = []

        def fetch(url):
            seen.append(url)
            return None

        review.build_context_view(
            'reader', '1.2-3',
            '--- a/src/foo.c\n+++ b/src/foo.c\n@@ -1 +1 @@\n-a\n+b\n'
            '--- a/inc/bar.h\n+++ b/inc/bar.h\n@@ -2 +2 @@\n-c\n+d\n',
            fetch=fetch)

        self.assertEqual(
            ['https://sources.debian.org/data/main/r/reader/1.2-3/src/foo.c',
             'https://sources.debian.org/data/main/r/reader/1.2-3/inc/bar.h'],
            seen)

    def test_no_parseable_files_renders_raw_diff(self):
        view = review.build_context_view(
            'reader', '1.2-3', 'not a diff\n', fetch=lambda url: None)
        self.assertIn('no original', view)


class CanonicalRecordTestCase(testtools.TestCase):

    def test_deterministic_over_inputs(self):
        a = review.canonical_record('fp1', 'bugfix', WHEN)
        b = review.canonical_record('fp1', 'bugfix', WHEN)
        self.assertEqual(a, b)

    def test_distinct_inputs_distinct_record(self):
        self.assertNotEqual(
            review.canonical_record('fp1', 'bugfix', WHEN),
            review.canonical_record('fp1', 'security', WHEN))

    def test_record_carries_the_fields(self):
        import json
        record = json.loads(review.canonical_record('fp1', 'bugfix', WHEN, note='looks ok'))
        self.assertEqual('fp1', record['fingerprint'])
        self.assertEqual('bugfix', record['category'])
        self.assertEqual(WHEN, record['decided_at'])
        self.assertEqual('looks ok', record['note'])
        self.assertEqual(review.REVIEW_RULE_VERSION, record['rule_version'])


class SigstoreSignerAbsentTestCase(testtools.TestCase):

    def test_missing_sigstore_raises_clear_error(self):
        try:
            import sigstore  # noqa: F401
        except ImportError:
            pass
        else:
            self.skipTest('sigstore is installed; the absent-extra path cannot be exercised')

        exc = self.assertRaises(
            RuntimeError, review.sigstore_signer, b'record')
        self.assertIn('pip install divergulent[verify]', str(exc))


class RealFetchTestCase(testtools.TestCase):
    """The real fetch builder is wired by the CLI but injected away in every
    other test, so it had no coverage. Exercise its construction directly: it
    must build an HttpClient over a real Cache and return a callable without
    touching the network (Cache stores its root lazily). This guards the
    ``Cache(default_cache_dir())`` wiring that a missing argument once broke."""

    def test_real_fetch_builds_a_callable(self):
        # Point the cache at a temp dir so nothing under $HOME is created.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = os.environ.get('DIVERGULENT_CACHE_DIR')
        os.environ['DIVERGULENT_CACHE_DIR'] = tmp.name

        def restore():
            if old is None:
                os.environ.pop('DIVERGULENT_CACHE_DIR', None)
            else:
                os.environ['DIVERGULENT_CACHE_DIR'] = old
        self.addCleanup(restore)

        fetch = review._real_fetch()
        self.assertTrue(callable(fetch))


class PagerTestCase(testtools.TestCase):
    """The pager must fall back to plain print when stdout is not a TTY, so
    scripted/non-interactive use (and the test suite) is unaffected."""

    def test_page_prints_when_not_a_tty(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            review._page('hello-context')
        self.assertIn('hello-context', buf.getvalue())


class ResolveFingerprintTestCase(testtools.TestCase):
    """``resolve_fingerprint`` matches a full hex or an unambiguous prefix."""

    def _ledger(self, *fingerprints):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = ledger_mod.create_ledger(os.path.join(tmp.name, 'ledger.sqlite'))
        self.addCleanup(conn.close)
        for fp_hex in fingerprints:
            ledger_mod.append_decision(
                conn, fingerprint=fp_hex, category='unknown', confidence='low',
                decided_by='substantive', rule_version=1, kind='heuristic',
                evidence=None, decided_at=WHEN, commit=False)
        conn.commit()
        return conn

    def test_full_fingerprint_resolves(self):
        conn = self._ledger('a' * 64, 'b' * 64)
        resolved, matches = review.resolve_fingerprint(conn, 'a' * 64)
        self.assertEqual('a' * 64, resolved)
        self.assertEqual(['a' * 64], matches)

    def test_unique_prefix_resolves(self):
        conn = self._ledger('abc' + '0' * 61, 'def' + '0' * 61)
        resolved, _ = review.resolve_fingerprint(conn, 'abc')
        self.assertEqual('abc' + '0' * 61, resolved)

    def test_ambiguous_prefix_returns_none_with_candidates(self):
        conn = self._ledger('abc1' + '0' * 60, 'abc2' + '0' * 60)
        resolved, matches = review.resolve_fingerprint(conn, 'abc')
        self.assertIsNone(resolved)
        self.assertEqual(2, len(matches))

    def test_unknown_returns_none_empty(self):
        conn = self._ledger('a' * 64)
        resolved, matches = review.resolve_fingerprint(conn, 'zzz')
        self.assertIsNone(resolved)
        self.assertEqual([], matches)


class RequeueTestCase(ReviewFixture, testtools.TestCase):
    """``requeue_one`` supersedes the live human verdict and makes the fingerprint
    pending again -- re-opening its item, or creating one when none exists."""

    def _review_it(self, conn, corpus_dir, index_path):
        fetch, _ = _recording_fetch()
        signer, _ = _fake_signer()
        ask, _ = _scripted_ask(review.CHOICE_ACCEPT)
        return review.review_one(
            conn, corpus_dir, index_path, self._item(conn),
            fetch=fetch, signer=signer, ask=ask, now=WHEN)

    def test_requeue_supersedes_human_and_reopens_item(self):
        conn, corpus_dir, index_path, fp_hex = self._setup(draft_category='bugfix')
        self._review_it(conn, corpus_dir, index_path)
        self.assertEqual([], ledger_mod.pending_review_items(conn))  # cleared by review

        outcome = review.requeue_one(conn, fp_hex, now='2026-06-15T00:00:00Z')
        conn.commit()

        self.assertEqual(1, outcome.superseded)
        self.assertEqual(1, outcome.reopened)
        self.assertFalse(outcome.created)
        # Pending again, and no LIVE human decision remains (verdict falls back).
        self.assertEqual(
            [fp_hex], [p['fingerprint'] for p in ledger_mod.pending_review_items(conn)])
        live_human = [r for r in ledger_mod.decisions_for(conn, fp_hex)
                      if r['kind'] == 'human' and r['superseded_at'] is None]
        self.assertEqual([], live_human)
        # The superseded human row is preserved in history.
        superseded_human = [r for r in ledger_mod.decisions_for(conn, fp_hex)
                            if r['kind'] == 'human' and r['superseded_at'] is not None]
        self.assertEqual(1, len(superseded_human))

    def test_requeue_already_pending_is_a_noop_reopen(self):
        # Without reviewing first, the fixture's item is still pending.
        conn, _corpus, _index, fp_hex = self._setup()
        outcome = review.requeue_one(conn, fp_hex, now='2026-06-15T00:00:00Z')
        conn.commit()
        self.assertEqual(0, outcome.reopened)
        self.assertFalse(outcome.created)

    def test_requeue_creates_item_when_none_exists(self):
        # A fingerprint with a human decision but NO queue row -> created=True.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = ledger_mod.create_ledger(os.path.join(tmp.name, 'ledger.sqlite'))
        self.addCleanup(conn.close)
        fp_hex = 'c' * 64
        ledger_mod.append_decision(
            conn, fingerprint=fp_hex, category='packaging', confidence='high',
            decided_by=review.DECIDED_BY, rule_version=review.REVIEW_RULE_VERSION,
            kind='human', verified=True, evidence=None, decided_at=WHEN, commit=False)
        conn.commit()

        outcome = review.requeue_one(conn, fp_hex, now='2026-06-15T00:00:00Z', reason='reconsidering')
        conn.commit()

        self.assertEqual(1, outcome.superseded)
        self.assertEqual(0, outcome.reopened)
        self.assertTrue(outcome.created)
        pending = ledger_mod.pending_review_items(conn)
        self.assertEqual([fp_hex], [p['fingerprint'] for p in pending])
        self.assertEqual('reconsidering', pending[0]['reason'])


class HistoryTestCase(ReviewFixture, testtools.TestCase):
    """``recent_human_decisions`` + ``history`` list recent verdicts, newest first."""

    def test_recent_human_decisions_newest_first_includes_superseded(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = ledger_mod.create_ledger(os.path.join(tmp.name, 'ledger.sqlite'))
        self.addCleanup(conn.close)
        for i, cat in enumerate(('bugfix', 'security', 'packaging')):
            ledger_mod.append_decision(
                conn, fingerprint=('%064x' % i), category=cat, confidence='high',
                decided_by=review.DECIDED_BY, rule_version=1, kind='human',
                verified=True, signed_by='reviewer@example.org',
                evidence='{"reviewed": {"source_package": "pkg%d"}}' % i,
                decided_at=WHEN, commit=False)
        conn.commit()

        rows = ledger_mod.recent_human_decisions(conn, limit=2)
        self.assertEqual(['packaging', 'security'], [r['category'] for r in rows])  # newest first

    def test_history_command_prints_rows(self):
        import io
        import contextlib
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger_path = os.path.join(tmp.name, 'ledger.sqlite')
        conn = ledger_mod.create_ledger(ledger_path)
        ledger_mod.append_decision(
            conn, fingerprint=('d' * 64), category='packaging', confidence='high',
            decided_by=review.DECIDED_BY, rule_version=1, kind='human', verified=True,
            signed_by='reviewer@example.org',
            evidence='{"reviewed": {"source_package": "mksh"}}', decided_at=WHEN, commit=False)
        conn.commit()
        conn.close()

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = review.main(['history', ledger_path, '--limit', '5'])
        out = buf.getvalue()
        self.assertEqual(0, rc)
        self.assertIn('packaging', out)
        self.assertIn('mksh', out)
        self.assertIn('reviewer@example.org', out)


class RequeueCommandTestCase(ReviewFixture, testtools.TestCase):
    """The ``requeue`` subcommand end-to-end: resolve, requeue, rebuild verdict."""

    def test_requeue_command_reopens_and_reports(self):
        import io
        import contextlib
        conn, corpus_dir, index_path, fp_hex = self._setup()
        ledger_path = os.path.join(corpus_dir, 'ledger.sqlite')
        # Mark the item reviewed + add a live human decision, then close (the CLI
        # opens its own connection).
        ledger_mod.append_decision(
            conn, fingerprint=fp_hex, category='packaging', confidence='high',
            decided_by=review.DECIDED_BY, rule_version=review.REVIEW_RULE_VERSION,
            kind='human', verified=True, evidence=None, decided_at=WHEN, commit=False)
        ledger_mod.mark_reviewed(conn, item_id=self._item(conn)['id'], reviewed_at=WHEN)
        conn.commit()
        conn.close()

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = review.main(['requeue', ledger_path, fp_hex[:12]])
        self.assertEqual(0, rc)
        self.assertIn('re-queued', buf.getvalue())

        check = sqlite3.connect(ledger_path)
        self.addCleanup(check.close)
        check.row_factory = sqlite3.Row
        pending = ledger_mod.pending_review_items(check)
        self.assertEqual([fp_hex], [p['fingerprint'] for p in pending])

    def test_requeue_command_rejects_unknown_fingerprint(self):
        import io
        import contextlib
        conn, corpus_dir, _index, _fp = self._setup()
        ledger_path = os.path.join(corpus_dir, 'ledger.sqlite')
        conn.close()

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = review.main(['requeue', ledger_path, 'ffffffffffff'])
        self.assertEqual(1, rc)
        self.assertIn('no fingerprint matches', buf.getvalue())
