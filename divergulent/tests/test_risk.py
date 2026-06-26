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
