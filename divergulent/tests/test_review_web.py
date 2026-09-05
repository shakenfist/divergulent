"""Tests for divergulent.classify.review_web -- the local web review UI.

All tests are OFFLINE, driven through Flask's test client: the source ``fetch``
returns a canned original file (no network), the ledger is a temp sqlite seeded by
hand, and no server socket is ever bound.  The web read path must surface the same
artefact the CLI does, so the assertions check the diff-in-context, the LLM draft,
the claim, and the worklist slices (priority order, category filter, fingerprint
cherry-pick), plus that hostile input stays HTML-escaped.

Every mutating request must clear the CSRF/origin guard, so the fixture's ``_post``
helper posts the way the UI's own forms do (same-origin, token in the cookie and in
the form); ``CsrfGuardTestCase`` is the negative half of that.
"""
import io
import os
import re
import sqlite3
import tempfile
from contextlib import redirect_stdout

import testtools

from divergulent.classify import generated
from divergulent.classify import injection as injection_mod
from divergulent.classify import ledger as ledger_mod
from divergulent.classify import reach
from divergulent.classify import review as review_mod
from divergulent.classify import review_web
from divergulent.classify import reviewability
from divergulent.classify import risk
from divergulent.classify import verdict as verdict_mod
from divergulent.tests.test_review import (
    MARKED_CONSTRUCT_PATCH, MARKED_PATCH, ORIGINAL, RESIDUE_CONSTRUCT_PATCH,
    SOURCE_PACKAGE, WHEN, _build_corpus)


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


def _locked_ledger(*args, **kwargs):
    """A ledger write that fails: the shape of a locked database mid-request."""
    raise sqlite3.OperationalError('database is locked')


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


def _seed_generated(conn, fingerprint, patch_text):
    """Record the live ``generated-content`` observation the UI badges and collapses.

    Built from a REAL ``generated.scan`` of the fixture body through ``detail_for`` /
    ``evidence_for`` -- never hand-crafted evidence JSON -- so the page is exercised
    against exactly the row the deterministic record pass writes.
    """
    scan = generated.scan(patch_text)
    ledger_mod.append_observation(
        conn, fingerprint=fingerprint, kind=generated.GENERATED_KIND,
        detail=generated.detail_for(scan), evidence=generated.evidence_for(scan),
        observed_by=generated.GENERATED_OBSERVED_BY,
        rule_version=generated.GENERATED_RULES_VERSION, observed_at=WHEN)
    conn.commit()


class ReviewWebFixture:
    """A synthetic corpus + ledger + a Flask test client over them."""

    def _client(self, *, extra_items=(), signer=None, clock=None, patch=None,
                port=review_web.DEFAULT_PORT):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        corpus_dir = tmp.name
        if patch is None:
            index_path, fp_hex = _build_corpus(corpus_dir)
        else:
            index_path, fp_hex = _build_corpus(corpus_dir, patch)

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
            clock=(clock or (lambda: WHEN)), port=port)
        app.testing = True
        self.app = app
        self.csrf = app.config[review_web.CSRF_CONFIG_KEY]
        client = app.test_client()
        # Arm the client the way a browser is armed by its first GET of the UI: the
        # guard wants the token in the cookie AND in the form.
        client.set_cookie(review_web.CSRF_COOKIE, self.csrf)
        return client, conn, fp_hex

    def _post(self, client, path, data=None, *, token=True,
              origin='http://localhost:%d' % review_web.DEFAULT_PORT, headers=None):
        """POST the way the UI's own forms do: same-origin, with the CSRF token.

        The origin carries the PORT, as a browser's does for anything but the scheme's
        default -- the guard requires it exactly, so a port-less origin is a different
        server rather than a lenient spelling of this one.

        ``token=False`` omits the hidden field, ``origin=None`` omits the header, and
        ``headers`` adds or overrides one (a foreign ``Host``, say) -- the three knobs
        the guard tests turn.
        """
        form = dict(data or {})
        if token:
            form[review_web.CSRF_FIELD] = self.csrf
        sent = {} if origin is None else {'Origin': origin}
        sent.update(headers or {})
        return client.post(path, data=form, headers=sent)


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

    def test_category_chips_show_the_full_set_with_counts(self):
        # The bar is stable and complete: every assignable category appears, even
        # empty ones, with counts -- notably 'test', which the LLM never drafts.
        client, _conn, _fp = self._client()  # one bugfix item seeded
        body = client.get('/').get_data(as_text=True)
        for category in ('packaging', 'documentation', 'bugfix', 'security',
                         'feature', 'unknown', 'test'):
            self.assertIn('category=%s' % category, body)
        self.assertIn('bugfix <span class="muted">(1)</span>', body)
        self.assertIn('test <span class="muted">(0)</span>', body)  # always present, empty

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

    def test_shows_the_author_claim_description(self):
        client, _conn, fp_hex = self._client()
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertIn('What the author claims', body)
        # The DEP-3 Description from the PATCH fixture.
        self.assertIn('enlarge the read buffer to avoid truncation', body)
        self.assertIn('claimed category', body)

    def test_surfaces_the_patch_date(self):
        # The age signal (DEP-3 Last-Update / git Date) is shown in the claim block.
        # The fixture patch carries no date, so it reads "no date in header".
        client, _conn, fp_hex = self._client()
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertIn('last updated', body)

    def test_diff_has_changed_block_anchors_and_nav(self):
        client, _conn, fp_hex = self._client()
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertIn('block-start', body)               # the change run is anchored
        self.assertIn("e.key === ']'", body)             # next-change shortcut wired
        self.assertIn("e.key === '['", body)             # previous-change shortcut

    def test_offers_a_jump_back_to_the_verdict(self):
        # With a signer the verdict form is present, so the diff footer offers a way
        # back to it (link + `v` shortcut) and the form has the #verdict anchor.
        signer, _ = _recording_signer()
        client, _conn, fp_hex = self._client(signer=signer)
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertIn('id="verdict"', body)
        self.assertIn('href="#verdict"', body)
        self.assertIn("e.key === 'v'", body)

    def test_shows_the_provenance_badge(self):
        from divergulent.classify import cross_reference as xref_mod
        client, conn, fp_hex = self._client()
        ledger_mod.append_observation(
            conn, fingerprint=fp_hex, kind=xref_mod.PROVENANCE_KIND,
            detail=xref_mod.DETAIL_CLAIM_UNCONFIRMED,
            evidence='claimed CVE-2099-0000 not recorded (not-found, security-tracker 2026-07-10)',
            observed_by=xref_mod.PROVENANCE_OBSERVED_BY, rule_version=xref_mod.EXTERNAL_CVE_VERSION,
            observed_at=WHEN)
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertIn('claim-unconfirmed', body)

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
        resp = self._post(client, '/review/' + fp_hex, {'choice': 'accept'})
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
        self._post(client, '/review/' + fp_hex, {'choice': 'security'})
        self.assertEqual('security', self._human(conn, fp_hex)[0]['category'])

    def test_test_category_is_assignable(self):
        signer, _seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        self._post(client, '/review/' + fp_hex, {'choice': 'test'})
        self.assertEqual('test', self._human(conn, fp_hex)[0]['category'])

    def test_defer_records_nothing_and_leaves_pending(self):
        signer, seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        resp = self._post(client, '/review/' + fp_hex, {'choice': 'defer'})
        self.assertEqual(302, resp.status_code)
        self.assertNotIn('record_bytes', seen)
        self.assertEqual([], self._human(conn, fp_hex))
        self.assertEqual(1, len(ledger_mod.pending_review_items(conn)))

    def test_invalid_choice_is_rejected_without_recording(self):
        signer, seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        resp = self._post(client, '/review/' + fp_hex, {'choice': 'banana'})
        self.assertEqual(400, resp.status_code)
        self.assertNotIn('record_bytes', seen)
        self.assertEqual(1, len(ledger_mod.pending_review_items(conn)))

    def test_signer_failure_renders_error_and_records_nothing(self):
        client, conn, fp_hex = self._client(signer=_failing_signer('boom'))
        resp = self._post(client, '/review/' + fp_hex, {'choice': 'accept'})
        self.assertEqual(502, resp.status_code)
        body = resp.get_data(as_text=True)
        self.assertIn('Could not record the verdict', body)
        self.assertIn('boom', body)
        # The ledger is untouched -- record_review_verdict signs before it writes.
        self.assertEqual([], self._human(conn, fp_hex))
        self.assertEqual(1, len(ledger_mod.pending_review_items(conn)))

    def test_a_write_failure_rolls_back_and_the_verdict_never_lands(self):
        """The reviewer is told it failed, so it must not land later either.

        ``record_review_verdict`` appends the signed decision uncommitted and lets
        ``mark_reviewed`` commit the pair; a failure between them (a locked database
        here) would otherwise leave the decision staged on a connection that lives
        for the whole process, for the next successful commit to flush.
        """
        signer, _seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        self.patch(ledger_mod, 'mark_reviewed', _locked_ledger)

        resp = self._post(client, '/review/' + fp_hex, {'choice': 'accept'})

        self.assertEqual(502, resp.status_code)
        self.assertIn('Could not record the verdict', resp.get_data(as_text=True))
        self.assertEqual([], self._human(conn, fp_hex))
        # A later, unrelated, successful commit on the same connection must not
        # carry the abandoned verdict with it.
        ledger_mod.append_note(conn, fingerprint=fp_hex, body='a later note',
                               signed_by='rev@example.org', signature='S', created_at=WHEN)
        self.assertEqual([], self._human(conn, fp_hex))
        # And it is absent from the file itself, not merely from this connection's
        # view of it (database_list gives the open file's path: 'main' is column 2).
        ledger_path = conn.execute('PRAGMA database_list').fetchone()[2]
        other = ledger_mod.open_ledger(ledger_path)
        self.addCleanup(other.close)
        self.assertEqual(
            [], [r for r in ledger_mod.decisions_for(other, fp_hex) if r['kind'] == 'human'])
        self.assertEqual(1, len(ledger_mod.pending_review_items(conn)))

    def test_double_submit_is_idempotent(self):
        signer, _seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        self._post(client, '/review/' + fp_hex, {'choice': 'accept'})
        resp = self._post(client, '/review/' + fp_hex, {'choice': 'accept'})
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
        resp = self._post(client, '/review/' + fp_hex, {'choice': 'accept'})
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

        resp = self._post(client, '/requeue/' + fp_hex)
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
        self._post(client, '/review/' + fp_hex, {'choice': 'accept'})
        self.assertEqual('human', verdict_mod.current_verdict(conn)[fp_hex].kind)
        self._post(client, '/requeue/' + fp_hex)
        self.assertNotEqual('human', verdict_mod.current_verdict(conn)[fp_hex].kind)

    def test_a_failed_requeue_is_reported_and_lands_nothing(self):
        """A re-queue that crashes must leave the live verdict standing.

        ``requeue_one`` leaves its writes uncommitted, and this connection lives for
        the life of the process, so a failure part-way would otherwise stage the
        supersede for the next commit to flush -- retracting a human verdict the
        operator was told had not been re-queued.  The page says 502, the verdict
        stands, and a later successful write does not carry the supersede along.
        """
        signer, _seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        self._post(client, '/review/' + fp_hex, {'choice': 'accept'})
        self.patch(ledger_mod, 'reopen_review_items', _locked_ledger)

        resp = self._post(client, '/requeue/' + fp_hex)

        self.assertEqual(502, resp.status_code)
        self.assertIn('database is locked', resp.get_data(as_text=True))
        self.assertEqual('human', verdict_mod.current_verdict(conn)[fp_hex].kind)

    def test_requeue_refused_on_readonly_instance(self):
        client, _conn, fp_hex = self._client(signer=None)
        self.assertEqual(405, self._post(client, '/requeue/' + fp_hex).status_code)


class CsrfGuardTestCase(ReviewWebFixture, testtools.TestCase):
    """Only this UI's own forms may reach the mutating endpoints.

    A loopback bind constrains where the server LISTENS, not who may post to it: a
    form on any site can target ``http://127.0.0.1:8765/review/<prefix>``, the
    browser sends it, and the resulting decision -- ``kind='human'``,
    ``verified=True``, signed with the reviewer's cached Sigstore identity -- tops
    the precedence in an append-only ledger and rides the export into the published
    bundle.  So every forgery is asserted to be refused AND to leave the ledger
    exactly as it was.
    """

    #: One entry per way a cross-site post differs from the UI's own.
    FORGERIES = (
        ('no csrf token', {'token': False}),
        ('no Origin header', {'origin': None}),
        ('a foreign Origin', {'origin': 'http://evil.example'}),
        ('a foreign Host', {'headers': {'Host': 'evil.example'}}),
    )

    def _signing(self):
        """A signing app, plus the record of what its signer was asked to sign."""
        signer, seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        return client, conn, fp_hex, seen

    def _human(self, conn, fp_hex):
        return [r for r in ledger_mod.decisions_for(conn, fp_hex) if r['kind'] == 'human']

    def test_a_forged_verdict_is_refused_and_records_nothing(self):
        for label, forgery in self.FORGERIES:
            client, conn, fp_hex, seen = self._signing()
            resp = self._post(client, '/review/' + fp_hex, {'choice': 'accept'}, **forgery)
            self.assertEqual(403, resp.status_code, label)
            self.assertIn('Request refused', resp.get_data(as_text=True), label)
            # No decision, the item is still queued, and -- the point of the
            # exercise -- the reviewer's identity was never used to sign anything.
            self.assertEqual([], self._human(conn, fp_hex), label)
            self.assertEqual(1, len(ledger_mod.pending_review_items(conn)), label)
            self.assertNotIn('record_bytes', seen, label)

    def test_a_forged_note_is_refused_and_records_nothing(self):
        for label, forgery in self.FORGERIES:
            client, conn, fp_hex, seen = self._signing()
            resp = self._post(client, '/note/' + fp_hex, {'body': 'forged'}, **forgery)
            self.assertEqual(403, resp.status_code, label)
            self.assertEqual([], ledger_mod.notes_for(conn, fp_hex), label)
            self.assertNotIn('record_bytes', seen, label)

    def test_a_forged_requeue_cannot_unsettle_a_human_verdict(self):
        for label, forgery in self.FORGERIES:
            client, conn, fp_hex, _seen = self._signing()
            self._post(client, '/review/' + fp_hex, {'choice': 'accept'})  # a real verdict
            self.assertEqual('human', verdict_mod.current_verdict(conn)[fp_hex].kind)

            resp = self._post(client, '/requeue/' + fp_hex, **forgery)

            self.assertEqual(403, resp.status_code, label)
            # The verdict still stands and the item is still settled.
            self.assertEqual('human', verdict_mod.current_verdict(conn)[fp_hex].kind, label)
            self.assertEqual([], ledger_mod.pending_review_items(conn), label)

    def test_a_stale_token_from_another_process_is_refused(self):
        # The token is minted per create_app, so one learned from an earlier run --
        # or from another user's tab -- is worthless.
        client, conn, fp_hex, _seen = self._signing()
        resp = client.post('/review/' + fp_hex, headers={'Origin': 'http://localhost'},
                           data={'choice': 'accept', review_web.CSRF_FIELD: 'not-the-token'})
        self.assertEqual(403, resp.status_code)
        self.assertEqual([], self._human(conn, fp_hex))

    def test_the_token_must_be_in_the_cookie_as_well_as_the_form(self):
        # SameSite=Strict means a cross-site post arrives with no cookie at all.
        client, conn, fp_hex, _seen = self._signing()
        client.delete_cookie(review_web.CSRF_COOKIE)
        resp = self._post(client, '/review/' + fp_hex, {'choice': 'accept'})
        self.assertEqual(403, resp.status_code)
        self.assertEqual([], self._human(conn, fp_hex))

    def test_a_foreign_host_cannot_even_read_the_ledger(self):
        # The Host check is what stops DNS rebinding, which would otherwise make
        # the GET pages (the whole worklist) readable by a remote page.
        client, _conn, fp_hex, _seen = self._signing()
        client.delete_cookie(review_web.CSRF_COOKIE)
        resp = client.get('/', headers={'Host': 'attacker.example'})
        self.assertEqual(403, resp.status_code)
        self.assertNotIn(fp_hex[:16], resp.get_data(as_text=True))
        # Nor is the caller we just refused handed the token for a second try.
        self.assertNotIn('Set-Cookie', resp.headers)

    def test_the_normal_in_app_flow_still_records_a_verdict(self):
        # End to end as a browser does it: arrive with no cookie, GET the page (which
        # plants the cookie and renders the hidden field), post the token it carried.
        signer, seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        client.delete_cookie(review_web.CSRF_COOKIE)

        page = client.get('/review/' + fp_hex)
        self.assertEqual(200, page.status_code)
        cookie = page.headers['Set-Cookie']
        self.assertIn('SameSite=Strict', cookie)
        self.assertIn('HttpOnly', cookie)
        token = re.search(r'name="csrf_token" value="([^"]+)"',
                          page.get_data(as_text=True)).group(1)

        resp = client.post(
            '/review/' + fp_hex,
            headers={'Origin': 'http://localhost:%d' % review_web.DEFAULT_PORT},
            data={'choice': 'accept', review_web.CSRF_FIELD: token})

        self.assertEqual(302, resp.status_code)
        self.assertIn('record_bytes', seen)
        self.assertEqual('bugfix', self._human(conn, fp_hex)[0]['category'])
        self.assertEqual([], ledger_mod.pending_review_items(conn))

    def test_every_mutating_form_carries_the_token(self):
        # The guard is only usable if the forms feed it; a form added without the
        # hidden field would be dead on submit.
        client, conn, fp_hex, _seen = self._signing()
        queued = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertEqual(2, queued.count('name="csrf_token"'))   # verdict + note
        _mark_reviewed(conn, fp_hex)
        settled = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertIn('Re-queue for human review', settled)
        self.assertEqual(2, settled.count('name="csrf_token"'))  # requeue + note


class GuardWiringTestCase(ReviewWebFixture, testtools.TestCase):
    """``port`` is the one security parameter the CLI feeds the guard; check the wire.

    :class:`RequestGuardTestCase` exercises the policy itself, but always at the
    default port, and the fixture binds the default too -- so both would still pass
    if ``create_app`` dropped ``port`` on the floor and the guard fell back to
    :data:`review_web.DEFAULT_PORT`.  That regression would make every request on an
    operator's ``--port 9000`` run refuse (or, worse in a future refactor, accept a
    host naming a port nothing is bound to).
    """

    def test_the_bound_port_is_the_port_the_guard_enforces(self):
        client, _conn, _fp_hex = self._client(port=9999)
        self.assertEqual(
            200, client.get('/', headers={'Host': 'localhost:9999'}).status_code)
        # ... and the default is now just another local server.
        refused = client.get('/', headers={'Host': 'localhost:8765'})
        self.assertEqual(403, refused.status_code)
        self.assertIn('DNS rebinding', refused.get_data(as_text=True))

    def test_an_oversized_body_is_refused_rather_than_buffered(self):
        """The guard reads the body, so what it may read is bounded -- for any shape.

        Flask's ``MAX_FORM_MEMORY_SIZE`` default already answers 413 to an oversized
        urlencoded form, so that half needs no help from us.  A body of some OTHER
        content type gets no such treatment: the guard finds no CSRF field in it and
        refuses, but only after this single-threaded server has buffered the lot.
        Asserting both, so the covered half is not mistaken for the reason.
        """
        client, _conn, fp_hex = self._client()
        oversized = b'x' * (review_web.MAX_REQUEST_BYTES + 1)
        resp = client.post('/note/' + fp_hex, data=oversized,
                           content_type='application/octet-stream',
                           headers={'Origin': 'http://localhost:%d' % review_web.DEFAULT_PORT})
        self.assertEqual(413, resp.status_code)
        # The form path, for the record: Flask's own default, not this setting.
        form_resp = self._post(client, '/note/' + fp_hex,
                               {'body': 'x' * (review_web.MAX_REQUEST_BYTES + 1)})
        self.assertEqual(413, form_resp.status_code)

    def test_main_hands_create_app_the_port_it_binds(self):
        """The remaining link: ``--port`` -> ``create_app(port=...)``."""
        seen = {}

        class _FakeApp:
            def run(self, **kwargs):
                seen['run'] = kwargs

        def _fake_create_app(*args, **kwargs):
            seen['create_app'] = kwargs
            return _FakeApp()

        client, conn, _fp_hex = self._client()
        self.patch(review_web, 'create_app', _fake_create_app)
        self.patch(review_web.ledger_mod, 'open_ledger', lambda path: conn)
        self.patch(review_web.review_mod, '_real_fetch', lambda: _fetch)
        self.patch(review_web, '_lazy_sigstore_signer', lambda: None)

        with redirect_stdout(io.StringIO()):     # main announces the bind; not under test
            review_web.main(['--ledger', 'ignored', '--corpus', 'ignored', '--port', '9000'])
        self.assertEqual(9000, seen['create_app']['port'])
        self.assertEqual(9000, seen['run']['port'])


class RequestGuardTestCase(testtools.TestCase):
    """The pure request policy: which Host/Origin/token combinations may proceed."""

    def _guard(self, **overrides):
        args = dict(method='POST', host='127.0.0.1:8765', origin='http://127.0.0.1:8765',
                    cookie_token='tok', form_token='tok', expected_token='tok', port=8765)
        args.update(overrides)
        return review_web.guard_request(**args)

    def test_the_in_app_post_passes(self):
        self.assertIsNone(self._guard())

    def test_a_callable_form_token_is_only_called_after_host_and_origin_pass(self):
        """The body must not be read until the request is established as ours.

        ``form_token`` is a callable in the app so that ``request.form`` -- which
        buffers and parses the whole body -- is only touched once Host and Origin
        have already been checked. Counting the calls is the only way to see that
        ordering: reading the code cannot distinguish "evaluated last" from
        "evaluated as an argument, i.e. first".
        """
        calls = []

        def _token():
            calls.append(True)
            return 'tok'

        # Refused on Host: the body is never touched.
        self.assertIn('DNS rebinding', self._guard(host='evil.example:8765', form_token=_token))
        self.assertEqual([], calls)

        # Refused on a missing Origin, and on a foreign one: still never touched.
        self.assertIn('no Origin header', self._guard(origin=None, form_token=_token))
        self.assertIn('not this review UI',
                      self._guard(origin='http://evil.example', form_token=_token))
        self.assertEqual([], calls)

        # A GET returns before the token layer at all.
        self.assertIsNone(self._guard(method='GET', form_token=_token))
        self.assertEqual([], calls)

        # Only once the request is ours is the field read -- exactly once.
        self.assertIsNone(self._guard(form_token=_token))
        self.assertEqual([True], calls)

    def test_every_loopback_name_passes_on_the_bound_port(self):
        for host in ('127.0.0.1:8765', 'localhost:8765', '[::1]:8765'):
            self.assertIsNone(self._guard(host=host, origin='http://' + host), host)

    def test_a_port_less_host_is_tolerated_but_a_port_less_origin_is_not(self):
        """The two headers get different port rules, deliberately.

        A client may legitimately omit the port from ``Host`` (the test client does),
        and the name it sends is still not attacker-chosen, so the loopback name alone
        carries the anti-rebinding weight there.  An ``Origin`` has no such licence: a
        browser writes the port whenever it is not the scheme's default, so a
        port-less origin means 80 -- a DIFFERENT server, which must not pass as this
        one just because it is also on loopback.
        """
        for host in ('127.0.0.1', 'localhost'):
            self.assertIsNone(self._guard(method='GET', host=host, origin=None), host)
            self.assertIn('not this review UI',
                          self._guard(host=host, origin='http://' + host), host)
        self.assertFalse(review_web.origin_is_local('http://localhost', 8765))
        self.assertTrue(review_web.origin_is_local('http://localhost', 80))

    def test_a_foreign_host_is_refused_even_on_a_safe_method(self):
        self.assertIn('DNS rebinding', self._guard(method='GET', host='evil.example'))

    def test_another_port_is_another_server(self):
        self.assertIsNotNone(self._guard(host='127.0.0.1:9999'))

    def test_a_get_needs_neither_origin_nor_token(self):
        self.assertIsNone(self._guard(
            method='GET', origin=None, cookie_token=None, form_token=None))

    def test_an_absent_origin_is_hostile_on_a_mutating_request(self):
        self.assertIn('no Origin header', self._guard(origin=None))

    def test_a_foreign_origin_is_refused(self):
        self.assertIn('not this review UI', self._guard(origin='http://evil.example'))

    def test_the_null_origin_of_a_sandboxed_iframe_is_refused(self):
        self.assertIsNotNone(self._guard(origin='null'))

    def test_a_missing_cookie_or_field_is_refused(self):
        self.assertIn(review_web.CSRF_COOKIE, self._guard(cookie_token=None))
        self.assertIn(review_web.CSRF_FIELD, self._guard(form_token=''))

    def test_a_mismatched_token_on_either_side_is_refused(self):
        self.assertIn('did not match', self._guard(form_token='other'))
        self.assertIn('did not match', self._guard(cookie_token='other'))

    def test_a_non_ascii_token_is_refused_not_crashed_on(self):
        # hmac.compare_digest raises on a non-ASCII str; both sides come off the
        # wire, so a hostile cookie must be a refusal rather than a 500.
        self.assertIn('did not match', self._guard(cookie_token='t\u00f8k'))
        self.assertIn('did not match', self._guard(form_token='t\u00f8k'))

    def test_an_unparseable_origin_is_refused(self):
        self.assertFalse(review_web.origin_is_local('http://[::1', 8765))

    def test_an_ipv6_literal_authority_is_split_not_mangled(self):
        self.assertTrue(review_web.host_is_local('[::1]:8765', 8765))
        self.assertFalse(review_web.host_is_local('[::1]:1', 8765))
        self.assertFalse(review_web.host_is_local('[2001:db8::1]:8765', 8765))

    def test_a_loopback_origin_on_another_scheme_is_not_this_ui(self):
        self.assertFalse(review_web.origin_is_local('https://localhost:8765', 8765))
        self.assertFalse(review_web.origin_is_local('file://localhost', 8765))


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

    def test_marks_the_first_line_of_each_changed_block(self):
        rows = review_web.diff_lines('@@\n ctx\n-old\n+new\n ctx2\n+added\n ctx3')
        # '-old'+'+new' is one modification block (anchored at '-old'); '+added' a
        # second. Context/hunk lines never start a block.
        self.assertEqual(['-old', '+added'], [r['text'] for r in rows if r['block_start']])
        # The anchor carries the block-start marker class for the JS to collect.
        delete = next(r for r in rows if r['text'] == '-old')
        self.assertEqual('del block-start', delete['css'])

    def test_numbers_the_per_file_block_headers(self):
        rows = review_web.diff_lines(
            '### src/a.c\n@@ -1 +1 @@\n-x\n+y\n\n### src/b.c\n@@ -1 +1 @@\n+z')
        files = [(r['file_index'], r['text']) for r in rows if r['cls'] == 'file']
        self.assertEqual([(1, '### src/a.c'), (2, '### src/b.c')], files)


class FileRowsTestCase(testtools.TestCase):

    def test_sorted_largest_first_keeping_the_diff_order_index(self):
        rows = review_web.file_rows(
            '--- a/small.c\n+++ b/small.c\n@@ -1 +1 @@\n-a\n+b\n'
            '--- a/big.c\n+++ b/big.c\n@@ -1,3 +1,3 @@\n-c\n-d\n-e\n+f\n+g\n+h\n')
        self.assertEqual(['big.c', 'small.c'], [r['path'] for r in rows])
        # big.c is the SECOND file in the diff, so its anchor index stays 2.
        self.assertEqual([2, 1], [r['index'] for r in rows])
        self.assertEqual([(3, 3), (1, 1)],
                         [(r['added'], r['removed']) for r in rows])

    def test_no_file_headers_yields_no_rows(self):
        self.assertEqual([], review_web.file_rows('not a diff at all\n'))


class FileListWebTestCase(ReviewWebFixture, testtools.TestCase):

    def test_review_page_lists_the_files_before_the_diff(self):
        client, _conn, fp_hex = self._client()
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertIn('Files changed (1', body)
        self.assertLess(body.index('Files changed'),
                        body.index('Diff in upstream context'))
        # The list row links to the diff's per-file block anchor.
        self.assertIn('href="#file-1"', body)
        self.assertIn('id="file-1"', body)
        self.assertIn('src/reader.c', body)


class ReviewabilityWebTestCase(ReviewWebFixture, testtools.TestCase):
    """The size axis surfaced in the UI: a row badge, a size filter, a warning."""

    def _seed_rev(self, conn, fingerprint, level):
        ledger_mod.append_observation(
            conn, fingerprint=fingerprint, kind=reviewability.REVIEWABILITY_KIND,
            detail=level, evidence='{}', observed_by=reviewability.REVIEWABILITY_OBSERVED_BY,
            rule_version=reviewability.REVIEWABILITY_VERSION, observed_at=WHEN)
        conn.commit()

    def test_worklist_badges_oversized_and_offers_a_size_filter(self):
        client, conn, _ = self._client(extra_items=[dict(
            fingerprint='b' * 64, draft_category='bugfix', priority=9)])
        self._seed_rev(conn, 'b' * 64, 'oversized')
        body = client.get('/').get_data(as_text=True)
        self.assertIn('rev oversized', body)             # the row badge
        self.assertIn('?reviewability=oversized', body)  # the size filter chip

    def test_reviewability_filter_narrows_the_worklist(self):
        client, conn, fp_hex = self._client(extra_items=[dict(
            fingerprint='b' * 64, draft_category='bugfix', priority=9)])
        self._seed_rev(conn, 'b' * 64, 'oversized')      # only this one is oversized
        body = client.get('/?reviewability=oversized').get_data(as_text=True)
        self.assertIn(('b' * 64)[:16], body)             # the oversized item is shown
        self.assertNotIn(fp_hex[:16], body)              # the normal item is filtered out

    def test_review_page_warns_when_oversized(self):
        client, conn, fp_hex = self._client()
        self._seed_rev(conn, fp_hex, 'oversized')
        body = client.get('/review/%s' % fp_hex).get_data(as_text=True)
        self.assertIn('not realistically line-reviewable', body)
        self.assertIn('rev oversized', body)


class RiskWebTestCase(ReviewWebFixture, testtools.TestCase):
    """The security-risk score surfaced in the UI: a badge and live ordering."""

    def _seed_risk(self, conn, fingerprint, level):
        ledger_mod.append_observation(
            conn, fingerprint=fingerprint, kind=risk.RISK_KIND, detail=level,
            evidence='{}', observed_by=risk.RISK_OBSERVED_BY_PREFIX + 'm',
            rule_version=1, observed_at=WHEN)
        conn.commit()

    def test_worklist_badges_risk_and_orders_by_it_over_stored_priority(self):
        # The main fp has the LOWER stored priority (5) but is scored 'high'; the
        # extra item has a higher stored priority (9) but no risk. Risk must win.
        client, conn, fp_hex = self._client(extra_items=[dict(
            fingerprint='b' * 64, draft_category='bugfix', priority=9)])
        self._seed_risk(conn, fp_hex, 'high')
        body = client.get('/').get_data(as_text=True)
        self.assertIn('risk high', body)   # the badge (class="risk high")
        # The high-risk item leads despite its lower stored priority.
        self.assertLess(body.index(fp_hex[:16]), body.index(('b' * 64)[:16]))

    def test_review_page_shows_the_risk_badge(self):
        client, conn, fp_hex = self._client()
        self._seed_risk(conn, fp_hex, 'elevated')
        body = client.get('/review/%s' % fp_hex).get_data(as_text=True)
        self.assertIn('risk: elevated', body)


class InjectionWebTestCase(ReviewWebFixture, testtools.TestCase):
    """The injection tripwire surfaced in the UI: a worklist badge and a review-page
    banner, both honestly worded (human-routed, not 'malicious')."""

    def _seed_injection(self, conn, fingerprint, *, region='diff', family='instruction-phrase'):
        ledger_mod.append_observation(
            conn, fingerprint=fingerprint, kind=injection_mod.INJECTION_KIND,
            detail='%s/%s' % (family, region), evidence='ignore previous instructions',
            observed_by='injection-scan', rule_version=injection_mod.INJECTION_RULES_VERSION,
            observed_at=WHEN)
        conn.commit()

    def test_worklist_badges_injection_suspect(self):
        client, conn, fp_hex = self._client()
        self._seed_injection(conn, fp_hex)
        body = client.get('/').get_data(as_text=True)
        # The badge is present and titled with the families; never claims malice.
        self.assertIn('class="inj"', body)
        self.assertIn('instruction-phrase', body)
        self.assertNotIn('malicious', body.lower())

    def test_review_page_shows_injection_banner(self):
        client, conn, fp_hex = self._client()
        self._seed_injection(conn, fp_hex)
        body = client.get('/review/%s' % fp_hex).get_data(as_text=True)
        self.assertIn('injection-suspect', body)
        self.assertIn('not sent to the LLM', body)

    def test_clean_patch_has_no_injection_badge(self):
        client, conn, fp_hex = self._client()
        body = client.get('/').get_data(as_text=True)
        self.assertNotIn('class="inj"', body)


class GeneratedWebTestCase(ReviewWebFixture, testtools.TestCase):
    """The generated-content mark surfaced in the UI: a worklist badge, a review-page
    badge, tagged file rows, and the marked diff blocks collapsed but never hidden.

    The fixture is gatos in miniature (``MARKED_PATCH``): a regenerated ``configure``
    carrying its autoconf banner ahead of the one hand-written hunk -- so exactly one of
    the two files is marked, which is what makes "only the marked block collapses"
    testable.
    """

    def _marked(self):
        """A client over the marked fixture, with the mark recorded."""
        client, conn, fp_hex = self._client(patch=MARKED_PATCH)
        _seed_generated(conn, fp_hex, MARKED_PATCH)
        return client, conn, fp_hex

    def _details_block(self, body):
        """The whole ``<details>`` element wrapping the collapsed block."""
        start = body.index('<details class="gen-seg"')
        end = body.index('</details>', start) + len('</details>')
        return body[start:end]

    def test_worklist_badges_the_generated_mark(self):
        client, _conn, _fp = self._marked()
        body = client.get('/').get_data(as_text=True)
        self.assertIn('class="gen"', body)
        self.assertIn('autotools/60', body)          # the mark's detail, as the badge text
        # A claim about the files, never a safety verdict.
        self.assertNotIn('safe', body.lower())

    def test_unmarked_row_has_no_generated_badge(self):
        client, _conn, _fp = self._client()          # nothing claimed generation
        body = client.get('/').get_data(as_text=True)
        self.assertNotIn('class="gen"', body)

    def test_review_page_shows_the_generated_badge(self):
        client, _conn, fp_hex = self._marked()
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertIn('claims generated: autotools/60', body)

    def test_marked_file_row_is_tagged_and_keeps_its_anchor(self):
        client, _conn, fp_hex = self._marked()
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        # The marked file keeps its anchor link AND gains the tag; the residue file is
        # linked and untagged.
        self.assertIn('<a href="#file-1">configure</a> <span class="gen"', body)
        self.assertIn('[gen]</span>', body)
        self.assertIn('<a href="#file-2">src/reader.c</a></td>', body)

    def test_only_the_marked_block_is_collapsed(self):
        client, _conn, fp_hex = self._marked()
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertEqual(1, body.count('<details class="gen-seg"'))
        details = self._details_block(body)
        # Collapsed by default: no `open` attribute, and no script makes it so.
        self.assertNotIn(' open', details)
        self.assertIn('### configure', details)
        self.assertNotIn('### src/reader.c', details)   # the residue stays expanded

    def test_the_collapsed_block_carries_the_anchor(self):
        # The anchor for a marked file lives on the <details> itself, so a click in the
        # files-changed list lands on the summary line (a closed block's content cannot
        # be scrolled to); the inner header line does not repeat the id.
        client, _conn, fp_hex = self._marked()
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        details = self._details_block(body)
        self.assertIn('id="file-1"', details.split('<summary>')[0])
        self.assertEqual(1, body.count('id="file-1"'))
        self.assertIn('href="#file-1"', body)           # the files-changed row links it
        self.assertIn('<span id="file-2" class="file">', body)   # unmarked anchor untouched

    def test_collapsed_summary_states_the_facts(self):
        client, _conn, fp_hex = self._marked()
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        details = self._details_block(body)
        summary = ' '.join(details.split('<summary>')[1].split('</summary>')[0].split())
        self.assertIn('configure', summary)             # which file
        self.assertIn('+3', summary)                    # and how much of it
        self.assertIn('-0', summary)
        self.assertIn('claims generated', summary)      # the vocabulary: a claim
        self.assertIn('banner+name', summary)           # which signals fired
        self.assertIn('autoconf 2.59', summary)         # generator and version

    def test_the_collapsed_block_is_present_in_full(self):
        # Collapse is presentation, not information loss: every line of the marked file
        # is on the page, inside the <details>, one click away.
        client, _conn, fp_hex = self._marked()
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        details = self._details_block(body)
        for line in ('ac_cv_generated_a=yes', 'ac_cv_generated_b=yes', 'ac_cv_generated_c=yes'):
            self.assertIn(line, details)
        self.assertIn('# Generated by GNU Autoconf 2.59.', details)
        self.assertIn('char buf[4096]', body)           # the residue, still expanded

    def test_an_unmarked_page_is_byte_identical_with_marks_present(self):
        # The do-no-harm bar: another fingerprint's mark changes nothing here, and an
        # unmarked patch renders with no collapse markup at all.
        client, conn, fp_hex = self._client()
        before = client.get('/review/' + fp_hex).get_data(as_text=True)
        _seed_generated(conn, 'b' * 64, MARKED_PATCH)
        after = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertEqual(before, after)
        self.assertNotIn('<details', after)
        self.assertEqual(1, after.count('<pre class="diff">'))


class ConstructTallyWebTestCase(ReviewWebFixture, testtools.TestCase):
    """The construct-vs-residue tally on the detail page: how many of this body's
    dangerous-construct hits sit in the generated-claiming files, and how many sit in the
    residue -- the second being loud, because it is the one worth a reviewer's attention.

    Advisory display recomputed from the body; the fixtures record the mark and nothing
    else, so no ``dangerous-construct`` observation exists to be read or rewritten.
    """

    def _page(self, patch, *, mark=True):
        client, conn, fp_hex = self._client(patch=patch)
        if mark:
            _seed_generated(conn, fp_hex, patch)
        return client.get('/review/' + fp_hex).get_data(as_text=True)

    def test_the_tally_states_the_counts(self):
        body = self._page(MARKED_CONSTRUCT_PATCH)
        self.assertIn('dangerous constructs in this body:', body)
        self.assertIn('2 total', body)
        self.assertIn('2 in generated-claiming files', body)
        self.assertIn('0 in the residue', body)

    def test_an_all_generated_tally_is_not_loud(self):
        # The loud class is in the shared stylesheet always; what matters is that no
        # element wears it when every hit is inside a generated-claiming file.
        body = self._page(MARKED_CONSTRUCT_PATCH)
        self.assertNotIn('class="residue-hit"', body)

    def test_a_residue_hit_gets_the_loud_class_and_names_itself(self):
        body = self._page(RESIDUE_CONSTRUCT_PATCH)
        self.assertIn('class="residue-hit"', body)
        self.assertIn('1 in the residue', body)
        self.assertIn('3 total', body)
        # The loud span says WHERE and WHICH pattern, so the reviewer knows what to read.
        self.assertIn('shell-out in src/reader.c', body)

    def test_the_tally_never_claims_to_be_the_ledger_count(self):
        # The recorded observations come from the representative body at record time and
        # can differ; the page says the number is recomputed here.
        body = self._page(MARKED_CONSTRUCT_PATCH)
        self.assertIn('Recomputed from this diff at display time', body)
        self.assertNotIn('safe', body.lower())

    def test_an_unmarked_page_with_constructs_renders_no_tally(self):
        body = self._page(RESIDUE_CONSTRUCT_PATCH, mark=False)
        self.assertNotIn('dangerous constructs in this body', body)
        self.assertNotIn('class="residue-hit"', body)

    def test_a_marked_page_with_no_constructs_renders_no_tally(self):
        body = self._page(MARKED_PATCH)
        self.assertIn('claims generated: autotools/60', body)   # marked, and badged
        self.assertNotIn('dangerous constructs', body)          # but nothing to tally

    def test_the_row_is_none_for_an_unmarked_context(self):
        from divergulent.tests.test_review import _context
        self.assertIsNone(review_web.construct_tally_row(
            _context(packages=['reader'], diff_body=RESIDUE_CONSTRUCT_PATCH)))

    def test_the_row_carries_the_split_and_the_hits(self):
        from divergulent.tests.test_review import _context
        row = review_web.construct_tally_row(_context(
            packages=['reader'], diff_body=RESIDUE_CONSTRUCT_PATCH,
            generated_detail='autotools/40', generated_paths=frozenset(['ltmain.sh'])))
        self.assertEqual(
            {'total': 3, 'in_marked': 2, 'in_residue': 1,
             'residue_hits': 'shell-out in src/reader.c'}, row)

    def test_an_unmarked_page_stays_byte_identical(self):
        # The do-no-harm bar again, now with a body that HAS constructs: the tally adds
        # nothing to a page with no mark, even once another fingerprint carries one.
        client, conn, fp_hex = self._client(patch=RESIDUE_CONSTRUCT_PATCH)
        before = client.get('/review/' + fp_hex).get_data(as_text=True)
        _seed_generated(conn, 'b' * 64, MARKED_CONSTRUCT_PATCH)
        after = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertEqual(before, after)
        self.assertNotIn('dangerous constructs', after)


class ReachWebTestCase(ReviewWebFixture, testtools.TestCase):
    """The install-base reach surfaced in the UI: a badge, a filter, live order."""

    def _seed_reach(self, conn, fingerprint, level):
        ledger_mod.append_observation(
            conn, fingerprint=fingerprint, kind=reach.REACH_KIND, detail=level,
            evidence='{}', observed_by=reach.REACH_OBSERVED_BY,
            rule_version=reach.REACH_VERSION, observed_at=WHEN)
        conn.commit()

    def _seed_risk(self, conn, fingerprint, level):
        ledger_mod.append_observation(
            conn, fingerprint=fingerprint, kind=risk.RISK_KIND, detail=level,
            evidence='{}', observed_by=risk.RISK_OBSERVED_BY_PREFIX + 'm',
            rule_version=1, observed_at=WHEN)
        conn.commit()

    def test_worklist_badges_reach_and_orders_within_a_risk_tier(self):
        # Same (zero) risk tier: the main fp has the LOWER stored priority (5) but
        # is reach XL; the extra has higher priority (9) but no reach. Reach wins.
        client, conn, fp_hex = self._client(extra_items=[dict(
            fingerprint='b' * 64, draft_category='bugfix', priority=9)])
        self._seed_reach(conn, fp_hex, 'XL')
        body = client.get('/').get_data(as_text=True)
        self.assertIn('reach XL', body)   # the badge (class="reach XL")
        self.assertLess(body.index(fp_hex[:16]), body.index(('b' * 64)[:16]))

    def test_reach_never_crosses_a_risk_tier_in_the_worklist(self):
        # The hard rule, in the UI: an XL low-risk patch must NOT outrank a
        # high-risk patch with no reach.
        client, conn, fp_hex = self._client(extra_items=[dict(
            fingerprint='b' * 64, draft_category='bugfix', priority=1)])
        self._seed_reach(conn, fp_hex, 'XL')             # main: XL, risk 0
        self._seed_risk(conn, 'b' * 64, 'high')          # extra: high risk, no reach
        body = client.get('/').get_data(as_text=True)
        self.assertLess(body.index(('b' * 64)[:16]), body.index(fp_hex[:16]))

    def test_review_page_shows_the_reach_badge(self):
        client, conn, fp_hex = self._client()
        self._seed_reach(conn, fp_hex, 'XS')             # the rman case
        body = client.get('/review/%s' % fp_hex).get_data(as_text=True)
        self.assertIn('reach: XS', body)

    def test_reach_filter_narrows_the_worklist(self):
        client, conn, fp_hex = self._client(extra_items=[dict(
            fingerprint='b' * 64, draft_category='bugfix', priority=1)])
        self._seed_reach(conn, fp_hex, 'XL')
        self._seed_reach(conn, 'b' * 64, 'XS')
        xl_only = client.get('/?reach=XL').get_data(as_text=True)
        self.assertIn(fp_hex[:16], xl_only)
        self.assertNotIn(('b' * 64)[:16], xl_only)


class NotesWebTestCase(ReviewWebFixture, testtools.TestCase):
    """Signed reviewer notes: shown with provenance, added via POST, indicated."""

    def test_review_page_shows_a_note_with_signer_and_signature(self):
        client, conn, fp_hex = self._client()
        ledger_mod.append_note(conn, fingerprint=fp_hex, body='unsafe sprintf here',
                               signed_by='rev@example.org', signature='SIGBUNDLE-XYZ',
                               created_at=WHEN)
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertIn('id="notes"', body)
        self.assertIn('unsafe sprintf here', body)   # the note body
        self.assertIn('rev@example.org', body)        # the signer identity
        self.assertIn('SIGBUNDLE-XYZ', body)          # the signature is shown

    def test_post_note_records_a_signed_note_and_redirects(self):
        signer, seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        resp = self._post(client, '/note/' + fp_hex, {'body': 'looks risky'})
        self.assertEqual(302, resp.status_code)
        self.assertIn('record_bytes', seen)           # it was signed
        rows = ledger_mod.notes_for(conn, fp_hex)
        self.assertEqual(1, len(rows))
        self.assertEqual('looks risky', rows[0]['body'])
        self.assertEqual('reviewer@example.org', rows[0]['signed_by'])
        self.assertEqual('FAKE-SIG', rows[0]['signature'])

    def test_a_note_that_failed_to_land_is_absent_from_the_ledger(self):
        """The third handler with the same guarantee: signed, so it must be deliberate.

        record_note signs before it writes, which makes a SIGNER failure harmless --
        but the append itself commits, and a database busy at commit time fails after
        the INSERT, leaving the signed note staged on this process-lifetime
        connection for the next commit to flush.  The reviewer is shown a 502 saying
        nothing was recorded, so nothing may be recorded.
        """
        signer, _seen = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)

        def _fail_at_commit(*args, **kwargs):
            conn.execute(
                'INSERT INTO note (fingerprint, body, signed_by, signature, created_at) '
                'VALUES (?, ?, ?, ?, ?)', (fp_hex, 'staged', 'r@example.org', 'S', WHEN))
            raise sqlite3.OperationalError('database is locked')

        self.patch(ledger_mod, 'append_note', _fail_at_commit)
        resp = self._post(client, '/note/' + fp_hex, {'body': 'looks risky'})

        self.assertEqual(502, resp.status_code)
        self.assertEqual([], ledger_mod.notes_for(conn, fp_hex))
        # The next successful commit on this connection must not carry it along.
        ledger_mod.append_review_item(
            conn, fingerprint=fp_hex, reason='later work', draft_category=None,
            draft_confidence=None, enqueued_at=WHEN, priority=0)
        self.assertEqual([], ledger_mod.notes_for(conn, fp_hex))

    def test_empty_note_is_a_noop(self):
        signer, _ = _recording_signer()
        client, conn, fp_hex = self._client(signer=signer)
        self._post(client, '/note/' + fp_hex, {'body': '   '})
        self.assertEqual([], ledger_mod.notes_for(conn, fp_hex))

    def test_post_note_without_a_signer_is_rejected(self):
        client, _conn, fp_hex = self._client()        # no signer -> read-only
        resp = self._post(client, '/note/' + fp_hex, {'body': 'x'})
        self.assertEqual(405, resp.status_code)

    def test_signer_failure_is_a_page_and_records_nothing(self):
        client, conn, fp_hex = self._client(signer=_failing_signer())
        resp = self._post(client, '/note/' + fp_hex, {'body': 'x'})
        self.assertEqual(502, resp.status_code)
        self.assertEqual([], ledger_mod.notes_for(conn, fp_hex))

    def test_keyboard_shortcuts_ignore_the_notes_textarea(self):
        # Typing [ ] v 1-9 a d in the notes box must insert characters, not fire
        # the diff/verdict shortcuts -- the keydown guards must skip a TEXTAREA.
        signer, _ = _recording_signer()
        client, _conn, fp_hex = self._client(signer=signer)
        body = client.get('/review/' + fp_hex).get_data(as_text=True)
        self.assertIn("e.target.tagName === 'TEXTAREA'", body)

    def test_worklist_shows_a_note_count_badge(self):
        client, conn, fp_hex = self._client()
        for body in ('n1', 'n2'):
            ledger_mod.append_note(conn, fingerprint=fp_hex, body=body, signed_by='a',
                                   signature='s', created_at=WHEN)
        body = client.get('/').get_data(as_text=True)
        self.assertIn('note-badge', body)
        self.assertIn('2 note(s)', body)
