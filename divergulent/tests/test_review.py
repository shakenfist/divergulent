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
