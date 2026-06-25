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
from divergulent.classify import review as review_mod
from divergulent.classify import review_web
from divergulent.classify import verdict as verdict_mod
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


def _recording_signer(signature='FAKE-SIG', signed_by='reviewer@example.org'):
    """A fake signer recording the bytes it signed; returns a fixed pair."""
    seen = {}

    def signer(record_bytes):
        seen['record_bytes'] = record_bytes
        return signature, signed_by

    return signer, seen


def _failing_signer(message='sigstore exploded'):
    """A fake signer that always raises -- the signing-failure path."""
    def signer(record_bytes):
        raise RuntimeError(message)
    return signer


def _settle(conn, *, fingerprint, category, kind='heuristic', decided_by='some-rule',
            verified=False):
    """Append a single live decision so the fingerprint has a settled verdict."""
    ledger_mod.append_decision(
        conn, fingerprint=fingerprint, category=category, confidence='high',
        decided_by=decided_by, rule_version=1, kind=kind, verified=verified,
        evidence=None, decided_at=WHEN, commit=True)


def _mark_reviewed(conn, fingerprint):
    """Clear the pending queue item for ``fingerprint`` (settle it, un-queued)."""
    for item in ledger_mod.pending_review_items(conn):
        if item['fingerprint'] == fingerprint:
            ledger_mod.mark_reviewed(conn, item_id=item['id'], reviewed_at=WHEN)


class ReviewWebFixture:
    """A synthetic corpus + ledger + a Flask test client over them."""

    def _client(self, *, extra_items=(), signer=None, clock=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        corpus_dir = tmp.name
        index_path, fp_hex = _build_corpus(corpus_dir)

        ledger_path = os.path.join(corpus_dir, 'ledger.sqlite')
        conn = ledger_mod.create_ledger(ledger_path)
        self.addCleanup(conn.close)
        ledger_mod.register_rules(conn, ledger_mod.default_registry())

        # A heuristic 'unknown' baseline so the fingerprint is genuine residue.
        ledger_mod.append_decision(
            conn, fingerprint=fp_hex, category='unknown', confidence='low',
            decided_by='substantive', rule_version=1, kind='heuristic',
            evidence=None, decided_at=WHEN, commit=False)
        # The representative (indexed) item, plus any extras the test asked for.
        _seed_item(conn, fingerprint=fp_hex, draft_category='bugfix', priority=5,
                   reason='verifier refuted the drafted category')
        for spec in extra_items:
            _seed_item(conn, **spec)

        app = review_web.create_app(
            conn, corpus_dir, index_path, fetch=_fetch, signer=signer,
            clock=(clock or (lambda: WHEN)))
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

    def test_package_filter_narrows_to_the_carrying_package(self):
        # The seed fingerprint is carried by SOURCE_PACKAGE ('reader'); a second
        # item is not in the index, so a package search excludes it.
        client, _conn, fp_hex = self._client(extra_items=[dict(
            fingerprint='b' * 64, draft_category='documentation', priority=9)])
        body = client.get('/?package=' + SOURCE_PACKAGE).get_data(as_text=True)
        self.assertIn(fp_hex[:16], body)
        self.assertNotIn('b' * 64, body)
        self.assertIn('carried by', body)              # the filter note

    def test_package_filter_is_a_substring_match(self):
        client, _conn, fp_hex = self._client()
        body = client.get('/?package=' + SOURCE_PACKAGE[:4]).get_data(as_text=True)
        self.assertIn(fp_hex[:16], body)               # 'read' matches 'reader'

    def test_unknown_package_yields_an_empty_worklist(self):
        client, _conn, fp_hex = self._client()
        body = client.get('/?package=nosuchpackage').get_data(as_text=True)
        self.assertNotIn(fp_hex[:16], body)
        self.assertIn('0 pending', body)

    def test_package_filter_composes_with_category(self):
        client, _conn, fp_hex = self._client()  # seed is bugfix, carried by reader
        hit = client.get('/?package=%s&category=bugfix' % SOURCE_PACKAGE).get_data(as_text=True)
        self.assertIn(fp_hex[:16], hit)
        miss = client.get('/?package=%s&category=security' % SOURCE_PACKAGE).get_data(as_text=True)
        self.assertNotIn(fp_hex[:16], miss)

    def test_package_box_is_prefilled_with_the_query(self):
        client, _conn, _fp = self._client()
        body = client.get('/?package=' + SOURCE_PACKAGE).get_data(as_text=True)
        self.assertIn('value="%s"' % SOURCE_PACKAGE, body)

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


class VerdictPostTestCase(ReviewWebFixture, testtools.TestCase):

    def _human(self, conn, fp_hex):
        return [r for r in ledger_mod.decisions_for(conn, fp_hex) if r['kind'] == 'human']

    def test_accept_records_signed_decision_and_dequeues(self):
        signer, seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        resp = client.post('/review/' + fp_hex, data={'choice': 'accept'})
        self.assertEqual(302, resp.status_code)
        # The signed bytes are exactly the canonical record for the draft category.
        self.assertEqual(
            review_mod.canonical_record(fp_hex, 'bugfix', WHEN), seen['record_bytes'])
        human = self._human(conn, fp_hex)[0]
        self.assertEqual('bugfix', human['category'])
        self.assertEqual('FAKE-SIG', human['signature'])
        self.assertEqual('reviewer@example.org', human['signed_by'])
        self.assertEqual(WHEN, human['decided_at'])
        # The item is dequeued and the human verdict tops the rebuilt cache.
        self.assertEqual([], ledger_mod.pending_review_items(conn))
        self.assertEqual('human', verdict_mod.current_verdict(conn)[fp_hex].kind)

    def test_override_records_the_override_category(self):
        signer, _seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        client.post('/review/' + fp_hex, data={'choice': 'security'})
        self.assertEqual('security', self._human(conn, fp_hex)[0]['category'])

    def test_test_category_is_assignable(self):
        signer, _seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        client.post('/review/' + fp_hex, data={'choice': 'test'})
        self.assertEqual('test', self._human(conn, fp_hex)[0]['category'])

    def test_defer_records_nothing_and_leaves_pending(self):
        signer, seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        resp = client.post('/review/' + fp_hex, data={'choice': 'defer'})
        self.assertEqual(302, resp.status_code)
        self.assertNotIn('record_bytes', seen)
        self.assertEqual([], self._human(conn, fp_hex))
        self.assertEqual(1, len(ledger_mod.pending_review_items(conn)))

    def test_invalid_choice_is_rejected_without_recording(self):
        signer, seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        resp = client.post('/review/' + fp_hex, data={'choice': 'banana'})
        self.assertEqual(400, resp.status_code)
        self.assertNotIn('record_bytes', seen)
        self.assertEqual(1, len(ledger_mod.pending_review_items(conn)))

    def test_signer_failure_renders_error_and_records_nothing(self):
        client, conn, fp_hex = self._client(signer=_failing_signer('boom'))
        resp = client.post('/review/' + fp_hex, data={'choice': 'accept'})
        self.assertEqual(502, resp.status_code)
        body = resp.get_data(as_text=True)
        self.assertIn('Could not record the verdict', body)
        self.assertIn('boom', body)
        # The ledger is untouched -- record_review_verdict signs before it writes.
        self.assertEqual([], self._human(conn, fp_hex))
        self.assertEqual(1, len(ledger_mod.pending_review_items(conn)))

    def test_double_submit_is_idempotent(self):
        signer, _seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        client.post('/review/' + fp_hex, data={'choice': 'accept'})
        resp = client.post('/review/' + fp_hex, data={'choice': 'accept'})
        self.assertEqual(302, resp.status_code)
        self.assertEqual(1, len(self._human(conn, fp_hex)))  # not double-recorded

    def test_review_page_shows_the_verdict_form_when_signing_enabled(self):
        signer, _seen = _recording_signer()
        client, _conn, fp_hex = self._client(signer=signer)
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertIn('name="choice"', body)
        self.assertIn('value="accept"', body)
        self.assertIn('value="test"', body)      # the test category is offered
        self.assertIn('value="defer"', body)

    def test_read_only_instance_hides_form_and_refuses_post(self):
        client, _conn, fp_hex = self._client(signer=None)  # read-only
        self.assertNotIn(
            'name="choice"', client.get('/review/' + fp_hex).get_data(as_text=True))
        resp = client.post('/review/' + fp_hex, data={'choice': 'accept'})
        self.assertEqual(405, resp.status_code)

    def test_verdict_form_has_numbered_keyboard_shortcuts(self):
        signer, _seen = _recording_signer()
        client, _conn, fp_hex = self._client(signer=signer)
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertIn('class="key"', body)          # numbered key hints
        self.assertIn('addEventListener', body)      # the keydown handler
        self.assertIn("name=choice", body)           # radios the handler targets


class AuditTestCase(ReviewWebFixture, testtools.TestCase):

    def _audited_client(self, **kwargs):
        client, conn, fp_hex = self._client(**kwargs)
        # Two settled (un-queued) verdicts to audit: a rule-classified packaging
        # patch and a verified-LLM documentation patch.
        _settle(conn, fingerprint='e' * 64, category='packaging',
                kind='heuristic', decided_by='autotools-regen')
        _settle(conn, fingerprint='f' * 64, category='documentation',
                kind='llm', decided_by='llm-triage:claude', verified=True)
        return client, conn, fp_hex

    def test_lists_settled_verdicts_and_excludes_queued(self):
        client, _conn, fp_hex = self._audited_client()
        body = client.get('/audit').get_data(as_text=True)
        self.assertIn('packaging', body)
        self.assertIn('documentation', body)
        self.assertIn('autotools-regen', body)
        # The seed fingerprint is still pending in the queue -> not in the audit.
        self.assertNotIn(fp_hex[:16], body)

    def test_category_filter(self):
        client, _conn, _fp = self._audited_client()
        body = client.get('/audit?category=packaging').get_data(as_text=True)
        self.assertIn('e' * 16, body)                  # the packaging row
        self.assertNotIn('f' * 16, body)               # the documentation row, filtered

    def test_source_filter_by_kind(self):
        client, _conn, _fp = self._audited_client()
        body = client.get('/audit?source=heuristic').get_data(as_text=True)
        self.assertIn('autotools-regen', body)         # heuristic row
        self.assertNotIn('f' * 16, body)               # the llm row is filtered

    def test_source_filter_by_decided_by_rule(self):
        client, _conn, _fp = self._audited_client()
        body = client.get('/audit?source=autotools-regen').get_data(as_text=True)
        self.assertIn('e' * 16, body)                  # only the autotools-regen row
        self.assertNotIn('f' * 16, body)

    def test_hostile_decided_by_is_escaped(self):
        client, conn, _fp = self._client()
        _settle(conn, fingerprint='e' * 64, category='packaging',
                decided_by='<script>alert(1)</script>')
        body = client.get('/audit').get_data(as_text=True)
        self.assertNotIn('<script>alert(1)</script>', body)
        self.assertIn('&lt;script&gt;', body)

    def test_settled_review_page_shows_verdict_and_requeue(self):
        signer, _seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        _mark_reviewed(conn, fp_hex)  # settle the seed: no longer queued
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertIn('current verdict', body)
        self.assertIn('Re-queue for human review', body)
        self.assertNotIn('name="choice"', body)        # no verdict form when settled


class RequeueTestCase(ReviewWebFixture, testtools.TestCase):

    def test_requeue_reopens_item_and_records_no_decision(self):
        signer, _seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        _mark_reviewed(conn, fp_hex)
        self.assertEqual([], ledger_mod.pending_review_items(conn))  # settled

        resp = client.post('/requeue/' + fp_hex)
        self.assertEqual(302, resp.status_code)
        self.assertIn('/audit', resp.headers['Location'])
        # Back in the queue, and NO decision was recorded by the re-queue.
        pending = [i['fingerprint'] for i in ledger_mod.pending_review_items(conn)]
        self.assertIn(fp_hex, pending)
        self.assertEqual(
            [], [r for r in ledger_mod.decisions_for(conn, fp_hex) if r['kind'] == 'human'])

    def test_requeue_supersedes_a_human_verdict(self):
        signer, _seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        # Record then re-queue: the human verdict is superseded (no longer live).
        client.post('/review/' + fp_hex, data={'choice': 'accept'})
        self.assertEqual('human', verdict_mod.current_verdict(conn)[fp_hex].kind)
        client.post('/requeue/' + fp_hex)
        self.assertNotEqual('human', verdict_mod.current_verdict(conn)[fp_hex].kind)

    def test_requeue_refused_on_readonly_instance(self):
        client, _conn, fp_hex = self._client(signer=None)
        self.assertEqual(405, client.post('/requeue/' + fp_hex).status_code)


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
