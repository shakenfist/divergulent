"""Tests for divergulent.classify.review_web -- the local web review UI.

All tests are OFFLINE, driven through Flask's test client: the source ``fetch``
returns a canned original file (no network), the ledger is a temp sqlite seeded by
hand, and no server socket is ever bound.  The web read path must surface the same
artefact the CLI does, so the assertions check the diff-in-context, the LLM draft,
the claim, and the worklist slices (priority order, category filter, fingerprint
cherry-pick), plus that hostile input stays HTML-escaped.
"""
import os
import tempfile

import testtools

from divergulent.classify import ledger as ledger_mod
from divergulent.classify import review_web
from divergulent.tests.test_review import ORIGINAL, SOURCE_PACKAGE, WHEN, _build_corpus


def _fetch(url):
    """A fake source fetch: always returns the canned original upstream file."""
    return ORIGINAL


def _seed_item(conn, *, fingerprint, draft_category, priority, reason=None):
    """Seed an llm draft + a pending review item for ``fingerprint``."""
    ledger_mod.append_decision(
        conn, fingerprint=fingerprint, category=draft_category, confidence='medium',
        decided_by='llm-triage:claude-sonnet-4-6', rule_version=1, kind='llm',
        verified=False, evidence='{"draft": {"reasoning": "enlarges a buffer"}}',
        decided_at=WHEN, commit=False)
    ledger_mod.append_review_item(
        conn, fingerprint=fingerprint, reason=reason, draft_category=draft_category,
        draft_confidence='medium', enqueued_at=WHEN, priority=priority)
    conn.commit()


class ReviewWebFixture:
    """A synthetic corpus + ledger + a Flask test client over them."""

    def _client(self, *, extra_items=()):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        corpus_dir = tmp.name
        index_path, fp_hex = _build_corpus(corpus_dir)

        ledger_path = os.path.join(corpus_dir, 'ledger.sqlite')
        conn = ledger_mod.create_ledger(ledger_path)
        self.addCleanup(conn.close)
        ledger_mod.register_rules(conn, ledger_mod.default_registry())

        # The representative (indexed) item, plus any extras the test asked for.
        _seed_item(conn, fingerprint=fp_hex, draft_category='bugfix', priority=5,
                   reason='verifier refuted the drafted category')
        for spec in extra_items:
            _seed_item(conn, **spec)

        app = review_web.create_app(conn, corpus_dir, index_path, fetch=_fetch)
        app.testing = True
        return app.test_client(), conn, fp_hex


class WorklistTestCase(ReviewWebFixture, testtools.TestCase):

    def test_lists_pending_items_with_priority_and_draft(self):
        client, _conn, fp_hex = self._client()
        body = client.get('/').get_data(as_text=True)
        self.assertIn(fp_hex[:16], body)         # fingerprint (short) shown
        self.assertIn('bugfix', body)            # draft category shown
        self.assertIn('Review next most important', body)

    def test_next_most_important_points_at_the_top_priority_item(self):
        # A higher-priority item should be the "next" target, ahead of the seed.
        client, _conn, fp_hex = self._client(extra_items=[dict(
            fingerprint='b' * 64, draft_category='documentation', priority=9)])
        body = client.get('/').get_data(as_text=True)
        self.assertIn('/review/' + 'b' * 64, body)
        # And it leads the table: the priority-9 item's row precedes the seed's.
        self.assertLess(body.index('b' * 64), body.index(fp_hex[:16]))

    def test_category_filter_narrows_the_worklist(self):
        client, _conn, fp_hex = self._client(extra_items=[dict(
            fingerprint='b' * 64, draft_category='documentation', priority=9)])
        body = client.get('/?category=documentation').get_data(as_text=True)
        self.assertIn('b' * 64, body)            # the documentation item
        self.assertNotIn(fp_hex, body)           # the bugfix seed is filtered out

    def test_fingerprint_search_redirects_to_the_review_page(self):
        client, _conn, fp_hex = self._client()
        resp = client.get('/?fingerprint=' + fp_hex)
        self.assertEqual(302, resp.status_code)
        self.assertIn('/review/' + fp_hex, resp.headers['Location'])

    def test_unique_prefix_search_redirects(self):
        client, _conn, fp_hex = self._client()
        resp = client.get('/?fingerprint=' + fp_hex[:12])
        self.assertEqual(302, resp.status_code)
        self.assertIn('/review/' + fp_hex, resp.headers['Location'])

    def test_unknown_fingerprint_search_renders_no_match(self):
        client, _conn, _fp = self._client()
        resp = client.get('/?fingerprint=zzzznomatch')
        self.assertEqual(404, resp.status_code)
        self.assertIn('No single match', resp.get_data(as_text=True))

    def test_hostile_reason_is_escaped(self):
        client, _conn, _fp = self._client(extra_items=[dict(
            fingerprint='c' * 64, draft_category='bugfix', priority=1,
            reason='<script>alert(1)</script>')])
        body = client.get('/').get_data(as_text=True)
        self.assertNotIn('<script>alert(1)</script>', body)
        self.assertIn('&lt;script&gt;', body)


class ReviewPageTestCase(ReviewWebFixture, testtools.TestCase):

    def test_renders_the_diff_in_context_and_the_draft(self):
        client, _conn, fp_hex = self._client()
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertIn('char buf[64]', body)          # original upstream context
        self.assertIn('char buf[4096]', body)        # the change
        self.assertIn('bugfix', body)                # the LLM draft category
        self.assertIn(SOURCE_PACKAGE, body)          # the carrying package

    def test_review_page_resolves_a_prefix(self):
        client, _conn, fp_hex = self._client()
        resp = client.get('/review/' + fp_hex[:12])
        self.assertEqual(200, resp.status_code)
        self.assertIn('char buf[4096]', resp.get_data(as_text=True))

    def test_unknown_fingerprint_is_404(self):
        client, _conn, _fp = self._client()
        resp = client.get('/review/deadbeef00')
        self.assertEqual(404, resp.status_code)
        self.assertIn('No single match', resp.get_data(as_text=True))

    def test_resolvable_fingerprint_without_index_row_shows_no_patch(self):
        # A fingerprint present in the ledger (so it resolves) but absent from the
        # phase-1 index has no representative patch -> the no-patch page, not a 500.
        client, _conn, _fp = self._client(extra_items=[dict(
            fingerprint='d' * 64, draft_category='bugfix', priority=1)])
        resp = client.get('/review/' + 'd' * 64)
        self.assertEqual(404, resp.status_code)
        self.assertIn('no representative patch', resp.get_data(as_text=True))


class LoopbackGuardTestCase(testtools.TestCase):

    def test_loopback_hosts_pass(self):
        for host in ('127.0.0.1', 'localhost', '::1'):
            self.assertEqual(host, review_web.require_loopback(host))

    def test_routable_host_is_refused(self):
        self.assertRaises(ValueError, review_web.require_loopback, '0.0.0.0')


class DiffLinesTestCase(testtools.TestCase):

    def test_classifies_each_line(self):
        rows = review_web.diff_lines('@@ -1 +1 @@\n-old\n+new\n unchanged')
        self.assertEqual(
            [('hunk', '@@ -1 +1 @@'), ('del', '-old'), ('add', '+new'), ('ctx', ' unchanged')],
            [(r['cls'], r['text']) for r in rows])
