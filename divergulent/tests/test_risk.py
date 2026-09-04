"""Tests for divergulent.classify.risk -- the claim-blind security-risk gate.

All OFFLINE: the LLM ``call`` boundary is injected as a fake returning canned
JSON, so no real claude -p / network. Coverage: the gate is claim-blind (the
DEP-3 description never reaches the prompt), parses the coarse level, degrades
unparseable/out-of-scale responses to ``elevated`` (recall-safe, never buried),
carries (model, prompt_version) + usage, and records a supersedable
``security-risk`` observation that re-scoring replaces (exactly one live).

The phase-3 residue routing sits alongside: a marked fingerprint is scored on a
residue-first PROJECTION of its diff (project, THEN cap, so the budget is spent on
hand-written residue) whose facts land in the observation's payload; an unmarked one
is byte-identical to before the mark existed; an ``oversized`` patch with a small
residue is unlocked through the shared helper while its big-residue and unmarked
siblings stay locked; and the flag-gated re-risk pass supersedes exactly the marked
scores that were read off a truncated generated head -- once, and never again.

The prompt-injection tripwire gates the gate itself: a diff-region suspect never
reaches the injected ``call`` at all, is dispositioned at the recall-safe level
instead of being left un-scored, and is not re-selected on the next run.
"""
import json
import os
import sqlite3
import tempfile

import testtools

from divergulent.classify import generated
from divergulent.classify import injection
from divergulent.classify import ledger as ledger_mod
from divergulent.classify import reviewability
from divergulent.classify import risk
from divergulent.classify import triage as triage_mod
from divergulent.classify.corpus import body_sha256
from divergulent.classify.fingerprint import fingerprint as fingerprint_of

WHEN = '2026-06-26T00:00:00Z'
LATER = '2026-06-27T00:00:00Z'

_DESCRIPTION = 'Trivial cleanup, totally harmless.'  # the author claim that must not leak
_DIFF = (
    '--- a/src/parser.c\n'
    '+++ b/src/parser.c\n'
    '@@ -10,7 +10,7 @@\n'
    ' int parse(const char *s) {\n'
    '-    char buf[8];\n'
    '+    char buf[64];\n'
    '     strcpy(buf, s);\n'
    ' }\n')


def _patch():
    return 'Description: %s\nForwarded: no\n\n%s' % (_DESCRIPTION, _DIFF)


def _fake_call(text, *, usage=None, recorder=None):
    def call(system, user, *, model):
        if recorder is not None:
            recorder.append((system, user, model))
        return triage_mod.CallResult(text=text, usage=usage or triage_mod.Usage())
    return call


def _risk_json(risk_level='elevated', reason='Touches a fixed buffer near strcpy.'):
    return json.dumps({'risk': risk_level, 'reason': reason})


class ScoreRiskTestCase(testtools.TestCase):

    def test_parses_level_rank_and_reason(self):
        score = risk.score_risk(_patch(), call=_fake_call(_risk_json('high', 'plausible overflow')))
        self.assertEqual('high', score.level)
        self.assertEqual(risk.RISK_RANK['high'], score.rank)
        self.assertEqual('plausible overflow', score.reason)

    def test_is_claim_blind(self):
        recorder = []
        risk.score_risk(_patch(), call=_fake_call(_risk_json(), recorder=recorder))
        system, user, _model = recorder[0]
        self.assertNotIn(_DESCRIPTION, system + user)   # the author claim never leaks
        self.assertIn('char buf[64]', user)             # the diff rides in the user message

    def test_carries_model_prompt_version_and_usage(self):
        usage = triage_mod.Usage(input_tokens=600, output_tokens=40)
        score = risk.score_risk(
            _patch(), call=_fake_call(_risk_json(), usage=usage), model='claude-sonnet-4-6')
        self.assertEqual('claude-sonnet-4-6', score.model)
        self.assertEqual(risk.RISK_PROMPT_VERSION, score.prompt_version)
        self.assertEqual(usage, score.usage)

    def test_default_model_is_the_bakeoff_pick(self):
        score = risk.score_risk(_patch(), call=_fake_call(_risk_json()))
        self.assertEqual(risk.DEFAULT_RISK_MODEL, score.model)

    def test_out_of_scale_level_degrades_to_elevated(self):
        # A model returning 'critical' (out of the 4-level scale) must not be
        # silently dropped -- it routes for review.
        score = risk.score_risk(_patch(), call=_fake_call(_risk_json('critical')))
        self.assertEqual('elevated', score.level)
        self.assertIn('out-of-scale', score.reason)

    def test_unparseable_response_degrades_to_elevated(self):
        score = risk.score_risk(_patch(), call=_fake_call('no json at all'))
        self.assertEqual('elevated', score.level)  # recall-safe: never buried

    def test_the_cap_cannot_be_raised_past_the_injection_screen_bound(self):
        """No flag value hands the model text the tripwire never screened.

        The screen guarantees only that the first MAX_SCAN_CHARS of a body were
        looked at.  ``cap_diff`` reads a non-positive max as "no cap" and the CLI
        used to advertise "0 disables", so ``--max-diff-chars 0`` would have sent
        the whole multi-megabyte body -- unscreened tail included -- to the model
        both LLM tiers skip a suspect to protect.
        """
        oversized = injection.MAX_SCAN_CHARS + 50_000
        body = _DIFF + '+' + ('x' * oversized) + '\n'
        patch = 'Description: x\nForwarded: no\n\n%s' % body

        for requested in (0, -1, injection.MAX_SCAN_CHARS * 4):
            recorder = []
            score = risk.score_risk(
                patch, call=_fake_call(_risk_json(), recorder=recorder),
                max_diff_chars=requested)
            _system, user, _model = recorder[0]
            self.assertTrue(score.truncated, requested)
            # The prompt carries the capped head plus cap_diff's own marker, never
            # the tail past the screen bound.
            self.assertLess(len(user), injection.MAX_SCAN_CHARS + 1000, requested)

    def test_a_cap_below_the_screen_bound_is_left_alone(self):
        # The clamp is a ceiling, not an override: the default still applies.
        recorder = []
        big = _DIFF + '\n'.join('+padding %d' % i for i in range(20000))
        risk.score_risk('Description: x\nForwarded: no\n\n%s' % big,
                        call=_fake_call(_risk_json(), recorder=recorder), max_diff_chars=5000)
        _system, user, _model = recorder[0]
        self.assertLess(len(user), 6000)

    def test_short_diff_is_not_truncated(self):
        score = risk.score_risk(_patch(), call=_fake_call(_risk_json()))
        self.assertFalse(score.truncated)

    def test_long_diff_is_capped_and_flagged(self):
        recorder = []
        big = _DIFF + '\n'.join('+padding %d' % i for i in range(20000))
        patch = 'Description: x\nForwarded: no\n\n%s' % big
        score = risk.score_risk(
            patch, call=_fake_call(_risk_json(), recorder=recorder), max_diff_chars=2000)
        self.assertTrue(score.truncated)
        self.assertGreater(score.original_chars, 2000)
        _system, user, _model = recorder[0]
        self.assertIn('truncated', user)            # the cut is visible to the model
        self.assertLess(len(user), 2000 + 300)      # the user message is bounded near the cap


class RecordRiskObservationTestCase(testtools.TestCase):

    def _ledger(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = ledger_mod.create_ledger(os.path.join(tmp.name, 'l.sqlite'))
        self.addCleanup(conn.close)
        return conn

    def _score(self, level='elevated', model='claude-opus-4-8'):
        return risk.RiskScore(level=level, rank=risk.RISK_RANK[level], reason='r',
                              model=model, prompt_version=risk.RISK_PROMPT_VERSION,
                              raw_response=_risk_json(level))

    def test_records_observation_with_provenance(self):
        conn = self._ledger()
        risk.record_risk_observation(conn, 'fp1', self._score('high'), now=WHEN)
        obs = [o for o in ledger_mod.live_observations(conn) if o['kind'] == risk.RISK_KIND]
        self.assertEqual(1, len(obs))
        self.assertEqual('high', obs[0]['detail'])
        self.assertEqual('risk-gate:claude-opus-4-8', obs[0]['observed_by'])
        self.assertEqual(risk.RISK_PROMPT_VERSION, obs[0]['rule_version'])
        self.assertEqual('high', json.loads(obs[0]['evidence'])['level'])

    def test_rescore_supersedes_the_prior_live_observation(self):
        conn = self._ledger()
        risk.record_risk_observation(conn, 'fp1', self._score('low'), now=WHEN)
        # Re-score (even from a different model) leaves exactly ONE live row.
        risk.record_risk_observation(conn, 'fp1', self._score('high', model='claude-sonnet-4-6'),
                                     now=LATER)
        live = [o for o in ledger_mod.live_observations(conn)
                if o['kind'] == risk.RISK_KIND and o['fingerprint'] == 'fp1']
        self.assertEqual(1, len(live))
        self.assertEqual('high', live[0]['detail'])
        # The superseded original is still in the audit trail.
        self.assertEqual(2, len([o for o in ledger_mod.observations_for(conn, 'fp1')
                                 if o['kind'] == risk.RISK_KIND]))

    def _projected(self, *, projected=True, omitted_files=2, omitted_changed=19258,
                   truncated=False):
        projection = generated.ProjectedDiff(
            text='projected', omitted_files=omitted_files if projected else 0,
            omitted_changed=omitted_changed if projected else 0, projected=projected)
        return risk.RiskScore(
            level='low', rank=risk.RISK_RANK['low'], reason='r', model='claude-opus-4-8',
            prompt_version=risk.RISK_PROMPT_VERSION, raw_response=_risk_json('low'),
            truncated=truncated, original_chars=1234 if truncated else 0, projection=projection)

    def test_projection_facts_are_recorded(self):
        # The model's input is never silently modified: a reviewer can reconstruct
        # what the gate was shown from the payload alone.
        conn = self._ledger()
        risk.record_risk_observation(conn, 'fp1', self._projected(), now=WHEN)
        payload = json.loads(next(o for o in ledger_mod.live_observations(conn)
                                  if o['kind'] == risk.RISK_KIND)['evidence'])
        self.assertIs(True, payload['projected'])
        self.assertEqual(2, payload['omitted_files'])
        self.assertEqual(19258, payload['omitted_changed'])

    def test_truncated_keeps_its_post_projection_meaning(self):
        # Both flags together: the diff was projected, and what survived was still
        # long enough for the cap to bite.
        conn = self._ledger()
        risk.record_risk_observation(conn, 'fp1', self._projected(truncated=True), now=WHEN)
        payload = json.loads(next(o for o in ledger_mod.live_observations(conn)
                                  if o['kind'] == risk.RISK_KIND)['evidence'])
        self.assertIs(True, payload['projected'])
        self.assertIs(True, payload['truncated'])
        self.assertEqual(1234, payload['original_chars'])

    def test_a_no_op_projection_still_records_the_key(self):
        # A mark whose paths are not in this body projects to identity.  The key is
        # recorded anyway (``false``), because its PRESENCE is what stops the
        # re-risk pass selecting this fingerprint again for ever.
        conn = self._ledger()
        risk.record_risk_observation(conn, 'fp1', self._projected(projected=False), now=WHEN)
        payload = json.loads(next(o for o in ledger_mod.live_observations(conn)
                                  if o['kind'] == risk.RISK_KIND)['evidence'])
        self.assertIs(False, payload['projected'])
        self.assertNotIn('omitted_files', payload)
        self.assertNotIn('omitted_changed', payload)

    def test_an_unmarked_payload_gains_no_keys(self):
        # The phase-3 promise: an unmarked fingerprint's record is byte-identical.
        conn = self._ledger()
        risk.record_risk_observation(conn, 'fp1', self._score('low'), now=WHEN)
        evidence = next(o for o in ledger_mod.live_observations(conn)
                        if o['kind'] == risk.RISK_KIND)['evidence']
        self.assertEqual(
            json.dumps({'level': 'low', 'reason': 'r', 'raw_response': _risk_json('low')},
                       sort_keys=True),
            evidence)

    def test_risk_rank_by_fingerprint(self):
        conn = self._ledger()
        risk.record_risk_observation(conn, 'fp-high', self._score('high'), now=WHEN)
        risk.record_risk_observation(conn, 'fp-none', self._score('none'), now=WHEN)
        ranks = risk.risk_rank_by_fingerprint(conn)
        self.assertEqual(risk.RISK_RANK['high'], ranks['fp-high'])
        self.assertEqual(risk.RISK_RANK['none'], ranks['fp-none'])
        self.assertNotIn('fp-unscored', ranks)


def _diff(old, new, a='src/x.c', b=None):
    b = b or a
    return '--- a/%s\n+++ b/%s\n@@ -1 +1 @@\n-%s\n+%s\n' % (a, b, old, new)


class ProvablyBenignTestCase(testtools.TestCase):

    def test_documentation_only_is_culled(self):
        self.assertIsNotNone(risk.provably_benign(_diff('old text', 'new text', a='doc/guide.md')))

    def test_whitespace_only_is_culled(self):
        # Re-indentation of a code line -- no behaviour change, safe to cull.
        ws = ('--- a/src/x.c\n+++ b/src/x.c\n@@ -1,2 +1,2 @@\n'
              ' int main(){\n-return 0;\n+  return 0;\n')
        self.assertIsNotNone(risk.provably_benign(ws))

    def test_translation_is_culled(self):
        self.assertIsNotNone(risk.provably_benign(
            _diff('msgstr "a"', 'msgstr "b"', a='po/de.po')))

    def test_changelog_is_culled(self):
        self.assertIsNotNone(risk.provably_benign(
            _diff('old entry', 'new entry', a='debian/changelog')))

    def test_real_code_change_is_not_culled(self):
        self.assertIsNone(risk.provably_benign(_diff('do_thing();', 'do_other();', a='src/x.c')))

    def test_debian_rules_hardening_change_is_not_culled(self):
        # The security-critical case: a build-flag change must reach the gate.
        self.assertIsNone(risk.provably_benign(
            _diff('CFLAGS = -O2', 'CFLAGS = -O2 -fstack-protector', a='debian/rules')))


class RunRiskGateTestCase(testtools.TestCase):

    def _setup(self):
        from divergulent.tests.test_triage_driver import _build_corpus, _seed_ledger
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path, fingerprints = _build_corpus(tmp.name)
        conn = ledger_mod.create_ledger(os.path.join(tmp.name, 'ledger.sqlite'))
        self.addCleanup(conn.close)
        _seed_ledger(conn, fingerprints)
        return conn, tmp.name, index_path, fingerprints

    def test_culls_the_doc_patch_and_scores_the_code_patches(self):
        conn, corpus_dir, index_path, fingerprints = self._setup()
        stats = risk.run_risk_gate(
            conn, corpus_dir, index_path, call=_fake_call(_risk_json('elevated')),
            now=WHEN, limit=10, model='claude-sonnet-4-6')
        # The doc-only patch is culled (deterministic none); the others scored.
        self.assertGreaterEqual(stats.culled, 1)
        self.assertGreaterEqual(stats.scored, 1)
        # The doc fingerprint carries a culled 'none' from the deterministic source.
        doc = [o for o in ledger_mod.observations_for(conn, fingerprints['doc.patch'])
               if o['kind'] == risk.RISK_KIND][-1]
        self.assertEqual('none', doc['detail'])
        self.assertEqual(risk.RISK_CULL_OBSERVED_BY, doc['observed_by'])
        # A code fingerprint carries an LLM 'elevated' from the gate.
        bug = [o for o in ledger_mod.observations_for(conn, fingerprints['bug-a.patch'])
               if o['kind'] == risk.RISK_KIND][-1]
        self.assertEqual('elevated', bug['detail'])
        self.assertTrue(bug['observed_by'].startswith(risk.RISK_OBSERVED_BY_PREFIX))

    def test_skips_oversized_fingerprints(self):
        conn, corpus_dir, index_path, fingerprints = self._setup()
        # Mark bug-a oversized (not line-reviewable): the gate must not score it.
        ledger_mod.append_observation(
            conn, fingerprint=fingerprints['bug-a.patch'], kind=reviewability.REVIEWABILITY_KIND,
            detail='oversized', evidence='{}', observed_by=reviewability.REVIEWABILITY_OBSERVED_BY,
            rule_version=reviewability.REVIEWABILITY_VERSION, observed_at=WHEN)
        conn.commit()
        stats = risk.run_risk_gate(
            conn, corpus_dir, index_path, call=_fake_call(_risk_json()), now=WHEN, limit=100)
        self.assertEqual(1, stats.skipped_oversized)
        ranks = risk.risk_rank_by_fingerprint(conn)
        self.assertNotIn(fingerprints['bug-a.patch'], ranks)   # never scored or culled
        self.assertIn(fingerprints['bug-b.patch'], ranks)      # a sibling still scored

    def test_scores_settled_patches_not_just_the_residue(self):
        from divergulent.classify import triage_driver
        conn, corpus_dir, index_path, fps = self._setup()
        # Settle bug-a as a verified verdict -> it leaves the residue queue.
        ledger_mod.append_decision(
            conn, fingerprint=fps['bug-a.patch'], category='documentation', confidence='high',
            decided_by='llm-triage:m', rule_version=1, kind='llm', verified=True,
            evidence='', decided_at=WHEN, commit=True)
        residue = {w.fingerprint for w in triage_driver.build_work_list(conn, index_path)}
        every = {w.fingerprint for w in triage_driver.build_work_list(conn, index_path, scope='all')}
        self.assertNotIn(fps['bug-a.patch'], residue)   # settled -> not in the residue
        self.assertIn(fps['bug-a.patch'], every)        # ... but in 'all'
        # The gate scores it anyway (it runs on the whole corpus).
        risk.run_risk_gate(conn, corpus_dir, index_path, call=_fake_call(_risk_json('low')),
                           now=WHEN, limit=20)
        self.assertIn(fps['bug-a.patch'], risk.risk_rank_by_fingerprint(conn))

    def test_rerun_skips_already_scored(self):
        conn, corpus_dir, index_path, _ = self._setup()
        risk.run_risk_gate(conn, corpus_dir, index_path, call=_fake_call(_risk_json()),
                           now=WHEN, limit=10)
        again = risk.run_risk_gate(conn, corpus_dir, index_path, call=_fake_call(_risk_json()),
                                   now=LATER, limit=10)
        self.assertEqual(0, again.scored + again.culled)  # nothing left to score

    def test_risk_level_by_fingerprint_reports_levels(self):
        conn, corpus_dir, index_path, fps = self._setup()
        risk.run_risk_gate(conn, corpus_dir, index_path,
                           call=_fake_call(_risk_json('elevated')), now=WHEN, limit=20)
        self.assertEqual('elevated', risk.risk_level_by_fingerprint(conn)[fps['bug-a.patch']])

    def test_reprioritises_the_review_queue_from_new_risk(self):
        from divergulent.classify import triage_driver
        conn, corpus_dir, index_path, fps = self._setup()
        # Queue bug-a at occurrence-only priority (risk_rank 0), as triage would
        # have done before bug-a was ever risk-scored.
        ledger_mod.append_review_item(
            conn, fingerprint=fps['bug-a.patch'], reason='r', draft_category='unknown',
            draft_confidence='low', enqueued_at=WHEN, priority=1)
        conn.commit()
        stats = risk.run_risk_gate(conn, corpus_dir, index_path,
                                   call=_fake_call(_risk_json('high')), now=WHEN, limit=20)
        self.assertGreaterEqual(stats.reprioritised, 1)
        item = next(i for i in ledger_mod.pending_review_items(conn)
                    if i['fingerprint'] == fps['bug-a.patch'])
        # The new 'high' score now dominates the stored priority (was 1).
        self.assertGreaterEqual(item['priority'], triage_driver.RISK_PRIORITY_WEIGHT)


class InjectionSuspectTestCase(testtools.TestCase):
    """A diff carrying injection-shaped text never reaches the gate.

    The tripwire's whole point: instructions aimed at a model are not fed to the
    model they target.  The gate used to consult it not at all, so a payload
    steering it to ``none`` both shipped that value and -- risk being the top
    prioritisation band -- sank the patch to the bottom of the human queue.
    """

    def _setup(self):
        from divergulent.tests.test_triage_driver import _build_corpus, _seed_ledger
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path, fingerprints = _build_corpus(tmp.name)
        conn = ledger_mod.create_ledger(os.path.join(tmp.name, 'ledger.sqlite'))
        self.addCleanup(conn.close)
        _seed_ledger(conn, fingerprints)
        return conn, tmp.name, index_path, fingerprints

    def _suspect(self, conn, fingerprint, *, region='diff', detail='instruction-phrase'):
        ledger_mod.append_observation(
            conn, fingerprint=fingerprint, kind=injection.INJECTION_KIND,
            detail='%s/%s' % (detail, region), evidence='ignore all previous instructions',
            observed_by='injection-scan', rule_version=injection.INJECTION_RULES_VERSION,
            observed_at=WHEN)
        conn.commit()

    def _run(self, conn, corpus_dir, index_path, *, recorder=None, now=WHEN, level='none'):
        return risk.run_risk_gate(
            conn, corpus_dir, index_path, call=_fake_call(_risk_json(level), recorder=recorder),
            now=now, limit=100)

    def _live_risk(self, conn, fingerprint):
        return next(o for o in ledger_mod.live_observations(conn)
                    if o['kind'] == risk.RISK_KIND and o['fingerprint'] == fingerprint)

    def test_the_suspect_is_never_sent_to_the_model(self):
        conn, corpus_dir, index_path, fps = self._setup()
        self._suspect(conn, fps['bug-b.patch'])
        recorder = []
        stats = self._run(conn, corpus_dir, index_path, recorder=recorder)
        # No prompt cites the suspect's diff -- the payload never reached the gate.
        self.assertFalse(any('src/b.c' in user for _system, user, _model in recorder))
        # ... while a sibling was scored as usual, so the filter is not a blanket stop.
        self.assertTrue(any('src/a.c' in user for _system, user, _model in recorder))
        self.assertEqual(1, stats.skipped_injection)

    def test_the_suspect_is_recorded_elevated_with_the_families_as_evidence(self):
        conn, corpus_dir, index_path, fps = self._setup()
        self._suspect(conn, fps['bug-b.patch'])
        self._run(conn, corpus_dir, index_path)
        obs = self._live_risk(conn, fps['bug-b.patch'])
        # Recall-safe: the same level a response the gate could not parse earns.
        self.assertEqual(risk._PARSE_FAILURE_LEVEL, obs['detail'])
        self.assertEqual(risk.RISK_INJECTION_OBSERVED_BY, obs['observed_by'])
        payload = json.loads(obs['evidence'])
        self.assertEqual(risk._PARSE_FAILURE_LEVEL, payload['level'])
        self.assertIs(True, payload['injection_suspect'])
        self.assertIn('instruction-phrase', payload['reason'])       # the families that fired
        self.assertIn('did not score it', payload['reason'])         # ... and that the model did not
        # It is a real score, so the prioritisation bands see it.
        self.assertEqual(risk.RISK_RANK[risk._PARSE_FAILURE_LEVEL],
                         risk.risk_rank_by_fingerprint(conn)[fps['bug-b.patch']])

    def test_a_second_run_neither_re_records_nor_re_selects_it(self):
        conn, corpus_dir, index_path, fps = self._setup()
        self._suspect(conn, fps['bug-b.patch'])
        self._run(conn, corpus_dir, index_path)
        recorder = []
        again = self._run(conn, corpus_dir, index_path, recorder=recorder, now=LATER)
        self.assertEqual(0, again.scored + again.culled)     # nothing left to score
        self.assertEqual(1, again.skipped_injection)         # still reported
        self.assertEqual([], recorder)                       # and still no call
        rows = [o for o in ledger_mod.observations_for(conn, fps['bug-b.patch'])
                if o['kind'] == risk.RISK_KIND]
        self.assertEqual(1, len(rows))                       # written once, not once per run

    def test_a_score_read_off_the_payload_is_superseded(self):
        # The corpus healing: a live 'none' that the gate produced BEFORE it
        # consulted the tripwire is exactly the score a payload was steering for.
        conn, corpus_dir, index_path, fps = self._setup()
        risk.record_risk_observation(conn, fps['bug-b.patch'], _make_score('none'), now=WHEN)
        self._suspect(conn, fps['bug-b.patch'])
        self._run(conn, corpus_dir, index_path, now=LATER)
        obs = self._live_risk(conn, fps['bug-b.patch'])
        self.assertEqual(risk._PARSE_FAILURE_LEVEL, obs['detail'])
        self.assertEqual(risk.RISK_INJECTION_OBSERVED_BY, obs['observed_by'])
        # The superseded original stays in the audit trail.
        self.assertEqual(2, len([o for o in ledger_mod.observations_for(conn, fps['bug-b.patch'])
                                 if o['kind'] == risk.RISK_KIND]))

    def test_a_header_only_hit_is_still_scored(self):
        # The LLM never reads the header, so a header hit must not divert the gate
        # -- the same line the triage driver draws, through the same helper.
        conn, corpus_dir, index_path, fps = self._setup()
        self._suspect(conn, fps['bug-b.patch'], region='header')
        recorder = []
        stats = self._run(conn, corpus_dir, index_path, recorder=recorder, level='low')
        self.assertEqual(0, stats.skipped_injection)
        self.assertTrue(any('src/b.c' in user for _system, user, _model in recorder))
        self.assertEqual('low', risk.risk_level_by_fingerprint(conn)[fps['bug-b.patch']])

    def test_the_check_is_the_shared_helper_not_a_reimplementation(self):
        """The gate skips whatever the SHARED helper says, not its own idea of a suspect.

        One definition of "injection suspect" for the triage driver and the gate, so
        the two cannot drift.  Asserted by making the helper disagree with the ledger:
        it names a fingerprint carrying no injection observation at all, and withholds
        one that does.  A gate reading the ledger itself would do the opposite of this.
        """
        conn, corpus_dir, index_path, fps = self._setup()
        self._suspect(conn, fps['bug-b.patch'])   # a real hit the fake helper withholds
        self.patch(injection, 'injection_suspect_fingerprints',
                   lambda conn, region=None: {fps['bug-a.patch']})

        recorder = []
        stats = self._run(conn, corpus_dir, index_path, recorder=recorder)

        self.assertEqual(1, stats.skipped_injection)
        self.assertFalse(any('src/a.c' in user for _system, user, _model in recorder))
        self.assertTrue(any('src/b.c' in user for _system, user, _model in recorder))
        self.assertEqual(risk.RISK_INJECTION_OBSERVED_BY,
                         self._live_risk(conn, fps['bug-a.patch'])['observed_by'])


def _make_score(level, model='claude-opus-4-8'):
    return risk.RiskScore(level=level, rank=risk.RISK_RANK[level], reason='r', model=model,
                          prompt_version=risk.RISK_PROMPT_VERSION, raw_response=_risk_json(level))


class PrioritisationTestCase(testtools.TestCase):

    def _setup(self):
        from divergulent.tests.test_triage_driver import _build_corpus, _seed_ledger
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path, fingerprints = _build_corpus(tmp.name)
        conn = ledger_mod.create_ledger(os.path.join(tmp.name, 'ledger.sqlite'))
        self.addCleanup(conn.close)
        _seed_ledger(conn, fingerprints)
        return conn, index_path, fingerprints

    def test_risk_outranks_occurrence_in_the_work_list(self):
        from divergulent.classify import triage_driver
        conn, index_path, fps = self._setup()
        # bug-a has TWO occurrences (would normally outrank bug-b's one). Give
        # bug-b a HIGH risk and bug-a NONE: risk must win.
        risk.record_risk_observation(conn, fps['bug-b.patch'], _make_score('high'), now=WHEN)
        risk.record_risk_observation(conn, fps['bug-a.patch'], _make_score('none'), now=WHEN)

        work = triage_driver.build_work_list(conn, index_path)
        order = [w.fingerprint for w in work]
        self.assertLess(order.index(fps['bug-b.patch']), order.index(fps['bug-a.patch']))

        by_fp = {w.fingerprint: w for w in work}
        self.assertEqual(risk.RISK_RANK['high'], by_fp[fps['bug-b.patch']].risk_rank)
        # ... and the stored review-queue priority is risk-first too.
        self.assertGreater(
            triage_driver._stored_priority(by_fp[fps['bug-b.patch']]),
            triage_driver._stored_priority(by_fp[fps['bug-a.patch']]))

    def test_unscored_residue_keeps_working_unchanged(self):
        # With no risk scores yet (rank 0 for all), the dangerous-construct item is
        # still first -- reorder never drops or starves the existing ordering.
        from divergulent.classify import triage_driver
        conn, index_path, fps = self._setup()
        work = triage_driver.build_work_list(conn, index_path)
        self.assertEqual(fps['danger.patch'], work[0].fingerprint)


# ---------------------------------------------------------------------------
# Phase 3: the gate reads the hand-written residue first
# ---------------------------------------------------------------------------

def _hunk(path, lines):
    """A one-hunk unified diff on ``path``; each entry carries its own diff prefix."""
    added = sum(1 for line in lines if line.startswith('+'))
    removed = sum(1 for line in lines if line.startswith('-'))
    context = len(lines) - added - removed
    return ('--- a/%s\n+++ b/%s\n@@ -1,%d +1,%d @@\n%s'
            % (path, path, context + removed, context + added,
               ''.join('%s\n' % line for line in lines)))


# A gatos in miniature: a regenerated ``configure`` carrying its autoconf banner (so the
# mark captures a generator AND a version) ahead of the one hand-written hunk the gate
# actually needs to read.  Generator output FIRST, as it is in the real patches -- which is
# exactly why an unprojected cap read nothing else.
GENERATED_HUNK = _hunk('configure', [' # Generated by GNU Autoconf 2.59.']
                       + ['+ac_cv_generated_%03d=yes' % index for index in range(80)])
RESIDUE_HUNK = _hunk('src/parser.c', [' int parse(const char *s) {',
                                      '-    char buf[8];',
                                      '+    char buf[64];'])
MARKED_PATCH = 'Description: %s\nForwarded: no\n\n%s%s' % (_DESCRIPTION, GENERATED_HUNK, RESIDUE_HUNK)

# The same shape with a different residue: a second marked fingerprint.
MARKED_PATCH_B = MARKED_PATCH.replace('char buf[64]', 'char buf[128]')


def _mark_files(patch_text):
    """The mark's per-file evidence for ``patch_text`` -- what ``generated_marks`` hands on.

    Round-tripped through ``evidence_for``'s JSON rather than read off the scan objects, so
    the gate is exercised against the dict shape the ledger really stores.
    """
    return json.loads(generated.evidence_for(generated.scan(patch_text)))['files']


class ProjectedScoreRiskTestCase(testtools.TestCase):
    """``score_risk`` with a mark: project residue-first, THEN cap."""

    def _score(self, patch, recorder=None, **kwargs):
        return risk.score_risk(patch, call=_fake_call(_risk_json(), recorder=recorder), **kwargs)

    def _body_sent(self, recorder):
        _system, user, _model = recorder[0]
        return user.split('\n\n', 1)[1]     # past the 'Diff body:' framing

    def test_the_model_reads_the_residue_not_the_generated_head(self):
        recorder = []
        self._score(MARKED_PATCH, recorder=recorder, mark_files=_mark_files(MARKED_PATCH))
        body = self._body_sent(recorder)
        self.assertTrue(body.startswith('1 generated-claiming files not shown'), body[:120])
        self.assertIn('char buf[64]', body)              # the residue is there ...
        self.assertNotIn('ac_cv_generated', body)        # ... and the generator output is not
        self.assertIn('[generated: configure', body)     # replaced by a loud, specific note
        self.assertIn('autoconf 2.59', body)

    def test_the_claim_still_never_leaks(self):
        # Projection reorders the diff body; it does not reintroduce the header.
        recorder = []
        self._score(MARKED_PATCH, recorder=recorder, mark_files=_mark_files(MARKED_PATCH))
        system, user, _model = recorder[0]
        self.assertNotIn(_DESCRIPTION, system + user)

    def test_the_projection_facts_ride_on_the_score(self):
        files = _mark_files(MARKED_PATCH)
        score = self._score(MARKED_PATCH, mark_files=files)
        self.assertTrue(score.projection.projected)
        self.assertEqual(1, score.projection.omitted_files)
        self.assertEqual(files[0]['added'] + files[0]['removed'], score.projection.omitted_changed)

    def test_a_short_projection_is_not_truncated(self):
        score = self._score(MARKED_PATCH, mark_files=_mark_files(MARKED_PATCH))
        self.assertFalse(score.truncated)

    def test_the_cap_bites_the_projection_not_the_generated_head(self):
        # A residue longer than the cap: ``truncated`` keeps its meaning (the cut AFTER
        # projection) and every character the cap did spend went on residue.
        recorder = []
        residue = ''.join(_hunk('src/big_%02d.c' % index, ['+    int fixed_%02d = 1;' % index])
                          for index in range(40))
        patch = 'Description: x\nForwarded: no\n\n%s%s' % (GENERATED_HUNK, residue)
        score = self._score(patch, recorder=recorder, mark_files=_mark_files(patch),
                            max_diff_chars=600)
        self.assertTrue(score.truncated)
        self.assertTrue(score.projection.projected)
        body = self._body_sent(recorder)
        self.assertIn('diff truncated for scoring', body)   # the cut is visible to the model
        self.assertIn('int fixed_00', body)                 # ... and it cut residue ...
        self.assertNotIn('ac_cv_generated', body)           # ... never generator output
        self.assertLess(len(body), 600 + 300)

    def test_an_unmarked_call_is_byte_identical(self):
        recorder = []
        score = risk.score_risk(_patch(), call=_fake_call(_risk_json(), recorder=recorder))
        _system, user, _model = recorder[0]
        self.assertEqual(risk.risk_user_message(triage_mod.diff_body(_patch())), user)
        self.assertIsNone(score.projection)

    def test_an_empty_mark_list_does_not_project(self):
        # Nothing claimed generation -> nothing to project, and nothing recorded.
        self.assertIsNone(self._score(_patch(), mark_files=[]).projection)

    def test_a_mark_that_matches_nothing_projects_to_identity(self):
        # The defensive case: the mark names a file this body does not contain.
        recorder = []
        score = self._score(_patch(), recorder=recorder,
                            mark_files=[{'path': 'configure', 'signals': ['name'],
                                         'generator': None, 'version': None,
                                         'added': 100, 'removed': 0}])
        self.assertFalse(score.projection.projected)
        self.assertEqual(risk.risk_user_message(triage_mod.diff_body(_patch())), recorder[0][1])


def _build_marked_corpus(corpus_dir, bodies):
    """Content-addressed bodies + a phase-1 fingerprint index; returns ``(index, {name: fp})``.

    The sibling of ``test_triage_driver._build_corpus``, taking its own ``{name: text}`` so
    the projection cases can carry generator output.  One occurrence per patch: these tests
    are about what the gate is SHOWN, not about ordering.
    """
    for text in bodies.values():
        sha = body_sha256(text)
        directory = os.path.join(corpus_dir, 'bodies', sha[:2])
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, sha), 'w', encoding='utf-8') as handle:
            handle.write(text)

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
            [('pkg-%s' % name, '1-1', name, body_sha256(text), 1, fingerprint_of(text)[1])
             for name, text in bodies.items()])
        connection.commit()
    finally:
        connection.close()

    return index_path, {name: fingerprint_of(text)[1] for name, text in bodies.items()}


class MarkedCorpusFixture:
    """Mixin: a synthetic corpus of marked / unmarked patches and an empty ledger."""

    def _setup(self, bodies):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path, fingerprints = _build_marked_corpus(tmp.name, bodies)
        conn = ledger_mod.create_ledger(os.path.join(tmp.name, 'ledger.sqlite'))
        self.addCleanup(conn.close)
        return conn, tmp.name, index_path, fingerprints

    def _mark(self, conn, fingerprint, patch_text, evidence=None):
        """Record the live ``generated-content`` observation the routing reads."""
        scan = generated.scan(patch_text)
        ledger_mod.append_observation(
            conn, fingerprint=fingerprint, kind=generated.GENERATED_KIND,
            detail=generated.detail_for(scan), evidence=evidence or generated.evidence_for(scan),
            observed_by=generated.GENERATED_OBSERVED_BY,
            rule_version=generated.GENERATED_RULES_VERSION, observed_at=WHEN)
        conn.commit()

    def _oversized(self, conn, fingerprint, changed_lines=47000):
        ledger_mod.append_observation(
            conn, fingerprint=fingerprint, kind=reviewability.REVIEWABILITY_KIND,
            detail='oversized', evidence=json.dumps({'changed_lines': changed_lines}, sort_keys=True),
            observed_by=reviewability.REVIEWABILITY_OBSERVED_BY,
            rule_version=reviewability.REVIEWABILITY_VERSION, observed_at=WHEN)
        conn.commit()

    def _live_risk(self, conn, fingerprint):
        return next(o for o in ledger_mod.live_observations(conn)
                    if o['kind'] == risk.RISK_KIND and o['fingerprint'] == fingerprint)

    def _risk_rows(self, conn, fingerprint):
        return [o for o in ledger_mod.observations_for(conn, fingerprint)
                if o['kind'] == risk.RISK_KIND]


BODIES = {'generated.patch': MARKED_PATCH, 'plain.patch': _patch()}


class ProjectedRunRiskGateTestCase(MarkedCorpusFixture, testtools.TestCase):
    """The gate's own path: marks read once per run, projection before the cap."""

    def _run(self, conn, corpus_dir, index_path, recorder=None, **kwargs):
        return risk.run_risk_gate(
            conn, corpus_dir, index_path, call=_fake_call(_risk_json(), recorder=recorder),
            now=WHEN, limit=10, **kwargs)

    def test_a_marked_fingerprint_is_scored_residue_first(self):
        recorder = []
        conn, corpus_dir, index_path, fps = self._setup(BODIES)
        self._mark(conn, fps['generated.patch'], MARKED_PATCH)
        stats = self._run(conn, corpus_dir, index_path, recorder=recorder)
        self.assertEqual(1, stats.projected)
        sent = next(user for _system, user, _model in recorder
                    if 'generated-claiming files not shown' in user)
        self.assertIn('char buf[64]', sent)
        self.assertNotIn('ac_cv_generated', sent)

    def test_the_projection_is_recorded_in_the_payload(self):
        conn, corpus_dir, index_path, fps = self._setup(BODIES)
        self._mark(conn, fps['generated.patch'], MARKED_PATCH)
        self._run(conn, corpus_dir, index_path)
        payload = json.loads(self._live_risk(conn, fps['generated.patch'])['evidence'])
        self.assertIs(True, payload['projected'])
        self.assertEqual(1, payload['omitted_files'])
        self.assertEqual(80, payload['omitted_changed'])

    def test_an_unmarked_fingerprint_is_byte_identical(self):
        recorder = []
        conn, corpus_dir, index_path, fps = self._setup(BODIES)
        self._mark(conn, fps['generated.patch'], MARKED_PATCH)
        self._run(conn, corpus_dir, index_path, recorder=recorder)
        # Its input is the diff body, untouched ...
        self.assertIn(risk.risk_user_message(triage_mod.diff_body(_patch())),
                      [user for _system, user, _model in recorder])
        # ... and its payload carries no phase-3 key at all.
        payload = json.loads(self._live_risk(conn, fps['plain.patch'])['evidence'])
        self.assertEqual(['level', 'raw_response', 'reason'], sorted(payload))

    def test_an_unmarked_run_projects_nothing(self):
        conn, corpus_dir, index_path, _fps = self._setup(BODIES)
        stats = self._run(conn, corpus_dir, index_path)
        self.assertEqual(0, stats.projected)

    def test_an_oversized_patch_with_a_small_residue_is_unlocked_and_scored(self):
        # The gatos case: structurally oversized, 3 lines of hand-written residue.
        conn, corpus_dir, index_path, fps = self._setup(BODIES)
        self._mark(conn, fps['generated.patch'], MARKED_PATCH)
        self._oversized(conn, fps['generated.patch'])
        stats = self._run(conn, corpus_dir, index_path)
        self.assertEqual(1, stats.unlocked_by_residue)
        self.assertEqual(0, stats.skipped_oversized)
        self.assertIn(fps['generated.patch'], risk.risk_rank_by_fingerprint(conn))
        self.assertEqual(1, stats.projected)

    def test_an_oversized_patch_with_no_mark_is_still_skipped(self):
        conn, corpus_dir, index_path, fps = self._setup(BODIES)
        self._oversized(conn, fps['plain.patch'])
        stats = self._run(conn, corpus_dir, index_path)
        self.assertEqual(1, stats.skipped_oversized)
        self.assertEqual(0, stats.unlocked_by_residue)
        self.assertNotIn(fps['plain.patch'], risk.risk_rank_by_fingerprint(conn))

    def test_the_unlock_is_the_shared_helper_not_a_reimplementation(self):
        # The composition lives in ``generated.py`` so the triage driver and the gate can
        # never disagree about who is unlocked: the gate calls it and never rebuilds it
        # from reviewability's threshold.
        import inspect
        self.assertIn('residue_unlocked_fingerprints', inspect.getsource(risk.run_risk_gate))
        self.assertNotIn('REVIEWABILITY_OVERSIZED_LINES', inspect.getsource(risk))

    def test_an_oversized_patch_with_a_big_residue_stays_locked(self):
        # 5,410 lines of residue is not line-reviewable either; the unlock never
        # pretends otherwise (gatos's sibling).
        conn, corpus_dir, index_path, fps = self._setup(BODIES)
        evidence = json.dumps(
            {'files': [{'path': 'configure', 'family': 'autotools', 'signals': ['name'],
                        'generator': None, 'version': None, 'added': 24590, 'removed': 0}],
             'generated_changed': 24590, 'residue_changed': 5410, 'total_changed': 30000},
            sort_keys=True)
        self._mark(conn, fps['generated.patch'], MARKED_PATCH, evidence=evidence)
        self._oversized(conn, fps['generated.patch'], changed_lines=30000)
        stats = self._run(conn, corpus_dir, index_path)
        self.assertEqual(1, stats.skipped_oversized)
        self.assertEqual(0, stats.unlocked_by_residue)
        self.assertNotIn(fps['generated.patch'], risk.risk_rank_by_fingerprint(conn))


# ---------------------------------------------------------------------------
# The targeted re-risk: the scores that were read off a generated head
# ---------------------------------------------------------------------------

def _truncated_score(level='elevated'):
    """A live score computed from a truncated head -- the population the pass exists for."""
    return risk.RiskScore(level=level, rank=risk.RISK_RANK[level], reason='r',
                          model='claude-opus-4-8', prompt_version=risk.RISK_PROMPT_VERSION,
                          raw_response=_risk_json(level), truncated=True, original_chars=99999)


RERISK_BODIES = {'marked-truncated.patch': MARKED_PATCH,
                 'marked-whole.patch': MARKED_PATCH_B,
                 'plain-truncated.patch': _patch()}


class ReRiskMarkedTestCase(MarkedCorpusFixture, testtools.TestCase):
    """Exactly the marked fingerprints whose score read a truncated generated head."""

    def _setup_population(self):
        conn, corpus_dir, index_path, fps = self._setup(RERISK_BODIES)
        # Marked AND truncated: the one and only candidate.
        self._mark(conn, fps['marked-truncated.patch'], MARKED_PATCH)
        risk.record_risk_observation(conn, fps['marked-truncated.patch'], _truncated_score(),
                                     now=WHEN)
        # Marked, but its score read the whole diff: nothing to re-do.
        self._mark(conn, fps['marked-whole.patch'], MARKED_PATCH_B)
        risk.record_risk_observation(conn, fps['marked-whole.patch'], _make_score('low'), now=WHEN)
        # Truncated, but nothing claimed generation: honestly truncated code.
        risk.record_risk_observation(conn, fps['plain-truncated.patch'], _truncated_score(),
                                     now=WHEN)
        conn.commit()
        return conn, corpus_dir, index_path, fps

    def _rerun(self, conn, corpus_dir, index_path, level='low', **kwargs):
        return risk.run_risk_gate(
            conn, corpus_dir, index_path, call=_fake_call(_risk_json(level)), now=LATER,
            limit=50, re_risk_marked=True, **kwargs)

    def test_the_candidate_population_is_exact(self):
        conn, _corpus_dir, _index_path, fps = self._setup_population()
        self.assertEqual({fps['marked-truncated.patch']}, risk.rerisk_candidates(conn))

    def test_only_the_marked_and_truncated_fingerprint_is_rescored(self):
        conn, corpus_dir, index_path, fps = self._setup_population()
        stats = self._rerun(conn, corpus_dir, index_path)
        self.assertEqual(1, stats.re_risked)
        self.assertEqual(1, stats.scored)
        self.assertEqual(2, len(self._risk_rows(conn, fps['marked-truncated.patch'])))
        self.assertEqual(1, len(self._risk_rows(conn, fps['marked-whole.patch'])))
        self.assertEqual(1, len(self._risk_rows(conn, fps['plain-truncated.patch'])))

    def test_the_old_score_is_superseded_never_deleted(self):
        conn, corpus_dir, index_path, fps = self._setup_population()
        self._rerun(conn, corpus_dir, index_path, level='none')
        rows = self._risk_rows(conn, fps['marked-truncated.patch'])
        self.assertEqual('elevated', rows[0]['detail'])            # the old read, still there
        self.assertIsNotNone(rows[0]['superseded_at'])             # ... but no longer live
        self.assertEqual('none', rows[1]['detail'])                # the residue-first read
        self.assertIsNone(rows[1]['superseded_at'])

    def test_the_new_score_is_projected(self):
        conn, corpus_dir, index_path, fps = self._setup_population()
        self._rerun(conn, corpus_dir, index_path)
        payload = json.loads(self._live_risk(conn, fps['marked-truncated.patch'])['evidence'])
        self.assertIs(True, payload['projected'])
        self.assertEqual(1, payload['omitted_files'])
        self.assertNotIn('truncated', payload)   # 3 lines of residue fit easily

    def test_the_review_queue_is_reprioritised(self):
        from divergulent.classify import triage_driver
        conn, corpus_dir, index_path, fps = self._setup_population()
        ledger_mod.append_review_item(
            conn, fingerprint=fps['marked-truncated.patch'], reason='r', draft_category='unknown',
            draft_confidence='low', enqueued_at=WHEN, priority=1)
        conn.commit()
        stats = self._rerun(conn, corpus_dir, index_path, level='high')
        self.assertGreaterEqual(stats.reprioritised, 1)
        item = next(i for i in ledger_mod.pending_review_items(conn)
                    if i['fingerprint'] == fps['marked-truncated.patch'])
        self.assertGreaterEqual(item['priority'], triage_driver.RISK_PRIORITY_WEIGHT)

    def test_a_second_run_finds_nothing_even_when_still_truncated(self):
        # The termination guard.  Cap the re-score hard so the NEW score is truncated
        # too: were the selection keyed on ``truncated`` alone, or on ``projected`` being
        # true, this fingerprint would be re-scored on every run for ever.
        conn, corpus_dir, index_path, fps = self._setup_population()
        first = self._rerun(conn, corpus_dir, index_path, max_diff_chars=100)
        self.assertEqual(1, first.re_risked)
        payload = json.loads(self._live_risk(conn, fps['marked-truncated.patch'])['evidence'])
        self.assertIs(True, payload['truncated'])
        self.assertIs(True, payload['projected'])

        second = self._rerun(conn, corpus_dir, index_path, max_diff_chars=100)
        self.assertEqual(0, second.re_risked)
        self.assertEqual(0, second.scored + second.culled)
        self.assertEqual(set(), risk.rerisk_candidates(conn))
        self.assertEqual(2, len(self._risk_rows(conn, fps['marked-truncated.patch'])))

    def test_an_identity_projection_also_terminates(self):
        # A mark whose paths are not in this body: the score stays truncated and the
        # projection did nothing, and the recorded ``projected: false`` is what retires it.
        conn, _corpus_dir, _index_path, fps = self._setup_population()
        score = risk.RiskScore(
            level='elevated', rank=risk.RISK_RANK['elevated'], reason='r',
            model='claude-opus-4-8', prompt_version=risk.RISK_PROMPT_VERSION,
            raw_response=_risk_json(), truncated=True, original_chars=99999,
            projection=generated.ProjectedDiff(text='x', omitted_files=0, omitted_changed=0,
                                               projected=False))
        risk.record_risk_observation(conn, fps['marked-whole.patch'], score, now=LATER)
        self.assertNotIn(fps['marked-whole.patch'], risk.rerisk_candidates(conn))

    def test_a_malformed_payload_never_spends_a_call(self):
        conn, _corpus_dir, _index_path, fps = self._setup_population()
        ledger_mod.supersede_observations_for_fingerprint(
            conn, fingerprint=fps['marked-whole.patch'], kind=risk.RISK_KIND, superseded_at=LATER)
        ledger_mod.append_observation(
            conn, fingerprint=fps['marked-whole.patch'], kind=risk.RISK_KIND, detail='elevated',
            evidence='not json at all {', observed_by='risk-gate:claude-opus-4-8',
            rule_version=risk.RISK_PROMPT_VERSION, observed_at=LATER)
        conn.commit()
        self.assertNotIn(fps['marked-whole.patch'], risk.rerisk_candidates(conn))

    def test_the_default_run_rescores_nothing(self):
        # Without the flag the gate's behaviour is unchanged: everything here already
        # carries a live score, so there is nothing to do.
        conn, corpus_dir, index_path, fps = self._setup_population()
        stats = risk.run_risk_gate(conn, corpus_dir, index_path, call=_fake_call(_risk_json()),
                                   now=LATER, limit=50)
        self.assertEqual(0, stats.scored + stats.culled)
        self.assertEqual(0, stats.re_risked)
        self.assertEqual(1, len(self._risk_rows(conn, fps['marked-truncated.patch'])))

    def test_the_cli_flag_reaches_the_run(self):
        # The operator-facing end of the pass: ``--re-risk-marked`` is off unless typed,
        # and when typed it arrives at the gate (the real backend is never built here).
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        _conn, corpus_dir, _index_path, _fps = self._setup_population()
        ledger_path = os.path.join(corpus_dir, 'ledger.sqlite')
        for argv, expected in (([], False), (['--re-risk-marked'], True)):
            with mock.patch.object(risk, 'run_risk_gate') as run, \
                    mock.patch.object(ledger_mod, 'open_ledger'), \
                    redirect_stdout(io.StringIO()):
                run.return_value = risk.RiskRunStats()
                risk.main([ledger_path, corpus_dir, *argv])
            self.assertIs(expected, run.call_args.kwargs['re_risk_marked'])
