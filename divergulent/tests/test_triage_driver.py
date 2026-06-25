"""Tests for divergulent.classify.triage_driver -- the step-4d bounded driver.

All tests are OFFLINE: the LLM ``call`` boundary is injected as a fake that
returns canned triage / verify JSON (routed on the verify-prompt marker, exactly
as ``test_verify_triage`` does), so no real LLM and no ``claude -p`` subprocess
ever runs.  A small synthetic corpus (content-addressed bodies + a phase-1
fingerprint index) and a ledger seeded with heuristic ``unknown`` decisions (so
the fingerprints are queued) plus one dangerous-construct observation are built
by hand.

Coverage:

  * the dangerous-construct fingerprint is triaged FIRST (priority ordering);
  * ``--limit`` caps the run and ``untriaged_remaining`` reflects the rest, with
    no silent truncation;
  * a verified result records a verified llm decision; a needs_human one enqueues
    a pending review item;
  * a cluster of >= K identical verified verdicts surfaces a candidate rule, and
    nothing below the threshold does;
  * the CLI writes the findings note and prints the honest summary.
"""
import io
import json
import os
import sqlite3
import tempfile
from unittest import mock
from contextlib import redirect_stdout

import testtools

from divergulent.classify import ledger as ledger_mod
from divergulent.classify import triage as triage_mod
from divergulent.classify import triage_driver
from divergulent.classify import verdict as verdict_mod
from divergulent.classify.corpus import body_sha256
from divergulent.classify.fingerprint import fingerprint


WHEN = '2026-06-14T00:00:00Z'

# The verify prompt carries this phrase; the triage prompt does not (see
# ``triage.build_verify_prompt`` / ``test_verify_triage``).  The fake ``call``
# routes on it so one injected ``call`` answers both passes.
_VERIFY_MARKER = 'adversarial reviewer'


# ---------------------------------------------------------------------------
# Synthetic diffs.  Each opens with a DEP-3 header (claim) the LLM must not see,
# and a distinct diff body.  The three "code-only bugfix" patches share a
# structural shape (one .c file) so a verified-bugfix cluster can form.
# ---------------------------------------------------------------------------

def _code_patch(name, marker):
    return (
        'Description: %s\n'
        'Forwarded: no\n'
        '\n'
        '--- a/src/%s.c\n'
        '+++ b/src/%s.c\n'
        '@@ -1,3 +1,3 @@\n'
        ' int f(void) {\n'
        '-    return %s;\n'
        '+    return %s + 1;\n'
        ' }\n' % (name, marker, marker, marker, marker))


# Three look-alike code-only bodies (distinct fingerprints, same shape).
BUG_A = _code_patch('alpha fix', 'a')
BUG_B = _code_patch('beta fix', 'b')
BUG_C = _code_patch('gamma fix', 'c')

# A doc-only patch (different structural shape).
DOC = (
    'Description: tidy the manpage\n'
    '--- a/doc/tool.1\n'
    '+++ b/doc/tool.1\n'
    '@@ -1,2 +1,2 @@\n'
    ' .TH TOOL 1\n'
    '-old\n'
    '+new\n')

# A patch carrying a dangerous construct (a shell-out added to a .sh file).
DANGER = (
    'Description: harmless cleanup\n'
    '--- a/bin/run.sh\n'
    '+++ b/bin/run.sh\n'
    '@@ -1,2 +1,3 @@\n'
    ' #!/bin/sh\n'
    ' echo start\n'
    '+system("/bin/sh -c rm")\n')


_BODIES = {
    'bug-a.patch': BUG_A,
    'bug-b.patch': BUG_B,
    'bug-c.patch': BUG_C,
    'doc.patch': DOC,
    'danger.patch': DANGER,
}


def _fp(text):
    return fingerprint(text)[1]


def _build_corpus(corpus_dir):
    """Lay down the content-addressed bodies + a phase-1 fingerprint index.

    Returns ``(index_path, {patch_name: fingerprint})``.  ``bug-a`` is given two
    provenance rows (two packages) so it has a higher occurrence count than its
    siblings -- used to assert occurrence-ordering among non-dangerous items.
    """
    for text in _BODIES.values():
        sha = body_sha256(text)
        directory = os.path.join(corpus_dir, 'bodies', sha[:2])
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, sha), 'w', encoding='utf-8') as handle:
            handle.write(text)

    patch_rows = [
        ('pkg-a', '1-1', 'bug-a.patch', body_sha256(BUG_A)),
        ('pkg-a2', '1-1', 'bug-a.patch', body_sha256(BUG_A)),  # second occurrence
        ('pkg-b', '1-1', 'bug-b.patch', body_sha256(BUG_B)),
        ('pkg-c', '1-1', 'bug-c.patch', body_sha256(BUG_C)),
        ('pkg-d', '1-1', 'doc.patch', body_sha256(DOC)),
        ('pkg-e', '1-1', 'danger.patch', body_sha256(DANGER)),
    ]
    name_for_sha = {body_sha256(text): name for name, text in _BODIES.items()}

    index_path = os.path.join(corpus_dir, 'fingerprints.sqlite')
    connection = sqlite3.connect(index_path)
    try:
        connection.execute(
            'CREATE TABLE patch ('
            'source_package TEXT NOT NULL, version TEXT NOT NULL, '
            'patch_name TEXT NOT NULL, raw_sha256 TEXT NOT NULL, '
            'normalisation_version INTEGER NOT NULL, fingerprint TEXT NOT NULL)')
        connection.executemany(
            'INSERT INTO patch (source_package, version, patch_name, raw_sha256, '
            'normalisation_version, fingerprint) VALUES (?, ?, ?, ?, ?, ?)',
            [(pkg, ver, pname, sha, 1, _fp(_BODIES[name_for_sha[sha]]))
             for pkg, ver, pname, sha in patch_rows])
        connection.commit()
    finally:
        connection.close()

    fingerprints = {name: _fp(text) for name, text in _BODIES.items()}
    return index_path, fingerprints


def _seed_ledger(conn, fingerprints):
    """Seed each fingerprint with a heuristic ``unknown`` decision (so it queues),
    and a live dangerous-construct observation on the DANGER fingerprint."""
    ledger_mod.register_rules(conn, ledger_mod.default_registry())
    for fp_hex in fingerprints.values():
        ledger_mod.append_decision(
            conn, fingerprint=fp_hex, category='unknown', confidence='low',
            decided_by='substantive', rule_version=1, kind='heuristic',
            evidence=None, decided_at=WHEN, commit=False)
    ledger_mod.append_observation(
        conn, fingerprint=fingerprints['danger.patch'], kind='dangerous-construct',
        detail='shell-out', evidence='system("/bin/sh -c rm")',
        observed_by='dangerous-construct-scan', rule_version=1, observed_at=WHEN)
    conn.commit()


def _triage_json(category='bugfix', confidence='high', reasoning='r'):
    return json.dumps({'category': category, 'confidence': confidence, 'reasoning': reasoning})


def _verify_json(agrees=True, confidence='high', reasoning='r'):
    return json.dumps({'agrees': agrees, 'confidence': confidence, 'reasoning': reasoning})


def _fixed_call(*, triage_response, verify_response, recorder=None):
    """A fake ``call`` that answers the triage draft and the verification.

    The same canned answer for every fingerprint -- enough to exercise routing,
    ordering, and clustering.  Records the (prompt, model) sequence so a test can
    assert which fingerprint was triaged first.
    """
    def call(prompt, *, model):
        if recorder is not None:
            recorder.append(prompt)
        if _VERIFY_MARKER in prompt:
            return verify_response
        return triage_response
    return call


class DriverFixture:
    """Mixin: a synthetic corpus + index + a seeded ledger."""

    def _setup(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        corpus_dir = tmp.name
        index_path, fingerprints = _build_corpus(corpus_dir)
        ledger_path = os.path.join(corpus_dir, 'ledger.sqlite')
        conn = ledger_mod.create_ledger(ledger_path)
        _seed_ledger(conn, fingerprints)
        self.addCleanup(conn.close)
        return corpus_dir, index_path, ledger_path, conn, fingerprints


class WorkListTestCase(DriverFixture, testtools.TestCase):

    def test_dangerous_construct_is_prioritised_first(self):
        corpus_dir, index_path, _, conn, fingerprints = self._setup()
        work_list = triage_driver.build_work_list(conn, index_path)
        # All five queued fingerprints are present.
        self.assertEqual(5, len(work_list))
        # The dangerous-construct fingerprint sorts first.
        self.assertEqual(fingerprints['danger.patch'], work_list[0].fingerprint)
        self.assertTrue(work_list[0].has_dangerous_construct)

    def test_occurrence_count_orders_non_dangerous_items(self):
        corpus_dir, index_path, _, conn, fingerprints = self._setup()
        work_list = triage_driver.build_work_list(conn, index_path)
        non_dangerous = [w for w in work_list if not w.has_dangerous_construct]
        # bug-a has two occurrences, so among the non-dangerous items it leads.
        self.assertEqual(fingerprints['bug-a.patch'], non_dangerous[0].fingerprint)
        self.assertEqual(2, non_dangerous[0].n_occurrences)


class RunTriageTestCase(DriverFixture, testtools.TestCase):

    def test_dangerous_construct_triaged_first_and_routed_to_human(self):
        corpus_dir, index_path, _, conn, fingerprints = self._setup()
        recorder = []
        call = _fixed_call(
            triage_response=_triage_json(category='bugfix'),
            verify_response=_verify_json(agrees=True), recorder=recorder)
        stats, triaged = triage_driver.run_triage(
            conn, corpus_dir, index_path, call=call, now=WHEN, limit=5)
        # The first triaged item is the dangerous-construct fingerprint.
        self.assertEqual(fingerprints['danger.patch'], triaged[0].item.fingerprint)
        # ... and it routed to human (the dangerous-construct escape hatch) and
        # enqueued a pending review item.
        self.assertEqual('needs_human', triaged[0].result.routing)
        pending = ledger_mod.pending_review_items(conn)
        self.assertIn(fingerprints['danger.patch'],
                      {item['fingerprint'] for item in pending})

    def test_limit_caps_the_run_and_reports_untriaged_remaining(self):
        corpus_dir, index_path, _, conn, _ = self._setup()
        call = _fixed_call(
            triage_response=_triage_json(category='bugfix'),
            verify_response=_verify_json(agrees=True))
        stats, triaged = triage_driver.run_triage(
            conn, corpus_dir, index_path, call=call, now=WHEN, limit=2)
        self.assertEqual(5, stats.queue_size)
        self.assertEqual(2, stats.triaged)
        # The cap is VISIBLE: three were not covered.
        self.assertEqual(3, stats.untriaged_remaining)
        self.assertEqual(2, len(triaged))

    def test_verified_results_record_verified_decisions(self):
        corpus_dir, index_path, _, conn, fingerprints = self._setup()
        # bugfix verified, matching nothing claimed (claims are doc/unknown here),
        # so the three code-only bodies (no dangerous construct, claim unknown)
        # verify.  Drive only those by limiting after sorting is awkward; instead
        # triage all and inspect the bug-a decision.
        call = _fixed_call(
            triage_response=_triage_json(category='bugfix', confidence='high'),
            verify_response=_verify_json(agrees=True, confidence='high'))
        triage_driver.run_triage(
            conn, corpus_dir, index_path, call=call, now=WHEN, limit=5)
        decisions = {d['fingerprint']: d for d in ledger_mod.live_decisions(conn)
                     if d['kind'] == 'llm'}
        bug_a = decisions[fingerprints['bug-a.patch']]
        self.assertEqual('bugfix', bug_a['category'])
        self.assertEqual(1, bug_a['verified'])

    def test_needs_human_results_enqueue_review_items(self):
        corpus_dir, index_path, _, conn, _ = self._setup()
        # The verifier refutes everything -> every result needs human.
        call = _fixed_call(
            triage_response=_triage_json(category='bugfix'),
            verify_response=_verify_json(agrees=False))
        stats, _ = triage_driver.run_triage(
            conn, corpus_dir, index_path, call=call, now=WHEN, limit=5)
        self.assertEqual(5, stats.needs_human)
        self.assertEqual(0, stats.verified)
        self.assertEqual(5, len(ledger_mod.pending_review_items(conn)))

    def test_stats_count_categories_and_mismatches(self):
        corpus_dir, index_path, _, conn, _ = self._setup()
        call = _fixed_call(
            triage_response=_triage_json(category='bugfix'),
            verify_response=_verify_json(agrees=True))
        stats, _ = triage_driver.run_triage(
            conn, corpus_dir, index_path, call=call, now=WHEN, limit=5)
        self.assertEqual(5, stats.by_category['bugfix'])
        # The doc patch claims documentation but content reads bugfix -> mismatch.
        self.assertGreaterEqual(stats.claim_mismatches, 1)


class CandidateRulesTestCase(DriverFixture, testtools.TestCase):

    def test_cluster_of_identical_verified_verdicts_surfaces_a_rule(self):
        corpus_dir, index_path, _, conn, _ = self._setup()
        # All-verified bugfix.  The three code-only bodies share a structural key
        # (one .c file, code-only) and all verify bugfix -> a candidate rule.
        call = _fixed_call(
            triage_response=_triage_json(category='bugfix', confidence='high'),
            verify_response=_verify_json(agrees=True, confidence='high'))
        _, triaged = triage_driver.run_triage(
            conn, corpus_dir, index_path, call=call, now=WHEN, limit=5)
        scan = triage_driver.candidate_rules(corpus_dir, triaged, min_members=3)
        self.assertEqual(1, len(scan.candidates))
        candidate = scan.candidates[0]
        self.assertEqual('bugfix', candidate.category)
        self.assertEqual(3, candidate.member_count)
        self.assertIn('code-only', candidate.structural_key)
        self.assertEqual([], scan.rejected)  # one clean category -> nothing refused

    def test_below_threshold_surfaces_no_rule(self):
        corpus_dir, index_path, _, conn, _ = self._setup()
        call = _fixed_call(
            triage_response=_triage_json(category='bugfix', confidence='high'),
            verify_response=_verify_json(agrees=True, confidence='high'))
        _, triaged = triage_driver.run_triage(
            conn, corpus_dir, index_path, call=call, now=WHEN, limit=5)
        # Threshold of 4 is above the 3-member code-only cluster.
        scan = triage_driver.candidate_rules(corpus_dir, triaged, min_members=4)
        self.assertEqual([], scan.candidates)

    def test_unverified_items_do_not_cluster(self):
        corpus_dir, index_path, _, conn, _ = self._setup()
        # Everything refuted -> nothing verified -> no candidate rules.
        call = _fixed_call(
            triage_response=_triage_json(category='bugfix', confidence='high'),
            verify_response=_verify_json(agrees=False))
        _, triaged = triage_driver.run_triage(
            conn, corpus_dir, index_path, call=call, now=WHEN, limit=5)
        self.assertEqual([], triage_driver.candidate_rules(corpus_dir, triaged, min_members=3).candidates)


class ReportTestCase(DriverFixture, testtools.TestCase):

    def test_report_surfaces_untriaged_remaining_and_candidates(self):
        corpus_dir, index_path, _, conn, _ = self._setup()
        call = _fixed_call(
            triage_response=_triage_json(category='bugfix', confidence='high'),
            verify_response=_verify_json(agrees=True, confidence='high'))
        stats, triaged = triage_driver.run_triage(
            conn, corpus_dir, index_path, call=call, now=WHEN, limit=2)
        scan = triage_driver.candidate_rules(corpus_dir, triaged, min_members=1)
        report = triage_driver.render_run_report(stats, scan)
        self.assertIn('# Phase 4 triage run findings', report)
        self.assertIn('Untriaged remaining', report)
        self.assertIn('3', report)  # 5 queued - 2 triaged
        self.assertIn('Candidate deterministic rules', report)


# ---------------------------------------------------------------------------
# CLI -- driven through triage.main with the real backend MONKEYPATCHED to the
# injected fake, so no real LLM / claude -p subprocess ever runs.
# ---------------------------------------------------------------------------

class CliTestCase(DriverFixture, testtools.TestCase):

    def _patch_backend(self, call):
        original = triage_mod.claude_cli_call
        triage_mod.claude_cli_call = call
        self.addCleanup(setattr, triage_mod, 'claude_cli_call', original)

    def test_cli_runs_writes_findings_and_prints_summary(self):
        corpus_dir, index_path, ledger_path, conn, _ = self._setup()
        # The CLI opens its own connection; close ours so it sees committed rows.
        conn.commit()

        call = _fixed_call(
            triage_response=_triage_json(category='bugfix', confidence='high'),
            verify_response=_verify_json(agrees=True, confidence='high'))
        self._patch_backend(call)

        findings_path = os.path.join(corpus_dir, 'triage-findings.md')
        out = io.StringIO()
        with redirect_stdout(out):
            code = triage_mod.main(
                [ledger_path, corpus_dir, '--index', index_path, '--limit', '5'])
        self.assertEqual(0, code)
        printed = out.getvalue()
        self.assertIn('triaged 5 of 5 queued', printed)
        self.assertIn('untriaged remaining', printed)
        # The findings note was written.
        self.assertTrue(os.path.exists(findings_path))
        with open(findings_path, encoding='utf-8') as handle:
            self.assertIn('# Phase 4 triage run findings', handle.read())

        # The run recorded llm decisions and rebuilt the verdict cache.
        check = sqlite3.connect(ledger_path)
        self.addCleanup(check.close)
        llm = [d for d in ledger_mod.live_decisions(check) if d['kind'] == 'llm']
        self.assertEqual(5, len(llm))
        # The current_verdict cache exists (rebuild ran).
        self.assertTrue(verdict_mod.current_verdict(check))


class ResilienceTestCase(DriverFixture, testtools.TestCase):
    """Resume safely and never let one bad patch abort the run or re-spend budget."""

    def test_a_re_run_never_recalls_the_model(self):
        # Budget safety: after a full run, a second run must spend NOTHING on the
        # model -- verified items have left the queue (settled), and any items
        # still queued (routed to human) are skipped because they are already
        # triaged. Either way the model is not called again.
        corpus_dir, index_path, _, conn, _ = self._setup()
        call = _fixed_call(
            triage_response=_triage_json(category='bugfix'),
            verify_response=_verify_json(agrees=True))
        first, _ = triage_driver.run_triage(
            conn, corpus_dir, index_path, call=call, now=WHEN, limit=5)
        self.assertEqual(5, first.triaged)

        calls = []

        def counting_call(prompt, *, model):
            calls.append(1)
            return _triage_json(category='bugfix')

        second, _ = triage_driver.run_triage(
            conn, corpus_dir, index_path, call=counting_call, now=WHEN, limit=5)
        self.assertEqual(0, len(calls), 'the model must not be re-called on a re-run')
        self.assertEqual(0, second.triaged)

    def test_backend_error_routes_each_to_human_and_does_not_crash(self):
        corpus_dir, index_path, _, conn, _ = self._setup()

        def boom(prompt, *, model):
            raise RuntimeError('claude -p failed (exit 1): Prompt is too long')

        stats, _ = triage_driver.run_triage(
            conn, corpus_dir, index_path, call=boom, now=WHEN, limit=5)
        self.assertEqual(0, stats.triaged)
        self.assertEqual(5, stats.errored)
        self.assertEqual(5, stats.needs_human)
        self.assertEqual(5, len(ledger_mod.pending_review_items(conn)))

        # The errored fingerprints were RECORDED, so a re-run skips them (no
        # re-spend, no crash) rather than re-calling the failing backend.
        second, _ = triage_driver.run_triage(
            conn, corpus_dir, index_path, call=boom, now=WHEN, limit=5)
        self.assertEqual(5, second.skipped_already_triaged)

    def test_oversized_diff_routed_to_human_without_calling_the_model(self):
        corpus_dir, index_path, _, conn, _ = self._setup()

        def boom(prompt, *, model):
            raise AssertionError('the model must not be called for an oversized diff')

        with mock.patch('divergulent.classify.triage_driver.MAX_DIFF_CHARS_FOR_LLM', 1):
            stats, _ = triage_driver.run_triage(
                conn, corpus_dir, index_path, call=boom, now=WHEN, limit=5)
        self.assertEqual(0, stats.triaged)
        self.assertEqual(5, stats.too_large)
        self.assertEqual(5, stats.needs_human)
        self.assertEqual(5, len(ledger_mod.pending_review_items(conn)))


class CrossBatchClusteringTestCase(DriverFixture, testtools.TestCase):
    """Rule discovery must cluster across the whole ledger, not just one run --
    a pattern that accumulates over several batches still surfaces a candidate."""

    def _seed_verified(self, conn, fingerprints, names, category='bugfix'):
        for name in names:
            ledger_mod.append_decision(
                conn, fingerprint=fingerprints[name], category=category,
                confidence='high', decided_by='llm-triage:m', rule_version=1,
                kind='llm', verified=True, evidence='', decided_at=WHEN, commit=False)
        conn.commit()

    def test_clusters_verified_decisions_across_batches(self):
        corpus_dir, index_path, _, conn, fingerprints = self._setup()
        # Three look-alike code-only patches verified as bugfix -- as if triaged
        # across separate runs (no single run's `triaged` list held all three).
        self._seed_verified(conn, fingerprints, ['bug-a.patch', 'bug-b.patch', 'bug-c.patch'])

        scan = triage_driver.candidate_rules_from_ledger(conn, corpus_dir, index_path)
        self.assertEqual(1, len(scan.candidates))
        self.assertEqual(3, scan.candidates[0].member_count)
        self.assertEqual('bugfix', scan.candidates[0].category)

        # The per-run view over an empty run finds nothing -- proving the value is
        # the cross-batch ledger clustering, not a single run.
        self.assertEqual([], triage_driver.candidate_rules(corpus_dir, []).candidates)

    def test_unverified_decisions_do_not_cluster_from_the_ledger(self):
        corpus_dir, index_path, _, conn, fingerprints = self._setup()
        for name in ['bug-a.patch', 'bug-b.patch', 'bug-c.patch']:
            ledger_mod.append_decision(
                conn, fingerprint=fingerprints[name], category='bugfix',
                confidence='high', decided_by='llm-triage:m', rule_version=1,
                kind='llm', verified=False, evidence='', decided_at=WHEN, commit=False)
        conn.commit()
        scan = triage_driver.candidate_rules_from_ledger(conn, corpus_dir, index_path)
        self.assertEqual([], scan.candidates)

    def test_counterexample_gate_refuses_an_ambiguous_structure(self):
        # bug-a/b verify bugfix, bug-c verifies security -- all three share the
        # code-only structural key.  The key carries two categories, so the gate
        # refuses the bugfix cluster rather than proposing an unsound rule.
        corpus_dir, index_path, _, conn, fingerprints = self._setup()
        self._seed_verified(conn, fingerprints, ['bug-a.patch', 'bug-b.patch'], category='bugfix')
        self._seed_verified(conn, fingerprints, ['bug-c.patch'], category='security')

        scan = triage_driver.candidate_rules_from_ledger(
            conn, corpus_dir, index_path, min_members=2)
        self.assertEqual([], scan.candidates)  # the 2-member bugfix cluster is gated out
        self.assertEqual(1, len(scan.rejected))
        rejected = scan.rejected[0]
        self.assertIn('code-only', rejected.structural_key)
        self.assertEqual({'bugfix': 2, 'security': 1}, rejected.category_counts)
        self.assertIn('NOT a rule', rejected.describe())

    def test_human_override_counts_as_a_counterexample(self):
        # A human verdict on a same-key fingerprint disagrees with the LLM cluster;
        # because mining uses the CURRENT verdict, the human category is counted and
        # the structure is (correctly) refused.
        corpus_dir, index_path, _, conn, fingerprints = self._setup()
        self._seed_verified(conn, fingerprints, ['bug-a.patch', 'bug-b.patch'], category='bugfix')
        ledger_mod.append_decision(
            conn, fingerprint=fingerprints['bug-c.patch'], category='security',
            confidence='high', decided_by='human-review', rule_version=1,
            kind='human', verified=True, evidence='', decided_at=WHEN, commit=True)

        scan = triage_driver.candidate_rules_from_ledger(
            conn, corpus_dir, index_path, min_members=2)
        self.assertEqual([], scan.candidates)
        self.assertEqual(1, len(scan.rejected))
        self.assertEqual({'bugfix': 2, 'security': 1}, scan.rejected[0].category_counts)
