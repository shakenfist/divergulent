"""Tests for divergulent.classify.risk -- the claim-blind security-risk gate.

All OFFLINE: the LLM ``call`` boundary is injected as a fake returning canned
JSON, so no real claude -p / network. Coverage: the gate is claim-blind (the
DEP-3 description never reaches the prompt), parses the coarse level, degrades
unparseable/out-of-scale responses to ``elevated`` (recall-safe, never buried),
carries (model, prompt_version) + usage, and records a supersedable
``security-risk`` observation that re-scoring replaces (exactly one live).
"""
import json
import os
import tempfile

import testtools

from divergulent.classify import ledger as ledger_mod
from divergulent.classify import risk
from divergulent.classify import triage as triage_mod

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
